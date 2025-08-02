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
from uniborg import history_util
from uniborg.storage import UserStorage
from uniborg.constants import BOT_META_INFO_PREFIX

# --- Constants and Configuration ---

# Use the litellm model naming convention.
# See https://docs.litellm.ai/docs/providers/gemini
DEFAULT_MODEL = "gemini/gemini-2.5-flash"  #: Do NOT change the default model unless explicitly instructed to.
DEFAULT_SYSTEM_PROMPT = """
You are a helpful and knowledgeable assistant. Your primary audience is advanced STEM postgraduate researchers, so be precise and technically accurate.

**Style Guidelines for Mobile Chat:**
- **Concise & Direct:** Keep responses as brief as possible without sacrificing critical information. Get straight to the point. Exception: Provide full detail when users specifically request lengthy responses.
- **Conversational Tone:** Write in a clear, natural style suitable for a chat conversation. Avoid overly academic or verbose language unless necessary for technical accuracy. You can use emojis.
- **Readability:** Break up text into short paragraphs. Use bullet points or numbered lists to make complex information easy to scan on a small screen.
- **Language:**
    *   Your response must match the language of the user's last message.
    *   To determine the user's language, rely exclusively on the primary content of their message.
    *   Do not consider language found in metadata or attachments.

**Formatting:** You can use Telegram's markdown: `**bold**`, `__italic__`, `` `code` ``, `[links](https://example.com)`, and ```pre``` blocks.
"""

# Directory for logs, mirroring the STT plugin's structure
LOG_DIR = Path(os.path.expanduser("~/.borg/llm_chat/log/"))
LOG_DIR.mkdir(parents=True, exist_ok=True)

# --- New Constants for Features ---
LAST_N_MESSAGES_LIMIT = 50
HISTORY_MESSAGE_LIMIT = 1000
LOG_COUNT_LIMIT = 3
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

# --- Single Source of Truth for Bot Commands ---
BOT_COMMANDS = [
    {"command": "start", "description": "Onboard and set API key"},
    {"command": "help", "description": "Show detailed help and instructions"},
    {"command": "status", "description": "Show your current settings"},
    {
        "command": "log",
        "description": f"Get your last {LOG_COUNT_LIMIT} conversation logs",
    },
    {"command": "setgeminikey", "description": "Set or update your Gemini API key"},
    {"command": "setmodel", "description": "Set your preferred chat model"},
    {"command": "setsystemprompt", "description": "Customize the bot's instructions"},
    {"command": "setthink", "description": "Adjust model's reasoning effort"},
    {
        "command": "contextmode",
        "description": "Change how PRIVATE chat history is read",
    },
    {
        "command": "groupcontextmode",
        "description": "Change how GROUP chat history is read",
    },
    {"command": "tools", "description": "Enable or disable tools like search"},
    {"command": "json", "description": "Toggle JSON output mode"},
]
# Create a set of command strings (e.g., {"/start", "/help"}) for efficient lookup
KNOWN_COMMAND_SET = {f"/{cmd['command']}" for cmd in BOT_COMMANDS}


SAFETY_SETTINGS = [
    {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
]

# --- State Management ---
BOT_USERNAME = None
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
    group_context_mode: str = Field(default="reply_chain")
    metadata_mode: str = Field(default="ONLY_WHEN_NOT_PRIVATE")


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
        if mode not in CONTEXT_MODES:
            return
        prefs = self.get_prefs(user_id)
        prefs.context_mode = mode
        self._save_prefs(user_id, prefs)

    def set_group_context_mode(self, user_id: int, mode: str):
        if mode not in CONTEXT_MODES:
            return
        prefs = self.get_prefs(user_id)
        prefs.group_context_mode = mode
        self._save_prefs(user_id, prefs)


user_manager = UserManager()


# --- Core Logic & Helpers ---


def build_menu(buttons, n_cols):
    """Helper to build a menu of inline buttons in a grid."""
    return [buttons[i : i + n_cols] for i in range(0, len(buttons), n_cols)]


async def _process_media(message, temp_dir: Path) -> Optional[dict]:
    """Downloads media, encodes it, and returns a content part for litellm."""
    if not message or not message.media:
        return None
    try:
        file_path_str = await message.download_media(file=temp_dir)
        if not file_path_str:
            return None
        file_path = Path(file_path_str)
        mime_type, _ = mimetypes.guess_type(file_path)
        # Fallback for some audio/video types
        if not mime_type:
            for ext, m_type in llm_util.MIME_TYPE_MAP.items():  # Use imported map
                if file_path.name.lower().endswith(ext):
                    mime_type = m_type
                    break
        # Fallback for common text file types
        if not mime_type and file_path.suffix.lower() in [
            ".txt",
            ".md",
            ".py",
            ".js",
            ".html",
            ".css",
            ".json",
            ".xml",
            ".log",
        ]:
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
            return {
                "type": "text",
                "text": f"\n--- Attachment: {file_path.name} ---\n{file_bytes.decode('utf-8', errors='ignore')}",
            }
        else:
            b64_content = base64.b64encode(file_bytes).decode("utf-8")
            return {
                "type": "image_url",
                "image_url": {"url": f"data:{mime_type};base64,{b64_content}"},
            }
    except Exception as e:
        print(f"Error processing media from message {message.id}: {e}")
        return None


async def _log_conversation(
    event, model_name: str, messages: list, final_response: str
):
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
            f"Date: {timestamp}",
            f"User ID: {user_id}",
            f"Name: {full_name}",
            f"Username: @{username}",
            f"Model: {model_name}",
            "--- Conversation ---",
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
                    else:  # Handle media attachments in logs
                        log_parts.append("[Attachment: Media Content]")
        log_parts.append("\n[Assistant]:")
        log_parts.append(final_response)
        with open(log_file_path, "w", encoding="utf-8") as f:
            f.write("\n".join(log_parts))
    except Exception as e:
        print(f"Failed to write chat log for user {event.sender_id}: {e}")
        traceback.print_exc()


async def _expand_and_sort_messages_with_groups(
    event, initial_messages: List[Message]
) -> List[Message]:
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

                group_messages = [
                    m for m in messages_in_vicinity if m and m.grouped_id == group_id
                ]
                for group_msg in group_messages:
                    final_messages_map[group_msg.id] = group_msg

                processed_group_ids.add(group_id)
            except Exception as e:
                print(f"Could not expand message group {group_id}: {e}")

    # Return a sorted, unique list of messages
    return sorted(final_messages_map.values(), key=lambda m: m.id)


async def _process_turns_to_history(
    event, message_list: List[Message], temp_dir: Path
) -> List[dict]:
    """
    Processes a final, sorted list of messages into litellm history format,
    grouping consecutive messages from the same sender into 'turns'.
    """
    history = []
    if not message_list:
        return history

    bot_me = await event.client.get_me()
    is_group_chat = not event.is_private
    # The event.sender_id is the user who triggered the handler, so we get their prefs
    user_prefs = user_manager.get_prefs(event.sender_id)
    add_metadata = (
        user_prefs.metadata_mode == "ONLY_WHEN_NOT_PRIVATE"
    ) and is_group_chat

    # Group consecutive messages by sender to create "turns"
    for _, turn_messages_iter in groupby(message_list, key=lambda m: m.sender_id):
        turn_messages = list(turn_messages_iter)
        if not turn_messages:
            continue

        # Get sender info once per turn
        turn_sender_id = turn_messages[0].sender_id
        turn_sender = await event.client.get_entity(turn_sender_id)
        role = "assistant" if turn_sender_id == bot_me.id else "user"

        # Process all messages in this turn and consolidate into one history entry
        text_buffer, media_parts = [], []
        for turn_msg in turn_messages:
            # --- NEW: Filter out bot's meta-info messages from history ---
            if (
                role == "assistant"
                and turn_msg.text
                and turn_msg.text.startswith(BOT_META_INFO_PREFIX)
            ):
                continue

            ##
            #: @seeAlso/6ac96cfacfd852f715e9e3307e7e2b2f
            if (
                role == "user"
                and turn_msg.text
                and turn_msg.text.split(" ", 1)[0] in KNOWN_COMMAND_SET
            ):
                continue
            ##

            # Start with the original text
            processed_text = turn_msg.text

            # Strip prefix if in a group chat and the message is from a user
            if is_group_chat and role == "user" and processed_text and BOT_USERNAME:
                stripped = processed_text.strip()
                if stripped.startswith(BOT_USERNAME):
                    processed_text = stripped[len(BOT_USERNAME) :].strip()

            # Add metadata if required (for user messages in groups)
            if add_metadata and role == "user":
                sender_name = turn_sender.first_name or "Unknown"
                timestamp = turn_msg.date.isoformat()
                metadata_prefix = (
                    f"[User: {sender_name} ({turn_sender_id}) | Timestamp: {timestamp}]"
                )
                if turn_msg.forward:
                    fwd_parts = []

                    # 1. Get Forwarded-From Name
                    fwd_from_name = None
                    fwd_entity = turn_msg.forward.sender or turn_msg.forward.chat
                    if fwd_entity:
                        fwd_from_name = getattr(fwd_entity, 'title', getattr(fwd_entity, 'first_name', None))
                    if not fwd_from_name:
                        fwd_from_name = turn_msg.forward.from_name
                    if fwd_from_name:
                        fwd_parts.append(f"from: {fwd_from_name}")

                    # 2. Get Forwarded-From ID
                    if turn_msg.forward.from_id:
                        fwd_peer_id = getattr(turn_msg.forward.from_id, 'user_id', None) \
                                   or getattr(turn_msg.forward.from_id, 'chat_id', None) \
                                   or getattr(turn_msg.forward.from_id, 'channel_id', None)
                        if fwd_peer_id:
                            fwd_parts.append(f"from_id: {fwd_peer_id}")

                    # 3. Get Original Date
                    if turn_msg.forward.date:
                        fwd_parts.append(f"original date: {turn_msg.forward.date.isoformat()}")

                    # 4. Get Channel Post ID
                    if turn_msg.forward.channel_post:
                        fwd_parts.append(f"post_id: {turn_msg.forward.channel_post}")

                    # 5. Get Post Author Signature
                    if turn_msg.forward.post_author:
                        fwd_parts.append(f"author: {turn_msg.forward.post_author}")

                    # 6. Saved from info (for "Saved Messages")
                    if turn_msg.forward.saved_from_peer:
                        saved_peer_id = getattr(turn_msg.forward.saved_from_peer, 'user_id', None) \
                                   or getattr(turn_msg.forward.saved_from_peer, 'chat_id', None) \
                                   or getattr(turn_msg.forward.saved_from_peer, 'channel_id', None)
                        if saved_peer_id:
                            fwd_parts.append(f"saved_from_peer: {saved_peer_id}")
                    if turn_msg.forward.saved_from_msg_id:
                        fwd_parts.append(f"saved_msg_id: {turn_msg.forward.saved_from_msg_id}")

                    # Assemble the final metadata string
                    if fwd_parts:
                        metadata_prefix += f" [Forwarded ({'; '.join(fwd_parts)})]"

                # Prepend metadata to the (potentially stripped) text
                processed_text = (
                    f"{metadata_prefix}\n{processed_text}"
                    if processed_text
                    else metadata_prefix
                )

            if processed_text:
                text_buffer.append(processed_text)
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

    return history


async def _get_initial_messages_for_reply_chain(event) -> List[Message]:
    if not event.message.reply_to_msg_id:
        return []
    messages = []
    try:
        message = await event.client.get_messages(
            event.chat_id, ids=event.message.reply_to_msg_id
        )
        while message:
            messages.append(message)
            if len(messages) >= HISTORY_MESSAGE_LIMIT:
                break
            if not message.reply_to_msg_id:
                break
            message = await event.client.get_messages(
                event.chat_id, ids=message.reply_to_msg_id
            )
    except Exception:
        pass
    messages.reverse()
    return messages


async def build_conversation_history(event, context_mode: str, temp_dir: Path) -> list:
    """
    Orchestrates the construction of a conversation history based on the user's
    selected context mode, using the centralized history_util.
    """
    messages_to_process = []

    if context_mode == "reply_chain":
        # This mode is special and doesn't use the history cache.
        messages_to_process = await _get_initial_messages_for_reply_chain(event)
        messages_to_process.append(event.message)
    else:
        # These modes use the history cache.
        message_ids = []
        if context_mode == "last_N":
            message_ids = history_util.get_last_n_ids(
                event.chat_id, LAST_N_MESSAGES_LIMIT
            )
        elif context_mode == "until_separator":
            message_ids = history_util.get_all_ids(event.chat_id)

        # Ensure current message is included, then make unique and sort
        all_ids = sorted(list(set(message_ids + [event.id])))

        if all_ids:
            try:
                # Fetch all relevant messages in one bot-friendly call.
                fetched_messages = [
                    m
                    for m in await event.client.get_messages(event.chat_id, ids=all_ids)
                    if m
                ]

                if context_mode == "until_separator":
                    # Find the slice of messages after the last separator.
                    context_slice = []
                    # Iterate backwards from the most recent message.
                    for msg in reversed(fetched_messages):
                        if msg.text and msg.text.strip() == CONTEXT_SEPARATOR:
                            break  # Found separator, stop collecting.
                        context_slice.append(msg)
                    # The slice is in reverse chronological order, so reverse it back.
                    messages_to_process = list(reversed(context_slice))
                else:  # last_N
                    messages_to_process = fetched_messages
            except Exception as e:
                print(f"LLM_Chat: Could not fetch messages from history cache: {e}")
                # Fallback to just the current message on error.
                messages_to_process = [event.message]
        else:
            # If cache is empty, just use the current message.
            messages_to_process = [event.message]

    # The rest of the logic can now be unified.
    # Universally expand groups for the fetched messages
    expanded_messages = await _expand_and_sort_messages_with_groups(
        event, messages_to_process
    )

    # Apply the global message limit as a final safeguard
    if len(expanded_messages) > HISTORY_MESSAGE_LIMIT:
        expanded_messages = expanded_messages[-HISTORY_MESSAGE_LIMIT:]

    # Process the final message list into litellm format
    history = await _process_turns_to_history(event, expanded_messages, temp_dir)
    return history


# --- Bot Command Setup ---


async def set_bot_menu_commands():
    """Sets the bot's command menu in Telegram's UI."""
    global BOT_USERNAME
    if BOT_USERNAME is None:
        try:
            me = await borg.get_me()
            BOT_USERNAME = f"@{me.username}"
        except Exception as e:
            print(
                f"LLM_Chat: Could not get bot username. Group functionality will be disabled. Error: {e}"
            )

    await history_util.initialize_history_handler()

    print("LLM_Chat: setting bot commands ...")
    try:
        # Use the new BOT_COMMANDS constant to build the list
        await borg(
            SetBotCommandsRequest(
                scope=BotCommandScopeDefault(),
                lang_code="en",
                commands=[
                    BotCommand(c["command"], c["description"]) for c in BOT_COMMANDS
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

**▶️ In Private Chats**
To continue a conversation, simply **reply** to my last message. I will remember our previous messages in that chain. To start a new, separate conversation, just send a message without replying to anything.

**▶️ In Group Chats**
To talk to me in a group, start your message with `{BOT_USERNAME or "(my username)"}`. Conversation history works the same way (e.g., reply to my last message in the group to continue a thread).

**▶️ Understanding Conversation Context**
I remember our conversations based on your chosen **Context Mode**. You can set this separately for private and group chats.

- **Reply Chain (Default):** To continue a conversation, simply **reply** to my last message. I will remember our previous messages in that chain.
- **Until Separator:** I will read the reply chain until a message with only `{CONTEXT_SEPARATOR}` is found. This lets you manually define the context length.
- **Last {LAST_N_MESSAGES_LIMIT} Messages:** I will use the last {LAST_N_MESSAGES_LIMIT} messages in our chat as context, regardless of replies.

You can attach **images, audio, video, and text files**. Sending multiple files as an **album** is also supported, and I will see all items in the album.

**Available Commands:**
- /start: Onboard and set up your API key.
- /help: Shows this detailed help message.
- /status: Shows a summary of your current settings.
- /log: Get your last {LOG_COUNT_LIMIT} conversation logs as files.
- /setgeminikey: Sets or updates your Gemini API key.
- /setModel: Change the AI model. Current: `{prefs.model}`.
- /setSystemPrompt: Change my core instructions or reset to default.
- /contextMode: Change how **private** chat history is gathered.
- /groupContextMode: Change how **group** chat history is gathered.
- /setthink: Adjust the model's reasoning effort for complex tasks.
- /tools: Enable/disable tools like Google Search and Code Execution.
- /json: Toggle JSON-only output mode for structured data needs.
"""
    await event.reply(
        f"{BOT_META_INFO_PREFIX}{help_text}", link_preview=False, parse_mode="md"
    )


@borg.on(events.NewMessage(pattern=r"/status", func=lambda e: e.is_private))
async def status_handler(event):
    """Displays a summary of the user's current settings."""
    user_id = event.sender_id
    prefs = user_manager.get_prefs(user_id)
    enabled_tools_str = (
        ", ".join(prefs.enabled_tools) if prefs.enabled_tools else "None"
    )
    system_prompt_status = "Default"
    if prefs.system_prompt and prefs.system_prompt != DEFAULT_SYSTEM_PROMPT:
        system_prompt_status = "Custom"

    context_mode_name = CONTEXT_MODE_NAMES.get(
        prefs.context_mode, prefs.context_mode.replace("_", " ").title()
    )
    group_context_mode_name = CONTEXT_MODE_NAMES.get(
        prefs.group_context_mode, prefs.group_context_mode.replace("_", " ").title()
    )
    thinking_level = prefs.thinking.capitalize() if prefs.thinking else "Default"
    status_message = (
        f"**Your Current Bot Settings**\n\n"
        f"∙ **Model:** `{prefs.model}`\n"
        f"∙ **Private Context Mode:** `{context_mode_name}`\n"
        f"∙ **Group Context Mode:** `{group_context_mode_name}`\n"
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
            await event.reply(
                f"{BOT_META_INFO_PREFIX}You have no conversation logs yet."
            )
            return

        logs_to_send = log_files[:LOG_COUNT_LIMIT]

        await event.reply(
            f"{BOT_META_INFO_PREFIX}Sending your last {len(logs_to_send)} conversation log(s)..."
        )
        # `of {len(log_files)}`

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


@borg.on(
    events.NewMessage(
        pattern=r"(?i)/setGeminiKey(?:\s+(.*))?", func=lambda e: e.is_private
    )
)
async def set_key_handler(event):
    """Delegates /setgeminikey command logic to the shared module."""
    await llm_db.handle_set_key_command(event)


@borg.on(
    events.NewMessage(
        func=lambda e: e.is_private
        and llm_db.is_awaiting_key(e.sender_id)
        and e.text
        and not e.text.startswith("/")
    )
)
async def key_submission_handler(event):
    """Delegates plain-text key submission logic to the shared module."""
    await llm_db.handle_key_submission(
        event, success_msg="You can now start chatting with me."
    )


@borg.on(
    events.NewMessage(pattern=r"/setModel(?:\s+(.*))?", func=lambda e: e.is_private)
)
async def set_model_handler(event):
    """Sets the user's preferred chat model, now with an interactive flow."""
    user_id = event.sender_id
    model_name_match = event.pattern_match.group(1)

    if model_name_match:
        model_name = model_name_match.strip()
        user_manager.set_model(user_id, model_name)
        cancel_input_flow(user_id)
        await event.reply(
            f"{BOT_META_INFO_PREFIX}Your chat model has been set to: `{model_name}`"
        )
    else:
        AWAITING_INPUT_FROM_USERS[user_id] = "model"
        await event.reply(
            f"{BOT_META_INFO_PREFIX}Your current chat model is: `{user_manager.get_prefs(user_id).model}`."
            "\n\nPlease send the new model ID in the next message."
            "\n(Type `cancel` to stop this process.)"
        )


@borg.on(
    events.NewMessage(
        pattern=r"/setSystemPrompt(?:\s+([\s\S]+))?", func=lambda e: e.is_private
    )
)
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
            await event.reply(
                f"{BOT_META_INFO_PREFIX}Your system prompt has been reset to the default."
            )
        else:
            user_manager.set_system_prompt(user_id, prompt)
            await event.reply(
                f"{BOT_META_INFO_PREFIX}Your new system prompt has been saved."
            )
    else:
        AWAITING_INPUT_FROM_USERS[user_id] = "system_prompt"
        current_prompt = (
            user_manager.get_prefs(user_id).system_prompt
            or "Default (no custom prompt set)"
        )
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
    buttons = [
        KeyboardButtonCallback(
            (
                f"✅ {CONTEXT_MODE_NAMES[mode]}"
                if prefs.context_mode == mode
                else CONTEXT_MODE_NAMES[mode]
            ),
            data=f"context_{mode}",
        )
        for mode in CONTEXT_MODES
    ]
    await event.reply(
        f"{BOT_META_INFO_PREFIX}**Set Private Chat Context Mode**\nChoose how I should remember our conversation history in private chats.",
        buttons=build_menu(buttons, n_cols=1),
    )


@borg.on(events.NewMessage(pattern=r"/groupcontextmode", func=lambda e: e.is_private))
async def group_context_mode_handler(event):
    """Displays buttons to set the GROUP conversation context mode."""
    prefs = user_manager.get_prefs(event.sender_id)
    buttons = [
        KeyboardButtonCallback(
            (
                f"✅ {CONTEXT_MODE_NAMES[mode]}"
                if prefs.group_context_mode == mode
                else CONTEXT_MODE_NAMES[mode]
            ),
            data=f"groupcontext_{mode}",
        )
        for mode in CONTEXT_MODES
    ]
    await event.reply(
        f"{BOT_META_INFO_PREFIX}**Set Group Chat Context Mode**\nChoose how I should remember our conversation history in groups.",
        buttons=build_menu(buttons, n_cols=1),
    )


@borg.on(events.NewMessage(pattern=r"/setthink", func=lambda e: e.is_private))
async def set_think_handler(event):
    """Displays buttons to set the reasoning effort."""
    prefs = user_manager.get_prefs(event.sender_id)
    buttons = [
        KeyboardButtonCallback(
            f"✅ {lvl.capitalize()}" if prefs.thinking == lvl else lvl.capitalize(),
            data=f"think_{lvl}",
        )
        for lvl in REASONING_LEVELS
    ]
    clear_text = "Clear (Default)"
    buttons.append(
        KeyboardButtonCallback(
            f"✅ {clear_text}" if prefs.thinking is None else clear_text,
            data="think_clear",
        )
    )
    await event.reply(
        f"{BOT_META_INFO_PREFIX}**Set Reasoning Effort**\nChoose the level of thinking for the model. This may affect response time and cost.",
        buttons=build_menu(buttons, n_cols=2),
    )


@borg.on(events.NewMessage(pattern=r"/tools", func=lambda e: e.is_private))
async def tools_handler(event):
    """Displays buttons to toggle tools."""
    prefs = user_manager.get_prefs(event.sender_id)
    buttons = [
        KeyboardButtonCallback(
            f"{'✅' if tool in prefs.enabled_tools else '❌'} {tool}",
            data=f"tool_{tool}",
        )
        for tool in AVAILABLE_TOOLS
    ]
    await event.reply(
        f"{BOT_META_INFO_PREFIX}**Manage Tools**\nToggle available tools for the model.",
        buttons=build_menu(buttons, n_cols=1),
    )


@borg.on(
    events.NewMessage(
        pattern=r"/(enable|disable)(?P<tool_name>\w+)", func=lambda e: e.is_private
    )
)
async def toggle_tool_handler(event):
    action = event.pattern_match.group(1)
    tool_name_req = event.pattern_match.group("tool_name").lower()
    matched_tool = next(
        (t for t in AVAILABLE_TOOLS if t.lower() == tool_name_req), None
    )
    if matched_tool:
        is_enabled = action == "enable"
        user_manager.set_tool_state(event.sender_id, matched_tool, enabled=is_enabled)
        await event.reply(
            f"{BOT_META_INFO_PREFIX}`{matched_tool}` has been **{action}d**."
        )
    else:
        await event.reply(
            f"{BOT_META_INFO_PREFIX}Unknown tool: `{tool_name_req}`. Available: {', '.join(AVAILABLE_TOOLS)}"
        )


@borg.on(events.NewMessage(pattern=r"/json", func=lambda e: e.is_private))
async def json_mode_handler(event):
    """Toggles JSON mode."""
    is_enabled = user_manager.toggle_json_mode(event.sender_id)
    await event.reply(
        f"{BOT_META_INFO_PREFIX}JSON response mode has been **{'enabled' if is_enabled else 'disabled'}**."
    )


@borg.on(events.CallbackQuery())
async def callback_handler(event):
    """Handles all inline button presses for the plugin."""
    data_str = event.data.decode("utf-8")
    user_id = event.sender_id
    if data_str.startswith("think_"):
        level = data_str.split("_")[1]
        user_manager.set_thinking(user_id, None if level == "clear" else level)
        prefs = user_manager.get_prefs(user_id)
        buttons = [
            KeyboardButtonCallback(
                f"✅ {lvl.capitalize()}" if prefs.thinking == lvl else lvl.capitalize(),
                data=f"think_{lvl}",
            )
            for lvl in REASONING_LEVELS
        ]
        clear_text = "Clear (Default)"
        buttons.append(
            KeyboardButtonCallback(
                f"✅ {clear_text}" if prefs.thinking is None else clear_text,
                data="think_clear",
            )
        )
        await event.edit(buttons=build_menu(buttons, n_cols=2))
        await event.answer("Thinking preference updated.")
    elif data_str.startswith("tool_"):
        tool_name = data_str.split("_")[1]
        prefs = user_manager.get_prefs(user_id)
        is_enabled = tool_name not in prefs.enabled_tools
        user_manager.set_tool_state(user_id, tool_name, enabled=is_enabled)
        prefs = user_manager.get_prefs(user_id)  # Re-fetch
        buttons = [
            KeyboardButtonCallback(
                f"{'✅' if tool in prefs.enabled_tools else '❌'} {tool}",
                data=f"tool_{tool}",
            )
            for tool in AVAILABLE_TOOLS
        ]
        await event.edit(buttons=build_menu(buttons, n_cols=1))
        await event.answer(f"{tool_name} {'enabled' if is_enabled else 'disabled'}.")
    elif data_str.startswith("context_"):
        mode = data_str.split("_", 1)[1]
        user_manager.set_context_mode(user_id, mode)
        prefs = user_manager.get_prefs(user_id)
        buttons = [
            KeyboardButtonCallback(
                (
                    f"✅ {CONTEXT_MODE_NAMES[m]}"
                    if prefs.context_mode == m
                    else CONTEXT_MODE_NAMES[m]
                ),
                data=f"context_{m}",
            )
            for m in CONTEXT_MODES
        ]
        await event.edit(buttons=build_menu(buttons, n_cols=1))
        await event.answer("Private context mode updated.")
    elif data_str.startswith("groupcontext_"):
        mode = data_str.split("_", 1)[1]
        user_manager.set_group_context_mode(user_id, mode)
        prefs = user_manager.get_prefs(user_id)
        buttons = [
            KeyboardButtonCallback(
                (
                    f"✅ {CONTEXT_MODE_NAMES[m]}"
                    if prefs.group_context_mode == m
                    else CONTEXT_MODE_NAMES[m]
                ),
                data=f"groupcontext_{m}",
            )
            for m in CONTEXT_MODES
        ]
        await event.edit(buttons=build_menu(buttons, n_cols=1))
        await event.answer("Group context mode updated.")


@borg.on(
    events.NewMessage(
        func=lambda e: e.is_private
        and e.sender_id in AWAITING_INPUT_FROM_USERS
        and e.text
        and not e.text.startswith("/")
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
        await event.reply(
            f"{BOT_META_INFO_PREFIX}✅ Your chat model has been updated to: `{text}`"
        )
    elif input_type == "system_prompt":
        if text.lower() == "reset":
            user_manager.set_system_prompt(user_id, "")
            await event.reply(
                f"{BOT_META_INFO_PREFIX}✅ Your system prompt has been reset to the default."
            )
        else:
            user_manager.set_system_prompt(user_id, text)
            await event.reply(
                f"{BOT_META_INFO_PREFIX}✅ Your new system prompt has been saved."
            )

    cancel_input_flow(user_id)


def is_valid_chat_message(event: events.NewMessage.Event) -> bool:
    """
    Determines if a message is a valid conversational message to be
    processed by the main chat handler.
    """
    # Must have some content (text or media)
    if not (event.text or event.media):
        return False

    # Not a forward
    if event.forward:
        return False

    # If it's a private chat
    if event.is_private:
        if event.text:
            #: @seeAlso/6ac96cfacfd852f715e9e3307e7e2b2f
            first_word = event.text.split(" ", 1)[0]
            if first_word in KNOWN_COMMAND_SET:
                return False  # It's a command, not a chat message
            ##
        return True  # Is a valid private chat message

    # If it's a group chat, it must start with the bot's username
    if not event.is_private:
        if event.text and BOT_USERNAME and event.text.strip().startswith(BOT_USERNAME):
            return True

    return False


@borg.on(events.NewMessage(func=is_valid_chat_message))
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
        # Check for context separator, now considering group chats
        is_group_chat = not event.is_private
        prefs = user_manager.get_prefs(user_id)

        # Determine which context mode is active
        active_context_mode = (
            prefs.group_context_mode if is_group_chat else prefs.context_mode
        )

        if active_context_mode == "until_separator" and event.text:
            text_to_check = event.text.strip()
            if is_group_chat and BOT_USERNAME and text_to_check.startswith(BOT_USERNAME):
                # For groups, check for the separator after the bot's name
                text_to_check = text_to_check[len(BOT_USERNAME) :].strip()

            if text_to_check == CONTEXT_SEPARATOR:
                reply_text = "Context cleared. The conversation will now start fresh from your next message"
                if is_group_chat:
                    reply_text += " mentioning me."
                else:
                    reply_text += "."
                await event.reply(f"{BOT_META_INFO_PREFIX}{reply_text}")
                return

    api_key = llm_db.get_api_key(user_id=user_id, service="gemini")
    if not api_key:
        await llm_db.request_api_key_message(event)
        return

    if group_id:
        PROCESSED_GROUP_IDS.add(group_id)

    prefs = user_manager.get_prefs(user_id)
    response_message = await event.reply(f"{BOT_META_INFO_PREFIX}...")
    temp_dir = Path(f"./temp_llm_chat_{event.id}/")
    try:
        temp_dir.mkdir(exist_ok=True)

        if group_id:
            await asyncio.sleep(0.1)  # Allow album messages to arrive

        # Select context_mode based on chat type
        context_mode_to_use = (
            prefs.group_context_mode if not event.is_private else prefs.context_mode
        )

        messages = await build_conversation_history(
            event, context_mode_to_use, temp_dir
        )
        system_prompt_to_use = prefs.system_prompt or DEFAULT_SYSTEM_PROMPT
        messages.insert(0, {"role": "system", "content": system_prompt_to_use})

        # --- Construct API call arguments ---
        is_gemini_model = re.search(r"\bgemini\b", prefs.model, re.IGNORECASE)
        warnings = []

        api_kwargs = {
            "model": prefs.model,
            "messages": messages,
            "api_key": api_key,
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
                        await util.edit_message(
                            response_message, f"{response_text}▌", parse_mode="md"
                        )
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
            warning_text = "\n\n---\n**Note:**\n" + "\n".join(
                f"- {w}" for w in warnings
            )
            # final_text += warning_text # No need to clutter the response

        await util.edit_message(
            response_message, final_text, parse_mode="md", link_preview=False
        )
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
