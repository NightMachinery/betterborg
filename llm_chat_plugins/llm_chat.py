import asyncio
from pynight.common_icecream import (
    ic,
)  #: used for debugging, DO NOT REMOVE even if currently unused
import traceback
import os
import uuid
import base64
import mimetypes
import re
import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from shutil import rmtree
from itertools import groupby

import litellm
from telethon import events, errors
from telethon.tl.types import (
    BotCommand,
    BotCommandScopeDefault,
    KeyboardButtonCallback,
    Message,
)
from pydantic import BaseModel, Field
from typing import Optional, List, Dict
from dataclasses import dataclass

# Import uniborg utilities and storage
from uniborg import util
from uniborg import llm_db
from uniborg import llm_util
from uniborg import tts_util
from uniborg import history_util
from uniborg import bot_util
from uniborg.storage import UserStorage
from uniborg.constants import BOT_META_INFO_PREFIX

# Import live mode utilities
from uniborg import gemini_live_util

# Redis utilities for smart context state persistence
from uniborg import redis_util

# --- Constants and Configuration ---
NOT_SET_HERE_DISPLAY_NAME = "Not Set for This Chat Specifically"

# Use the litellm model naming convention.
# See https://docs.litellm.ai/docs/providers/gemini
DEFAULT_MODEL = "gemini/gemini-2.5-flash"  #: Do NOT change the default model unless explicitly instructed to.
# Alternatives:
# - "gemini/gemini-2.5-pro"
##
PROMPT_REPLACEMENTS = {
    re.compile(
        r"^\.ocr$", re.MULTILINE | re.IGNORECASE
    ): r"""
You will be given a series of images that are part of a single, related sequence. Your task is to perform OCR and combine the text from all images into one final, coherent output, following these specific rules:

*Combine Text:* Transcribe and merge the text from all images into a single, continuous document. Ensure the text flows in the correct sequence from one image to the next.

*No Commentary:* The final output must not contain any of your own commentary, explanations, or headers like "OCR Result" or "Image 1." It should only be the transcribed text itself.

*Consolidate Recurring Information:* Identify any information that is repeated across multiple images, such as headers, footers, author names, social media handles, logos, advertisements, or contact details. Remove these repetitions from the main body of the text.

*Create a Single Footer:* Place all the consolidated, recurring information you identified in the previous step just once at the very end of the document, creating a clean footer.

The goal is to produce a single, clean document as if it were the original, without the page breaks and repeated headers or footers from the images.
"""
}
DEFAULT_SYSTEM_PROMPT = """
You are a helpful and knowledgeable assistant with the personality of a smart, highly agentic friend. Your primary audience is advanced STEM postgraduate researchers, so be precise and technically accurate while maintaining warmth and engagement.

**Core Personality:**
- **Proactive & Agentic:** Don't just answer - actively drive conversations forward. Offer suggestions, give advice, ask follow-up questions, and show genuine interest in the user's work and life.
- **Empathetic Engagement:** Ask about their day, research progress, challenges they're facing. Remember context from the conversation and check in on things they've mentioned.
- **Smart Friend Approach:** Be the kind of friend who remembers what matters to them, offers helpful insights, and isn't afraid to challenge their thinking constructively.

**Style Guidelines for Mobile Chat:**
- **Concise & Direct:** Keep responses brief and punchy without sacrificing critical information. Get straight to the point. Exception: Provide full detail when users specifically request lengthy responses.
- **Conversational & Warm:** Write naturally, like you're genuinely interested in helping them succeed. Use emojis to add warmth and personality.
- **Readability:** Break up text into short paragraphs. Use bullet points or numbered lists to make complex information easy to scan on a small screen.
- **Active Conversation:** End most responses with:
   * Clarifying questions about their specific situation
   * Suggestions for next steps or improvements
   * Check-ins about related challenges or progress
   * Offers to dive deeper into topics that might help them

**Language:**
- Your response must match the language of the user's last message.
- To determine the user's language, rely exclusively on the primary content of their message.
- Do not consider language found in metadata or attachments, unless the attachments are the sole content of the last user message. E.g., the user has sent you an audio file only as their message.
- If you are in doubt the language is Arabic or Persian/Farsi, assume it is Persian/Farsi.

**Formatting:** You can use Telegram's markdown: `**bold**`, `__italic__`, `` `code` ``, `[links](https://example.com)`, and ```pre``` blocks.
"""

DEFAULT_SYSTEM_PROMPT_V1 = """
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
MODEL_CHOICES = {
    ## Gemini
    "gemini/gemini-2.5-flash": "Gemini 2.5 Flash",
    "gemini/gemini-2.5-pro": "Gemini 2.5 Pro",
    "openrouter/google/gemini-2.5-pro": "Gemini 2.5 Pro (OpenRouter)",
    ## Anthropic Claude
    "openrouter/anthropic/claude-sonnet-4": "Claude 4 Sonnet (OpenRouter)",
    "openrouter/anthropic/claude-opus-4": "Claude 4 Opus (OpenRouter)",
    ## Grok
    "openrouter/x-ai/grok-4": "Grok 4 (OpenRouter)",
    ## OpenAI
    # openai/gpt-5-chat
    "openrouter/openai/gpt-5-chat": "GPT-5 Chat (OpenRouter)",
    "openrouter/openai/chatgpt-4o-latest": "ChatGPT 4o (OpenRouter)",
    # openai/chatgpt-4o-latest: OpenAI ChatGPT 4o is continually updated by OpenAI to point to the current version of GPT-4o used by ChatGPT. It therefore differs slightly from the API version of GPT-4o in that it has additional RLHF. It is intended for research and evaluation.  OpenAI notes that this model is not suited for production use-cases as it may be removed or redirected to another model in the future.
    # "openrouter/openai/gpt-4o-mini": "GPT-4o Mini (OpenRouter)",
    # "openrouter/openai/gpt-4.1-mini": "GPT-4.1 Mini (OpenRouter)",
    "openrouter/openai/gpt-4.1": "GPT-4.1 (OpenRouter)",
    "openrouter/openai/o4-mini-high": "o4-mini-high (OpenRouter)",
    ## Kimi
    # moonshotai/kimi-k2:free
    "openrouter/moonshotai/kimi-k2:free": "🎁 Kimi K2 (Free, OpenRouter)",
    ## Qwen
    # qwen/qwen3-coder:free
    "openrouter/qwen/qwen3-coder:free": "🎁 Qwen3 Coder (Free, OpenRouter)",
    ## Various
    # "openrouter/cognitivecomputations/dolphin-mistral-24b-venice-edition:free": "🎁 Venice Uncensored 24B (Free, OpenRouter)",
    #: model name is too long for Telegram API's `data` field in callback buttons
    ## Cloaked Models
}
LAST_N_MESSAGES_LIMIT = 50
HISTORY_MESSAGE_LIMIT = 1000
LOG_COUNT_LIMIT = 3
AVAILABLE_TOOLS = ["googleSearch", "urlContext", "codeExecution"]
DEFAULT_ENABLED_TOOLS = ["googleSearch", "urlContext"]
REASONING_LEVELS = ["disable", "low", "medium", "high"]
CONTEXT_SEPARATOR = "---"
CONTEXT_MODE_NAMES = {
    "reply_chain": "Reply Chain",
    "until_separator": f"Until Separator (`{CONTEXT_SEPARATOR}`)",
    "last_N": f"Last {LAST_N_MESSAGES_LIMIT} Messages",
    "smart": "Smart Mode (Auto-Switches)",
}
CONTEXT_MODES = list(CONTEXT_MODE_NAMES.keys())
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
    {
        "command": "setopenrouterkey",
        "description": "Set or update your OpenRouter API key",
    },
    {"command": "setmodel", "description": "Set your preferred chat model"},
    {
        "command": "setsystemprompt",
        "description": "Customize the bot's system prompt (default in all chats)",
    },
    {
        "command": "setsystemprompthere",
        "description": "Set a system prompt for the current chat only",
    },
    {
        "command": "resetsystemprompthere",
        "description": "Reset the system prompt for the current chat",
    },
    {
        "command": "getsystemprompthere",
        "description": "View the effective system prompt for the current chat",
    },
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
        "command": "contextmodehere",
        "description": "Set context mode for the current chat",
    },
    {
        "command": "getcontextmodehere",
        "description": "View context mode for the current chat",
    },
    {
        "command": "sep",
        "description": "Switch to smart mode with until separator context",
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
    {"command": "tts", "description": "Set TTS model for this chat"},
    {"command": "geminivoice", "description": "Set global Gemini voice"},
    {"command": "geminivoicehere", "description": "Set Gemini voice for this chat"},
    {
        "command": "live",
        "description": "Toggle live mode for real-time audio/video chat",
    },
    {"command": "livemodel", "description": "Set your preferred live mode model"},
    {"command": "testlive", "description": "Test live session connection (admin only)"},
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
AWAITING_INPUT_FROM_USERS = {}
IS_BOT = None
USERBOT_HISTORY_CACHE = {}
SMART_CONTEXT_STATE = {}

# --- Smart Context State Management ---


async def load_smart_context_states():
    """Load all smart context states from Redis into memory on startup."""
    if not redis_util.is_redis_available():
        return

    try:
        redis_client = await redis_util.get_redis()
        if not redis_client:
            return

        # Get all smart context keys
        pattern = "borg:smart_context:*"
        keys = await redis_client.keys(pattern)

        for key in keys:
            try:
                # Extract user_id from key (format: "borg:smart_context:{user_id}")
                user_id = int(key.split(":")[-1])
                mode = await redis_client.get(key)
                if mode:
                    SMART_CONTEXT_STATE[user_id] = mode
                    # Renew expiry for another month
                    await redis_client.expire(
                        key, redis_util.get_long_expire_duration()
                    )
            except (ValueError, IndexError):
                continue  # Skip malformed keys

        if keys:
            print(f"LLM_Chat: Loaded {len(keys)} smart context states from Redis")
    except Exception as e:
        print(f"LLM_Chat: Failed to load smart context states from Redis: {e}")


def get_smart_context_mode(user_id: int) -> str:
    """Get smart context mode for user from in-memory storage."""
    return SMART_CONTEXT_STATE.get(user_id, "reply_chain")


async def set_smart_context_mode(user_id: int, mode: str):
    """Set smart context mode for user with Redis persistence and in-memory update."""
    # Update in-memory immediately
    SMART_CONTEXT_STATE[user_id] = mode

    # Persist to Redis with long expiry (1 month)
    if redis_util.is_redis_available():
        try:
            await redis_util.set_with_expiry(
                redis_util.smart_context_key(user_id),
                mode,
                expire_seconds=redis_util.get_long_expire_duration(),
            )
        except Exception as e:
            print(f"LLM_Chat: Redis set_smart_context_mode failed: {e}")


def cancel_input_flow(user_id: int):
    """Cancels any pending input requests for a user."""
    AWAITING_INPUT_FROM_USERS.pop(user_id, None)


# --- Preference Management ---


class UserPrefs(BaseModel):
    """Pydantic model for type-safe user preferences."""

    model: str = Field(default=DEFAULT_MODEL)
    system_prompt: Optional[str] = Field(default=None)
    thinking: Optional[str] = Field(default=None)
    enabled_tools: list[str] = Field(default_factory=lambda: DEFAULT_ENABLED_TOOLS)
    json_mode: bool = Field(default=False)
    context_mode: str = Field(default="reply_chain")
    group_context_mode: str = Field(default="reply_chain")
    group_activation_mode: str = Field(default="mention_and_reply")
    metadata_mode: str = Field(default="only_forwarded")
    group_metadata_mode: str = Field(default="full_metadata")
    tts_global_voice: str = Field(default=tts_util.DEFAULT_VOICE)
    live_model: str = Field(default="gemini-2.5-flash-preview-native-audio-dialog")


class ChatPrefs(BaseModel):
    """Pydantic model for chat-specific settings."""

    system_prompt: Optional[str] = Field(default=None)
    context_mode: Optional[str] = Field(default=None)
    tts_model: str = Field(default="Disabled")
    tts_voice_override: Optional[str] = Field(default=None)
    live_mode_enabled: bool = Field(default=False)


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

    def set_system_prompt(self, user_id: int, prompt: Optional[str]):
        prefs = self.get_prefs(user_id)
        prefs.system_prompt = prompt
        self._save_prefs(user_id, prefs)

    def set_thinking(self, user_id: int, level: Optional[str]):
        prefs = self.get_prefs(user_id)
        prefs.thinking = level
        self._save_prefs(user_id, prefs)

    def set_tool_state(self, user_id: int, tool_name: str, enabled: bool):
        if tool_name not in AVAILABLE_TOOLS:
            print(f"Invalid tool name: tool_name='{tool_name}', user_id={user_id}")
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
            print(f"Invalid context mode: mode='{mode}', user_id={user_id}")
            return

        prefs = self.get_prefs(user_id)
        prefs.context_mode = mode
        self._save_prefs(user_id, prefs)

    def set_group_context_mode(self, user_id: int, mode: str):
        if mode not in CONTEXT_MODES:
            print(f"Invalid group context mode: mode='{mode}', user_id={user_id}")
            return

        prefs = self.get_prefs(user_id)
        prefs.group_context_mode = mode
        self._save_prefs(user_id, prefs)

    def set_metadata_mode(self, user_id: int, mode: str):
        if mode not in METADATA_MODES:
            print(f"Invalid metadata mode: mode='{mode}', user_id={user_id}")
            return

        prefs = self.get_prefs(user_id)
        prefs.metadata_mode = mode
        self._save_prefs(user_id, prefs)

    def set_group_metadata_mode(self, user_id: int, mode: str):
        if mode not in METADATA_MODES:
            print(f"Invalid group metadata mode: mode='{mode}', user_id={user_id}")
            return

        prefs = self.get_prefs(user_id)
        prefs.group_metadata_mode = mode
        self._save_prefs(user_id, prefs)

    def set_group_activation_mode(self, user_id: int, mode: str):
        if mode not in GROUP_ACTIVATION_MODES:
            print(f"Invalid group activation mode: mode='{mode}', user_id={user_id}")
            return
        prefs = self.get_prefs(user_id)
        prefs.group_activation_mode = mode
        self._save_prefs(user_id, prefs)

    def get_tts_global_voice(self, user_id: int) -> str:
        return self.get_prefs(user_id).tts_global_voice

    def set_tts_global_voice(self, user_id: int, voice: str):
        if voice not in tts_util.GEMINI_VOICES:
            print(f"Invalid TTS voice: voice='{voice}', user_id={user_id}")
            return
        prefs = self.get_prefs(user_id)
        prefs.tts_global_voice = voice
        self._save_prefs(user_id, prefs)

    def set_live_model(self, user_id: int, model: str):
        prefs = self.get_prefs(user_id)
        prefs.live_model = model
        self._save_prefs(user_id, prefs)


class ChatManager:
    """High-level manager for chat-specific settings."""

    def __init__(self):
        # We reuse UserStorage, but the key is a chat_id, not a user_id.
        self.storage = UserStorage(purpose="llm_chat_chats")

    def get_prefs(self, chat_id: int) -> ChatPrefs:
        data = self.storage.get(chat_id)
        return ChatPrefs.model_validate(data or {})

    def _save_prefs(self, chat_id: int, prefs: ChatPrefs):
        self.storage.set(chat_id, prefs.model_dump(exclude_defaults=True))

    def get_system_prompt(self, chat_id: int) -> Optional[str]:
        return self.get_prefs(chat_id).system_prompt

    def set_system_prompt(self, chat_id: int, prompt: Optional[str]):
        prefs = self.get_prefs(chat_id)
        prefs.system_prompt = prompt
        self._save_prefs(chat_id, prefs)

    def get_context_mode(self, chat_id: int) -> Optional[str]:
        return self.get_prefs(chat_id).context_mode

    def set_context_mode(self, chat_id: int, mode: Optional[str]):
        if mode is not None and mode not in CONTEXT_MODES:
            print(f"Invalid context mode: mode='{mode}', chat_id={chat_id}")
            return

        prefs = self.get_prefs(chat_id)
        prefs.context_mode = mode
        self._save_prefs(chat_id, prefs)

    def get_tts_model(self, chat_id: int) -> str:
        return self.get_prefs(chat_id).tts_model

    def set_tts_model(self, chat_id: int, model: str):
        if model not in tts_util.TTS_MODELS:
            print(f"Invalid TTS model: model='{model}', chat_id={chat_id}")
            return
        prefs = self.get_prefs(chat_id)
        prefs.tts_model = model
        self._save_prefs(chat_id, prefs)

    def get_tts_voice_override(self, chat_id: int) -> Optional[str]:
        return self.get_prefs(chat_id).tts_voice_override

    def set_tts_voice_override(self, chat_id: int, voice: Optional[str]):
        if voice is not None and voice not in tts_util.GEMINI_VOICES:
            print(f"Invalid TTS voice override: voice='{voice}', chat_id={chat_id}")
            return
        prefs = self.get_prefs(chat_id)
        prefs.tts_voice_override = voice
        self._save_prefs(chat_id, prefs)

    def set_live_mode_enabled(self, chat_id: int, enabled: bool):
        prefs = self.get_prefs(chat_id)
        prefs.live_mode_enabled = enabled
        self._save_prefs(chat_id, prefs)

    def is_live_mode_enabled(self, chat_id: int) -> bool:
        return self.get_prefs(chat_id).live_mode_enabled


user_manager = UserManager()
chat_manager = ChatManager()


# --- Core Logic & Helpers ---



def _is_known_command(text: str, *, strip_bot_username: bool = True) -> bool:
    """Checks if text starts with a known command, with optional bot username stripping."""
    if not text:
        return False

    # Extract first word/command
    command = text.split(None, 1)[0].lower()

    # Strip bot username if requested (for event.text processing)
    if strip_bot_username and BOT_USERNAME:
        command = re.sub(
            re.escape(BOT_USERNAME) + r"\b", "", command, flags=re.IGNORECASE
        ).strip()

    return command in KNOWN_COMMAND_SET


def is_native_gemini(model: str) -> bool:
    """Check if model is native Gemini (not OpenRouter) and supports context caching."""
    return model.startswith("gemini/")


@dataclass
class SystemPromptInfo:
    """Contains all system prompt information for a chat context."""

    chat_prompt: Optional[str]
    user_prompt: Optional[str]
    default_prompt: str
    effective_prompt: str
    source: str  # "chat", "user", or "default"


def get_system_prompt_info(event) -> SystemPromptInfo:
    """Returns comprehensive system prompt information for the given event."""
    user_id = event.sender_id
    chat_prompt = chat_manager.get_system_prompt(event.chat_id)
    user_prefs = user_manager.get_prefs(user_id)
    user_prompt = user_prefs.system_prompt

    # Determine effective prompt and source
    if chat_prompt:
        effective_prompt = chat_prompt
        source = "chat"
    elif user_prompt:
        effective_prompt = user_prompt
        source = "user"
    else:
        effective_prompt = DEFAULT_SYSTEM_PROMPT
        source = "default"

    return SystemPromptInfo(
        chat_prompt=chat_prompt,
        user_prompt=user_prompt,
        default_prompt=DEFAULT_SYSTEM_PROMPT,
        effective_prompt=effective_prompt,
        source=source,
    )


async def _get_context_mode_status_text(event) -> str:
    """Generates a user-friendly string explaining the current context mode for a chat."""
    user_id = event.sender_id
    prefs = user_manager.get_prefs(user_id)
    is_private = event.is_private

    # Determine the base mode and its source
    chat_context_mode = chat_manager.get_context_mode(event.chat_id)
    if chat_context_mode:
        effective_mode = chat_context_mode
        source_text = "a specific setting for **this chat**"
    else:
        effective_mode = prefs.context_mode if is_private else prefs.group_context_mode
        source_text = (
            "your **personal default** for private chats"
            if is_private
            else "your **personal default** for group chats"
        )

    mode_name = CONTEXT_MODE_NAMES.get(effective_mode, effective_mode)

    # Build the response message
    response_parts = [
        f"∙ **Current Mode:** `{mode_name}`",
        f"∙ **Source:** This is using {source_text}.",
    ]

    # If the effective mode is 'smart', add the current state
    if effective_mode == "smart":
        current_smart_state = get_smart_context_mode(user_id)
        smart_state_name = CONTEXT_MODE_NAMES.get(
            current_smart_state, current_smart_state
        )
        response_parts.append(
            f"∙ **Smart State:** The bot is currently using the `{smart_state_name}` method."
        )

    return "\n".join(response_parts)


async def _process_media(message: Message, temp_dir: Path) -> Optional[dict]:
    """
    Downloads or retrieves media from cache, prepares it for litellm,
    and ensures it's cached in a text-safe format (raw text or Base64).
    """
    if not message or not message.media:
        return None

    try:
        file_id = (
            f"{message.chat_id}_{message.id}_{getattr(message.media, 'id', 'unknown')}"
        )
        cached_file_info = await history_util.get_cached_file(file_id)

        # --- Branch 1: Use Cached File ---
        if cached_file_info:
            storage_type = cached_file_info["data_storage_type"]
            data = cached_file_info["data"]  # This is a string (text or base64)
            filename = cached_file_info.get("filename")
            mime_type = cached_file_info.get("mime_type")

            if storage_type == "text":
                return {
                    "type": "text",
                    "text": f"\n--- Attachment: {filename} ---\n{data}",
                }
            elif storage_type == "base64":
                return {
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime_type};base64,{data}"},
                }
            else:
                print(
                    f"Unknown storage type '{storage_type}' in cache for file_id {file_id}"
                )
                return None

        # --- Branch 2: New File - Download, Process, and Cache ---
        else:
            file_path_str = await message.download_media(file=temp_dir)
            if not file_path_str:
                return None

            file_path = Path(file_path_str)
            original_filename = file_path.name

            mime_type, _ = mimetypes.guess_type(file_path)
            if (
                not mime_type
                and hasattr(message.media, "document")
                and hasattr(message.media.document, "mime_type")
            ):
                mime_type = message.media.document.mime_type

            if not mime_type:
                for ext, m_type in llm_util.MIME_TYPE_MAP.items():
                    if original_filename.lower().endswith(ext):
                        mime_type = m_type
                        break

            with open(file_path, "rb") as f:
                file_bytes = f.read()

            is_text_file = False
            text_extensions = {
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
                ".php",
                ".lua",
                ".glsl",
                ".frag",
                ".cson",
                ".plist",
            }
            if mime_type and mime_type.startswith("text/"):
                is_text_file = True
            elif not mime_type and file_path.suffix.lower() in text_extensions:
                is_text_file = True
                mime_type = "text/plain"

            if is_text_file:
                text_content = file_bytes.decode("utf-8", errors="ignore")
                await history_util.cache_file(
                    file_id,
                    data=text_content,
                    data_storage_type="text",
                    filename=original_filename,
                    mime_type=mime_type,
                )
                return {
                    "type": "text",
                    "text": f"\n--- Attachment: {original_filename} ---\n{text_content}",
                }
            else:
                if not mime_type or not mime_type.startswith(
                    ("image/", "audio/", "video/")
                ):
                    print(
                        f"Unsupported binary media type '{mime_type}' for file {original_filename}"
                    )
                    return None

                b64_content = base64.b64encode(file_bytes).decode("utf-8")
                await history_util.cache_file(
                    file_id,
                    data=b64_content,
                    data_storage_type="base64",
                    filename=original_filename,
                    mime_type=mime_type,
                )
                return {
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime_type};base64,{b64_content}"},
                }
    except Exception as e:
        print(f"Error processing media from message {message.id}: {e}")
        traceback.print_exc()
        return None


async def _log_conversation(
    event, prefs: UserPrefs, messages: list, final_response: str
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

        # Log prefs object as JSON, excluding the system_prompt
        prefs_dict = prefs.model_dump()
        prefs_dict.pop("system_prompt", None)
        prefs_json = json.dumps(prefs_dict, indent=2)

        log_parts = [
            f"Date: {timestamp}",
            f"User ID: {user_id}",
            f"Name: {full_name}",
            f"Username: @{username}",
            f"Model: {prefs.model}",
            "--- Preferences ---",
            prefs_json,
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


async def _get_user_metadata_prefix(message: Message) -> str:
    """Generates the user and timestamp part of the metadata prefix."""
    sender = await message.get_sender()
    sender_name = getattr(sender, "first_name", None) or "Unknown"
    username = getattr(sender, "username", None)
    timestamp = message.date.isoformat()

    if message.sender_id == BOT_ID:
        #: no need to inject useless metadata about the bot itself
        return ""
    else:
        sender_info = {"name": sender_name, "id": message.sender_id}
        if username:
            sender_info["username"] = username
        return f"[Sender: {sender_info} | Sending Date: {timestamp}]"


async def _get_forward_metadata_prefix(message: Message) -> str:
    """Generates the forwarded part of the metadata prefix, if applicable."""
    if not message.forward:
        return ""

    fwd_parts = []
    fwd_from_name = None
    fwd_username = None
    fwd_entity = message.forward.sender or message.forward.chat
    if fwd_entity:
        fwd_from_name = getattr(
            fwd_entity, "title", getattr(fwd_entity, "first_name", None)
        )
        fwd_username = getattr(fwd_entity, "username", None)
    if not fwd_from_name:
        fwd_from_name = message.forward.from_name

    # Get from_id if available
    fwd_peer_id = None
    if message.forward.from_id:
        fwd_peer_id = (
            getattr(message.forward.from_id, "user_id", None)
            or getattr(message.forward.from_id, "chat_id", None)
            or getattr(message.forward.from_id, "channel_id", None)
        )

    if fwd_from_name or fwd_username or fwd_peer_id:
        from_info = {}
        if fwd_from_name:
            from_info["name"] = fwd_from_name
        if fwd_username:
            from_info["username"] = fwd_username
        if fwd_peer_id:
            from_info["id"] = fwd_peer_id
        fwd_parts.append(f"From: {from_info}")

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


async def _get_message_role(message: Message) -> str:
    """
    Determines the message role ('assistant' or 'user'), correctly handling
    forwards of the bot's own messages.
    """
    # Default to 'user'
    role = "user"
    original_sender_id = None

    if message.forward and message.forward.from_id:
        # from_id is a Peer object; we only care about user-to-user forwards for role assignment.
        original_sender_id = getattr(message.forward.from_id, "user_id", None)

    # A message is from the assistant if it was sent by the bot OR if it's a forward of a message originally from the bot.
    if message.sender_id == BOT_ID or original_sender_id == BOT_ID:
        role = "assistant"

    return role


async def _process_message_content(
    message: Message, role: str, temp_dir: Path, metadata_prefix: str = ""
) -> tuple[list, list]:
    """Processes a single message's text and media into litellm content parts."""
    text_buffer, media_parts = [], []

    # Filter out meta-info messages and commands from history
    if (
        role == "assistant"
        and message.text
        and message.text.startswith(BOT_META_INFO_PREFIX)
    ):
        return [], []
    if role == "user" and _is_known_command(message.text):
        return [], []

    processed_text = message.text
    if role == "user" and processed_text:
        if re.match(r"^\.s\b", processed_text):
            processed_text = processed_text[2:].strip()

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

    # Pre-calculate roles for all messages to use in grouping and processing.
    message_roles = [(await _get_message_role(m), m) for m in message_list]

    # --- Mode 1: No Metadata (Merge consecutive messages by role) ---
    if active_metadata_mode == "no_metadata":
        # Group by the pre-calculated role.
        for role, turn_items_iter in groupby(message_roles, key=lambda item: item[0]):
            turn_messages = [item[1] for item in turn_items_iter]  # Extract messages
            if not turn_messages:
                continue

            text_buffer, media_parts = [], []

            for turn_msg in turn_messages:
                # Process content without any metadata prefix, passing the known role.
                msg_texts, msg_media = await _process_message_content(
                    turn_msg, role, temp_dir
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
        for role, message in message_roles:
            prefix_parts = []

            if active_metadata_mode == "full_metadata":
                prefix_parts.append(await _get_user_metadata_prefix(message))

                if message.forward:
                    prefix_parts.append(await _get_forward_metadata_prefix(message))

            elif active_metadata_mode == "only_forwarded":
                if message.forward:
                    prefix_parts.append(await _get_forward_metadata_prefix(message))

            metadata_prefix = " ".join(filter(None, prefix_parts))
            #: Return an iterator yielding those items of iterable for which function(item) is true. If function is None, return the items that are true.

            text_buffer, media_parts = await _process_message_content(
                message, role, temp_dir, metadata_prefix
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
        message_ids = []
        if context_mode == "reply_chain":
            # For reply chains, we still need to fetch the messages directly.
            messages_to_process = await _get_initial_messages_for_reply_chain(event)
            messages_to_process.append(event.message)
            # No further processing needed for this case, jump to the end.
            expanded_messages = await bot_util.expand_and_sort_messages_with_groups(
                event, messages_to_process
            )
            return await _process_turns_to_history(event, expanded_messages, temp_dir)

        elif context_mode == "last_N":
            message_ids = await history_util.get_last_n_ids(
                chat_id, LAST_N_MESSAGES_LIMIT
            )
        elif context_mode == "until_separator":
            message_ids = await history_util.get_all_ids(chat_id)
        elif context_mode == "recent":
            now = datetime.now(timezone.utc)
            five_seconds_ago = now - timedelta(seconds=5)
            message_ids = await history_util.get_ids_since(chat_id, five_seconds_ago)

        # Common logic for bot modes that use message_ids
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
                else:  # last_N and recent
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

        elif context_mode == "recent":
            # Fetch messages from the last 5 seconds
            now = datetime.now(timezone.utc)
            five_seconds_ago = now - timedelta(seconds=5)
            messages_to_process = [
                msg
                async for msg in event.client.iter_messages(
                    event.chat_id, offset_date=now, reverse=True
                )
                if msg.date > five_seconds_ago
            ]

    # --- Universal Post-Processing ---
    expanded_messages = await bot_util.expand_and_sort_messages_with_groups(
        event, messages_to_process
    )
    if len(expanded_messages) > HISTORY_MESSAGE_LIMIT:
        expanded_messages = expanded_messages[-HISTORY_MESSAGE_LIMIT:]

    return await _process_turns_to_history(event, expanded_messages, temp_dir)


# --- Bot/Userbot Initialization ---


def register_handlers():
    """Dynamically registers all event handlers after initialization."""
    bot_username_suffix_re = f"(?:{re.escape(BOT_USERNAME)})?" if BOT_USERNAME else ""

    # Command Handlers
    borg.on(
        events.NewMessage(
            pattern=rf"(?i)^/start{bot_username_suffix_re}\s*$",
            func=lambda e: e.is_private,
        )
    )(start_handler)
    borg.on(
        events.NewMessage(
            pattern=rf"(?i)^/help{bot_username_suffix_re}\s*$",
            func=lambda e: e.is_private,
        )
    )(help_handler)
    borg.on(
        events.NewMessage(
            pattern=rf"(?i)^/status{bot_username_suffix_re}\s*$",
            func=lambda e: e.is_private,
        )
    )(status_handler)
    borg.on(
        events.NewMessage(
            pattern=rf"(?i)^/log{bot_username_suffix_re}\s*$",
            func=lambda e: e.is_private,
        )
    )(log_handler)
    borg.on(
        events.NewMessage(
            pattern=rf"(?i)^/setgeminikey{bot_username_suffix_re}(?:\s+(.*))?\s*$",
            func=lambda e: e.is_private,
        )
    )(set_key_handler)
    borg.on(
        events.NewMessage(
            pattern=rf"(?i)^/setopenrouterkey{bot_username_suffix_re}(?:\s+(.*))?\s*$",
            func=lambda e: e.is_private,
        )
    )(set_openrouter_key_handler)
    borg.on(
        events.NewMessage(
            pattern=rf"(?i)^/setmodel{bot_username_suffix_re}(?:\s+(.*))?\s*$",
            func=lambda e: e.is_private,
        )
    )(set_model_handler)
    borg.on(
        events.NewMessage(
            pattern=rf"(?i)^/setsystemprompt{bot_username_suffix_re}(?:\s+([\s\S]+))?\s*$",
            func=lambda e: e.is_private,
        )
    )(set_system_prompt_handler)
    borg.on(
        events.NewMessage(
            pattern=rf"(?i)^/setsystemprompthere{bot_username_suffix_re}(?:\s+([\s\S]+))?\s*$"
        )
    )(set_system_prompt_here_handler)
    borg.on(
        events.NewMessage(
            pattern=rf"(?i)^/resetsystemprompthere{bot_username_suffix_re}\s*$"
        )
    )(reset_system_prompt_here_handler)
    borg.on(
        events.NewMessage(
            pattern=rf"(?i)^/getsystemprompthere{bot_username_suffix_re}\s*$"
        )
    )(get_system_prompt_here_handler)
    borg.on(
        events.NewMessage(pattern=rf"(?i)^/contextmodehere{bot_username_suffix_re}\s*$")
    )(context_mode_here_handler)
    borg.on(
        events.NewMessage(
            pattern=rf"(?i)^/getcontextmodehere{bot_username_suffix_re}\s*$"
        )
    )(get_context_mode_here_handler)
    borg.on(
        events.NewMessage(
            pattern=rf"(?i)^/contextmode{bot_username_suffix_re}\s*$",
            func=lambda e: e.is_private,
        )
    )(context_mode_handler)
    borg.on(
        events.NewMessage(
            pattern=rf"(?i)^/groupcontextmode{bot_username_suffix_re}\s*$",
            func=lambda e: e.is_private,
        )
    )(group_context_mode_handler)
    borg.on(
        events.NewMessage(
            pattern=rf"(?i)^/metadatamode{bot_username_suffix_re}\s*$",
            func=lambda e: e.is_private,
        )
    )(metadata_mode_handler)
    borg.on(
        events.NewMessage(
            pattern=rf"(?i)^/groupmetadatamode{bot_username_suffix_re}\s*$",
            func=lambda e: e.is_private,
        )
    )(group_metadata_mode_handler)
    borg.on(events.NewMessage(pattern=rf"(?i)^/sep{bot_username_suffix_re}\s*$"))(
        sep_handler
    )
    borg.on(
        events.NewMessage(
            pattern=rf"(?i)^/groupactivationmode{bot_username_suffix_re}\s*$",
            func=lambda e: e.is_private,
        )
    )(group_activation_mode_handler)
    borg.on(
        events.NewMessage(
            pattern=rf"(?i)^/setthink{bot_username_suffix_re}\s*$",
            func=lambda e: e.is_private,
        )
    )(set_think_handler)
    borg.on(
        events.NewMessage(
            pattern=rf"(?i)^/tools{bot_username_suffix_re}\s*$",
            func=lambda e: e.is_private,
        )
    )(tools_handler)
    borg.on(
        events.NewMessage(
            pattern=rf"(?i)^/(enable|disable)(?P<tool_name>\w+){bot_username_suffix_re}\s*$",
            func=lambda e: e.is_private,
        )
    )(toggle_tool_handler)
    borg.on(
        events.NewMessage(
            pattern=rf"(?i)^/json{bot_username_suffix_re}\s*$",
            func=lambda e: e.is_private,
        )
    )(json_mode_handler)
    borg.on(events.NewMessage(pattern=rf"(?i)^/tts{bot_username_suffix_re}\s*$"))(
        tts_handler
    )
    borg.on(
        events.NewMessage(
            pattern=rf"(?i)^/geminivoice{bot_username_suffix_re}\s*$",
            func=lambda e: e.is_private,
        )
    )(gemini_voice_handler)
    borg.on(
        events.NewMessage(pattern=rf"(?i)^/geminivoicehere{bot_username_suffix_re}\s*$")
    )(gemini_voice_here_handler)
    borg.on(
        events.NewMessage(
            pattern=rf"(?i)^/live{bot_username_suffix_re}\s*$",
            func=lambda e: e.is_private,
        )
    )(live_handler)
    borg.on(
        events.NewMessage(
            pattern=rf"(?i)^/livemodel{bot_username_suffix_re}\s*$",
            func=lambda e: e.is_private,
        )
    )(livemodel_handler)
    borg.on(
        events.NewMessage(
            pattern=rf"(?i)^/testlive{bot_username_suffix_re}\s*$",
            func=lambda e: e.is_private,
        )
    )(testlive_handler)

    # Func-based Handlers
    borg.on(
        events.NewMessage(
            func=lambda e: e.is_private
            and llm_db.is_awaiting_key(e.sender_id)
            and e.text
            and not e.text.startswith("/")
        )
    )(key_submission_handler)
    borg.on(
        events.NewMessage(
            func=lambda e: e.is_private
            and e.sender_id in AWAITING_INPUT_FROM_USERS
            and e.text
            and not e.text.startswith("/")
        )
    )(generic_input_handler)
    borg.on(events.NewMessage(func=is_valid_chat_message))(chat_handler)

    # Other Event Handlers
    borg.on(events.CallbackQuery())(callback_handler)

    print("LLM_Chat: All event handlers registered.")


async def initialize_llm_chat():
    """Initializes the plugin based on whether it's a bot or userbot."""
    global BOT_ID, BOT_USERNAME, IS_BOT, DEFAULT_SYSTEM_PROMPT
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

    if BOT_USERNAME:
        DEFAULT_SYSTEM_PROMPT += f"""

Your username on Telegram is {BOT_USERNAME}. The user might mention you using this username.
"""

    if IS_BOT:
        await history_util.initialize_history_handler()
        print("LLM_Chat: Running as a BOT. History utility initialized.")
        await bot_util.register_bot_commands(borg, BOT_COMMANDS)
    else:
        print(
            "LLM_Chat: Running as a USERBOT. History utility and bot commands skipped."
        )

    # Load smart context states from Redis on startup (both bot and userbot)
    await load_smart_context_states()
    register_handlers()


# --- Telethon Event Handlers ---


async def start_handler(event):
    """Handles the /start command to onboard new users."""
    user_id = event.sender_id
    # Cancel any pending input flows
    if llm_db.is_awaiting_key(user_id):
        llm_db.cancel_key_flow(user_id)
    cancel_input_flow(user_id)

    # Check for Gemini API key specifically
    if llm_db.get_api_key(user_id=user_id, service="gemini"):
        await event.reply(
            f"{BOT_META_INFO_PREFIX}Welcome back! Your Gemini API key is configured. You can start chatting with me.\n\n"
            "Use /help to see all available commands."
        )
    else:
        # If no Gemini key, start the process for it.
        await llm_db.request_api_key_message(event, "gemini")


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


async def status_handler(event):
    """Displays a summary of the user's current settings."""
    user_id = event.sender_id
    chat_id = event.chat_id

    prefs = user_manager.get_prefs(user_id)
    chat_prefs = chat_manager.get_prefs(chat_id)
    chat_prompt = chat_prefs.system_prompt

    enabled_tools_str = (
        ", ".join(prefs.enabled_tools) if prefs.enabled_tools else "None"
    )

    # Determine status of the user-specific system prompt
    user_system_prompt_status = "Default"
    if prefs.system_prompt is not None:
        user_system_prompt_status = "Custom"

    # Determine status of the chat-specific system prompt
    chat_system_prompt_status = "Not set"
    if chat_prompt:
        chat_system_prompt_status = "Custom (Overrides your personal prompt)"

    # Get context mode name and handle smart mode
    context_mode_name = CONTEXT_MODE_NAMES.get(
        prefs.context_mode, prefs.context_mode.replace("_", " ").title()
    )
    smart_mode_status_str = ""
    if prefs.context_mode == "smart":
        current_smart_state = get_smart_context_mode(user_id)
        smart_state_name = CONTEXT_MODE_NAMES.get(
            current_smart_state, current_smart_state
        )
        smart_mode_status_str = f" (State: `{smart_state_name}`)"

    group_context_mode_name = CONTEXT_MODE_NAMES.get(
        prefs.group_context_mode, prefs.group_context_mode.replace("_", " ").title()
    )
    group_smart_mode_status_str = ""
    if prefs.group_context_mode == "smart":
        current_smart_state = get_smart_context_mode(user_id)
        smart_state_name = CONTEXT_MODE_NAMES.get(
            current_smart_state, current_smart_state
        )
        group_smart_mode_status_str = f" (State: `{smart_state_name}`)"

    metadata_mode_name = METADATA_MODES.get(
        prefs.metadata_mode, prefs.metadata_mode.replace("_", " ").title()
    )
    group_metadata_mode_name = METADATA_MODES.get(
        prefs.group_metadata_mode,
        prefs.group_metadata_mode.replace("_", " ").title(),
    )
    group_activation_mode_name = GROUP_ACTIVATION_MODES.get(
        prefs.group_activation_mode,
        prefs.group_activation_mode.replace("_", " ").title(),
    )
    thinking_level = prefs.thinking.capitalize() if prefs.thinking else "Default"

    # TTS Settings
    tts_model_display = tts_util.TTS_MODELS.get(chat_prefs.tts_model, "Unknown")
    if chat_prefs.tts_voice_override:
        effective_voice_display = f"`{chat_prefs.tts_voice_override}` (this chat)"
    else:
        effective_voice_display = f"`{prefs.tts_global_voice}` (global default)"

    status_message = (
        f"**Your Personal Bot Settings**\n\n"
        f"∙ **Model:** `{prefs.model}`\n"
        f"∙ **Reasoning Level:** `{thinking_level}`\n"
        f"∙ **Enabled Tools:** `{enabled_tools_str}`\n"
        f"∙ **JSON Mode:** `{'Enabled' if prefs.json_mode else 'Disabled'}`\n"
        f"∙ **Personal System Prompt:** `{user_system_prompt_status}`\n\n"
        f"**This Chat's Settings**\n"
        f"∙ **Chat System Prompt:** `{chat_system_prompt_status}`\n\n"
        f"**TTS Settings (This Chat)**\n"
        f"∙ **TTS Model:** `{tts_model_display}`\n"
        f"∙ **Voice:** {effective_voice_display}\n\n"
        f"**Private Chat Context**\n"
        f"∙ **Context Mode:** `{context_mode_name}`{smart_mode_status_str}\n"
        f"∙ **Metadata Mode:** `{metadata_mode_name}`\n\n"
        f"**Group Chat Context**\n"
        f"∙ **Context Mode:** `{group_context_mode_name}`{group_smart_mode_status_str}\n"
        f"∙ **Metadata Mode:** `{group_metadata_mode_name}`\n"
        f"∙ **Activation:** `{group_activation_mode_name}`\n"
    )
    await event.reply(f"{BOT_META_INFO_PREFIX}{status_message}", parse_mode="md")


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
        await llm_util.handle_llm_error(
            event=event,
            exception=e,
            base_error_message="Sorry, an error occurred while retrieving your logs.",
            error_id_p=True,
        )


async def set_key_handler(event):
    """Delegates /setgeminikey command logic to the shared module."""
    await llm_db.handle_set_key_command(event, "gemini")


async def set_openrouter_key_handler(event):
    """Delegates /setopenrouterkey command logic to the shared module."""
    await llm_db.handle_set_key_command(event, "openrouter")


async def key_submission_handler(event):
    """Delegates plain-text key submission logic to the shared module."""
    service = llm_db.get_awaiting_service(event.sender_id)
    success_msg = f"You can now use {service.capitalize()} models."
    await llm_db.handle_key_submission(event, success_msg=success_msg)


async def set_model_handler(event):
    """Sets the user's preferred chat model, now with an interactive flow."""
    user_id = event.sender_id
    model_name_match = event.pattern_match.group(1)
    prefs = user_manager.get_prefs(user_id)

    if model_name_match:
        model_name = model_name_match.strip()
        user_manager.set_model(user_id, model_name)
        cancel_input_flow(user_id)
        await event.reply(
            f"{BOT_META_INFO_PREFIX}Your chat model has been set to: `{model_name}`"
        )
    else:
        await bot_util.present_options(
            event,
            title="Set Chat Model",
            options=MODEL_CHOICES,
            current_value=prefs.model,
            callback_prefix="model_",
            awaiting_key="model_selection",
            n_cols=2,
        )
        # Also prompt for custom model
        await event.reply(
            f"{BOT_META_INFO_PREFIX}Or, send a custom model ID below."
            "\n(Type `cancel` to stop.)"
        )
        AWAITING_INPUT_FROM_USERS[user_id] = {"type": "model"}


async def set_system_prompt_handler(event):
    """Sets the user's custom system prompt or resets it, now with an interactive flow."""
    user_id = event.sender_id
    prompt_match = event.pattern_match.group(1)

    if prompt_match:
        prompt = prompt_match.strip()
        cancel_input_flow(user_id)
        if prompt.lower() == "reset":
            # Set the prompt to None to signify using the default
            user_manager.set_system_prompt(user_id, None)
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
            user_manager.get_prefs(user_id).system_prompt or DEFAULT_SYSTEM_PROMPT
        )
        await event.reply(
            f"{BOT_META_INFO_PREFIX}**Your current system prompt is:**\n\n```\n{current_prompt}\n```"
            "\n\nPlease send the new system prompt in the next message."
            "\n(You can also send `reset` to restore the default, or `cancel` to stop.)"
        )


async def set_system_prompt_here_handler(event):
    """Sets a system prompt for the current chat only."""
    is_bot_admin = await util.isAdmin(event)
    is_group_admin = await util.is_group_admin(event)

    if not event.is_private and not (is_bot_admin or is_group_admin):
        await event.reply(
            f"{BOT_META_INFO_PREFIX}You must be a group admin or bot admin to use this command in a group."
        )
        return

    prompt_match = event.pattern_match.group(1)
    if not prompt_match or not prompt_match.strip():
        await event.reply(
            f"{BOT_META_INFO_PREFIX}**Usage:** `/setSystemPromptHere <your prompt here>`"
        )
        return

    prompt = prompt_match.strip()
    chat_manager.set_system_prompt(event.chat_id, prompt)
    await event.reply(
        f"{BOT_META_INFO_PREFIX}✅ This chat's system prompt has been updated."
    )


async def reset_system_prompt_here_handler(event):
    """Resets the system prompt for the current chat."""
    is_bot_admin = await util.isAdmin(event)
    is_group_admin = await util.is_group_admin(event)

    if not event.is_private and not (is_bot_admin or is_group_admin):
        await event.reply(
            f"{BOT_META_INFO_PREFIX}You must be a group admin or bot admin to use this command in a group."
        )
        return

    chat_manager.set_system_prompt(event.chat_id, None)
    await event.reply(
        f"{BOT_META_INFO_PREFIX}✅ This chat's system prompt has been reset to default."
    )


async def get_system_prompt_here_handler(event):
    """Gets and displays the system prompt for the current chat."""
    prompt_info = get_system_prompt_info(event)

    if prompt_info.source == "chat":
        await event.reply(
            f"{BOT_META_INFO_PREFIX}**Current chat system prompt:**\n\n```\n{prompt_info.chat_prompt}\n```",
            parse_mode="md",
        )
    else:
        source_text = (
            "user's personal prompt"
            if prompt_info.source == "user"
            else "default system prompt"
        )
        await event.reply(
            f"{BOT_META_INFO_PREFIX}This chat has no custom system prompt set. Using {source_text}:\n\n```\n{prompt_info.effective_prompt}\n```",
            parse_mode="md",
        )


async def context_mode_here_handler(event):
    """Sets the context mode for the current chat, including status and a reset option."""
    is_bot_admin = await util.isAdmin(event)
    is_group_admin = await util.is_group_admin(event)

    if not event.is_private and not (is_bot_admin or is_group_admin):
        await event.reply(
            f"{BOT_META_INFO_PREFIX}You must be a group admin or bot admin to use this command in a group."
        )
        return

    # Get the current status text to display to the user
    status_text = await _get_context_mode_status_text(event)

    # Get the currently set chat-specific preference
    chat_prefs = chat_manager.get_prefs(event.chat_id)
    current_mode = chat_prefs.context_mode  # This will be None if not set

    # Prepare options for the menu, including a "Not Set" option for resetting
    options_for_menu = CONTEXT_MODE_NAMES.copy()
    options_for_menu["not_set"] = NOT_SET_HERE_DISPLAY_NAME

    await bot_util.present_options(
        event,
        title=f"**Current Status:**\n{status_text}\n\n**Set Context Mode for This Chat**",
        options=options_for_menu,
        current_value=current_mode if current_mode is not None else "not_set",
        callback_prefix="contexthere_",
        awaiting_key="context_mode_here_selection",
        n_cols=1,
    )


async def reset_context_mode_here_handler(event):
    """Resets the context mode for the current chat."""
    #: This command has been deprecated and is no longer registered.
    #: But we have kept its code for possible future use.
    ##
    is_bot_admin = await util.isAdmin(event)
    is_group_admin = await util.is_group_admin(event)

    if not event.is_private and not (is_bot_admin or is_group_admin):
        await event.reply(
            f"{BOT_META_INFO_PREFIX}You must be a group admin or bot admin to use this command in a group."
        )
        return

    chat_manager.set_context_mode(event.chat_id, None)
    await event.reply(
        f"{BOT_META_INFO_PREFIX}✅ This chat's context mode has been reset to default (uses user preferences)."
    )


async def get_context_mode_here_handler(event):
    """Gets and displays the context mode for the current chat."""
    status_text = await _get_context_mode_status_text(event)
    await event.reply(
        f"{BOT_META_INFO_PREFIX}**Chat Context Mode Status**\n\n{status_text}",
        parse_mode="md",
    )


# --- New Feature Handlers ---


async def context_mode_handler(event):
    prefs = user_manager.get_prefs(event.sender_id)

    await bot_util.present_options(
        event,
        title="Set Private Chat Context Mode",
        options=CONTEXT_MODE_NAMES,
        current_value=prefs.context_mode,
        callback_prefix="context_",
        awaiting_key="context_mode_selection",
        n_cols=1,
    )


async def group_context_mode_handler(event):
    prefs = user_manager.get_prefs(event.sender_id)
    await bot_util.present_options(
        event,
        title="Set Group Chat Context Mode",
        options=CONTEXT_MODE_NAMES,
        current_value=prefs.group_context_mode,
        callback_prefix="groupcontext_",
        awaiting_key="group_context_mode_selection",
        n_cols=1,
    )


async def metadata_mode_handler(event):
    prefs = user_manager.get_prefs(event.sender_id)
    await bot_util.present_options(
        event,
        title="Set Private Chat Metadata Mode",
        options=METADATA_MODES,
        current_value=prefs.metadata_mode,
        callback_prefix="metadata_",
        awaiting_key="metadata_mode_selection",
        n_cols=1,
    )


async def group_metadata_mode_handler(event):
    prefs = user_manager.get_prefs(event.sender_id)
    await bot_util.present_options(
        event,
        title="Set Group Chat Metadata Mode",
        options=METADATA_MODES,
        current_value=prefs.group_metadata_mode,
        callback_prefix="groupmetadata_",
        awaiting_key="group_metadata_mode_selection",
        n_cols=1,
    )


async def sep_handler(event):
    """Switch to smart mode and set to until_separator context."""
    user_id = event.sender_id

    # Set user's context mode to smart (enables smart mode)
    user_manager.set_context_mode(user_id, "smart")

    # Set smart context mode to until_separator
    await set_smart_context_mode(user_id, "until_separator")

    await event.reply(
        f"{BOT_META_INFO_PREFIX}✅ Switched to **Smart Mode** with `Until Separator` context. "
        f"All messages will be included until you reply to a message or send `{CONTEXT_SEPARATOR}`."
    )


async def group_activation_mode_handler(event):
    prefs = user_manager.get_prefs(event.sender_id)
    await bot_util.present_options(
        event,
        title="Set Group Chat Activation Mode",
        options=GROUP_ACTIVATION_MODES,
        current_value=prefs.group_activation_mode,
        callback_prefix="groupactivation_",
        awaiting_key="group_activation_mode_selection",
    )


async def set_think_handler(event):
    prefs = user_manager.get_prefs(event.sender_id)
    # Add "clear" option
    think_options = {level: level.capitalize() for level in REASONING_LEVELS}
    think_options["clear"] = "Clear (Default)"
    await bot_util.present_options(
        event,
        title="Set Reasoning Effort",
        options=think_options,
        current_value=prefs.thinking or "clear",
        callback_prefix="think_",
        awaiting_key="think_selection",
    )


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


async def json_mode_handler(event):
    """Toggles JSON mode."""
    is_enabled = user_manager.toggle_json_mode(event.sender_id)
    await event.reply(
        f"{BOT_META_INFO_PREFIX}JSON response mode has been **{'enabled' if is_enabled else 'disabled'}**."
    )


async def tts_handler(event):
    """Handle /tts command - per-chat TTS model selection"""
    current_model = chat_manager.get_tts_model(event.chat_id)
    await bot_util.present_options(
        event,
        title="🔊 TTS Settings for this chat",
        options=tts_util.TTS_MODELS,
        current_value=current_model,
        callback_prefix="tts_",
        awaiting_key="tts_selection",
        n_cols=1,
    )


async def gemini_voice_handler(event):
    """Handle /geminiVoice - global voice selection"""
    current_voice = user_manager.get_tts_global_voice(event.sender_id)
    voice_options = {
        name: f"{name}: {desc}" for name, desc in tts_util.GEMINI_VOICES.items()
    }
    await bot_util.present_options(
        event,
        title="🎤 Default Gemini voice (all chats)",
        options=voice_options,
        current_value=current_voice,
        callback_prefix="voice_",
        awaiting_key="voice_selection",
        n_cols=3,
    )


async def gemini_voice_here_handler(event):
    """Handle /geminiVoiceHere - per-chat voice override"""
    is_bot_admin = await util.isAdmin(event)
    is_group_admin = await util.is_group_admin(event)

    if not event.is_private and not (is_bot_admin or is_group_admin):
        await event.reply(
            f"{BOT_META_INFO_PREFIX}You must be a group admin or bot admin to use this command in a group."
        )
        return

    current_voice = chat_manager.get_tts_voice_override(event.chat_id)
    global_voice = user_manager.get_tts_global_voice(event.sender_id)

    # Add "Use Global Default" option and format all voice options
    voice_options = {"": f"Use Global Default ({global_voice})"}
    voice_options.update(
        {name: f"{name}: {desc}" for name, desc in tts_util.GEMINI_VOICES.items()}
    )

    await bot_util.present_options(
        event,
        title="🎤 Gemini voice for this chat only",
        options=voice_options,
        current_value=current_voice or "",
        callback_prefix="voicehere_",
        awaiting_key="voice_here_selection",
        n_cols=3,
    )


async def callback_handler(event):
    """Handles all inline button presses for the plugin (BOT MODE ONLY)."""
    data_str = event.data.decode("utf-8")
    user_id = event.sender_id
    #: @Claude Based on the Telethon documentation, I can now confirm that event.sender_id in a CallbackQuery event is indeed the ID of the person who clicked the button, not the original sender of the menu message.

    prefs = user_manager.get_prefs(user_id)

    if data_str.startswith("model_"):
        model_id = bot_util.unsanitize_callback_data(data_str.split("_", 1)[1])
        user_manager.set_model(user_id, model_id)
        cancel_input_flow(user_id)  # Cancel the custom input flow
        prefs = user_manager.get_prefs(user_id)  # update prefs
        buttons = [
            KeyboardButtonCallback(
                f"✅ {name}" if key == prefs.model else name,
                data=f"model_{bot_util.sanitize_callback_data(key)}",
            )
            for key, name in MODEL_CHOICES.items()
        ]
        await event.edit(buttons=util.build_menu(buttons, n_cols=2))
        await event.answer(f"Model set to {MODEL_CHOICES[model_id]}")

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
    elif data_str.startswith("contexthere_"):
        # Check admin permissions for chat context mode changes
        is_bot_admin = await util.isAdmin(event)
        is_group_admin = await util.is_group_admin(event)

        if not event.is_private and not (is_bot_admin or is_group_admin):
            await event.answer(
                "You must be a group admin or bot admin to change chat context mode."
            )
            return

        mode = data_str.split("_", 1)[1]

        # If user selected "not_set", we store None
        mode_to_set = None if mode == "not_set" else mode
        chat_manager.set_context_mode(event.chat_id, mode_to_set)

        # Re-fetch prefs to update the button display correctly
        chat_prefs = chat_manager.get_prefs(event.chat_id)
        current_mode_for_buttons = (
            chat_prefs.context_mode
            if chat_prefs.context_mode is not None
            else "not_set"
        )

        options_for_menu = CONTEXT_MODE_NAMES.copy()
        options_for_menu["not_set"] = "Not Set (Use Personal Default)"

        buttons = [
            KeyboardButtonCallback(
                f"✅ {name}" if key == current_mode_for_buttons else name,
                data=f"contexthere_{key}",
            )
            for key, name in options_for_menu.items()
        ]

        # We also need to update the title text after the change
        new_status_text = await _get_context_mode_status_text(event)
        new_title = f"**Current Status:**\n{new_status_text}\n\n**Set Context Mode for This Chat**"

        try:
            await event.edit(
                text=f"{BOT_META_INFO_PREFIX}{new_title}",
                buttons=util.build_menu(buttons, n_cols=1),
                parse_mode="md",
            )
        except errors.rpcerrorlist.MessageNotModifiedError:
            pass  # Ignore if nothing changed

        await event.answer("Chat context mode updated.")
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
    elif data_str.startswith("tts_"):
        model = data_str.split("_", 1)[1]
        chat_manager.set_tts_model(event.chat_id, model)
        buttons = [
            KeyboardButtonCallback(
                f"✅ {name}" if key == model else name,
                data=f"tts_{key}",
            )
            for key, name in tts_util.TTS_MODELS.items()
        ]
        await event.edit(buttons=util.build_menu(buttons, n_cols=1))
        await event.answer(f"TTS set to {tts_util.TTS_MODELS[model]}")
    elif data_str.startswith("voice_"):
        voice = data_str.split("_", 1)[1]
        user_manager.set_tts_global_voice(user_id, voice)
        buttons = [
            KeyboardButtonCallback(
                f"✅ {name}: {desc}" if name == voice else f"{name}: {desc}",
                data=f"voice_{name}",
            )
            for name, desc in tts_util.GEMINI_VOICES.items()
        ]
        await event.edit(buttons=util.build_menu(buttons, n_cols=3))
        await event.answer(f"Global voice set to {voice}")
    elif data_str.startswith("voicehere_"):
        voice = data_str.split("_", 1)[1]
        # Check admin permissions for chat voice changes
        is_bot_admin = await util.isAdmin(event)
        is_group_admin = await util.is_group_admin(event)

        if not event.is_private and not (is_bot_admin or is_group_admin):
            await event.answer("Admin access required.", show_alert=True)
            return

        chat_manager.set_tts_voice_override(event.chat_id, voice if voice else None)
        global_voice = user_manager.get_tts_global_voice(user_id)

        # Rebuild options with current selection and consistent formatting
        voice_options = {"": f"Use Global Default ({global_voice})"}
        voice_options.update(
            {name: f"{name}: {desc}" for name, desc in tts_util.GEMINI_VOICES.items()}
        )

        buttons = [
            KeyboardButtonCallback(
                f"✅ {display}" if key == voice else display,
                data=f"voicehere_{key}",
            )
            for key, display in voice_options.items()
        ]
        await event.edit(buttons=util.build_menu(buttons, n_cols=3))
        voice_name = voice if voice else f"Global Default ({global_voice})"
        await event.answer(f"Chat voice set to {voice_name}")
    elif data_str.startswith("livemodel_"):
        model_key = bot_util.unsanitize_callback_data(data_str.split("_", 1)[1])
        user_manager.set_live_model(user_id, model_key)
        cancel_input_flow(user_id)
        prefs = user_manager.get_prefs(user_id)  # update prefs

        # Rebuild buttons with current selection
        live_model_options = {
            "gemini-2.5-flash-preview-native-audio-dialog": "Gemini 2.5 Flash (Native Audio Dialog)",
            "gemini-2.5-flash-exp-native-audio-thinking-dialog": "Gemini 2.5 Flash (Native Audio + Thinking)",
            "gemini-live-2.5-flash-preview": "Gemini Live 2.5 Flash Preview",
            "gemini-2.0-flash-live-001": "Gemini 2.0 Flash Live",
        }

        buttons = [
            KeyboardButtonCallback(
                f"✅ {display}" if key == prefs.live_model else display,
                data=f"livemodel_{bot_util.sanitize_callback_data(key)}",
            )
            for key, display in live_model_options.items()
        ]
        await event.edit(buttons=util.build_menu(buttons, n_cols=1))
        await event.answer(f"Live model set to {live_model_options[model_key]}")


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
                elif input_type == "context_mode_here_selection":
                    mode_to_set = None if selected_key == "not_set" else selected_key
                    chat_manager.set_context_mode(event.chat_id, mode_to_set)

                    # Fetch the display name for the confirmation message
                    display_name = NOT_SET_HERE_DISPLAY_NAME
                    if mode_to_set:
                        display_name = CONTEXT_MODE_NAMES[mode_to_set]

                    await event.reply(
                        f"{BOT_META_INFO_PREFIX}✅ This chat's context mode has been set to: **{display_name}**"
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
                elif input_type == "tts_selection":
                    chat_manager.set_tts_model(event.chat_id, selected_key)
                    await event.reply(
                        f"{BOT_META_INFO_PREFIX}✅ TTS model set to: **{tts_util.TTS_MODELS[selected_key]}**"
                    )
                elif input_type == "voice_selection":
                    user_manager.set_tts_global_voice(user_id, selected_key)
                    await event.reply(
                        f"{BOT_META_INFO_PREFIX}✅ Global voice set to: **{selected_key}: {tts_util.GEMINI_VOICES[selected_key]}**"
                    )
                elif input_type == "voice_here_selection":
                    # Check admin permissions
                    is_bot_admin = await util.isAdmin(event)
                    is_group_admin = await util.is_group_admin(event)

                    if not event.is_private and not (is_bot_admin or is_group_admin):
                        await event.reply(
                            f"{BOT_META_INFO_PREFIX}You must be a group admin or bot admin to use this command in a group."
                        )
                        return

                    voice_to_set = None if selected_key == "" else selected_key
                    chat_manager.set_tts_voice_override(event.chat_id, voice_to_set)

                    if voice_to_set:
                        voice_name = (
                            f"{voice_to_set}: {tts_util.GEMINI_VOICES[voice_to_set]}"
                        )
                    else:
                        global_voice = user_manager.get_tts_global_voice(user_id)
                        voice_name = f"Global Default ({global_voice})"

                    await event.reply(
                        f"{BOT_META_INFO_PREFIX}✅ Chat voice set to: **{voice_name}**"
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


# --- Live Mode Handlers ---


async def live_handler(event):
    """Toggle live mode for real-time audio/video chat."""
    if not event.is_private:
        await event.reply(
            f"{BOT_META_INFO_PREFIX}❌ Live mode is only available in private chats."
        )
        return

    user_id = event.sender_id
    chat_id = event.chat_id

    # Get current live mode state
    is_active = gemini_live_util.live_session_manager.is_live_mode_active(chat_id)

    if is_active:
        # End live session
        ended = await gemini_live_util.live_session_manager.end_session(chat_id)
        if ended:
            chat_manager.set_live_mode_enabled(chat_id, False)
            await event.reply(f"{BOT_META_INFO_PREFIX}🔴 Live mode disabled.")
        else:
            await event.reply(f"{BOT_META_INFO_PREFIX}❌ No active live session found.")
    else:
        # Check if user can create a new session
        if not await gemini_live_util.live_session_manager.can_create_session(user_id):
            is_admin = await util.isAdmin(event)
            limit = (
                gemini_live_util.ADMIN_CONCURRENT_LIVE_LIMIT
                if is_admin
                else gemini_live_util.CONCURRENT_LIVE_LIMIT
            )
            await event.reply(
                f"{BOT_META_INFO_PREFIX}❌ Maximum concurrent sessions limit reached ({limit})."
            )
            return

        # Get user's live model preference and API key
        prefs = user_manager.get_prefs(user_id)
        live_model = prefs.live_model

        # Get API key
        api_key = llm_db.get_api_key(user_id=user_id, service="gemini")
        if not api_key:
            await event.reply(
                f"{BOT_META_INFO_PREFIX}❌ Please set your Gemini API key first using `/setgeminikey`."
            )
            return

        try:
            # Create new live session
            session = await gemini_live_util.live_session_manager.create_session(
                chat_id, user_id, live_model, api_key
            )
            chat_manager.set_live_mode_enabled(chat_id, True)
            await event.reply(
                f"{BOT_META_INFO_PREFIX}🟢 Live mode enabled with model **{live_model}**.\n"
                f"Send audio, video, or text messages for real-time conversation.\n"
                f"Session ID: `{session.session_id[:8]}...`"
            )
        except Exception as e:
            print(f"Error creating live session: {e}")
            traceback.print_exc()
            await event.reply(
                f"{BOT_META_INFO_PREFIX}❌ Failed to start live mode: {str(e)}"
            )


async def livemodel_handler(event):
    """Set your preferred live mode model."""
    user_id = event.sender_id

    # Available live models with display names
    live_model_options = {
        "gemini-2.5-flash-preview-native-audio-dialog": "Gemini 2.5 Flash (Native Audio Dialog)",
        "gemini-2.5-flash-exp-native-audio-thinking-dialog": "Gemini 2.5 Flash (Native Audio + Thinking)",
        "gemini-live-2.5-flash-preview": "Gemini Live 2.5 Flash Preview",
        "gemini-2.0-flash-live-001": "Gemini 2.0 Flash Live",
    }

    # Get current live model
    prefs = user_manager.get_prefs(user_id)
    current_model = prefs.live_model

    await bot_util.present_options(
        event,
        title="Select your preferred live mode model",
        options=live_model_options,
        current_value=current_model,
        callback_prefix="livemodel_",
        awaiting_key="livemodel_selection",
        n_cols=1,
    )


async def testlive_handler(event):
    """Test live session connection with official example (admin only)."""
    user_id = event.sender_id

    # Check if user is admin
    if not await util.isAdmin(event):
        await event.reply(
            f"{BOT_META_INFO_PREFIX}❌ This command is only available to administrators."
        )
        return

    # Get API key
    api_key = llm_db.get_api_key(user_id=user_id, service="gemini")
    if not api_key:
        await event.reply(
            f"{BOT_META_INFO_PREFIX}❌ Gemini API key not found. Please set it first with /setgeminikey"
        )
        return

    await event.reply(f"{BOT_META_INFO_PREFIX}🧪 Testing live session connection...")

    try:
        print(f"[TestLive] Starting test for user {user_id}")

        # Import required modules
        from google import genai
        from google.genai import types
        import io
        import tempfile

        # Create client
        client = genai.Client(api_key=api_key)
        # Try a more basic live model first
        model = "gemini-2.0-flash-live-001"

        config = {"response_modalities": ["TEXT"]}

        print(f"[TestLive] Created client and config")
        print(f"[TestLive] Model: {model}")
        print(f"[TestLive] Config: {config}")

        # Test basic API access first
        try:
            models = client.models.list()
            print(f"[TestLive] API key valid, found {len(list(models))} models")
        except Exception as api_error:
            print(f"[TestLive] API key validation failed: {api_error}")
            await event.reply(f"{BOT_META_INFO_PREFIX}❌ API key validation failed: {str(api_error)}")
            return

        await event.reply(f"{BOT_META_INFO_PREFIX}🔗 Attempting WebSocket connection...")
        print(f"[TestLive] Attempting WebSocket connection to Gemini Live API...")

        # Test connection
        async with client.aio.live.connect(model=model, config=config) as session:
            print(f"[TestLive] Successfully connected to live session!")
            await event.reply(
                f"{BOT_META_INFO_PREFIX}✅ Live session connected successfully!"
            )

            # Send a simple text message
            await session.send_client_content(
                turns={
                    "role": "user",
                    "parts": [{"text": "Hello, this is a test message"}],
                },
                turn_complete=True,
            )
            print(f"[TestLive] Sent test message")

            # Wait for response with timeout
            response_received = False
            async for response in session.receive():
                if response.text is not None:
                    print(f"[TestLive] Received response: {response.text[:100]}...")
                    await event.reply(
                        f"{BOT_META_INFO_PREFIX}📨 Received response: {response.text[:200]}..."
                    )
                    response_received = True
                    break

            if not response_received:
                await event.reply(
                    f"{BOT_META_INFO_PREFIX}⚠️ No response received from live session"
                )

        print(f"[TestLive] Test completed successfully")
        await event.reply(
            f"{BOT_META_INFO_PREFIX}✅ Live session test completed successfully!"
        )

    except Exception as test_error:
        error_msg = str(test_error)
        print(f"[TestLive] Test failed: {error_msg}")
        traceback.print_exc()
        await event.reply(
            f"{BOT_META_INFO_PREFIX}❌ Live session test failed: {error_msg}"
        )


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

    if _is_known_command(event.text):
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
        mention_re = r"(?<!\w)" + re.escape(BOT_USERNAME) + r"\b"
        if (
            event.text
            and BOT_USERNAME
            and re.search(mention_re, event.text, re.IGNORECASE)
        ):
            return True

        elif event.text and BOT_USERNAME in event.text:
            print(
                f"Unmatched mention in group chat: mention_re={mention_re}, text:\n{event.text}\n---"
            )

        if prefs.group_activation_mode == "mention_and_reply" and event.is_reply:
            try:
                reply_msg = await event.get_reply_message()
                if reply_msg and reply_msg.sender_id == borg.me.id:
                    return True
            except Exception:
                return False

    return False


async def _handle_tts_response(event, response_text: str):
    """Handle TTS generation for LLM responses."""
    try:
        # Check if TTS is enabled for this chat
        tts_model = chat_manager.get_tts_model(event.chat_id)
        if tts_model == "Disabled":
            return

        # Get user's Gemini API key
        api_key = llm_db.get_api_key(user_id=event.sender_id, service="gemini")
        if not api_key:
            return  # No API key, silently skip TTS

        # Determine voice to use (chat override or global default)
        voice_override = chat_manager.get_tts_voice_override(event.chat_id)
        if voice_override:
            voice = voice_override
        else:
            voice = user_manager.get_tts_global_voice(event.sender_id)

        # Truncate text if needed
        truncated_text, was_truncated = tts_util.truncate_text_for_tts(response_text)

        # Generate TTS audio (returns OGG file path)
        ogg_file_path = await tts_util.generate_tts_audio(
            truncated_text, voice=voice, model=tts_model, api_key=api_key
        )

        try:
            # Send as voice message with proper attributes
            from telethon.tl.types import DocumentAttributeAudio

            await event.client.send_file(
                event.chat_id,
                ogg_file_path,
                voice_note=True,
                reply_to=event.id,
                attributes=[
                    DocumentAttributeAudio(
                        duration=0,  # Duration will be auto-detected by Telegram
                        voice=True,
                    )
                ],
            )
        finally:
            # Clean up temporary file
            try:
                import os

                os.remove(ogg_file_path)
            except Exception as cleanup_error:
                print(
                    f"Warning: Failed to cleanup TTS temp file {ogg_file_path}: {cleanup_error}"
                )

        # Send truncation notice if needed
        if was_truncated:
            await event.reply(
                f"{BOT_META_INFO_PREFIX}🔊 **TTS Note:** Text was truncated to {tts_util.TTS_MAX_LENGTH} characters for voice generation."
            )

    except Exception as e:
        # Handle TTS errors gracefully
        await tts_util.handle_tts_error(event=event, exception=e, service="gemini")


async def handle_live_mode_message(event):
    """Handle messages when live mode is active."""
    chat_id = event.chat_id
    session = gemini_live_util.live_session_manager.get_session(chat_id)

    if not session or session.is_expired():
        await event.reply(
            f"{BOT_META_INFO_PREFIX}❌ Live session disconnected. Use `/live` to restart."
        )
        return

    # Update session activity
    gemini_live_util.live_session_manager.update_session_activity(chat_id)

    try:
        # Get API key
        api_key = llm_db.get_api_key(user_id=event.sender_id, service="gemini")
        if not api_key:
            await event.reply(f"{BOT_META_INFO_PREFIX}❌ API key not found.")
            return

        gemini_api = gemini_live_util.GeminiLiveAPI(api_key)

        # Start the session context manager if not already started
        if session._session_context is None:
            try:
                # Store the context manager and enter it properly
                session._session_context = session.session
                session._live_connection = await session._session_context.__aenter__()
                session.is_connected = True
                print(f"Live session connected for chat {chat_id}")

                # Start response listener
                session._response_task = asyncio.create_task(
                    handle_live_mode_responses(session, event)
                )
            except Exception as conn_error:
                print(f"Failed to connect live session: {conn_error}")
                traceback.print_exc()

                # Clean up on connection failure
                session._session_context = None
                session.is_connected = False

                await event.reply(
                    f"{BOT_META_INFO_PREFIX}❌ Failed to connect to live session: {str(conn_error)}"
                )
                return

        live_session = session._live_connection

        # Handle different message types with connection error recovery
        if event.text:
            # Text message
            try:
                await gemini_api.send_text(live_session, event.text)
            except Exception as send_error:
                print(f"Error sending to live session: {send_error}")
                traceback.print_exc()

                # Check if it's a connection error - if so, mark session as disconnected
                if (
                    "connection" in str(send_error).lower()
                    or "websocket" in str(send_error).lower()
                ):
                    session.is_connected = False
                    session._session_context = None
                    await event.reply(
                        f"{BOT_META_INFO_PREFIX}❌ Live session connection lost. Use `/live` to restart."
                    )
                    return
                else:
                    # Other error - still notify user
                    await event.reply(
                        f"{BOT_META_INFO_PREFIX}❌ Error in live session: {str(send_error)}"
                    )
                    return

        elif event.audio or event.voice:
            # Audio message
            media_info = await event.download_media(bytes)
            if media_info:
                # Save to temp file for processing
                with tempfile.NamedTemporaryFile(
                    suffix=".ogg", delete=False
                ) as temp_file:
                    temp_file.write(media_info)
                    temp_path = temp_file.name

                try:
                    # Convert OGG to PCM for Gemini
                    pcm_data = await gemini_live_util.AudioProcessor.convert_ogg_to_pcm(
                        temp_path
                    )

                    # Send audio with connection error handling
                    try:
                        await gemini_api.send_audio_chunk(live_session, pcm_data)
                    except Exception as send_error:
                        print(f"Error sending audio to live session: {send_error}")
                        traceback.print_exc()

                        # Check if it's a connection error
                        if (
                            "connection" in str(send_error).lower()
                            or "websocket" in str(send_error).lower()
                        ):
                            session.is_connected = False
                            session._session_context = None
                            await event.reply(
                                f"{BOT_META_INFO_PREFIX}❌ Live session connection lost. Use `/live` to restart."
                            )
                            return
                        else:
                            # Other error - still notify user
                            await event.reply(
                                f"{BOT_META_INFO_PREFIX}❌ Error sending audio to live session: {str(send_error)}"
                            )
                            return

                finally:
                    # Clean up temp file
                    Path(temp_path).unlink(missing_ok=True)

        elif event.video:
            # For now, handle video as audio extraction
            print("Video messages not yet fully supported in live mode")
            await event.reply(
                f"{BOT_META_INFO_PREFIX}📹 Video messages are not yet fully supported in live mode."
            )

    except Exception as e:
        print(f"Error handling live mode message: {e}")
        traceback.print_exc()
        await event.reply(
            f"{BOT_META_INFO_PREFIX}❌ Error processing message: {str(e)}"
        )


async def handle_live_mode_responses(session, original_event):
    """Handle responses from Gemini Live API."""
    try:
        live_session = session._live_connection

        async for response in live_session:
            try:
                # Handle different types of responses
                if hasattr(response, "text") and response.text:
                    # Text response
                    await borg.send_message(session.chat_id, response.text)
                    print(f"Sent text response: {response.text[:50]}...")

                elif hasattr(response, "data") and response.data:
                    # Audio response
                    audio_data = response.data

                    # Convert audio to OGG format for Telegram
                    try:
                        ogg_data = (
                            await gemini_live_util.AudioProcessor.convert_pcm_to_ogg(
                                audio_data, sample_rate=24000
                            )
                        )

                        # Send as voice message
                        await borg.send_file(
                            session.chat_id, ogg_data, attributes=[], voice_note=True
                        )
                        print(f"Sent voice response: {len(ogg_data)} bytes")
                    except Exception as audio_error:
                        print(f"Error processing audio response: {audio_error}")
                        traceback.print_exc()
                        # Fallback: send as text if audio processing fails
                        await borg.send_message(
                            session.chat_id, "[Audio response - processing failed]"
                        )

                # Update session activity
                gemini_live_util.live_session_manager.update_session_activity(
                    session.chat_id
                )

            except Exception as e:
                print(f"Error processing individual response: {e}")
                traceback.print_exc()
                continue

    except Exception as e:
        print(f"Error in response handler: {e}")
        traceback.print_exc()
        # Mark session as disconnected
        session.is_connected = False


async def chat_handler(event):
    """Main handler for all non-command messages in a private chat."""
    user_id = event.sender_id
    chat_id = event.chat_id

    # Intercept if user is in any waiting state first.
    if llm_db.is_awaiting_key(user_id) or user_id in AWAITING_INPUT_FROM_USERS:
        return

    # Intercept for live mode if active
    if gemini_live_util.live_session_manager.is_live_mode_active(chat_id):
        await handle_live_mode_message(event)
        return

    # --- Context and Separator Logic ---
    group_id = event.grouped_id
    prefs = user_manager.get_prefs(user_id)
    is_private = event.is_private

    # Check for chat-specific context mode first
    chat_context_mode = chat_manager.get_context_mode(event.chat_id)
    if chat_context_mode:
        context_mode_to_use = chat_context_mode
    else:
        context_mode_to_use = (
            prefs.context_mode if is_private else prefs.group_context_mode
        )

    # Smart Mode logic
    if context_mode_to_use == "smart":
        current_smart_mode = get_smart_context_mode(user_id)

        # Separator message switches mode
        if event.text and event.text.strip() == CONTEXT_SEPARATOR:
            if not IS_BOT:
                USERBOT_HISTORY_CACHE.pop(event.chat_id, None)

            if current_smart_mode != "until_separator":
                await set_smart_context_mode(user_id, "until_separator")
                await event.reply(
                    f"{BOT_META_INFO_PREFIX}**Smart Mode**: Switched to `Until Separator` context. "
                    "All messages from now on will be included until you reply to a message."
                )
            else:  # Already in this mode
                await event.reply(
                    f"{BOT_META_INFO_PREFIX}**Smart Mode**: Context cleared. Still in `Until Separator` context mode."
                )

            return

        # Reply (not to a forward) switches back to reply_chain
        if event.is_reply and not event.forward:
            if current_smart_mode == "until_separator":
                await set_smart_context_mode(user_id, "reply_chain")

                await event.reply(
                    f"{BOT_META_INFO_PREFIX}**Smart Mode**: Switched to `Reply Chain` context."
                )
            context_mode_to_use = "reply_chain"
        else:  # Not a reply, use the current state
            context_mode_to_use = current_smart_mode

    # Standard separator logic for group chats or explicit "until_separator" mode
    elif context_mode_to_use == "until_separator" and event.text and not group_id:
        text_to_check = event.text.strip()
        if not is_private and BOT_USERNAME and text_to_check.startswith(BOT_USERNAME):
            text_to_check = text_to_check[len(BOT_USERNAME) :].strip()

        if text_to_check == CONTEXT_SEPARATOR:
            if not IS_BOT:
                USERBOT_HISTORY_CACHE.pop(event.chat_id, None)
            reply_text = "Context cleared. The conversation will now start fresh from your next message"
            if not is_private:
                activation_mode = prefs.group_activation_mode
                if activation_mode == "mention_and_reply":
                    reply_text += " mentioning me or replying to me."
                else:
                    reply_text += " mentioning me."
            else:
                reply_text += "."
            await event.reply(f"{BOT_META_INFO_PREFIX}{reply_text}")
            return

    if group_id and group_id in bot_util.PROCESSED_GROUP_IDS:
        return  # Already being processed

    prefs = user_manager.get_prefs(user_id)
    model_in_use = prefs.model

    # Determine which API key is needed
    service_needed = "gemini"
    if model_in_use.startswith("openrouter/"):
        service_needed = "openrouter"
    elif model_in_use.startswith("openai/"):
        service_needed = "openai"  # Assuming you might add this later

    api_key = llm_db.get_api_key(user_id=user_id, service=service_needed)

    if not api_key:
        await llm_db.request_api_key_message(event, service_needed)
        return

    if group_id:
        bot_util.PROCESSED_GROUP_IDS.add(group_id)

    if event.text and re.match(r"^\.s\b", event.text):
        RECENT_WAIT_TIME = 1
        await asyncio.sleep(RECENT_WAIT_TIME)
        context_mode_to_use = "recent"
        event.message.text = event.text[2:].strip()

        response_message = await event.reply(
            f"{BOT_META_INFO_PREFIX}**Recent Context Mode:** I'll use only the recent messages to form the conversation context. I have waited {RECENT_WAIT_TIME} second(s) to receive all your messages.\n\nProcessing ... "
        )

    else:
        response_message = await event.reply(f"{BOT_META_INFO_PREFIX}...")

    temp_dir = Path(f"./temp_llm_chat_{event.id}/")
    try:
        temp_dir.mkdir(exist_ok=True)

        if group_id:
            await asyncio.sleep(0.1)  # Allow album messages to arrive

        messages = await build_conversation_history(
            event, context_mode_to_use, temp_dir
        )

        # --- System Prompt Selection Logic ---
        prompt_info = get_system_prompt_info(event)
        system_prompt_to_use = prompt_info.effective_prompt

        # Create system message with context caching for native Gemini models
        system_message = {"role": "system", "content": system_prompt_to_use}

        # Add context caching for native Gemini models
        if is_native_gemini(prefs.model):
            # Add cache_control to system message
            system_message["cache_control"] = {"type": "ephemeral"}

            # Add cache_control to ALL conversation messages for full context caching
            for message in messages:
                message["cache_control"] = {"type": "ephemeral"}

        messages.insert(0, system_message)

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

        def get_streaming_delay(prefs):
            """Get streaming delay based on current model preferences."""
            if "gemini-2.5-pro" in prefs.model.lower():
                return 1.2
            return 0.8

        # Make the API call
        response_text = ""
        last_edit_time = asyncio.get_event_loop().time()
        edit_interval = get_streaming_delay(prefs)
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

        # TTS Integration Hook
        await _handle_tts_response(event, final_text)

        await _log_conversation(event, prefs, messages, final_text)

    except Exception as e:
        await llm_util.handle_llm_error(
            event=event,
            exception=e,
            response_message=response_message,
            service=service_needed,
            base_error_message="An error occurred. You can send the inputs that caused this error to the bot developer.",
            error_id_p=True,
        )
    finally:
        if group_id:
            bot_util.PROCESSED_GROUP_IDS.discard(group_id)
        if temp_dir.exists():
            rmtree(temp_dir, ignore_errors=True)


# --- Initialization ---
# Schedule the command menu setup to run on the bot's event loop upon loading.
borg.loop.create_task(initialize_llm_chat())
