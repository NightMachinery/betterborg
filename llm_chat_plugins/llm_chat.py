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
from typing import Optional, List, Dict

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
PROMPT_REPLACEMENTS = {
    re.compile(r"^\.ocr$", re.IGNORECASE): r"""
You will be given a series of images that are part of a single, related sequence. Your task is to perform OCR and combine the text from all images into one final, coherent output, following these specific rules:

*Combine Text:* Transcribe and merge the text from all images into a single, continuous document. Ensure the text flows in the correct sequence from one image to the next.

*No Commentary:* The final output must not contain any of your own commentary, explanations, or headers like "OCR Result" or "Image 1." It should only be the transcribed text itself.

*Consolidate Recurring Information:* Identify any information that is repeated across multiple images, such as headers, footers, author names, social media handles, logos, advertisements, or contact details. Remove these repetitions from the main body of the text.

*Create a Single Header and Footer:* Place all the consolidated, recurring information you identified in the previous step just once at the very beginning and the very end of the document, creating a clean header and a clean footer.

The goal is to produce a single, clean document as if it were the original, without the page breaks and repeated headers or footers from the images.
"""
}
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
GROUP_ACTIVATION_MODES = {
    "mention_only": "Mention Only",
    "mention_and_reply": "Mention and Replies",
}
METADATA_MODES = {
    "no_metadata": "No Metadata (Merged Turns)",
    "separate_turns": "Separate Turns",
    "only_forwarded": "Only Forwarded Metadata",
    "full_metadata": "Full Metadata",
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
    {
        "command": "metadatamode",
        "description": "Change how PRIVATE chat metadata is handled",
    },
    {
        "command": "groupmetadatamode",
        "description": "Change how GROUP chat metadata is handled",
    },
    {
        "command": "groupactivationmode",
        "description": "Change how the bot is triggered in groups",
    },
    {"command": "tools", "description": "Enable or disable tools like search"},
    {"command": "json", "description": "Toggle JSON output mode"},
]
# Create a set of command strings (e.g., {"/start", "/help"}) for efficient lookup
KNOWN_COMMAND_SET = {f"/{cmd['command']}".lower() for cmd in BOT_COMMANDS}


SAFETY_SETTINGS = [
    {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
]

# --- State Management ---
BOT_USERNAME = None
BOT_ID = None
PROCESSED_GROUP_IDS = set()
AWAITING_INPUT_FROM_USERS = {}
IS_BOT = None
USERBOT_HISTORY_CACHE = {}


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
    group_activation_mode: str = Field(default="mention_and_reply")
    metadata_mode: str = Field(default="only_forwarded")
    group_metadata_mode: str = Field(default="full_metadata")


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

    def set_metadata_mode(self, user_id: int, mode: str):
        if mode not in METADATA_MODES:
            return
        prefs = self.get_prefs(user_id)
        prefs.metadata_mode = mode
        self._save_prefs(user_id, prefs)

    def set_group_metadata_mode(self, user_id: int, mode: str):
        if mode not in METADATA_MODES:
            return
        prefs = self.get_prefs(user_id)
        prefs.group_metadata_mode = mode
        self._save_prefs(user_id, prefs)

    def set_group_activation_mode(self, user_id: int, mode: str):
        if mode not in GROUP_ACTIVATION_MODES:
            return
        prefs = self.get_prefs(user_id)
        prefs.group_activation_mode = mode
        self._save_prefs(user_id, prefs)


user_manager = UserManager()


# --- Core Logic & Helpers ---


async def present_options(
    event,
    *,
    title: str,
    options: Dict[str, str],
    current_value: any,
    callback_prefix: str,
    awaiting_key: str,
    n_cols: int = 2,
):
    """
    Presents options to the user either as buttons (bot) or a text menu (userbot).
    """
    user_id = event.sender_id
    if IS_BOT:
        buttons = [
            KeyboardButtonCallback(
                f"✅ {display_name}" if key == current_value else display_name,
                data=f"{callback_prefix}{key}",
            )
            for key, display_name in options.items()
        ]
        await event.reply(
            f"{BOT_META_INFO_PREFIX}**{title}**",
            buttons=util.build_menu(buttons, n_cols=n_cols),
        )
    else:
        option_keys = list(options.keys())
        menu_text = [f"**{title}**\n"]
        for i, key in enumerate(option_keys):
            display_name = options[key]
            prefix = "✅ " if key == current_value else ""
            menu_text.append(f"{i + 1}. {prefix}{display_name}")

        menu_text.append("\nPlease reply with the number of your choice.")
        menu_text.append("(Type `cancel` to stop.)")

        AWAITING_INPUT_FROM_USERS[user_id] = {
            "type": awaiting_key,
            "keys": option_keys,
        }
        await event.reply(f"{BOT_META_INFO_PREFIX}\n".join(menu_text))


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
            ".yaml",
            ".csv",
            ".sql",
            ".java",
            ".c",
            ".h",
            ".cpp",
            ".go",
            ".sh",
            ".rb",
            ".swift",
            ".toml",
            ".conf",
            ".ini",
            ".org",
            ".m",
            ".applescript",
            ".as",
            ".osa",
            ".nu",
            ".nush",
            ".el",
            ".ss",
            ".scm",
            ".lisp",
            ".rkt",
            ".jl",
            ".scala",
            ".sc",
            ".kt",
            ".clj",
            ".cljs",
            ".jxa",
            ".dart",
            ".rs",
            ".cr",
            ".zsh",
            ".dash",
            ".bash",
            # ".ml",
            ".php",
            ".lua",
            ".glsl",
            ".frag",
            ".cson",
            ".plist",
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


async def _get_user_metadata_prefix(message: Message) -> str:
    """Generates the user and timestamp part of the metadata prefix."""
    sender = await message.get_sender()
    sender_name = getattr(sender, "first_name", None) or "Unknown"
    timestamp = message.date.isoformat()

    if message.sender_id == BOT_ID:
        #: no need to inject useless metadata about the bot itself
        return ""
    else:
        return f"[Sender: {sender_name} (ID: {message.sender_id}) | Sending Date: {timestamp}]"


async def _get_forward_metadata_prefix(message: Message) -> str:
    """Generates the forwarded part of the metadata prefix, if applicable."""
    if not message.forward:
        return ""

    fwd_parts = []
    fwd_from_name = None
    fwd_entity = message.forward.sender or message.forward.chat
    if fwd_entity:
        fwd_from_name = getattr(
            fwd_entity, "title", getattr(fwd_entity, "first_name", None)
        )
    if not fwd_from_name:
        fwd_from_name = message.forward.from_name
    if fwd_from_name:
        fwd_parts.append(f"From (Name): {fwd_from_name}")

    if message.forward.from_id:
        fwd_peer_id = (
            getattr(message.forward.from_id, "user_id", None)
            or getattr(message.forward.from_id, "chat_id", None)
            or getattr(message.forward.from_id, "channel_id", None)
        )
        if fwd_peer_id:
            fwd_parts.append(f"From (ID): {fwd_peer_id}")

    if message.forward.date:
        fwd_parts.append(f"Original Message Date: {message.forward.date.isoformat()}")

    if message.forward.channel_post:
        fwd_parts.append(f"Post's ID in Channel: {message.forward.channel_post}")

    if message.forward.post_author:
        fwd_parts.append(f"Post Author: {message.forward.post_author}")

    if message.forward.saved_from_peer:
        saved_peer_id = (
            getattr(message.forward.saved_from_peer, "user_id", None)
            or getattr(message.forward.saved_from_peer, "chat_id", None)
            or getattr(message.forward.saved_from_peer, "channel_id", None)
        )
        if saved_peer_id:
            fwd_parts.append(f"Saved From ID: {saved_peer_id}")
    if message.forward.saved_from_msg_id:
        fwd_parts.append(f"Saved Message ID: {message.forward.saved_from_msg_id}")

    if fwd_parts:
        return f"[Forwarded ({'; '.join(fwd_parts)})]"
    return ""


async def _process_message_content(
    message: Message, temp_dir: Path, metadata_prefix: str = ""
) -> tuple[list, list]:
    """Processes a single message's text and media into litellm content parts."""
    text_buffer, media_parts = [], []
    role = "assistant" if message.sender_id == BOT_ID else "user"

    # Filter out meta-info messages and commands from history
    if (
        role == "assistant"
        and message.text
        and message.text.startswith(BOT_META_INFO_PREFIX)
    ):
        return [], []
    if (
        role == "user"
        and message.text
        and message.text.split(" ", 1)[0].lower() in KNOWN_COMMAND_SET
    ):
        return [], []

    processed_text = message.text
    if role == "user" and processed_text:
        stripped_text = processed_text.strip()
        for pattern, replacement in PROMPT_REPLACEMENTS.items():
            if pattern.fullmatch(stripped_text):
                processed_text = replacement
                break

    if not message.is_private and role == "user" and processed_text and BOT_USERNAME:
        stripped = processed_text.strip()
        if stripped.startswith(BOT_USERNAME):
            processed_text = stripped[len(BOT_USERNAME) :].strip()

    if metadata_prefix:
        processed_text = (
            f"{metadata_prefix}\n{processed_text}"
            if processed_text
            else metadata_prefix
        )

    if processed_text:
        text_buffer.append(processed_text)

    media_part = await _process_media(message, temp_dir)
    if media_part:
        media_parts.append(media_part)

    return text_buffer, media_parts


async def _finalize_content_parts(text_buffer: list, media_parts: list) -> list:
    """Combines text and media parts into a final list for a history entry."""
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

    return content_parts


async def _process_turns_to_history(
    event, message_list: List[Message], temp_dir: Path
) -> List[dict]:
    """
    Processes a final, sorted list of messages into litellm history format,
    respecting the user's chosen metadata and context settings.
    """
    history = []
    if not message_list:
        return history

    user_prefs = user_manager.get_prefs(event.sender_id)
    active_metadata_mode = (
        user_prefs.group_metadata_mode
        if not event.is_private
        else user_prefs.metadata_mode
    )

    # --- Mode 1: No Metadata (Merge consecutive messages) ---
    if active_metadata_mode == "no_metadata":
        for _, turn_messages_iter in groupby(message_list, key=lambda m: m.sender_id):
            turn_messages = list(turn_messages_iter)
            if not turn_messages:
                continue

            role = "assistant" if turn_messages[0].sender_id == BOT_ID else "user"
            text_buffer, media_parts = [], []

            for turn_msg in turn_messages:
                # Process content without any metadata prefix
                msg_texts, msg_media = await _process_message_content(
                    turn_msg, temp_dir
                )
                text_buffer.extend(msg_texts)
                media_parts.extend(msg_media)

            if not text_buffer and not media_parts:
                continue

            final_content_parts = await _finalize_content_parts(
                text_buffer, media_parts
            )
            if not final_content_parts:
                continue

            final_content = (
                final_content_parts[0]["text"]
                if len(final_content_parts) == 1
                and final_content_parts[0]["type"] == "text"
                else final_content_parts
            )
            history.append({"role": role, "content": final_content})

    # --- Modes 2, 3, 4: Separate Turns ---
    else:
        for message in message_list:
            role = "assistant" if message.sender_id == BOT_ID else "user"
            prefix_parts = []

            if active_metadata_mode == "full_metadata":
                prefix_parts.append(await _get_user_metadata_prefix(message))

                if message.forward:
                    prefix_parts.append(await _get_forward_metadata_prefix(message))

            elif active_metadata_mode == "only_forwarded":
                if message.forward:
                    prefix_parts.append(await _get_forward_metadata_prefix(message))

            metadata_prefix = " ".join(prefix_parts)

            text_buffer, media_parts = await _process_message_content(
                message, temp_dir, metadata_prefix
            )

            if not text_buffer and not media_parts:
                continue

            final_content_parts = await _finalize_content_parts(
                text_buffer, media_parts
            )
            if not final_content_parts:
                continue

            final_content = (
                final_content_parts[0]["text"]
                if len(final_content_parts) == 1
                and final_content_parts[0]["type"] == "text"
                else final_content_parts
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
    selected context mode, using the appropriate method for a bot or userbot.
    """
    messages_to_process = []
    chat_id = event.chat_id

    if IS_BOT:
        # --- Bot Logic (using history_util cache) ---
        if context_mode == "reply_chain":
            messages_to_process = await _get_initial_messages_for_reply_chain(event)
            messages_to_process.append(event.message)
        else:
            message_ids = []
            if context_mode == "last_N":
                message_ids = history_util.get_last_n_ids(
                    chat_id, LAST_N_MESSAGES_LIMIT
                )
            elif context_mode == "until_separator":
                message_ids = history_util.get_all_ids(chat_id)

            all_ids = sorted(list(set(message_ids + [event.id])))
            if all_ids:
                try:
                    fetched_messages = [
                        m
                        for m in await event.client.get_messages(chat_id, ids=all_ids)
                        if m
                    ]
                    if context_mode == "until_separator":
                        context_slice = []
                        for msg in reversed(fetched_messages):
                            if msg.text and msg.text.strip() == CONTEXT_SEPARATOR:
                                break
                            context_slice.append(msg)
                        messages_to_process = list(reversed(context_slice))
                    else:  # last_N
                        messages_to_process = fetched_messages
                except Exception as e:
                    print(f"LLM_Chat (Bot): Could not fetch from history cache: {e}")
                    messages_to_process = [event.message]
            else:
                messages_to_process = [event.message]
    else:
        # --- Userbot Logic (using direct API calls + cache) ---
        if context_mode == "reply_chain":
            messages_to_process = await _get_initial_messages_for_reply_chain(event)
            messages_to_process.append(event.message)

        elif context_mode == "last_N":
            history_iter = event.client.iter_messages(
                chat_id, limit=LAST_N_MESSAGES_LIMIT
            )
            messages_to_process = [msg async for msg in history_iter]
            messages_to_process.reverse()

        elif context_mode == "until_separator":
            cached_history = USERBOT_HISTORY_CACHE.get(chat_id)
            if not cached_history:
                full_history = [
                    msg
                    async for msg in event.client.iter_messages(
                        chat_id, limit=HISTORY_MESSAGE_LIMIT
                    )
                ]
                cached_history = list(reversed(full_history))
                USERBOT_HISTORY_CACHE[chat_id] = cached_history
            else:
                last_id = cached_history[-1].id
                if event.id > last_id:
                    new_messages = [
                        msg
                        async for msg in event.client.iter_messages(
                            chat_id, min_id=last_id
                        )
                    ]
                    cached_history.extend(list(reversed(new_messages)))
                    if len(cached_history) > HISTORY_MESSAGE_LIMIT:
                        cached_history = cached_history[-HISTORY_MESSAGE_LIMIT:]
                    USERBOT_HISTORY_CACHE[chat_id] = cached_history

            context_slice = []
            for msg in reversed(cached_history):
                context_slice.append(msg)
                if msg.text and msg.text.strip() == CONTEXT_SEPARATOR:
                    break
            messages_to_process = list(reversed(context_slice))

    # --- Universal Post-Processing ---
    expanded_messages = await _expand_and_sort_messages_with_groups(
        event, messages_to_process
    )
    if len(expanded_messages) > HISTORY_MESSAGE_LIMIT:
        expanded_messages = expanded_messages[-HISTORY_MESSAGE_LIMIT:]

    return await _process_turns_to_history(event, expanded_messages, temp_dir)


# --- Bot/Userbot Initialization ---


async def initialize_llm_chat():
    """Initializes the plugin based on whether it's a bot or userbot."""
    global BOT_ID, BOT_USERNAME, IS_BOT
    if IS_BOT is None:
        IS_BOT = await borg.is_bot()

    if BOT_USERNAME is None:
        me = await borg.get_me()

        BOT_ID = me.id

        if me.username:
            BOT_USERNAME = f"@{me.username}"
        else:
            if not IS_BOT:
                print(
                    "LLM_Chat (Userbot): No username found. Group mention features will be unavailable."
                )

    if IS_BOT:
        await history_util.initialize_history_handler()
        print("LLM_Chat: Running as a BOT. History utility initialized.")

        print("LLM_Chat: Setting bot commands...")
        try:
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
    else:
        print(
            "LLM_Chat: Running as a USERBOT. History utility and bot commands skipped."
        )


# --- Telethon Event Handlers ---


@borg.on(events.NewMessage(pattern=r"(?i)/start", func=lambda e: e.is_private))
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


@borg.on(events.NewMessage(pattern=r"(?i)/help", func=lambda e: e.is_private))
async def help_handler(event):
    """Provides detailed help information about features and usage."""
    if llm_db.is_awaiting_key(event.sender_id):
        llm_db.cancel_key_flow(event.sender_id)
        await event.reply(f"{BOT_META_INFO_PREFIX}API key setup cancelled.")
    cancel_input_flow(event.sender_id)
    prefs = user_manager.get_prefs(event.sender_id)

    # Dynamically build the group trigger instructions based on user settings
    activation_instructions = []
    if BOT_USERNAME:
        activation_instructions.append(f"start your message with `{BOT_USERNAME}`")
    if prefs.group_activation_mode == "mention_and_reply":
        activation_instructions.append("**reply** to one of my messages")

    if not activation_instructions:
        activation_instructions.append(
            "ask the bot developer to set a username for this bot and then start your message with `@bot_username`"
        )

    group_trigger_text = " or ".join(activation_instructions)

    help_text = f"""
**Hello! I am a Telegram chat bot powered by Google's Gemini.** It's like ChatGPT but in Telegram!

To get started, you'll need a free Gemini API key. Send me /setgeminikey to help you set this up.

**How to Chat with Me**

**▶️ In Private Chats**
To continue a conversation, simply **reply** to my last message. I will remember our previous messages in that chain. To start a new, separate conversation, just send a message without replying to anything.

**▶️ In Group Chats**
To talk to me in a group, {group_trigger_text}. Conversation history works the same way (e.g., reply to my last message in the group to continue a thread).

**▶️ Understanding Conversation Context**
I remember our conversations based on your chosen settings. You can configure these separately for private and group chats.

- **Context Mode:** This controls *which* messages are included.
  - `Reply Chain (Default)`: Only messages in the current reply thread.
  - `Until Separator`: The reply chain up to a message containing only `{CONTEXT_SEPARATOR}`.
  - `Last {LAST_N_MESSAGES_LIMIT} Messages`: The most recent messages in the chat.

- **Metadata Mode:** This controls *how* messages are formatted for the AI.
  - `No Metadata`: Merges consecutive messages and adds no extra info.
  - `Separate Turns`: Each message is a new turn, but no extra info.
  - `Only Forwarded`: Adds sender/time details only to forwarded messages.
  - `Full Metadata`: Adds sender/time details to every message (in groups).

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
- /metadataMode: Change how **private** chat metadata is handled.
- /groupMetadataMode: Change how **group** chat metadata is handled.
- /groupActivationMode: Change how I am triggered in groups.
- /setthink: Adjust the model's reasoning effort for complex tasks.
- /tools: Enable/disable tools like Google Search and Code Execution.
- /json: Toggle JSON-only output mode for structured data needs.
"""
    await event.reply(
        f"{BOT_META_INFO_PREFIX}{help_text}", link_preview=False, parse_mode="md"
    )


@borg.on(events.NewMessage(pattern=r"(?i)/status", func=lambda e: e.is_private))
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
    metadata_mode_name = METADATA_MODES.get(
        prefs.metadata_mode, prefs.metadata_mode.replace("_", " ").title()
    )
    group_metadata_mode_name = METADATA_MODES.get(
        prefs.group_metadata_mode, prefs.group_metadata_mode.replace("_", " ").title()
    )
    group_activation_mode_name = GROUP_ACTIVATION_MODES.get(
        prefs.group_activation_mode,
        prefs.group_activation_mode.replace("_", " ").title(),
    )
    thinking_level = prefs.thinking.capitalize() if prefs.thinking else "Default"
    status_message = (
        f"**Your Current Bot Settings**\n\n"
        f"∙ **Model:** `{prefs.model}`\n"
        f"∙ **Reasoning Level:** `{thinking_level}`\n"
        f"∙ **Enabled Tools:** `{enabled_tools_str}`\n"
        f"∙ **JSON Mode:** `{'Enabled' if prefs.json_mode else 'Disabled'}`\n"
        f"∙ **System Prompt:** `{system_prompt_status}`\n\n"
        f"**Private Chat Settings**\n"
        f"∙ **Context Mode:** `{context_mode_name}`\n"
        f"∙ **Metadata Mode:** `{metadata_mode_name}`\n\n"
        f"**Group Chat Settings**\n"
        f"∙ **Context Mode:** `{group_context_mode_name}`\n"
        f"∙ **Metadata Mode:** `{group_metadata_mode_name}`\n"
        f"∙ **Activation:** `{group_activation_mode_name}`\n"
    )
    await event.reply(f"{BOT_META_INFO_PREFIX}{status_message}", parse_mode="md")


@borg.on(events.NewMessage(pattern=r"(?i)/log", func=lambda e: e.is_private))
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
        pattern=r"(?i)/setgeminikey(?:\s+(.*))?", func=lambda e: e.is_private
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
    events.NewMessage(pattern=r"(?i)/setmodel(?:\s+(.*))?", func=lambda e: e.is_private)
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
        AWAITING_INPUT_FROM_USERS[user_id] = {"type": "model"}
        await event.reply(
            f"{BOT_META_INFO_PREFIX}Your current chat model is: `{user_manager.get_prefs(user_id).model}`."
            "\n\nPlease send the new model ID in the next message."
            "\n(Type `cancel` to stop this process.)"
        )


@borg.on(
    events.NewMessage(
        pattern=r"(?i)/setsystemprompt(?:\s+([\s\S]+))?", func=lambda e: e.is_private
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
        AWAITING_INPUT_FROM_USERS[user_id] = {"type": "system_prompt"}
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
@borg.on(events.NewMessage(pattern=r"(?i)/contextmode", func=lambda e: e.is_private))
async def context_mode_handler(event):
    prefs = user_manager.get_prefs(event.sender_id)
    await present_options(
        event,
        title="Set Private Chat Context Mode",
        options=CONTEXT_MODE_NAMES,
        current_value=prefs.context_mode,
        callback_prefix="context_",
        awaiting_key="context_mode_selection",
        n_cols=1,
    )


@borg.on(
    events.NewMessage(pattern=r"(?i)/groupcontextmode", func=lambda e: e.is_private)
)
async def group_context_mode_handler(event):
    prefs = user_manager.get_prefs(event.sender_id)
    await present_options(
        event,
        title="Set Group Chat Context Mode",
        options=CONTEXT_MODE_NAMES,
        current_value=prefs.group_context_mode,
        callback_prefix="groupcontext_",
        awaiting_key="group_context_mode_selection",
        n_cols=1,
    )


@borg.on(events.NewMessage(pattern=r"(?i)/metadatamode", func=lambda e: e.is_private))
async def metadata_mode_handler(event):
    prefs = user_manager.get_prefs(event.sender_id)
    await present_options(
        event,
        title="Set Private Chat Metadata Mode",
        options=METADATA_MODES,
        current_value=prefs.metadata_mode,
        callback_prefix="metadata_",
        awaiting_key="metadata_mode_selection",
        n_cols=1,
    )


@borg.on(
    events.NewMessage(pattern=r"(?i)/groupmetadatamode", func=lambda e: e.is_private)
)
async def group_metadata_mode_handler(event):
    prefs = user_manager.get_prefs(event.sender_id)
    await present_options(
        event,
        title="Set Group Chat Metadata Mode",
        options=METADATA_MODES,
        current_value=prefs.group_metadata_mode,
        callback_prefix="groupmetadata_",
        awaiting_key="group_metadata_mode_selection",
        n_cols=1,
    )


@borg.on(
    events.NewMessage(pattern=r"(?i)/groupactivationmode", func=lambda e: e.is_private)
)
async def group_activation_mode_handler(event):
    prefs = user_manager.get_prefs(event.sender_id)
    await present_options(
        event,
        title="Set Group Chat Activation Mode",
        options=GROUP_ACTIVATION_MODES,
        current_value=prefs.group_activation_mode,
        callback_prefix="groupactivation_",
        awaiting_key="group_activation_mode_selection",
    )


@borg.on(events.NewMessage(pattern=r"(?i)/setthink", func=lambda e: e.is_private))
async def set_think_handler(event):
    prefs = user_manager.get_prefs(event.sender_id)
    # Add "clear" option
    think_options = {level: level.capitalize() for level in REASONING_LEVELS}
    think_options["clear"] = "Clear (Default)"
    await present_options(
        event,
        title="Set Reasoning Effort",
        options=think_options,
        current_value=prefs.thinking or "clear",
        callback_prefix="think_",
        awaiting_key="think_selection",
    )


@borg.on(events.NewMessage(pattern=r"(?i)/tools", func=lambda e: e.is_private))
async def tools_handler(event):
    prefs = user_manager.get_prefs(event.sender_id)
    # For this one, the current value is a list, so we handle it differently
    if IS_BOT:
        buttons = [
            KeyboardButtonCallback(
                f"{'✅' if tool in prefs.enabled_tools else '❌'} {tool}",
                data=f"tool_{tool}",
            )
            for tool in AVAILABLE_TOOLS
        ]
        await event.reply(
            f"{BOT_META_INFO_PREFIX}**Manage Tools**",
            buttons=util.build_menu(buttons, n_cols=1),
        )
    else:
        menu_text = ["**Manage Tools**\n"]
        for i, tool in enumerate(AVAILABLE_TOOLS):
            prefix = "✅" if tool in prefs.enabled_tools else "❌"
            menu_text.append(f"{i + 1}. {prefix} {tool}")
        menu_text.append("\nReply with a number to toggle that tool.")
        AWAITING_INPUT_FROM_USERS[event.sender_id] = {
            "type": "tool_selection",
            "keys": AVAILABLE_TOOLS,
        }
        await event.reply(f"{BOT_META_INFO_PREFIX}\n".join(menu_text))


@borg.on(
    events.NewMessage(
        pattern=r"(?i)/(enable|disable)(?P<tool_name>\w+)", func=lambda e: e.is_private
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


@borg.on(events.NewMessage(pattern=r"(?i)/json", func=lambda e: e.is_private))
async def json_mode_handler(event):
    """Toggles JSON mode."""
    is_enabled = user_manager.toggle_json_mode(event.sender_id)
    await event.reply(
        f"{BOT_META_INFO_PREFIX}JSON response mode has been **{'enabled' if is_enabled else 'disabled'}**."
    )


@borg.on(events.CallbackQuery())
async def callback_handler(event):
    """Handles all inline button presses for the plugin (BOT MODE ONLY)."""
    data_str = event.data.decode("utf-8")
    user_id = event.sender_id
    prefs = user_manager.get_prefs(user_id)

    if data_str.startswith("think_"):
        level = data_str.split("_")[1]
        user_manager.set_thinking(user_id, None if level == "clear" else level)
        prefs = user_manager.get_prefs(user_id)  # update prefs
        think_options = {level: level.capitalize() for level in REASONING_LEVELS}
        think_options["clear"] = "Clear (Default)"
        buttons = [
            KeyboardButtonCallback(
                f"✅ {display}" if (prefs.thinking or "clear") == key else display,
                data=f"think_{key}",
            )
            for key, display in think_options.items()
        ]
        await event.edit(buttons=util.build_menu(buttons, n_cols=2))
        await event.answer("Thinking preference updated.")
    elif data_str.startswith("tool_"):
        tool_name = data_str.split("_")[1]
        is_enabled = tool_name not in prefs.enabled_tools
        user_manager.set_tool_state(user_id, tool_name, enabled=is_enabled)
        prefs = user_manager.get_prefs(user_id)  # update prefs
        buttons = [
            KeyboardButtonCallback(
                f"{'✅' if tool in prefs.enabled_tools else '❌'} {tool}",
                data=f"tool_{tool}",
            )
            for tool in AVAILABLE_TOOLS
        ]
        await event.edit(buttons=util.build_menu(buttons, n_cols=1))
        await event.answer(f"{tool_name} {'enabled' if is_enabled else 'disabled'}.")
    elif data_str.startswith("context_"):
        mode = data_str.split("_", 1)[1]
        user_manager.set_context_mode(user_id, mode)
        prefs = user_manager.get_prefs(user_id)  # update prefs
        buttons = [
            KeyboardButtonCallback(
                f"✅ {name}" if key == prefs.context_mode else name,
                data=f"context_{key}",
            )
            for key, name in CONTEXT_MODE_NAMES.items()
        ]
        await event.edit(buttons=util.build_menu(buttons, n_cols=1))
        await event.answer("Private context mode updated.")
    elif data_str.startswith("groupcontext_"):
        mode = data_str.split("_", 1)[1]
        user_manager.set_group_context_mode(user_id, mode)
        prefs = user_manager.get_prefs(user_id)  # update prefs
        buttons = [
            KeyboardButtonCallback(
                f"✅ {name}" if key == prefs.group_context_mode else name,
                data=f"groupcontext_{key}",
            )
            for key, name in CONTEXT_MODE_NAMES.items()
        ]
        await event.edit(buttons=util.build_menu(buttons, n_cols=1))
        await event.answer("Group context mode updated.")
    elif data_str.startswith("metadata_"):
        mode = data_str.split("_", 1)[1]
        user_manager.set_metadata_mode(user_id, mode)
        prefs = user_manager.get_prefs(user_id)
        buttons = [
            KeyboardButtonCallback(
                f"✅ {name}" if key == prefs.metadata_mode else name,
                data=f"metadata_{key}",
            )
            for key, name in METADATA_MODES.items()
        ]
        await event.edit(buttons=util.build_menu(buttons, n_cols=1))
        await event.answer("Private metadata mode updated.")
    elif data_str.startswith("groupmetadata_"):
        mode = data_str.split("_", 1)[1]
        user_manager.set_group_metadata_mode(user_id, mode)
        prefs = user_manager.get_prefs(user_id)
        buttons = [
            KeyboardButtonCallback(
                f"✅ {name}" if key == prefs.group_metadata_mode else name,
                data=f"groupmetadata_{key}",
            )
            for key, name in METADATA_MODES.items()
        ]
        await event.edit(buttons=util.build_menu(buttons, n_cols=1))
        await event.answer("Group metadata mode updated.")
    elif data_str.startswith("groupactivation_"):
        mode = data_str.split("_", 1)[1]
        user_manager.set_group_activation_mode(user_id, mode)
        prefs = user_manager.get_prefs(user_id)  # update prefs
        buttons = [
            KeyboardButtonCallback(
                f"✅ {name}" if key == prefs.group_activation_mode else name,
                data=f"groupactivation_{key}",
            )
            for key, name in GROUP_ACTIVATION_MODES.items()
        ]
        await event.edit(buttons=util.build_menu(buttons, n_cols=2))
        await event.answer("Group activation mode updated.")


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
    flow_data = AWAITING_INPUT_FROM_USERS.get(user_id)
    if not flow_data:
        return

    input_type = flow_data.get("type")

    if text.lower() == "cancel":
        cancel_input_flow(user_id)
        await event.reply(f"{BOT_META_INFO_PREFIX}Process cancelled.")
        return

    # Handle simple text inputs
    if input_type == "model":
        user_manager.set_model(user_id, text)
        await event.reply(f"{BOT_META_INFO_PREFIX}✅ Model updated to: `{text}`")
    elif input_type == "system_prompt":
        if text.lower() == "reset":
            user_manager.set_system_prompt(user_id, "")
            await event.reply(
                f"{BOT_META_INFO_PREFIX}✅ System prompt reset to default."
            )
        else:
            user_manager.set_system_prompt(user_id, text)
            await event.reply(f"{BOT_META_INFO_PREFIX}✅ System prompt updated.")
    # Handle numeric menu selections
    elif input_type and input_type.endswith("_selection"):
        try:
            choice_idx = int(text) - 1
            option_keys = flow_data.get("keys", [])
            if 0 <= choice_idx < len(option_keys):
                selected_key = option_keys[choice_idx]
                if input_type == "context_mode_selection":
                    user_manager.set_context_mode(user_id, selected_key)
                    await event.reply(
                        f"{BOT_META_INFO_PREFIX}✅ Private context mode set to: **{CONTEXT_MODE_NAMES[selected_key]}**"
                    )
                elif input_type == "group_context_mode_selection":
                    user_manager.set_group_context_mode(user_id, selected_key)
                    await event.reply(
                        f"{BOT_META_INFO_PREFIX}✅ Group context mode set to: **{CONTEXT_MODE_NAMES[selected_key]}**"
                    )
                elif input_type == "metadata_mode_selection":
                    user_manager.set_metadata_mode(user_id, selected_key)
                    await event.reply(
                        f"{BOT_META_INFO_PREFIX}✅ Private metadata mode set to: **{METADATA_MODES[selected_key]}**"
                    )
                elif input_type == "group_metadata_mode_selection":
                    user_manager.set_group_metadata_mode(user_id, selected_key)
                    await event.reply(
                        f"{BOT_META_INFO_PREFIX}✅ Group metadata mode set to: **{METADATA_MODES[selected_key]}**"
                    )
                elif input_type == "group_activation_mode_selection":
                    user_manager.set_group_activation_mode(user_id, selected_key)
                    await event.reply(
                        f"{BOT_META_INFO_PREFIX}✅ Group activation mode set to: **{GROUP_ACTIVATION_MODES[selected_key]}**"
                    )
                elif input_type == "think_selection":
                    level = None if selected_key == "clear" else selected_key
                    user_manager.set_thinking(user_id, level)
                    await event.reply(
                        f"{BOT_META_INFO_PREFIX}✅ Reasoning level updated."
                    )
                elif input_type == "tool_selection":
                    prefs = user_manager.get_prefs(user_id)
                    is_enabled = selected_key not in prefs.enabled_tools
                    user_manager.set_tool_state(user_id, selected_key, is_enabled)
                    await event.reply(
                        f"{BOT_META_INFO_PREFIX}✅ Tool **{selected_key}** has been {'enabled' if is_enabled else 'disabled'}."
                    )
            else:
                await event.reply(
                    f"{BOT_META_INFO_PREFIX}Invalid number. Please try again."
                )
                return
        except ValueError:
            await event.reply(
                f"{BOT_META_INFO_PREFIX}Please reply with a valid number."
            )
            return

    cancel_input_flow(user_id)


async def is_valid_chat_message(event: events.NewMessage.Event) -> bool:
    """
    Determines if a message is a valid conversational message to be
    processed by the main chat handler.
    """
    # Universal filters
    if not (event.text or event.media):
        return False
    if event.forward:
        return False
    if event.text and event.text.split(" ", 1)[0].lower() in KNOWN_COMMAND_SET:
        return False

    # Userbot-specific filters
    if not IS_BOT:
        if event.out:
            return False

    # Private chats are always valid if they pass the filters above
    if event.is_private:
        return True

    # Group chats: must be a mention or a reply to self
    if not event.is_private:
        prefs = user_manager.get_prefs(event.sender_id)
        if event.text and BOT_USERNAME and event.text.strip().startswith(BOT_USERNAME):
            return True
        if prefs.group_activation_mode == "mention_and_reply" and event.is_reply:
            try:
                reply_msg = await event.get_reply_message()
                if reply_msg and reply_msg.sender_id == borg.me.id:
                    return True
            except Exception:
                return False

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
            if (
                is_group_chat
                and BOT_USERNAME
                and text_to_check.startswith(BOT_USERNAME)
            ):
                # For groups, check for the separator after the bot's name
                text_to_check = text_to_check[len(BOT_USERNAME) :].strip()

            if text_to_check == CONTEXT_SEPARATOR:
                if not IS_BOT:
                    USERBOT_HISTORY_CACHE.pop(event.chat_id, None)
                reply_text = "Context cleared. The conversation will now start fresh from your next message"
                if is_group_chat:
                    activation_mode = prefs.group_activation_mode
                    if activation_mode == "mention_and_reply":
                        reply_text += " mentioning me or replying to me."
                    else:  # mention_only
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
borg.loop.create_task(initialize_llm_chat())
