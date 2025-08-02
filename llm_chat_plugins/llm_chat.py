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
from itertools import groupby

import litellm
from telethon import events, errors
from telethon.tl.functions.bots import SetBotCommandsRequest
from telethon.tl.types import (
    BotCommand,
    BotCommandScopeDefault,
    KeyboardButtonCallback,
    Message,
)
from pydantic import BaseModel, Field
from typing import Optional, List

# Import uniborg utilities and storage
from uniborg import util
from uniborg import llm_db
from uniborg import llm_util
from uniborg.storage import UserStorage
from uniborg.llm_util import BOT_META_INFO_PREFIX

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
LAST_N_MESSAGES_LIMIT = 50
HISTORY_MESSAGE_LIMIT = 1000
LOG_COUNT_LIMIT = 5
AVAILABLE_TOOLS = ["googleSearch", "urlContext", "codeExecution"]
DEFAULT_ENABLED_TOOLS = ["googleSearch", "urlContext"]
REASONING_LEVELS = ["disable", "low", "medium", "high"]
CONTEXT_MODES = ["reply_chain", "until_separator", "last_N"]
CONTEXT_SEPARATOR = "---"
CONTEXT_MODE_NAMES = {
    "reply_chain": "Reply Chain",
    "until_separator": f"Until Separator (`{CONTEXT_SEPARATOR}`)",
    "last_N": f"Last {LAST_N_MESSAGES_LIMIT} Messages",
}


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
    context_mode: str = Field(default="reply_chain")

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

    def set_context_mode(self, user_id: int, mode: str):
        prefs = self.get_prefs(user_id)
        prefs.context_mode = mode
        self._save_prefs(user_id, prefs)


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

async def _expand_and_sort_messages_with_groups(event, initial_messages: List[Message]) -> List[Message]:
    """
    Takes a list of messages and ensures all members of any represented media
    group are included in the final, sorted list.
    """
    final_messages_map = {m.id: m for m in initial_messages}
    processed_group_ids = set()

    # Iterate over a copy, as we might fetch more messages and add to the map
    messages_to_check = list(initial_messages)

    for msg in messages_to_check:
        group_id = msg.grouped_id
        if group_id and group_id not in processed_group_ids:
            try:
                # Fetch all messages in the vicinity of the current message to find group members
                k = 30  # Search range
                search_ids = range(msg.id - k, msg.id + k + 1)
                messages_in_vicinity = await event.client.get_messages(
                    event.chat_id, ids=list(search_ids)
                )

                group_messages = [m for m in messages_in_vicinity if m and m.grouped_id == group_id]
                for group_msg in group_messages:
                    final_messages_map[group_msg.id] = group_msg

                processed_group_ids.add(group_id)
            except Exception as e:
                print(f"Could not expand message group {group_id}: {e}")

    # Return a sorted, unique list of messages
    return sorted(final_messages_map.values(), key=lambda m: m.id)

async def _process_turns_to_history(event, message_list: List[Message], temp_dir: Path) -> List[dict]:
    """
    Processes a final, sorted list of messages into litellm history format,
    grouping consecutive messages from the same sender into 'turns'.
    """
    history = []
    if not message_list:
        return history

    bot_me = await event.client.get_me()

    # Group consecutive messages by sender to create "turns"
    for _, turn_messages_iter in groupby(message_list, key=lambda m: m.sender_id):
        turn_messages = list(turn_messages_iter)
        if not turn_messages:
            continue

        role = "assistant" if turn_messages[0].sender_id == bot_me.id else "user"

        # Process all messages in this turn and consolidate into one history entry
        text_buffer, media_parts = [], []
        for turn_msg in turn_messages:
            # --- NEW: Filter out bot's meta-info messages from history ---
            if role == "assistant" and turn_msg.text and turn_msg.text.startswith(BOT_META_INFO_PREFIX):
                continue

            if turn_msg.text:
                text_buffer.append(turn_msg.text)
            media_part = await _process_media(turn_msg, temp_dir)
            if media_part:
                media_parts.append(media_part)

        # If the turn is empty after filtering, skip it
        if not text_buffer and not media_parts:
            continue

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
            existing_text_part = next((p for p in content_parts if p["type"] == "text"), None)
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

    return history


async def _get_initial_messages_for_reply_chain(event) -> List[Message]:
    if not event.message.reply_to_msg_id:
        return []
    messages = []
    try:
        message = await event.client.get_messages(event.chat_id, ids=event.message.reply_to_msg_id)
        while message:
            messages.append(message)
            if len(messages) >= HISTORY_MESSAGE_LIMIT:
                break
            if not message.reply_to_msg_id:
                break
            message = await event.client.get_messages(event.chat_id, ids=message.reply_to_msg_id)
    except Exception:
        pass
    messages.reverse()
    return messages

async def _get_initial_messages_for_separator(event) -> List[Message]:
    if not event.message.reply_to_msg_id:
        return []
    messages = []
    try:
        message = await event.client.get_messages(event.chat_id, ids=event.message.reply_to_msg_id)
        while message:
            if message.text and message.text.strip() == CONTEXT_SEPARATOR:
                break
            messages.append(message)
            if len(messages) >= HISTORY_MESSAGE_LIMIT:
                break
            if not message.reply_to_msg_id:
                break
            message = await event.client.get_messages(event.chat_id, ids=message.reply_to_msg_id)
    except Exception:
        pass
    messages.reverse()
    return messages

async def _get_initial_messages_for_last_n(event) -> List[Message]:
    messages = []
    try:
        # Fetch one more to exclude the triggering message itself
        limit = LAST_N_MESSAGES_LIMIT + 1
        messages = await event.client.get_messages(event.chat_id, limit=limit)
        messages = [m for m in messages if m.id != event.id]
        if len(messages) > LAST_N_MESSAGES_LIMIT:
            messages = messages[:LAST_N_MESSAGES_LIMIT]
    except Exception as e:
        print(f"Error fetching last {LAST_N_MESSAGES_LIMIT} messages: {e}")
        return []
    messages.reverse()
    return messages

async def build_conversation_history(event, context_mode: str, temp_dir: Path) -> list:
    """
    Orchestrates the construction of a conversation history based on the user's
    selected context mode, including group expansion and global limits.
    """
    initial_messages = []
    if context_mode == "reply_chain":
        initial_messages = await _get_initial_messages_for_reply_chain(event)
    elif context_mode == "until_separator":
        initial_messages = await _get_initial_messages_for_separator(event)
    elif context_mode == "last_N":
        initial_messages = await _get_initial_messages_for_last_n(event)

    initial_messages += [event.message]

    # Universally expand groups for the fetched messages
    expanded_messages = await _expand_and_sort_messages_with_groups(event, initial_messages)

    # Apply the global message limit as a final safeguard
    if len(expanded_messages) > HISTORY_MESSAGE_LIMIT:
        expanded_messages = expanded_messages[-HISTORY_MESSAGE_LIMIT:]

    # Process the final message list into litellm format
    history = await _process_turns_to_history(event, expanded_messages, temp_dir)
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
                    BotCommand("status", "Show your current settings"),
                    BotCommand("log", f"Get your last {LOG_COUNT_LIMIT} conversation logs"),
                    BotCommand("setgeminikey", "Set or update your Gemini API key"),
                    BotCommand("setmodel", "Set your preferred chat model"),
                    BotCommand("setsystemprompt", "Customize the bot's instructions"),
                    BotCommand("setthink", "Adjust model's reasoning effort"),
                    BotCommand("contextmode", "Change how conversation history is read"),
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
        await event.reply(
            f"{BOT_META_INFO_PREFIX}Welcome back! Your Gemini API key is configured. You can start chatting with me.\n\n"
            "Use /help to see all available commands."
        )
    else:
        await llm_db.request_api_key_message(event)

@borg.on(events.NewMessage(pattern="/help", func=lambda e: e.is_private))
async def help_handler(event):
    """Provides detailed help information about features and usage."""
    if llm_db.is_awaiting_key(event.sender_id):
        llm_db.cancel_key_flow(event.sender_id)
        await event.reply(f"{BOT_META_INFO_PREFIX}API key setup cancelled.")
    cancel_input_flow(event.sender_id)
    prefs = user_manager.get_prefs(event.sender_id)
    help_text = f"""
**Hello! I am a Telegram chat bot powered by Google's Gemini.** It's like ChatGPT but in Telegram!

To get started, you'll need a free Gemini API key. Send me /setgeminikey to help you set this up.

**How to Chat with Me**

**▶️ Understanding Conversation Context**
I remember our conversations based on your chosen **Context Mode**.

- **Reply Chain (Default):** To continue a conversation, simply **reply** to my last message. I will remember our previous messages in that chain.
- **Until Separator:** I will read the reply chain until a message with only `{CONTEXT_SEPARATOR}` is found. This lets you manually define the context length.
- **Last {LAST_N_MESSAGES_LIMIT} Messages:** I will use the last {LAST_N_MESSAGES_LIMIT} messages in our chat as context, regardless of replies.
- **Starting Fresh:** To start a new, separate conversation in "Reply Chain" mode, just send a message without replying to anything.

You can attach **images, audio, video, and text files**. Sending multiple files as an **album** is also supported, and I will see all items in the album.

**Available Commands**
- /start: Onboard and set up your API key.
- /help: Shows this detailed help message.
- /status: Shows a summary of your current settings.
- /log: Get your last {LOG_COUNT_LIMIT} conversation logs as files.
- /setgeminikey: Sets or updates your Gemini API key.
- /setModel: Change the AI model. Current: `{prefs.model}`.
- /setSystemPrompt: Change my core instructions or reset to default.
- /contextMode: Change how conversation history is gathered.
- /setthink: Adjust the model's reasoning effort for complex tasks.
- /tools: Enable/disable tools like Google Search and Code Execution.
- /json: Toggle JSON-only output mode for structured data needs.
"""
    await event.reply(f"{BOT_META_INFO_PREFIX}{help_text}", link_preview=False, parse_mode="md")

@borg.on(events.NewMessage(pattern=r"/status", func=lambda e: e.is_private))
async def status_handler(event):
    """Displays a summary of the user's current settings."""
    user_id = event.sender_id
    prefs = user_manager.get_prefs(user_id)
    enabled_tools_str = ", ".join(prefs.enabled_tools) if prefs.enabled_tools else "None"
    system_prompt_status = "Default"
    if prefs.system_prompt and prefs.system_prompt != DEFAULT_SYSTEM_PROMPT:
        system_prompt_status = "Custom"

    context_mode_name = CONTEXT_MODE_NAMES.get(prefs.context_mode, prefs.context_mode.replace('_', ' ').title())
    thinking_level = prefs.thinking.capitalize() if prefs.thinking else "Default"
    status_message = (
        f"**Your Current Bot Settings**\n\n"
        f"∙ **Model:** `{prefs.model}`\n"
        f"∙ **Context Mode:** `{context_mode_name}`\n"
        f"∙ **Reasoning Level:** `{thinking_level}`\n"
        f"∙ **Enabled Tools:** `{enabled_tools_str}`\n"
        f"∙ **JSON Mode:** `{'Enabled' if prefs.json_mode else 'Disabled'}`\n"
        f"∙ **System Prompt:** `{system_prompt_status}`"
    )
    await event.reply(f"{BOT_META_INFO_PREFIX}{status_message}", parse_mode="md")


@borg.on(events.NewMessage(pattern="/log", func=lambda e: e.is_private))
async def log_handler(event):
    """Sends the last few conversation logs to the user."""
    user_id = event.sender_id
    user_log_dir = LOG_DIR / str(user_id)

    if not user_log_dir.is_dir():
        await event.reply(f"{BOT_META_INFO_PREFIX}You have no conversation logs yet.")
        return

    try:
        log_files = sorted(
            [p for p in user_log_dir.glob("*.txt") if p.is_file()],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )

        if not log_files:
            await event.reply(f"{BOT_META_INFO_PREFIX}You have no conversation logs yet.")
            return

        logs_to_send = log_files[:LOG_COUNT_LIMIT]

        await event.reply(
            f"{BOT_META_INFO_PREFIX}Sending your last {len(logs_to_send)} of {len(log_files)} conversation log(s)..."
        )

        # Sending files doesn't need the prefix, but the caption does if we want it ignored
        for log_file in logs_to_send:
            await event.client.send_file(
                event.chat_id,
                file=log_file,
                caption=f"{BOT_META_INFO_PREFIX}Log: `{log_file.name}`",
                reply_to=event.id,
            )
    except Exception as e:
        print(f"Error sending logs for user {user_id}: {e}")
        traceback.print_exc()
        await event.reply(
            f"{BOT_META_INFO_PREFIX}Sorry, an error occurred while retrieving your logs."
        )


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
        await event.reply(f"{BOT_META_INFO_PREFIX}Your chat model has been set to: `{model_name}`")
    else:
        AWAITING_INPUT_FROM_USERS[user_id] = "model"
        await event.reply(
            f"{BOT_META_INFO_PREFIX}Your current chat model is: `{user_manager.get_prefs(user_id).model}`."
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
            await event.reply(f"{BOT_META_INFO_PREFIX}Your system prompt has been reset to the default.")
        else:
            user_manager.set_system_prompt(user_id, prompt)
            await event.reply(f"{BOT_META_INFO_PREFIX}Your new system prompt has been saved.")
    else:
        AWAITING_INPUT_FROM_USERS[user_id] = "system_prompt"
        current_prompt = user_manager.get_prefs(user_id).system_prompt or "Default (no custom prompt set)"
        await event.reply(
            f"{BOT_META_INFO_PREFIX}**Your current system prompt is:**\n\n```\n{current_prompt}\n```"
            "\n\nPlease send the new system prompt in the next message."
            "\n(You can also send `reset` to restore the default, or `cancel` to stop.)"
        )


# --- New Feature Handlers ---
@borg.on(events.NewMessage(pattern=r"/contextmode", func=lambda e: e.is_private))
async def context_mode_handler(event):
    """Displays buttons to set the conversation context mode."""
    prefs = user_manager.get_prefs(event.sender_id)
    buttons = [KeyboardButtonCallback(
        f"✅ {CONTEXT_MODE_NAMES[mode]}" if prefs.context_mode == mode else CONTEXT_MODE_NAMES[mode],
        data=f"context_{mode}"
    ) for mode in CONTEXT_MODES]
    await event.reply(
        f"{BOT_META_INFO_PREFIX}**Set Conversation Context Mode**\nChoose how I should remember our conversation history.",
        buttons=build_menu(buttons, n_cols=1)
    )

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
        f"{BOT_META_INFO_PREFIX}**Set Reasoning Effort**\nChoose the level of thinking for the model. This may affect response time and cost.",
        buttons=build_menu(buttons, n_cols=2)
    )

@borg.on(events.NewMessage(pattern=r"/tools", func=lambda e: e.is_private))
async def tools_handler(event):
    """Displays buttons to toggle tools."""
    prefs = user_manager.get_prefs(event.sender_id)
    buttons = [KeyboardButtonCallback(
        f"{'✅' if tool in prefs.enabled_tools else '❌'} {tool}", data=f"tool_{tool}"
    ) for tool in AVAILABLE_TOOLS]
    await event.reply(f"{BOT_META_INFO_PREFIX}**Manage Tools**\nToggle available tools for the model.", buttons=build_menu(buttons, n_cols=1))

@borg.on(events.NewMessage(pattern=r"/(enable|disable)(?P<tool_name>\w+)", func=lambda e: e.is_private))
async def toggle_tool_handler(event):
    action = event.pattern_match.group(1)
    tool_name_req = event.pattern_match.group("tool_name").lower()
    matched_tool = next((t for t in AVAILABLE_TOOLS if t.lower() == tool_name_req), None)
    if matched_tool:
        is_enabled = action == "enable"
        user_manager.set_tool_state(event.sender_id, matched_tool, enabled=is_enabled)
        await event.reply(f"{BOT_META_INFO_PREFIX}`{matched_tool}` has been **{action}d**.")
    else:
        await event.reply(f"{BOT_META_INFO_PREFIX}Unknown tool: `{tool_name_req}`. Available: {', '.join(AVAILABLE_TOOLS)}")

@borg.on(events.NewMessage(pattern=r"/json", func=lambda e: e.is_private))
async def json_mode_handler(event):
    """Toggles JSON mode."""
    is_enabled = user_manager.toggle_json_mode(event.sender_id)
    await event.reply(f"{BOT_META_INFO_PREFIX}JSON response mode has been **{'enabled' if is_enabled else 'disabled'}**.")

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
        prefs = user_manager.get_prefs(user_id)
        is_enabled = tool_name not in prefs.enabled_tools
        user_manager.set_tool_state(user_id, tool_name, enabled=is_enabled)
        prefs = user_manager.get_prefs(user_id) # Re-fetch
        buttons = [KeyboardButtonCallback(
            f"{'✅' if tool in prefs.enabled_tools else '❌'} {tool}", data=f"tool_{tool}"
        ) for tool in AVAILABLE_TOOLS]
        await event.edit(buttons=build_menu(buttons, n_cols=1))
        await event.answer(f"{tool_name} {'enabled' if is_enabled else 'disabled'}.")
    elif data_str.startswith("context_"):
        mode = data_str.split("_", 1)[1]
        user_manager.set_context_mode(user_id, mode)
        prefs = user_manager.get_prefs(user_id)
        buttons = [KeyboardButtonCallback(
            f"✅ {CONTEXT_MODE_NAMES[m]}" if prefs.context_mode == m else CONTEXT_MODE_NAMES[m],
            data=f"context_{m}"
        ) for m in CONTEXT_MODES]
        await event.edit(buttons=build_menu(buttons, n_cols=1))
        await event.answer("Context mode updated.")


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
        await event.reply(f"{BOT_META_INFO_PREFIX}Process cancelled.")
        return

    if input_type == "model":
        user_manager.set_model(user_id, text)
        await event.reply(f"{BOT_META_INFO_PREFIX}✅ Your chat model has been updated to: `{text}`")
    elif input_type == "system_prompt":
        if text.lower() == "reset":
            user_manager.set_system_prompt(user_id, "")
            await event.reply(f"{BOT_META_INFO_PREFIX}✅ Your system prompt has been reset to the default.")
        else:
            user_manager.set_system_prompt(user_id, text)
            await event.reply(f"{BOT_META_INFO_PREFIX}✅ Your new system prompt has been saved.")

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

    else:
        # If the message is only a separator and we are in `until_separator` context mode, reply that the context has been cleared.
        prefs = user_manager.get_prefs(user_id)
        if (
            prefs.context_mode == "until_separator"
            and event.text
            and event.text.strip() == CONTEXT_SEPARATOR
        ):
            await event.reply(
                f"{BOT_META_INFO_PREFIX}Context cleared. The conversation will now start fresh from your next message."
            )
            return


    api_key = llm_db.get_api_key(user_id=user_id, service="gemini")
    if not api_key:
        await llm_db.request_api_key_message(event)
        return

    if group_id:
        PROCESSED_GROUP_IDS.add(group_id)

    prefs = user_manager.get_prefs(user_id)
    response_message = await event.reply("...")
    temp_dir = Path(f"./temp_llm_chat_{event.id}/")
    try:
        temp_dir.mkdir(exist_ok=True)

        if group_id:
            await asyncio.sleep(0.1) # Allow album messages to arrive

        messages = await build_conversation_history(event, prefs.context_mode, temp_dir)
        system_prompt_to_use = prefs.system_prompt or DEFAULT_SYSTEM_PROMPT
        messages.insert(0, {"role": "system", "content": system_prompt_to_use})

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
            # final_text += warning_text # No need to clutter the response

        await util.edit_message(response_message, final_text, parse_mode="md", link_preview=False)
        await _log_conversation(event, prefs.model, messages, final_text)

    except Exception:
        error_text = f"{BOT_META_INFO_PREFIX}An error occurred. You can send the inputs that caused this error to the bot developer."
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
