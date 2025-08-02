import asyncio
import traceback
import os
import uuid
import base64
import mimetypes
import re
from datetime import datetime
from pathlib import Path
from shutil import rmtree

import litellm
from telethon import events, errors
from telethon.tl.functions.bots import SetBotCommandsRequest
from telethon.tl.types import (
    BotCommand,
    BotCommandScopeDefault,
    KeyboardButtonCallback,
)
from pydantic import BaseModel, Field
from typing import Optional

# Import uniborg utilities and storage
from uniborg import util
from uniborg import llm_db
from uniborg import llm_util
from uniborg.storage import UserStorage

# --- Constants and Configuration ---

# Use the litellm model naming convention.
# See https://docs.litellm.ai/docs/providers/gemini
DEFAULT_MODEL = "gemini/gemini-2.5-flash" #: Do NOT change the default model unless explicitly instructed to.
DEFAULT_SYSTEM_PROMPT = """
You are a helpful and knowledgeable assistant. Your primary audience is advanced STEM postgraduate researchers, so be precise and technically accurate.

**Style Guidelines for Mobile Chat:**
- **Concise & Direct:** Keep responses as brief as possible without sacrificing critical information. Get straight to the point. Exception: Provide full detail when users specifically request lengthy responses.
- **Conversational Tone:** Write in a clear, natural style suitable for a chat conversation. Avoid overly academic or verbose language unless necessary for technical accuracy. You can use emojis.
- **Readability:** Break up text into short paragraphs. Use bullet points or numbered lists to make complex information easy to scan on a small screen.

**Formatting:** You can use Telegram's markdown: `**bold**`, `__italic__`, `` `code` ``, `[links](https://example.com)`, and ```pre``` blocks.
"""

# Directory for logs, mirroring the STT plugin's structure
LOG_DIR = Path(os.path.expanduser("~/.borg/llm_chat/log/"))
LOG_DIR.mkdir(parents=True, exist_ok=True)

# --- New Constants for Features ---
AVAILABLE_TOOLS = ["googleSearch", "urlContext", "codeExecution"]
DEFAULT_ENABLED_TOOLS = ["googleSearch", "urlContext"]
REASONING_LEVELS = ["disable", "low", "medium", "high"]

SAFETY_SETTINGS = [
    {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
]

# --- State Management ---
PROCESSED_GROUP_IDS = set()
AWAITING_INPUT_FROM_USERS = {}

def cancel_input_flow(user_id: int):
    """Cancels any pending input requests for a user."""
    AWAITING_INPUT_FROM_USERS.pop(user_id, None)


# --- User Preference Management ---

class UserPrefs(BaseModel):
    """Pydantic model for type-safe user preferences."""
    model: str = Field(default=DEFAULT_MODEL)
    system_prompt: str = Field(default=DEFAULT_SYSTEM_PROMPT)
    thinking: Optional[str] = Field(default=None)
    enabled_tools: list[str] = Field(default_factory=lambda: DEFAULT_ENABLED_TOOLS)
    json_mode: bool = Field(default=False)

class UserManager:
    """High-level manager for user preferences, using the UserStorage class."""
    def __init__(self):
        self.storage = UserStorage(purpose="llm_chat")

    def get_prefs(self, user_id: int) -> UserPrefs:
        data = self.storage.get(user_id)
        return UserPrefs.model_validate(data or {})

    def _save_prefs(self, user_id: int, prefs: UserPrefs):
        self.storage.set(user_id, prefs.model_dump(exclude_defaults=True))

    def set_model(self, user_id: int, model_name: str):
        prefs = self.get_prefs(user_id)
        prefs.model = model_name
        self._save_prefs(user_id, prefs)

    def set_system_prompt(self, user_id: int, prompt: str):
        prefs = self.get_prefs(user_id)
        prefs.system_prompt = prompt
        self._save_prefs(user_id, prefs)

    def set_thinking(self, user_id: int, level: Optional[str]):
        prefs = self.get_prefs(user_id)
        prefs.thinking = level
        self._save_prefs(user_id, prefs)

    def set_tool_state(self, user_id: int, tool_name: str, enabled: bool):
        if tool_name not in AVAILABLE_TOOLS:
            return
        prefs = self.get_prefs(user_id)
        if enabled and tool_name not in prefs.enabled_tools:
            prefs.enabled_tools.append(tool_name)
        elif not enabled and tool_name in prefs.enabled_tools:
            prefs.enabled_tools.remove(tool_name)
        self._save_prefs(user_id, prefs)

    def toggle_json_mode(self, user_id: int) -> bool:
        prefs = self.get_prefs(user_id)
        prefs.json_mode = not prefs.json_mode
        self._save_prefs(user_id, prefs)
        return prefs.json_mode


user_manager = UserManager()


# --- Core Logic & Helpers ---

def build_menu(buttons, n_cols):
    """Helper to build a menu of inline buttons in a grid."""
    return [buttons[i:i + n_cols] for i in range(0, len(buttons), n_cols)]

async def _process_media(message, temp_dir: Path) -> Optional[dict]:
    """Downloads media, encodes it, and returns a content part for litellm."""
    if not message or not message.media:
        return None
    try:
        file_path_str = await message.download_media(file=temp_dir)
        if not file_path_str: return None
        file_path = Path(file_path_str)
        mime_type, _ = mimetypes.guess_type(file_path)
        # Fallback for some audio/video types
        if not mime_type:
            for ext, m_type in llm_util.MIME_TYPE_MAP.items(): # Use imported map
                if file_path.name.lower().endswith(ext):
                    mime_type = m_type
                    break
        # Fallback for common text file types
        if not mime_type and file_path.suffix.lower() in ['.txt', '.md', '.py', '.js', '.html', '.css', '.json', '.xml', '.log']:
             mime_type = "text/plain"
        if not mime_type or not (
            mime_type.startswith(("image/", "audio/", "video/", "text/"))
        ):
            print(f"Unsupported media type '{mime_type}' for file {file_path.name}")
            return None
        with open(file_path, "rb") as f:
            file_bytes = f.read()
        if mime_type.startswith("text/"):
            # For text, return a standard text part to be merged.
            return {"type": "text", "text": f"\n--- Attachment: {file_path.name} ---\n{file_bytes.decode('utf-8', errors='ignore')}"}
        else:
            b64_content = base64.b64encode(file_bytes).decode("utf-8")
            return {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{b64_content}"}}
    except Exception as e:
        print(f"Error processing media from message {message.id}: {e}")
        return None

async def _log_conversation(event, model_name: str, messages: list, final_response: str):
    """Formats and writes the conversation log to a user-specific file."""
    try:
        user = await event.get_sender()
        user_id = user.id
        first_name = user.first_name or ""
        last_name = user.last_name or ""
        username = user.username or "N/A"
        full_name = f"{first_name} {last_name}".strip()

        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        unique_id = uuid.uuid4().hex
        log_filename = f"{timestamp}_{unique_id}.txt"

        user_log_dir = LOG_DIR / str(user_id)
        user_log_dir.mkdir(exist_ok=True)
        log_file_path = user_log_dir / log_filename

        log_parts = [
            f"Date: {timestamp}", f"User ID: {user_id}", f"Name: {full_name}",
            f"Username: @{username}", f"Model: {model_name}", "--- Conversation ---"
        ]
        for msg in messages:
            role = msg.get("role", "unknown").capitalize()
            content = msg.get("content")
            log_parts.append(f"\n[{role}]:")
            if isinstance(content, str):
                log_parts.append(content)
            elif isinstance(content, list):
                for part in content:
                    if part.get("type") == "text":
                        log_parts.append(part.get("text", ""))
                    else: # Handle media attachments in logs
                        log_parts.append("[Attachment: Media Content]")
        log_parts.append("\n[Assistant]:")
        log_parts.append(final_response)
        with open(log_file_path, "w", encoding="utf-8") as f:
            f.write("\n".join(log_parts))
    except Exception as e:
        print(f"Failed to write chat log for user {event.sender_id}: {e}")
        traceback.print_exc()

async def build_conversation_history(event) -> list:
    """
    Constructs a conversation history from the reply chain, processing and
    grouping media albums correctly.
    """
    if not event.message.reply_to_msg_id:
        return []
    history = []
    # 1. Traverse the reply chain to get all historical messages
    messages_in_chain = []
    try:
        message = await event.client.get_messages(
            event.chat_id, ids=event.message.reply_to_msg_id
        )
        while message:
            messages_in_chain.append(message)
            if not message.reply_to_msg_id:
                break
            message = await event.client.get_messages(
                event.chat_id, ids=message.reply_to_msg_id
            )
    except Exception:
        pass  # Stop if we can't fetch a message
    messages_in_chain.reverse()  # Sort from oldest to newest
    if not messages_in_chain:
        return []
    bot_me = await event.client.get_me()
    temp_dir = Path(f"./temp_llm_chat_history_{event.id}/")
    temp_dir.mkdir(exist_ok=True)
    processed_message_ids = set()
    try:
        for msg in messages_in_chain:
            if msg.id in processed_message_ids:
                continue
            # This is the "turn". It could be a single message or a group/album.
            turn_messages = [msg]
            group_id = msg.grouped_id
            if group_id:
                try:
                    k = 20
                    search_ids = range(msg.id - k, msg.id + k + 1)
                    messages_in_vicinity = await event.client.get_messages(
                        event.chat_id, ids=list(search_ids)
                    )
                    group_messages = [
                        m for m in messages_in_vicinity if m and m.grouped_id == group_id
                    ]
                    if group_messages:
                        turn_messages = sorted(group_messages, key=lambda m: m.id)
                except Exception as e:
                    print(f"Error gathering group {group_id} in history: {e}")
                    turn_messages = [msg]
            # Now, process all messages in this turn and consolidate into one history entry
            role = "assistant" if turn_messages[0].sender_id == bot_me.id else "user"
            text_buffer, media_parts = [], []
            for turn_msg in turn_messages:
                processed_message_ids.add(turn_msg.id)
                if turn_msg.text:
                    text_buffer.append(turn_msg.text)
                media_part = await _process_media(turn_msg, temp_dir)
                if media_part:
                    media_parts.append(media_part)
            content_parts = []
            if text_buffer:
                content_parts.append({"type": "text", "text": "\n".join(text_buffer)})
            text_from_files = []
            for part in media_parts:
                if part.get("type") == "text":
                    text_from_files.append(part["text"])
                else:
                    content_parts.append(part)
            if text_from_files:
                combined_file_text = "\n".join(text_from_files)
                existing_text_part = next(
                    (p for p in content_parts if p["type"] == "text"), None
                )
                if existing_text_part:
                    existing_text_part["text"] += "\n" + combined_file_text
                else:
                    content_parts.insert(0, {"type": "text", "text": combined_file_text})
            if not content_parts:
                continue
            final_content = (
                content_parts[0]["text"]
                if len(content_parts) == 1 and content_parts[0]["type"] == "text"
                else content_parts
            )
            history.append({"role": role, "content": final_content})
    finally:
        if temp_dir.exists():
            rmtree(temp_dir, ignore_errors=True)
    return history

# --- Bot Command Setup ---

async def set_bot_menu_commands():
    """Sets the bot's command menu in Telegram's UI."""
    print("LLM_Chat: setting bot commands ...")
    try:
        await asyncio.sleep(5)
        await borg(
            SetBotCommandsRequest(
                scope=BotCommandScopeDefault(),
                lang_code="en",
                commands=[
                    BotCommand("start", "Onboard and set API key"),
                    BotCommand("help", "Show detailed help and instructions"),
                    BotCommand("setgeminikey", "Set or update your Gemini API key"),
                    BotCommand("setmodel", "Set your preferred chat model"),
                    BotCommand("setsystemprompt", "Customize the bot's instructions"),
                    BotCommand("setthink", "Adjust model's reasoning effort"),
                    BotCommand("tools", "Enable or disable tools like search"),
                    BotCommand("json", "Toggle JSON output mode"),
                ],
            )
        )
        print("LLM_Chat: Bot command menu has been updated.")
    except Exception as e:
        print(f"LLM_Chat: Failed to set bot commands: {e}")

# --- Telethon Event Handlers ---

@borg.on(events.NewMessage(pattern="/start", func=lambda e: e.is_private))
async def start_handler(event):
    """Handles the /start command to onboard new users."""
    user_id = event.sender_id
    if llm_db.is_awaiting_key(user_id):
        llm_db.cancel_key_flow(user_id)
    cancel_input_flow(user_id)
    if llm_db.get_api_key(user_id=user_id, service="gemini"):
        await event.reply("Welcome back! Your Gemini API key is configured. You can start chatting with me.\n\nUse /help to see all available commands.")
    else:
        await llm_db.request_api_key_message(event)

@borg.on(events.NewMessage(pattern="/help", func=lambda e: e.is_private))
async def help_handler(event):
    """Provides detailed help information about features and usage."""
    if llm_db.is_awaiting_key(event.sender_id):
        llm_db.cancel_key_flow(event.sender_id)
        await event.reply("API key setup cancelled.")
    cancel_input_flow(event.sender_id)
    prefs = user_manager.get_prefs(event.sender_id)
    help_text = f"""
**Hello! I am a Telegram chat bot powered by Google's Gemini.** It's like ChatGPT but in Telegram!

To get started, you'll need a free Gemini API key. Send me /setgeminikey to help you set this up.

**How to Chat with Me**

**▶️ Understanding Conversations (Reply Chains)**
I remember our conversations by following the **reply chain**. This is the key to having a continuous, context-aware chat.

- **Continuing the Chat:** To continue a conversation, simply **reply** to the last message of that conversation. I will remember our previous messages in that chain.
- **Adding More Detail:** You can also **reply to your OWN message** to add more thoughts, context, or files before I've even answered. I will see it all as part of the same turn.
- **Starting Fresh:** To start a new, separate conversation, just send a new message without replying to anything.

You can also attach **images, audio, video, and text files**. Sending multiple files as an **album** is also supported.

**Available Commands**
- /start: Onboard and set up your API key.
- /help: Shows this detailed help message.
- /setgeminikey: Sets or updates your Gemini API key.
- /setModel: Change the AI model. Current: `{prefs.model}`.
- /setSystemPrompt: Change my core instructions or reset to default.
- /setthink: Adjust the model's reasoning effort for complex tasks.
- /tools: Enable/disable tools like Google Search and Code Execution.
- /json: Toggle JSON-only output mode for structured data needs.
"""
    await event.reply(help_text, link_preview=False, parse_mode="md")


@borg.on(events.NewMessage(pattern=r"(?i)/setGeminiKey(?:\s+(.*))?", func=lambda e: e.is_private))
async def set_key_handler(event):
    """Delegates /setgeminikey command logic to the shared module."""
    await llm_db.handle_set_key_command(event)


@borg.on(
    events.NewMessage(
        func=lambda e: e.is_private
        and llm_db.is_awaiting_key(e.sender_id)
        and e.text and not e.text.startswith("/")
    )
)
async def key_submission_handler(event):
    """Delegates plain-text key submission logic to the shared module."""
    await llm_db.handle_key_submission(
        event,
        success_msg="You can now start chatting with me."
    )


@borg.on(events.NewMessage(pattern=r"/setModel(?:\s+(.*))?", func=lambda e: e.is_private))
async def set_model_handler(event):
    """Sets the user's preferred chat model, now with an interactive flow."""
    user_id = event.sender_id
    model_name_match = event.pattern_match.group(1)

    if model_name_match:
        model_name = model_name_match.strip()
        user_manager.set_model(user_id, model_name)
        cancel_input_flow(user_id)
        await event.reply(f"Your chat model has been set to: `{model_name}`")
    else:
        AWAITING_INPUT_FROM_USERS[user_id] = "model"
        await event.reply(
            f"Your current chat model is: `{user_manager.get_prefs(user_id).model}`."
            "\n\nPlease send the new model ID in the next message."
            "\n(Type `cancel` to stop this process.)"
        )


@borg.on(events.NewMessage(pattern=r"/setSystemPrompt(?:\s+([\s\S]+))?", func=lambda e: e.is_private))
async def set_system_prompt_handler(event):
    """Sets the user's custom system prompt or resets it, now with an interactive flow."""
    user_id = event.sender_id
    prompt_match = event.pattern_match.group(1)

    if prompt_match:
        prompt = prompt_match.strip()
        cancel_input_flow(user_id)
        if prompt.lower() == "reset":
            # Set the prompt to an empty string to signify using the default
            user_manager.set_system_prompt(user_id, "")
            await event.reply("Your system prompt has been reset to the default.")
        else:
            user_manager.set_system_prompt(user_id, prompt)
            await event.reply("Your new system prompt has been saved.")
    else:
        AWAITING_INPUT_FROM_USERS[user_id] = "system_prompt"
        current_prompt = user_manager.get_prefs(user_id).system_prompt or "Default (no custom prompt set)"
        await event.reply(
            f"**Your current system prompt is:**\n\n```\n{current_prompt}\n```"
            "\n\nPlease send the new system prompt in the next message."
            "\n(You can also send `reset` to restore the default, or `cancel` to stop.)"
        )


# --- New Feature Handlers ---

@borg.on(events.NewMessage(pattern=r"/setthink", func=lambda e: e.is_private))
async def set_think_handler(event):
    """Displays buttons to set the reasoning effort."""
    prefs = user_manager.get_prefs(event.sender_id)
    buttons = [KeyboardButtonCallback(
        f"✅ {lvl.capitalize()}" if prefs.thinking == lvl else lvl.capitalize(), data=f"think_{lvl}"
    ) for lvl in REASONING_LEVELS]
    clear_text = "Clear (Default)"
    buttons.append(KeyboardButtonCallback(f"✅ {clear_text}" if prefs.thinking is None else clear_text, data="think_clear"))
    await event.reply(
        "**Set Reasoning Effort**\nChoose the level of thinking for the model. This may affect response time and cost.",
        buttons=build_menu(buttons, n_cols=2)
    )

@borg.on(events.NewMessage(pattern=r"/tools", func=lambda e: e.is_private))
async def tools_handler(event):
    """Displays buttons to toggle tools."""
    prefs = user_manager.get_prefs(event.sender_id)
    buttons = [KeyboardButtonCallback(
        f"{'✅' if tool in prefs.enabled_tools else '❌'} {tool}", data=f"tool_{tool}"
    ) for tool in AVAILABLE_TOOLS]
    await event.reply("**Manage Tools**\nToggle available tools for the model.", buttons=build_menu(buttons, n_cols=1))

@borg.on(events.NewMessage(pattern=r"/(enable|disable)(?P<tool_name>\w+)", func=lambda e: e.is_private))
async def toggle_tool_handler(event):
    action = event.pattern_match.group(1)
    tool_name_req = event.pattern_match.group("tool_name").lower()
    matched_tool = next((t for t in AVAILABLE_TOOLS if t.lower() == tool_name_req), None)
    if matched_tool:
        is_enabled = action == "enable"
        user_manager.set_tool_state(event.sender_id, matched_tool, enabled=is_enabled)
        await event.reply(f"`{matched_tool}` has been **{action}d**.")
    else:
        await event.reply(f"Unknown tool: `{tool_name_req}`. Available: {', '.join(AVAILABLE_TOOLS)}")

@borg.on(events.NewMessage(pattern=r"/json", func=lambda e: e.is_private))
async def json_mode_handler(event):
    """Toggles JSON mode."""
    is_enabled = user_manager.toggle_json_mode(event.sender_id)
    await event.reply(f"JSON response mode has been **{'enabled' if is_enabled else 'disabled'}**.")

@borg.on(events.CallbackQuery())
async def callback_handler(event):
    """Handles all inline button presses for the plugin."""
    data_str = event.data.decode("utf-8")
    user_id = event.sender_id
    if data_str.startswith("think_"):
        level = data_str.split("_")[1]
        user_manager.set_thinking(user_id, None if level == "clear" else level)
        prefs = user_manager.get_prefs(user_id)
        buttons = [KeyboardButtonCallback(
            f"✅ {lvl.capitalize()}" if prefs.thinking == lvl else lvl.capitalize(), data=f"think_{lvl}"
        ) for lvl in REASONING_LEVELS]
        clear_text = "Clear (Default)"
        buttons.append(KeyboardButtonCallback(f"✅ {clear_text}" if prefs.thinking is None else clear_text, data="think_clear"))
        await event.edit(buttons=build_menu(buttons, n_cols=2))
        await event.answer("Thinking preference updated.")
    elif data_str.startswith("tool_"):
        tool_name = data_str.split("_")[1]
        # Toggle tool state
        prefs = user_manager.get_prefs(user_id)
        is_enabled = tool_name not in prefs.enabled_tools
        user_manager.set_tool_state(user_id, tool_name, enabled=is_enabled)
        # Re-generate buttons
        prefs = user_manager.get_prefs(user_id) # Re-fetch
        buttons = [KeyboardButtonCallback(
            f"{'✅' if tool in prefs.enabled_tools else '❌'} {tool}", data=f"tool_{tool}"
        ) for tool in AVAILABLE_TOOLS]
        await event.edit(buttons=build_menu(buttons, n_cols=1))
        await event.answer(f"{tool_name} {'enabled' if is_enabled else 'disabled'}.")


@borg.on(
    events.NewMessage(
        func=lambda e: e.is_private
        and e.sender_id in AWAITING_INPUT_FROM_USERS
        and e.text and not e.text.startswith("/")
    )
)
async def generic_input_handler(event):
    """Handles plain-text submissions for interactive commands."""
    user_id = event.sender_id
    text = event.text.strip()
    input_type = AWAITING_INPUT_FROM_USERS.get(user_id)

    if text.lower() == "cancel":
        cancel_input_flow(user_id)
        await event.reply("Process cancelled.")
        return

    if input_type == "model":
        user_manager.set_model(user_id, text)
        await event.reply(f"✅ Your chat model has been updated to: `{text}`")
    elif input_type == "system_prompt":
        if text.lower() == "reset":
            user_manager.set_system_prompt(user_id, "")
            await event.reply("✅ Your system prompt has been reset to the default.")
        else:
            user_manager.set_system_prompt(user_id, text)
            await event.reply("✅ Your new system prompt has been saved.")

    cancel_input_flow(user_id)


@borg.on(events.NewMessage(func=lambda e: e.is_private and (e.text or e.media) and not (e.text and e.text.startswith('/')) and not e.forward))
async def chat_handler(event):
    """Main handler for all non-command messages in a private chat."""
    user_id = event.sender_id

    # Intercept if user is in any waiting state first.
    if llm_db.is_awaiting_key(user_id) or user_id in AWAITING_INPUT_FROM_USERS:
        return

    # --- Grouped Message Handling ---
    group_id = event.grouped_id
    if group_id:
        if group_id in PROCESSED_GROUP_IDS:
            return  # Already being processed

    api_key = llm_db.get_api_key(user_id=user_id, service="gemini")
    if not api_key:
        await llm_db.request_api_key_message(event)
        return

    PROCESSED_GROUP_IDS.add(group_id)
    ###

    prefs = user_manager.get_prefs(user_id)
    response_message = await event.reply("...")
    temp_dir = Path(f"./temp_llm_chat_{event.id}/")
    try:
        messages = await build_conversation_history(event)
        system_prompt_to_use = prefs.system_prompt or DEFAULT_SYSTEM_PROMPT
        messages.insert(0, {"role": "system", "content": system_prompt_to_use})

        # --- Gather and Process Current User Turn (including groups) ---
        current_turn_messages = [event.message]
        if group_id:
            await asyncio.sleep(0.1)  # Allow album messages to arrive
            try:
                # Search a range around the event's ID to find all messages in the group
                k = 20
                search_ids = range(event.id - k, event.id + k + 1)
                messages_in_vicinity = await event.client.get_messages(event.chat_id, ids=list(search_ids))
                group_messages = [m for m in messages_in_vicinity if m and m.grouped_id == group_id]
                if group_messages:
                    current_turn_messages = sorted(group_messages, key=lambda m: m.id)
            except Exception as e:
                print(f"Could not gather grouped messages for group {group_id}: {e}")
                # Fallback to just the trigger message

        content_parts = []
        if current_turn_messages:
            temp_dir.mkdir(exist_ok=True)
            text_buffer, media_parts = [], []
            for msg in current_turn_messages:
                if msg.text:
                    text_buffer.append(msg.text)
                if msg.media:
                    media_part = await _process_media(msg, temp_dir)
                    if media_part:
                        media_parts.append(media_part)

            # Consolidate all text and media into a single user message
            if text_buffer:
                content_parts.append({"type": "text", "text": "\n".join(text_buffer)})

            text_from_files = []
            for part in media_parts:
                if part['type'] == 'text':
                    text_from_files.append(part['text'])
                else:
                    content_parts.append(part)

            if text_from_files:
                combined_file_text = "\n".join(text_from_files)
                if content_parts and content_parts[0]['type'] == 'text':
                    content_parts[0]['text'] += "\n" + combined_file_text
                else:
                    content_parts.insert(0, {'type': 'text', 'text': combined_file_text})

        if content_parts:
            final_content = content_parts[0]['text'] if len(content_parts) == 1 and content_parts[0]['type'] == 'text' else content_parts
            messages.append({"role": "user", "content": final_content})

        # --- Construct API call arguments ---
        is_gemini_model = re.search(r'\bgemini\b', prefs.model, re.IGNORECASE)
        warnings = []

        api_kwargs = {
            "model": prefs.model, "messages": messages, "api_key": api_key,
            "stream": True,
        }

        if prefs.json_mode:
            api_kwargs["response_format"] = {"type": "json_object"}
            if prefs.enabled_tools:
                warnings.append("Tools are disabled (not supported in JSON mode).")

        if is_gemini_model:
            api_kwargs["safety_settings"] = SAFETY_SETTINGS
            # Corrected Logic: Only enable tools if JSON mode is OFF.
            if prefs.enabled_tools and not prefs.json_mode:
                api_kwargs["tools"] = [{t: {}} for t in prefs.enabled_tools]
            if prefs.thinking:
                api_kwargs["reasoning_effort"] = prefs.thinking
        else:
            # Add warnings if user has Gemini-specific settings enabled
            if prefs.enabled_tools:
                warnings.append("Tools are disabled (Gemini-only feature).")
            if prefs.thinking:
                warnings.append("Reasoning effort is disabled (Gemini-only feature).")

        # Make the API call
        response_text = ""
        last_edit_time = asyncio.get_event_loop().time()
        edit_interval = 0.8
        response_stream = await litellm.acompletion(**api_kwargs)
        async for chunk in response_stream:
            delta = chunk.choices[0].delta.content
            if delta:
                response_text += delta
                current_time = asyncio.get_event_loop().time()
                if (current_time - last_edit_time) > edit_interval:
                    try:
                        # Add a cursor to indicate the bot is still "typing"
                        await util.edit_message(response_message, f"{response_text}▌", parse_mode="md")
                        last_edit_time = current_time
                    except errors.rpcerrorlist.MessageNotModifiedError:
                        # This error is expected if the content hasn't changed
                        pass
                    except Exception as e:
                        # Log other edit errors but don't stop the stream
                        print(f"Error during message edit: {e}")

        # Final edit to remove the cursor and show the complete message

        final_text = response_text.strip() or "__[No response]__"

        if warnings:
            warning_text = "\n\n---\n**Note:**\n" + "\n".join(f"- {w}" for w in warnings)

            #: no need to clutter the response in Telegram
            # final_text += warning_text

        await util.edit_message(response_message, final_text, parse_mode="md", link_preview=False)
        await _log_conversation(event, prefs.model, messages, final_text)

    except Exception:
        error_text = "An error occurred. You can send the inputs that caused this error to the bot developer."
        await response_message.edit(error_text)
        traceback.print_exc()
    finally:
        if group_id:
            PROCESSED_GROUP_IDS.discard(group_id)
        if temp_dir.exists():
            rmtree(temp_dir, ignore_errors=True)


# --- Initialization ---
# Schedule the command menu setup to run on the bot's event loop upon loading.
borg.loop.create_task(set_bot_menu_commands())
