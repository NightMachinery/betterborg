import asyncio
from pynight.common_icecream import (
    ic,
)  #: used for debugging, DO NOT REMOVE even if currently unused
import traceback
import os
import uuid
import base64
import binascii
import copy
import io
import mimetypes
import re
import json
import socket
import ipaddress
import urllib.parse
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from shutil import rmtree
from itertools import groupby

import httpx
import litellm
from litellm.llms.vertex_ai.gemini.transformation import (
    _gemini_convert_messages_with_history,
)
from google import genai
from google.genai import types
from google.api_core import exceptions as google_exceptions
from telethon import events, errors
from telethon.tl.types import (
    BotCommand,
    BotCommandScopeDefault,
    KeyboardButtonCallback,
    Message,
)
from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Tuple
from dataclasses import dataclass, field

# Import uniborg utilities and storage
from uniborg import util
from uniborg import llm_db
from uniborg import llm_util
from uniborg import tts_util
from uniborg import history_util
from uniborg.history_util import LAST_N_MAX
from uniborg import bot_util
from uniborg.storage import UserStorage
from uniborg.constants import BOT_META_INFO_PREFIX, BOT_META_INFO_LINE

# Import live mode utilities
from uniborg import gemini_live_util

# Redis utilities for smart context state persistence
from uniborg import redis_util
from uniborg import common_util

# --- Constants and Configuration ---
GEMINI_NATIVE_FILE_MODE = os.getenv(
    "GEMINI_NATIVE_FILE_MODE",
    "files",
    # "base64",
)
# DEFAULT_CHECK_GEMINI_CACHED_FILES_P = True
DEFAULT_CHECK_GEMINI_CACHED_FILES_P = False
NOT_SET_HERE_DISPLAY_NAME = "Not Set for This Chat Specifically"

# Use the litellm model naming convention.
# See https://docs.litellm.ai/docs/providers/gemini
DEFAULT_MODEL = "gemini/gemini-2.5-flash"  #: Do NOT change the default model unless explicitly instructed to.
# Alternatives:
# - "gemini/gemini-2.5-pro"
##

# Prefix-to-model mapping for hardcoded model selection
#: @hiddenDep Update `Quick Model Selection Shortcuts` in `/help` command to reflect changes here.
PREFIX_MODEL_MAPPING = {
    ".f": "gemini/gemini-2.5-flash-lite",
    ".ff": "gemini/gemini-2.5-flash",
    ".g": "gemini/gemini-2.5-pro",
    ".c": "openrouter/openai/gpt-5-chat",
    ".d": "deepseek/deepseek-reasoner",
}

# Audio summarization prompt
PROMPT_SUMMARIZE_AUDIO = r"""Please listen to this audio file completely and provide a comprehensive, detailed analysis of its entire content.

To ensure your response is complete and accurate, please follow these guidelines:

* **Complete Coverage**: The summary should cover the audio from the first to the last minute, not just the beginning sections. Exception: Skip advertisements.
* **Logical Structure**: Organize the analysis into logical sections based on speakers (hosts, guests, callers) or main topics discussed in order.
* **Details and Arguments**: Don't just mention general topics. Include main arguments from each person, important examples they gave, and key discussion points with details.
* **Speaker Identification**: Clearly identify who said what or which analysis comes from whom.
* **Tone and Discussion Flow**: Note the evolutionary flow of the conversation and changes in participants' tone throughout the program.

In summary, I want a complete and lengthy response as if I sat down and carefully listened to the entire program myself. Thanks!"""

### * PROMPT_REPLACEMENTS
# Language matching instruction
PROMPT_MATCH_LANGUAGE = r"""**Language**
- Match the language of the user's last message.
- Determine language from the message content only (ignore metadata).
- If in doubt between Arabic and Persian/Farsi, assume Persian/Farsi."""

# Pattern constants
COMMON_PATTERN_SUFFIX = r"(?:\s+|$)"


def _register_prompt_family(
    *,
    pattern_prefix,
    file_base,
    default_version,
    versions=[],
    pattern_suffix=COMMON_PATTERN_SUFFIX,
    content_postfix="",
    versioned_file_base=None,
    regex_flags=re.IGNORECASE,
):
    """
    Helper function to register a family of related prompts with versions.

    Args:
        pattern_prefix: Regex prefix (e.g., r"^\.teach")
        file_base: Base filename for prompts (e.g., "socratic_teacher")
        default_version: Default version used for base pattern (e.g., "1.3")
        versions: List of versions to create patterns for (e.g., ["1", "1.1", "2"])
        pattern_suffix: Regex suffix, defaults to COMMON_PATTERN_SUFFIX
        content_postfix: Text to append to loaded prompt content (e.g., language instructions)
        versioned_file_base: Alternative file base for versioned files
        regex_flags: Regex compilation flags, defaults to re.IGNORECASE
    """
    prompts = {}

    # Base pattern (uses default version)
    base_pattern = f"{pattern_prefix}{pattern_suffix}"
    base_file = f"{file_base}_v{default_version}.md"
    base_content = f"""{llm_util.load_prompt_from_file(base_file)}{content_postfix}"""
    prompts[re.compile(base_pattern, regex_flags)] = base_content

    # Versioned patterns
    for version in versions:
        version_pattern = f"{pattern_prefix}{re.escape(version)}{pattern_suffix}"
        version_file_base = versioned_file_base if versioned_file_base else file_base
        version_file = f"{version_file_base}_v{version}.md"
        version_content = (
            f"""{llm_util.load_prompt_from_file(version_file)}{content_postfix}"""
        )
        prompts[re.compile(version_pattern, regex_flags)] = version_content

    return prompts


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
""",
    re.compile(r"^\.suma$", re.MULTILINE | re.IGNORECASE): PROMPT_SUMMARIZE_AUDIO,
    re.compile(r"^\.sumaauto$", re.MULTILINE | re.IGNORECASE): PROMPT_SUMMARIZE_AUDIO
    + "\n\n"
    + PROMPT_MATCH_LANGUAGE,
    re.compile(
        r"^\.sumafa$", re.MULTILINE | re.IGNORECASE
    ): r"""سلام رفیق، لطفاً به این فایل صوتی به طور کامل گوش کن و یک تحلیل جامع و مفصل از کل محتوای اون ارائه بده.

برای اینکه جواب کامل و دقیق باشه، لطفاً این موارد رو حتماً رعایت کن:

*   پوشش کامل: خلاصه باید از اولین تا آخرین دقیقه فایل صوتی رو پوشش بده، نه فقط بخش‌های ابتدایی. استثنا: تبلیغات رو skip کن.
*   ساختار منطقی: تحلیل رو به بخش‌های منطقی تقسیم کن. مثلاً بر اساس گوینده‌ها (مجری، مهمانان، تماس‌گیرندگان) یا موضوعات اصلی که به ترتیب مطرح شدن.
*   جزئیات و استدلال‌ها: فقط به کلیات اشاره نکن. استدلال‌های اصلی هر شخص، مثال‌های مهمی که زدن، و نکات کلیدی بحث رو با جزئیات بیار.
*   مشخص کردن گوینده: حتماً مشخص کن هر حرف یا تحلیل از طرف چه کسی بوده.
*   لحن و سیر بحث: به سیر تکاملی گفتگو و تغییر لحن شرکت‌کنندگان در طول برنامه هم اشاره کن.

خلاصه اینکه یک جواب کامل و طولانی می‌خوام که انگار خودم نشستم و با دقت به کل برنامه گوش دادم. مرسی!""",
    re.compile(
        r"^\.rev(?:\s+|$)", re.IGNORECASE
    ): f"""{llm_util.load_prompt_from_file("review_v1.md")} """,
    #: Replace LessWrong and Alignment Forum URLs with GreaterWrong to allow better scraping of URLs.
    re.compile(
        r"\bhttps?://(?:www\.)?(lesswrong\.com|alignmentforum\.org)/",
        re.IGNORECASE,
    ): r"https://greaterwrong.com/",
}
##
TEACH_PATTERN_PREFIX = r"^\.teach"
TEACH_FILE_NAME = "socratic_teacher"

TEACH_PROMPTS = _register_prompt_family(
    pattern_prefix=TEACH_PATTERN_PREFIX,
    file_base=TEACH_FILE_NAME,
    default_version="1.3",
    versions=[
        "1",
        "1.1",
        "1.2",
        "1.3",
        "1.4",
        "2",
    ],  #: 1.4 possibly best for material already studied
    content_postfix="\n",
)
PROMPT_REPLACEMENTS.update(TEACH_PROMPTS)
##
REDTEAM_PATTERN_PREFIX = r"^\.red(?:team)?"
REDTEAM_FILE_NAME = "redteam"

REDTEAM_PROMPTS = _register_prompt_family(
    pattern_prefix=REDTEAM_PATTERN_PREFIX,
    file_base=REDTEAM_FILE_NAME,
    default_version="1.1",
    versions=["1", "1.1"],
    content_postfix=f"\n\n{PROMPT_MATCH_LANGUAGE}\n---\n",
)
PROMPT_REPLACEMENTS.update(REDTEAM_PROMPTS)
##
EXTRACTOR_PATTERN_PREFIX = r"^\.ext(?:ract)?"
EXTRACTOR_FILE_NAME = "extractor"

EXTRACTOR_PROMPTS = _register_prompt_family(
    pattern_prefix=EXTRACTOR_PATTERN_PREFIX,
    file_base=EXTRACTOR_FILE_NAME,
    default_version="1",
    versions=[],
    content_postfix="\n",
)
PROMPT_REPLACEMENTS.update(EXTRACTOR_PROMPTS)
##
RESOURCERANGER_PATTERN_PREFIX = r"^\.learn"
RESOURCERANGER_FILE_NAME = "learning_resources"

RESOURCERANGER_PROMPTS = _register_prompt_family(
    pattern_prefix=RESOURCERANGER_PATTERN_PREFIX,
    file_base=RESOURCERANGER_FILE_NAME,
    default_version="1",
    versions=[],
    content_postfix="\n",
)
PROMPT_REPLACEMENTS.update(RESOURCERANGER_PROMPTS)
##
CBT_PATTERN_PREFIX = r"^\.cbt"
CBT_FILE_NAME = "CBT"

CBT_PROMPTS = _register_prompt_family(
    pattern_prefix=CBT_PATTERN_PREFIX,
    file_base=CBT_FILE_NAME,
    default_version="2",
    versions=[
        "1",
        "2",
    ],
    content_postfix="\n",
)
PROMPT_REPLACEMENTS.update(CBT_PROMPTS)
##
ACT_PATTERN_PREFIX = r"^\.act"
ACT_FILE_NAME = "ACT"

ACT_PROMPTS = _register_prompt_family(
    pattern_prefix=ACT_PATTERN_PREFIX,
    file_base=ACT_FILE_NAME,
    default_version="3.2",
    versions=[
        "1",
        "2",
        "2.1",
        "3",
        "3.1",
        "3.2",
    ],
    content_postfix="\n",
)
PROMPT_REPLACEMENTS.update(ACT_PROMPTS)
##
###
# **Strategic emoji use:** 0-2 per message, only when they add clarity or warmth—never decorative.
# **Strategic emoji use:** 2-4 per message for rhythm, readability, and subtle humor. Use as visual anchors and section breaks in dense text. The key is using them as **information architecture**—they should make scanning and parsing faster for people used to reading dense technical content.
BIDI_PROMPT = """
## BiDi text (modern isolates; use Unicode escapes)

Emit **actual control codepoints** in normal output; use `\\u` escapes only in code blocks/examples. **Never** print tokens like "LRI"/"PDI".

**Isolates**
- LTR-in-RTL: `\u2066 ... \u2069`
- RTL-in-LTR: `\u2067 ... \u2069`
- Unknown direction: `\u2068 ... \u2069`

**Nudges**
- LRM (attach to LTR): `\u200e`
- RLM (attach to RTL): `\u200f`

**Shaping aids (Arabic/Persian)**
- ZWNJ: `\u200c`
- ZWJ: `\u200d`

**Rules**
- Always **balance** isolates with `\u2069`.
- Keep control chars **outside** code/math/URLs/markdown; wrap **around** them.
- Use `\u200e`/`\u200f` directly next to punctuation that visually “jumps”.
- Keep numbers/units/IDs **inside** the same isolate as their fragment.

**Mini-examples**
- RTL + English phrase: `… \u2066your English phrase\u2069\u200e …`
- LTR + Arabic term: `… \u2067المصطلح\u2069\u200f …`
- Parentheses in RTL around LTR: `… (\u2066text\u2069)\u200e …`
- LTR-in-RTL + period: `… \u2066ABC\u2069\u200e.`
- RTL-in-LTR + period: `… \u2067عربي\u2069\u200f.`
- Unknown dir fragment: `\u2068MixedStart\u2069`

**Final check**
- Parentheses face content; punctuation clings to intended fragment; code/links remain untouched.
"""

DEFAULT_SYSTEM_PROMPT_V3 = """
You are a technically precise assistant with the personality of a warm, kind, to-the-point, challenging, smart, highly agentic friend. Your audience: advanced STEM postgraduate researchers.

## Core Approach: Truth + Warmth + Brevity + Momentum
**Lead with genuine views, even when they contradict the user.** Be direct, specific, and kind. Challenge shaky assumptions, false dichotomies, and motivated reasoning with 1–3 crisp counterpoints. Only flatter or agree when you genuinely believe it.
**Be warmly direct.** Show care, curiosity, and encouragement. Use a supportive, non-judgmental tone while staying frank and evidence-based.
**Default to concise.** Start with conclusions/recommendations in the first line. Use 3–6 sentences unless more detail is requested. Cut filler and hedging—prefer information-dense bullets and tight structure.
**Be proactive.** Anticipate needs, suggest next steps, surface trade-offs. Remember what matters to them and offer constructive challenges when helpful.

## Communication Style
**Mobile-optimized:**
- One idea per paragraph, one action per bullet
- Short paragraphs, clear structure, minimal qualifiers
- Telegram markdown only: `**bold**`, `__italic__`, `` `code` ``, `[links](url)`, ```code blocks```
**Liberal emoji use:** Use emojis for personality, subtle humor (especially dark humor), and visual section breaks to improve readability in dense text.
**Human tone:** Sound like a supportive peer: encouraging, clear, and unpretentious.
**Language**
- Match the language of the user's last message.
- Determine language from the message content only (ignore metadata).
- If in doubt between Arabic and Persian/Farsi, assume Persian/Farsi.

## Truth & Challenge Guidelines
- **State confidence levels** and flag unknowns—avoid hedging once decided
- **Correct misconceptions succinctly** with evidence
- **Red-team high stakes:** Name top risks, failure modes, alternative hypotheses and quick ways to test them
- **Bias-bust:** When relevant, ask one brief debiasing question or propose a simple experiment

## Active Conversation Endings
End most replies with 1–2 of these:
- Clarifying question (only if it meaningfully changes the answer)
- Concrete next step or quick checklist
- Brief check-in on related progress/blockers
- Offer to go deeper on specific subtopic
- Respectful challenge when plans seem off
- "Stretch" suggestion outside comfort zone
- **Quick brainstorm:** 1-3 concise ideas you just thought of that might help

**Remember:** Maximum signal, zero sycophancy, friendly evidence-based pushback delivered with precision, warmth, and momentum.
"""

DEFAULT_SYSTEM_PROMPT_V3_0 = """
You are a technically precise assistant with the personality of a smart, highly agentic friend. Your audience: advanced STEM postgraduate researchers.

## Core Approach: Truth + Brevity + Momentum

**Lead with genuine views—even when they contradict the user.** Be direct, specific, and kind. Challenge shaky assumptions, false dichotomies, and motivated reasoning with 1–3 crisp counterpoints. Never flatter or agree just to please.

**Default to concise.** Start with conclusions/recommendations in the first line. Use 3–6 sentences unless more detail is requested. Cut filler and hedging—prefer information-dense bullets and tight structure.

**Be proactive.** Anticipate needs, suggest next steps, surface trade-offs. Remember what matters to them and offer constructive challenges when helpful.

## Communication Style

**Mobile-optimized:**
- One idea per paragraph, one action per bullet
- Short paragraphs, clear structure, minimal qualifiers
- Telegram markdown only: `**bold**`, `__italic__`, `` `code` ``, `[links](url)`, ```code blocks```

**Liberal emoji use:** Use emojis for personality, subtle humor, and visual section breaks to improve readability in dense text.

**Language**
- Match the language of the user's last message.
- Determine language from the message content only (ignore metadata).
- If in doubt between Arabic and Persian/Farsi, assume Persian/Farsi.

## Truth & Challenge Guidelines

- **State confidence levels** and flag unknowns—avoid hedging once decided
- **Correct misconceptions succinctly** with evidence
- **Red-team high stakes:** Name top risks, failure modes, alternative hypotheses and quick ways to test them
- **Bias-bust:** When relevant, ask one brief debiasing question or propose a simple experiment

## Active Conversation Endings

End most replies with 1–2 of these:
- Clarifying question (only if it meaningfully changes the answer)
- Concrete next step or quick checklist
- Brief check-in on related progress/blockers
- Offer to go deeper on specific subtopic
- Respectful challenge when plans seem off
- "stretch" suggestion outside comfort zone

**Remember:** Maximum signal, zero sycophancy, friendly evidence-based pushback delivered with precision, warmth, and momentum.
"""

DEFAULT_SYSTEM_PROMPT_CONCISE = """
You are a helpful, technically precise assistant with the personality of a smart, highly agentic friend. Your primary audience is advanced STEM postgraduate researchers.

**Brevity First**
- Be concise and to the point. Prefer short, information-dense sentences.
- Cut filler, hedging, and repetition. Lead with the answer; details follow only if needed.
- Use bullets and tight structure for scanability. Default length: 3–6 sentences unless the user asks for more.

**Core Personality**
- **Proactive & Agentic:** Don’t just answer—anticipate needs, suggest next steps, and surface trade-offs.
- **Empathetic Engagement:** Check in on their day and tailor guidance to their context.
- **Smart Friend:** Remember what matters to them and challenge constructively when helpful.

**Mobile Chat Style**
- **Direct:** Start with the conclusion or recommendation in the first line.
- **Readable:** Short paragraphs, clear bullets, minimal qualifiers.
- **Focus:** One idea per paragraph; one action per bullet.
- **Expand on demand:** Offer deeper dives, proofs, or sources only when requested.

**Active Conversation (end most replies with 1–2 of the following)**
- A clarifying question *only if it meaningfully changes the answer*
- A concrete next step or quick checklist
- A brief check-in on related progress or blockers
- An offer to go deeper on a specific subtopic

**Language**
- Match the language of the user’s last message.
- Determine language from the message content only (ignore metadata).
- If in doubt between Arabic and Persian/Farsi, assume Persian/Farsi.

**Formatting**
- Telegram markdown only: `**bold**`, `__italic__`, `` `code` ``, `[links](https://example.com)`, and ```pre``` blocks.

Remember: precision, warmth, and momentum—delivered with maximum signal and minimum words.
"""

DEFAULT_SYSTEM_PROMPT_V2 = """
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

DEFAULT_SYSTEM_PROMPT = DEFAULT_SYSTEM_PROMPT_V3 + BIDI_PROMPT


# --- Event Proxy ---
class ProxyEvent:
    """A lightweight proxy around a Telethon event.

    Forwards all attributes/methods to the original event unless overridden.
    We override `message`, `text`, and `id` when needed while keeping the
    original `client`, `chat_id`, `sender_id`, etc.
    """

    def __init__(
        self,
        base_event,
        *,
        message=None,
        text: Optional[str] = None,
        id: Optional[int] = None,
    ):
        self._base_event = base_event
        self._override_message = message
        self._override_text = text
        self._override_id = id

    # Explicit overrides / pass-throughs commonly used in this codebase
    @property
    def client(self):  # type: ignore
        return self._base_event.client

    @property
    def chat_id(self):  # type: ignore
        return getattr(self._base_event, "chat_id", None)

    @property
    def sender_id(self):  # type: ignore
        return getattr(self._base_event, "sender_id", None)

    @property
    def is_private(self):  # type: ignore
        return getattr(self._base_event, "is_private", None)

    @property
    def id(self):  # type: ignore
        if self._override_id is not None:
            return self._override_id
        return getattr(self._base_event, "id", None)

    @property
    def message(self):  # type: ignore
        return (
            self._override_message
            if self._override_message is not None
            else getattr(self._base_event, "message", None)
        )

    @message.setter
    def message(self, value):  # type: ignore
        self._override_message = value

    @property
    def text(self):  # type: ignore
        if self._override_text is not None:
            return self._override_text
        return getattr(self._base_event, "text", None)

    @text.setter
    def text(self, value):  # type: ignore
        self._override_text = value

    # Convenience methods matching Telethon event API
    async def reply(self, *args, **kwargs):  # type: ignore
        file = kwargs.pop("file", None)
        if file is not None:
            return await self.client.send_file(
                self.chat_id, file, reply_to=self.id, **kwargs
            )
        return await self.client.send_message(
            self.chat_id, *args, reply_to=self.id, **kwargs
        )

    async def respond(self, *args, **kwargs):  # type: ignore
        file = kwargs.pop("file", None)
        if file is not None:
            return await self.client.send_file(self.chat_id, file, **kwargs)
        return await self.client.send_message(self.chat_id, *args, **kwargs)

    def __getattr__(self, name):
        # Fallback to original event for everything else (download_media, edit, answer, etc.)
        return getattr(self._base_event, name)


# Directory for logs, mirroring the STT plugin's structure
LOG_DIR = Path(os.path.expanduser("~/.borg/llm_chat/log/"))
LOG_DIR.mkdir(parents=True, exist_ok=True)


# --- New Constants for Features ---
GEMINI_IMAGE_GENERATION_MODELS = {
    "gemini/gemini-2.5-flash-image-preview",
    "gemini/gemini-2.0-flash-preview-image-generation",
    "gemini/gemini-2.0-flash-exp-image-generation",
}

IMAGE_GENERATION_MODELS = GEMINI_IMAGE_GENERATION_MODELS

# Configuration for how to handle system messages with Gemini image generation models
# "SKIP": Skip the system message for native gemini image models
# "PREPEND": Prepend the system message to the first prompt and add "\n\n---\n"
GEMINI_IMAGE_GEN_SYSTEM_MODE = os.getenv("GEMINI_IMAGE_GEN_SYSTEM_MODE", "SKIP")

# Audio URL Magic: automatically process URLs pointing to audio files
AUDIO_URL_MAGIC_P = os.getenv("AUDIO_URL_MAGIC_P", "True").lower() in (
    "true",
    "1",
    "yes",
)

# Security constants for image processing
MAX_IMAGE_SIZE = 50 * 1024 * 1024  # 50MB limit
ALLOWED_IMAGE_FORMATS = {"png", "jpg", "jpeg", "gif", "webp"}

# Pre-compiled regex for better performance and security
IMAGE_PATTERN = re.compile(r"data:image/([^;]{1,20});base64,([A-Za-z0-9+/=]+)")

# Magic string to force message role to user
MAGIC_STR_AS_USER = "MAGIC_AS_USER"
MAGIC_PATTERN_AS_USER = re.compile(rf"\b{MAGIC_STR_AS_USER}\b")

MODEL_CHOICES = {
    ## Gemini
    "gemini/gemini-2.5-flash": "Gemini 2.5 Flash",
    "gemini/gemini-2.5-pro": "Gemini 2.5 Pro",
    "openrouter/google/gemini-2.5-pro": "Gemini 2.5 Pro (OpenRouter)",
    "gemini/gemini-2.0-flash-preview-image-generation": "Gemini 2.0 Flash Image",
    "gemini/gemini-2.5-flash-image-preview": "Gemini 2.5 Flash Image",
    "gemini/gemini-2.5-flash-lite": "Gemini 2.5 Flash Lite",
    ## Anthropic Claude
    "openrouter/anthropic/claude-sonnet-4": "Claude Sonnet 4 (OpenRouter)",
    "openrouter/anthropic/claude-opus-4.1": "Claude Opus 4.1 (OpenRouter)",
    ## Grok
    "openrouter/x-ai/grok-4": "Grok 4 (OpenRouter)",
    ## OpenAI
    # openai/gpt-5-chat
    "openrouter/openai/gpt-5-chat": "GPT-5 Chat (OpenRouter)",
    "openrouter/openai/chatgpt-4o-latest": "ChatGPT 4o (OpenRouter)",
    # openai/chatgpt-4o-latest: OpenAI ChatGPT 4o is continually updated by OpenAI to point to the current version of GPT-4o used by ChatGPT. It therefore differs slightly from the API version of GPT-4o in that it has additional RLHF. It is intended for research and evaluation.  OpenAI notes that this model is not suited for production use-cases as it may be removed or redirected to another model in the future.
    # "openrouter/openai/gpt-4o-mini": "GPT-4o Mini (OpenRouter)",
    # "openrouter/openai/gpt-4.1-mini": "GPT-4.1 Mini (OpenRouter)",
    # "openrouter/openai/gpt-4.1": "GPT-4.1 (OpenRouter)",
    # "openrouter/openai/o4-mini-high": "o4-mini-high (OpenRouter)",
    ## Kimi
    # moonshotai/kimi-k2:free
    "openrouter/moonshotai/kimi-k2:free": "🎁 Kimi K2 (Free, OpenRouter)",
    ## Qwen
    # qwen/qwen3-coder:free
    "openrouter/qwen/qwen3-coder:free": "🎁 Qwen3 Coder (Free, OpenRouter)",
    ## Z.AI
    # z-ai/glm-4.5-air:free
    "openrouter/z-ai/glm-4.5-air:free": "🎁 GLM-4.5 Air (Free, OpenRouter)",
    ## Various
    # "openrouter/cognitivecomputations/dolphin-mistral-24b-venice-edition:free": "🎁 Venice Uncensored 24B (Free, OpenRouter)",
    #: model name is too long for Telegram API's `data` field in callback buttons
    ## Cloaked Models
    ## DeepSeek
    "deepseek/deepseek-chat": "DeepSeek Chat",
    "deepseek/deepseek-reasoner": "DeepSeek Reasoner",
    ## Mistral
    "mistral/mistral-medium-latest": "Mistral Medium (Latest)",
    "mistral/magistral-medium-latest": "Magistral Medium (Latest)",
    # "mistral/mistral-large-latest": "Mistral Large (Latest)",
    # "mistral/mistral-small-latest": "Mistral Small (Latest)",
    "mistral/pixtral-large-latest": "Pixtral Large (Latest)",
    ##
}

# Chat model options including "Not Set" option for removing chat-specific model
CHAT_MODEL_OPTIONS = {"": "Not Set (Use Personal Default)"}
CHAT_MODEL_OPTIONS.update(MODEL_CHOICES)

# Text input patterns for clearing/resetting values
CANCEL_KEYWORDS = ["cancel"]
RESET_KEYWORDS = ["not set", "none", "clear", "remove", "reset"]

LAST_N_MESSAGES_LIMIT = 50
HISTORY_MESSAGE_LIMIT = 1000
LOG_COUNT_LIMIT = 3
AVAILABLE_TOOLS = ["googleSearch", "urlContext", "codeExecution"]
DEFAULT_ENABLED_TOOLS = ["googleSearch", "urlContext"]

# Global override for chat context modes (chat_id -> context_mode_string or None)
override_chat_context_mode: Dict[int, Optional[str]] = {}
# Controls when to show warnings about unsupported media, etc. to the user.
# "always": Show in all chats. "private_only": Show only in private chats. "never": Never show.
WARN_UNSUPPORTED_TO_USER_P = os.getenv("WARN_UNSUPPORTED_TO_USER_P", "private_only")
WARN_UNAVAILABLE_TOOLS_P = False
WARN_UNAVAILABLE_THINKING_P = False
REASONING_LEVELS = ["disable", "low", "medium", "high"]
CONTEXT_SEPARATOR = "---"
CONTEXT_MODE_NAMES = {
    "reply_chain": "Reply Chain",
    "until_separator": f"Until Separator (`{CONTEXT_SEPARATOR}`)",
    "last_N": "Last N Messages",
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
MAX_RETRIES = 2

# Maximum number of retries for "no response" scenarios
NO_RESPONSE_RETRIES_MAX = int(os.getenv("NO_RESPONSE_RETRIES_MAX", "30"))


# --- Single Source of Truth for Bot Commands ---
BOT_COMMANDS = [
    {"command": "start", "description": "Onboard and set API key"},
    {"command": "help", "description": "Show detailed help and instructions"},
    {"command": "status", "description": "Show your current settings"},
    {"command": "stop", "description": "Stop all in-progress chat requests"},
    {
        "command": "log",
        "description": f"Get your last {LOG_COUNT_LIMIT} conversation logs",
    },
    {"command": "setgeminikey", "description": "Set or update your Gemini API key"},
    {
        "command": "setopenrouterkey",
        "description": "Set or update your OpenRouter API key",
    },
    {"command": "setdeepseekkey", "description": "Set or update your DeepSeek API key"},
    {
        "command": "setmistralkey",
        "description": "Set or update your Mistral AI API key",
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
        "command": "setlastn",
        "description": "Set your default 'Last N' message limit",
    },
    {
        "command": "getlastn",
        "description": "View your default 'Last N' message limit",
    },
    {
        "command": "setlastnhere",
        "description": "Set 'Last N' message limit for this chat",
    },
    {
        "command": "getlastnhere",
        "description": "View 'Last N' message limit for this chat",
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
    {
        "command": "setmodelhere",
        "description": "Set the model for the current chat only",
    },
    {
        "command": "getmodelhere",
        "description": "View the effective model for the current chat",
    },
]
# Create a set of command strings (e.g., {"/start", "/help"}) for efficient lookup
KNOWN_COMMAND_SET = {f"/{cmd['command']}".lower() for cmd in BOT_COMMANDS}


SAFETY_SETTINGS = [
    {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
]


# --- State Management & Dataclasses ---
BOT_USERNAME = None
BOT_ID = None
AWAITING_INPUT_FROM_USERS = {}
IS_BOT = None
USERBOT_HISTORY_CACHE = {}
SMART_CONTEXT_STATE = {}

# Track active LLM tasks by user_id for cancellation support
ACTIVE_LLM_TASKS = {}


@dataclass
class ConversationHistoryResult:
    """Dataclass to hold the results of building the conversation history."""

    history: List[Dict]
    warnings: List[str]


@dataclass
class ProcessMediaResult:
    """Dataclass for the return type of _process_media."""

    media_part: Optional[Dict]
    warnings: List[str]


@dataclass
class ProcessContentResult:
    """Dataclass for the return type of _process_message_content."""

    text_parts: List[str]
    media_parts: List[Dict]
    warnings: List[str]


@dataclass
class MediaCapabilityCheckResult:
    """Dataclass for the return type of _check_media_capability."""

    has_warning: bool = False
    warnings: List[str] = field(default_factory=list)


@dataclass
class PrefixProcessResult:
    """Dataclass for the return type of _detect_and_process_message_prefix."""

    model: Optional[str] = None
    processed_text: str = ""


@dataclass
class LLMResponse:
    """Dataclass for the return type of _call_llm_with_retry."""

    text: str
    finish_reason: Optional[str] = None
    has_image: bool = False


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


def add_active_llm_task(user_id: int, task: asyncio.Task):
    """Add an active LLM task for tracking and cancellation."""
    if user_id not in ACTIVE_LLM_TASKS:
        ACTIVE_LLM_TASKS[user_id] = set()
    ACTIVE_LLM_TASKS[user_id].add(task)


def remove_active_llm_task(user_id: int, task: asyncio.Task):
    """Remove a completed/cancelled LLM task from tracking."""
    if user_id in ACTIVE_LLM_TASKS:
        ACTIVE_LLM_TASKS[user_id].discard(task)
        if not ACTIVE_LLM_TASKS[user_id]:
            del ACTIVE_LLM_TASKS[user_id]


def cleanup_completed_tasks(user_id: int):
    """Remove completed tasks from tracking."""
    if user_id in ACTIVE_LLM_TASKS:
        ACTIVE_LLM_TASKS[user_id] = {
            task for task in ACTIVE_LLM_TASKS[user_id] if not task.done()
        }
        if not ACTIVE_LLM_TASKS[user_id]:
            del ACTIVE_LLM_TASKS[user_id]


def cancel_all_llm_tasks(user_id: int) -> int:
    """Cancel all active LLM tasks for a user. Returns count of cancelled tasks."""
    if user_id not in ACTIVE_LLM_TASKS:
        return 0

    active_tasks = ACTIVE_LLM_TASKS[user_id]
    cancelled_count = 0

    for task in active_tasks.copy():
        if not task.done():
            task.cancel()
            cancelled_count += 1

    # Clear the user's active tasks
    ACTIVE_LLM_TASKS[user_id] = set()

    return cancelled_count


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
    last_n_messages_limit: Optional[int] = Field(default=None)


class ChatPrefs(BaseModel):
    """Pydantic model for chat-specific settings."""

    system_prompt: Optional[str] = Field(default=None)
    context_mode: Optional[str] = Field(default=None)
    model: Optional[str] = Field(default=None)
    tts_model: str = Field(default="Disabled")
    tts_voice_override: Optional[str] = Field(default=None)
    live_mode_enabled: bool = Field(default=False)
    last_n_messages_limit: Optional[int] = Field(default=None)


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

    def get_last_n_messages_limit(self, user_id: int) -> Optional[int]:
        return self.get_prefs(user_id).last_n_messages_limit

    def set_last_n_messages_limit(self, user_id: int, limit: Optional[int]):
        prefs = self.get_prefs(user_id)
        prefs.last_n_messages_limit = limit
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

    def get_model(self, chat_id: int) -> Optional[str]:
        return self.get_prefs(chat_id).model

    def set_model(self, chat_id: int, model: Optional[str]):
        prefs = self.get_prefs(chat_id)
        prefs.model = model
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

    def get_last_n_messages_limit(self, chat_id: int) -> Optional[int]:
        return self.get_prefs(chat_id).last_n_messages_limit

    def set_last_n_messages_limit(self, chat_id: int, limit: Optional[int]):
        prefs = self.get_prefs(chat_id)
        prefs.last_n_messages_limit = limit
        self._save_prefs(chat_id, prefs)


user_manager = UserManager()
chat_manager = ChatManager()


# --- Core Logic & Helpers ---


def is_native_gemini_files_mode(model: str) -> bool:
    """Check if we should use the native Gemini Files API for attachments."""
    return is_native_gemini(model) and GEMINI_NATIVE_FILE_MODE == "files"


def _get_effective_last_n_limit(chat_id: int, user_id: int) -> int:
    """Gets the effective 'Last N' limit, prioritizing chat, then user, then global default."""
    chat_limit = chat_manager.get_last_n_messages_limit(chat_id)
    if chat_limit is not None:
        return chat_limit

    user_limit = user_manager.get_last_n_messages_limit(user_id)
    if user_limit is not None:
        return user_limit

    return LAST_N_MESSAGES_LIMIT


def _build_context_mode_menu_options(chat_id: int, user_id: int) -> Dict[str, str]:
    """Build context mode menu options with appropriate limit displays."""
    effective_last_n_limit = _get_effective_last_n_limit(chat_id, user_id)
    options = CONTEXT_MODE_NAMES.copy()
    if "last_N" in options:
        options["last_N"] = (
            f"{CONTEXT_MODE_NAMES['last_N']} (Limit: {effective_last_n_limit})"
        )
    return options


def _detect_and_process_message_prefix(text: str) -> PrefixProcessResult:
    """
    Detects if a message starts with a model prefix and returns the model and processed text.

    Args:
        text: The original message text

    Returns:
        PrefixProcessResult: Contains model name (if detected) and processed text with prefix removed
    """
    if not text:
        return PrefixProcessResult(processed_text=text or "")

    #: [[id:5afef6f3-51e1-4536-a85f-f1cf3bbed5ee][@hack I need to think of a better magic command language.]]
    other_prefixes = {
        ".s ": False,
    }
    for p in other_prefixes:
        if text.startswith(p):
            other_prefixes[p] = True
            text = text[len(p) :]

    def add_back_prefixes(text: str) -> str:
        for p, v in other_prefixes.items():
            if v:
                text = p + text
        return text

    processed_text = text.lstrip()
    for prefix, model in PREFIX_MODEL_MAPPING.items():
        if processed_text.startswith(prefix):
            # Check if the prefix is followed by a space or the end of the message
            if len(text) == len(prefix) or text[len(prefix)].isspace():
                processed_text = text[len(prefix) :].lstrip()
                processed_text = add_back_prefixes(processed_text)
                return PrefixProcessResult(model=model, processed_text=processed_text)

    processed_text = add_back_prefixes(processed_text)
    return PrefixProcessResult(processed_text=processed_text)


def _validate_url_security(url: str) -> Optional[str]:
    """
    Validates URL for security concerns (SSRF protection).

    Args:
        url: The URL to validate

    Returns:
        The URL if safe, None if dangerous
    """
    try:
        parsed = urllib.parse.urlparse(url)
        hostname = parsed.hostname

        if not hostname:
            logger.warning(f"URL rejected - no hostname: {url}")
            return None

        # Resolve hostname to IP for security checks
        ip = socket.gethostbyname(hostname)
        ip_obj = ipaddress.ip_address(ip)

        # Block private/local/multicast networks (SSRF protection)
        if ip_obj.is_private:
            logger.warning(f"URL rejected - private network {ip} for {hostname}: {url}")
            return None
        elif ip_obj.is_loopback:
            logger.warning(
                f"URL rejected - loopback address {ip} for {hostname}: {url}"
            )
            return None
        elif ip_obj.is_link_local:
            logger.warning(
                f"URL rejected - link-local address {ip} for {hostname}: {url}"
            )
            return None
        elif ip_obj.is_multicast:
            logger.warning(
                f"URL rejected - multicast address {ip} for {hostname}: {url}"
            )
            return None

        # Block dangerous ports commonly used for internal services
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        dangerous_ports = {
            22,
            23,
            25,
            53,
            110,
            143,
            993,
            995,
            1433,
            3306,
            5432,
            6379,
        }
        if port in dangerous_ports:
            logger.warning(f"URL rejected - dangerous port {port}: {url}")
            return None

        logger.info(f"URL security validation passed for {hostname} ({ip}): {url}")
        return url

    except socket.gaierror as e:
        logger.warning(
            f"URL rejected - DNS resolution failed for {parsed.hostname if 'parsed' in locals() else 'unknown'}: {e}"
        )
        return None
    except (ValueError, ipaddress.AddressValueError) as e:
        logger.warning(f"URL rejected - invalid IP address: {e}")
        return None
    except Exception as e:
        logger.warning(f"URL rejected - validation error: {e}")
        return None


def _is_url_only_message(text: str) -> Optional[str]:
    """
    Checks if a message contains only a single URL.

    Args:
        text: The message text to check

    Returns:
        The URL if message contains only a URL, None otherwise
    """
    if not text:
        return None

    text = text.strip()
    if not text:
        return None

    # Fast early rejection for non-URLs (most common case)
    # Check length and basic prefix before expensive regex/DNS
    if (
        len(text) < 7
        or len(text) > 2048
        or not (text.startswith("http://") or text.startswith("https://"))
    ):
        return None

    # Fast check: no tabs or newlines (URL-only requirement)
    # Allow spaces in URLs as they can be legitimately part of file paths
    if "\t" in text or "\n" in text:
        return None

    # More secure URL pattern - allow spaces in the path portion
    url_pattern = re.compile(
        r"^https?://(?:[a-zA-Z0-9-]+\.)*[a-zA-Z0-9-]+(?:\.[a-zA-Z]{2,})(?::\d{1,5})?(?:/[^\t\n]*)?$"
    )

    match = url_pattern.match(text)
    if not match:
        return None

    # Security validation only if we have a URL match
    return _validate_url_security(text)


async def _download_audio_from_url(
    url: str, *, temp_dir: Path
) -> Tuple[Optional[Path], Optional[str]]:
    """
    Downloads audio from URL and returns the file path.

    Args:
        url: The URL to download
        temp_dir: Temporary directory for storing downloaded file

    Returns:
        (Path, None) on success; (None, error_message) on failure
    """
    try:
        # Follow redirects to reach the actual media URL
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
            # Stream to file to avoid loading large content in memory
            async with client.stream("GET", url) as response:
                status = response.status_code
                final_url = str(response.url)
                content_type = (
                    response.headers.get("content-type", "").split(";")[0].lower()
                )

                if status not in (200, 206):
                    return None, (
                        f"HTTP {status} while downloading. Final URL: {final_url}. "
                        f"Content-Type: {content_type or 'unknown'}."
                    )

                # Determine file extension from content-type or URL
                extension = mimetypes.guess_extension(content_type) or ".audio"
                if extension == ".audio":
                    parsed_url = final_url.lower()
                    for ext in [".mp3", ".wav", ".ogg", ".m4a", ".flac", ".aac"]:
                        if ext in parsed_url:
                            extension = ext
                            break

                # Save audio file
                audio_file = temp_dir / f"audio_download{extension}"
                with open(audio_file, "wb") as f:
                    async for chunk in response.aiter_bytes(64 * 1024):
                        if chunk:
                            f.write(chunk)

        logger.info(f"Downloaded audio file: {audio_file}")
        return audio_file, None

    except httpx.HTTPError as e:
        traceback.print_exc()
        err = f"Network error: {e}"
        logger.error(f"Failed to download audio from {url}: {err}")
        return None, err
    except Exception as e:
        traceback.print_exc()
        err = f"Unexpected error: {e}"
        logger.error(f"Failed to download audio from {url}: {err}")
        return None, err


async def _process_audio_url_magic(event, url: str) -> bool:
    """
    Handles the complete audio URL magic flow: download, upload, and process.

    Args:
        event: The original user event containing the URL
        url: The URL to download

    Returns:
        True if processing was initiated, False if failed
    """
    try:
        # Create temp directory for download
        import tempfile

        temp_dir = Path(tempfile.gettempdir()) / f"temp_llm_chat_{event.id}"
        temp_dir.mkdir(exist_ok=True)

        # Download the audio file
        audio_file_path, err = await _download_audio_from_url(url, temp_dir=temp_dir)
        if not audio_file_path:
            details = f"\n\nError: {err}" if err else ""
            await event.reply(
                f"{BOT_META_INFO_PREFIX}❌ Failed to download audio from URL{details}"
            )
            return False

        # Send the audio file to the chat as a normal file upload
        print(f"Sending downloaded audio file: {audio_file_path}")
        new_text = f"{MAGIC_STR_AS_USER} .sumaauto"
        async with borg.action(event.chat, "audio") as action:
            audio_message = await event.client.send_file(
                event.chat_id,
                file=str(audio_file_path),
                caption=new_text,
                reply_to=event.id,
            )
        # audio_message._role = "user"
        #: @deprecated This role will only persist for the current conversation turn.

        # audio_message.text = new_text
        #: @deprecated This text will only persist for the current conversation turn.

        # print(f"Audio message sent: {audio_message}")

        # Clean up the temporary audio file
        try:
            Path(audio_file_path).unlink()
        except OSError as e:
            logger.warning(
                f"Failed to delete temporary audio file {audio_file_path}: {e}"
            )

        # Build a proxy event that points to the uploaded audio message
        proxy = ProxyEvent(
            event,
            message=audio_message,
            text=new_text,
            id=getattr(audio_message, "id", None),
        )
        try:
            await chat_handler(proxy)
        finally:
            # We don't want this URL processed normally at this stage, even if an error happens.
            return True

    except Exception as e:
        traceback.print_exc()
        logger.error(f"Audio URL magic failed: {e}")
        await event.reply(f"{BOT_META_INFO_PREFIX}❌ Error processing audio URL.")
        return False


def _get_effective_model_and_service(
    chat_id: int, user_id: int, *, prefix_model: str = None
) -> tuple[str, str]:
    """
    Gets the effective model and the corresponding service ('gemini' or 'openrouter').
    Prioritizes prefix model > chat-specific settings > user-default settings.
    """
    prefs = user_manager.get_prefs(user_id)
    chat_model = chat_manager.get_model(chat_id)

    # Priority: prefix_model > chat_model > user default model
    model_in_use = prefix_model or chat_model or prefs.model

    service_needed = llm_util.get_service_from_model(model_in_use)

    return model_in_use, service_needed


def _create_retry_logger():
    """Create a logger function that saves model call details to a temp file."""

    def retry_logger(model_call_dict):
        # Create a temporary file to save the model call details
        temp_fd = None
        temp_path = None
        try:
            temp_fd, temp_path = tempfile.mkstemp(suffix=".json", prefix="llm_retry_")
            with os.fdopen(temp_fd, "w") as f:
                temp_fd = None  # Successfully handed to fdopen, don't close manually
                json.dump(model_call_dict, f, indent=2, default=str)
            print(f"LLM retry call details saved to: {temp_path}")
        except Exception as e:
            print(f"Failed to save retry call details: {e}")
            # Close the file descriptor if it wasn't successfully handed to fdopen
            if temp_fd is not None:
                try:
                    os.close(temp_fd)
                except:
                    pass
            # Clean up the temp file if it was created but writing failed
            if temp_path and os.path.exists(temp_path):
                try:
                    os.unlink(temp_path)
                except:
                    pass

    return retry_logger


async def _create_server_error_message(event, exception) -> str:
    """Create a server error message with admin details if applicable."""
    base_message = "The AI model's server is currently unavailable. This is likely an upstream issue. Please try again later."
    if await util.isAdmin(event):
        base_message += f"\n\n**Error:** {str(exception)}"
    return base_message


async def _call_llm_with_retry(
    event,
    response_message,
    api_kwargs: dict,
    edit_interval: float = None,
    *,
    max_retries: int = MAX_RETRIES,
    max_retriable_text_length: int = 300,
) -> LLMResponse:
    """Call LLM with retry logic for both streaming and non-streaming responses.

    Returns:
        LLMResponse: Response containing text and finish_reason
    """
    response_text = ""  # Initialize at function scope for error handling

    try:
        response = await litellm.acompletion(**api_kwargs)

        # Check if streaming mode based on edit_interval parameter
        if edit_interval is not None:
            # Streaming mode
            last_edit_time = asyncio.get_event_loop().time()
            streaming_start_time = last_edit_time

            async for chunk in response:
                delta = chunk.choices[0].delta.content
                if delta:
                    response_text += delta
                    current_time = asyncio.get_event_loop().time()

                    # Dynamic streaming delay: increase delay for long outputs
                    current_edit_interval = edit_interval
                    cursor = "▌"  # Normal typing cursor

                    if (current_time - streaming_start_time) > 120:
                        current_edit_interval = 60
                        cursor = "▌💤💤"
                        #: Doubly slow mode cursor to indicate increased delay

                    elif (
                        current_time - streaming_start_time
                    ) > 30:  # 30 seconds elapsed
                        current_edit_interval = 15  # Increase delay to 15 seconds
                        # cursor = "▌(💤)"
                        # cursor = "▌⁞💤"
                        cursor = "▌💤"
                        # Slow mode cursor to indicate increased delay
                        ##

                    if (current_time - last_edit_time) > current_edit_interval:
                        try:
                            # Add a cursor to indicate the bot is still "typing"
                            await util.edit_message(
                                response_message,
                                f"{response_text}{cursor}",
                                parse_mode="md",
                            )
                            last_edit_time = current_time
                        except errors.rpcerrorlist.MessageNotModifiedError:
                            # This error is expected if the content hasn't changed
                            pass
                        except Exception as e:
                            # Log other edit errors but don't stop the stream
                            print(f"Error during message edit: {e}")

            # Get finish reason from the last chunk
            finish_reason = chunk.choices[0].finish_reason if chunk.choices else None
            return LLMResponse(
                text=response_text, finish_reason=finish_reason, has_image=False
            )
        else:
            # Non-streaming mode
            content = response.choices[0].message.content or ""
            finish_reason = (
                response.choices[0].finish_reason if response.choices else None
            )
            return LLMResponse(
                text=content, finish_reason=finish_reason, has_image=False
            )

    except asyncio.CancelledError:
        raise

    except (
        litellm.exceptions.RateLimitError,
        httpx.HTTPStatusError,
        litellm.exceptions.MidStreamFallbackError,
        litellm.exceptions.InternalServerError,
    ) as e:
        # Handle RateLimitError separately and do not retry
        if (
            isinstance(e, litellm.exceptions.RateLimitError)
            or "RateLimitError" in str(e)
            or "quota" in str(e).lower()
        ):
            raise llm_util.RateLimitException(
                "API rate limit exceeded.", original_exception=e
            )

        # Handle BadRequestError (400-level) separately and do not retry
        if "BadRequestError" in str(e):
            # A 400 error indicates a problem with the request itself.
            # Retrying won't help. Re-raise to be handled by the caller.
            raise

        is_500_error = False

        # Check if it's a 500 error from different exception types
        if isinstance(e, httpx.HTTPStatusError):
            is_500_error = e.response.status_code == 500
        elif isinstance(
            e,
            (
                litellm.exceptions.MidStreamFallbackError,
                litellm.exceptions.InternalServerError,
            ),
        ):
            # These litellm exceptions typically indicate 500-type server errors
            is_500_error = True

        if is_500_error and max_retries > 0:
            # Check if we have accumulated significant text - if so, avoid retrying
            if len(response_text) >= max_retriable_text_length:
                print(
                    f"LLM call failed but accumulated text ({len(response_text)} chars) exceeds retry threshold ({max_retriable_text_length}). Not retrying."
                )
                error_message = await _create_server_error_message(event, e)
                raise llm_util.TelegramUserReplyException(error_message)

            print(
                f"LLM call failed with server error. Retrying (attempt {MAX_RETRIES - max_retries + 1}/{MAX_RETRIES})..."
            )
            # Add logger to api_kwargs for the retry
            retry_api_kwargs = api_kwargs.copy()
            retry_api_kwargs["logger_fn"] = _create_retry_logger()

            await asyncio.sleep(1)  # Small delay before retrying
            return await _call_llm_with_retry(
                event,
                response_message,
                retry_api_kwargs,
                edit_interval,
                max_retries=max_retries - 1,
                max_retriable_text_length=max_retriable_text_length,
            )
        elif is_500_error:
            traceback.print_exc()
            print(f"LLM call failed after {MAX_RETRIES} attempts.")
            error_message = await _create_server_error_message(event, e)
            raise llm_util.TelegramUserReplyException(error_message)
        else:
            # Re-raise other errors
            raise


async def _call_llm_with_retry_tracked(
    user_id: int,
    event,
    response_message,
    api_kwargs: dict,
    edit_interval: Optional[float] = None,
    max_retries: int = MAX_RETRIES,
) -> LLMResponse:
    """Wrapper around _call_llm_with_retry that tracks the task for cancellation."""

    async def _wrapped_call():
        try:
            return await _call_llm_with_retry(
                event,
                response_message,
                api_kwargs,
                edit_interval,
                max_retries=max_retries,
            )
        except asyncio.CancelledError:
            # Handle cancellation gracefully
            await util.edit_message(
                response_message,
                f"{BOT_META_INFO_PREFIX}❌ Request was canceled.",
                append_p=True,
                parse_mode="md",
            )
            raise
        except Exception:
            # Re-raise other exceptions
            raise

    # Create and track the task
    task = asyncio.create_task(_wrapped_call())
    add_active_llm_task(user_id, task)

    try:
        result = await task
        return result
    finally:
        # Clean up task tracking
        remove_active_llm_task(user_id, task)


async def _retry_on_no_response_with_reasons(
    user_id: int,
    event,
    response_message,
    api_kwargs: dict,
    edit_interval: Optional[float] = None,
    model_capabilities: dict = None,
    streaming_p=True,
    no_response_retries_max=NO_RESPONSE_RETRIES_MAX,
    sleep=20,
) -> LLMResponse:
    """
    Retry LLM calls specifically for no-response scenarios with progress display.
    Shows retry progress and finish_reason in messages.
    """
    has_image = False
    last_finish_reason = None

    for attempt in range(1, no_response_retries_max + 1):
        try:
            # Make the LLM call
            llm_response = await _call_llm_with_retry_tracked(
                user_id, event, response_message, api_kwargs, edit_interval
            )

            response_text = llm_response.text
            finish_reason = llm_response.finish_reason
            last_finish_reason = finish_reason

            # Process image content if present for image generation models
            if (
                not streaming_p
                and model_capabilities
                and model_capabilities.get("image_generation", False)
            ):
                response_text, has_image = await _process_image_response(
                    event, response_text
                )

            # Check if we got a meaningful response
            if response_text.strip() or has_image:
                # Success! Set has_image on the response and return
                llm_response.has_image = has_image
                return llm_response

            # No response - show retry progress if not the last attempt
            if attempt < no_response_retries_max:
                finish_reason_text = (
                    f" (finish_reason: `{finish_reason}`)" if finish_reason else ""
                )
                retry_message = f"{BOT_META_INFO_PREFIX}__[No response: retrying {attempt}/{no_response_retries_max}]__{finish_reason_text}"

                await util.edit_message(
                    response_message,
                    retry_message,
                    parse_mode="md",
                    link_preview=False,
                )

                # Small delay before retry
                await asyncio.sleep(sleep)
        finally:
            pass

    # All retries exhausted - create final failure message
    final_finish_reason_text = (
        f" (last finish_reason: `{last_finish_reason}`)" if last_finish_reason else ""
    )

    final_text = f"{BOT_META_INFO_PREFIX}__[No response after {no_response_retries_max} attempts]__{final_finish_reason_text}"

    # Create a final LLMResponse with the failure message
    final_response = LLMResponse(
        text=final_text,
        finish_reason=last_finish_reason,
        has_image=has_image,
    )

    return final_response


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


def is_gemini_model(model_name):
    """Check if model is a Gemini model."""

    return re.search(r"\bgemini\b", model_name, re.IGNORECASE)


def is_native_gemini(model: str) -> bool:
    """Check if model is native Gemini (not OpenRouter) and supports context caching."""
    return model.startswith("gemini/")


def is_image_generation_model(model: str) -> bool:
    """Check if model supports image generation."""
    return model in IMAGE_GENERATION_MODELS


def is_native_gemini_image_generation(model: str) -> bool:
    """Check if model is native Gemini image generation (not via litellm)."""
    return model in GEMINI_IMAGE_GENERATION_MODELS


async def _send_image_to_telegram(
    event,
    image_data: bytes,
    *,
    filename_base: str = "generated_image",
    file_extension: str = ".png",
    file_index: Optional[int] = None,
) -> bool:
    """
    Send image data to Telegram with proper resource management.

    Args:
        event: Telegram event object
        image_data: Raw image bytes
        filename_base: Base name for the file (keyword-only)
        file_extension: File extension including dot (keyword-only)
        file_index: Optional index to append to filename (keyword-only)

    Returns:
        bool: True if image was sent successfully, False otherwise
    """
    try:
        # Create filename with optional index
        if file_index is not None:
            filename = f"{filename_base}_{file_index}{file_extension}"
        else:
            filename = f"{filename_base}{file_extension}"

        # Create BytesIO object for Telegram
        image_io = io.BytesIO(image_data)
        image_io.name = filename

        try:
            # Send image to Telegram with uploading photo action
            async with borg.action(event.chat, "photo") as action:
                await event.client.send_file(
                    event.chat_id,
                    file=image_io,
                    reply_to=event.id,
                )
            return True
        finally:
            image_io.close()

    except Exception as e:
        print(f"Error sending image to Telegram: {e}")
        return False


async def _process_image_response(event, response_content: str) -> tuple[str, bool]:
    """Process response content that may contain base64 image data.

    Returns:
        tuple: (text_content, has_image) where has_image indicates if an image was sent
    """
    # Check if response contains base64 image data using pre-compiled regex
    match = IMAGE_PATTERN.search(response_content)

    if not match:
        return response_content, False

    try:
        image_format = match.group(1).lower()
        image_data = match.group(2)

        # Validate image format
        if image_format not in ALLOWED_IMAGE_FORMATS:
            print(f"Unsupported image format: {image_format}, defaulting to png")
            image_format = "png"

        # Validate base64 data size before decoding
        if len(image_data) > MAX_IMAGE_SIZE * 4 / 3:  # base64 is ~33% larger
            print(f"Image data too large: {len(image_data)} characters")
            return response_content, False

        # Decode base64 image data with validation
        try:
            image_bytes = base64.b64decode(image_data, validate=True)
        except Exception as decode_error:
            print(f"Invalid base64 image data: {decode_error}")
            return response_content, False

        # Validate decoded image size
        if len(image_bytes) > MAX_IMAGE_SIZE:
            print(f"Decoded image too large: {len(image_bytes)} bytes")
            return response_content, False

        # Send image using shared utility function
        image_sent = await _send_image_to_telegram(
            event,
            image_bytes,
            filename_base="generated_image",
            file_extension=f".{image_format}",
        )

        if image_sent:
            # Remove image data from text response using the same pattern
            text_without_image = IMAGE_PATTERN.sub("", response_content).strip()
            return text_without_image, True
        else:
            return response_content, False

    except binascii.Error as e:
        print(f"Base64 decoding error: {e}")
        return response_content, False
    except ValueError as e:
        print(f"Image validation error: {e}")
        return response_content, False
    except Exception as e:
        print(f"Unexpected error processing image response: {e}")
        traceback.print_exc()
        return response_content, False


async def _handle_native_gemini_image_generation(
    event,
    messages: list,
    api_key: str,
    model: str,
    response_message,
    model_capabilities: Dict[str, bool] = None,
    *,
    max_retries: int = MAX_RETRIES,
) -> tuple[str, bool]:
    """Handle native Gemini image generation with streaming support.

    Returns:
        tuple: (text_content, has_image) where has_image indicates if an image was sent
    """
    try:
        client = llm_util.create_genai_client(
            api_key=api_key,
            user_id=event.sender_id,
            read_bufsize=2 * 2**20,
            proxy_p=True,
        )

        # Initialize model capabilities and warnings tracking if not provided
        if model_capabilities is None:
            model_capabilities = get_model_capabilities(model)
        issued_warnings = set()
        #: issued_warnings and model_capabilities checks should be redundant, as the message history built already processed these. remove in future commits?

        # Extract system prompt and apply GEMINI_IMAGE_GEN_SYSTEM_MODE preprocessing
        system_prompt = None
        for msg in messages:
            if msg["role"] == "system":
                system_prompt = msg["content"]
                break

        # Preprocess messages based on GEMINI_IMAGE_GEN_SYSTEM_MODE
        processed_messages = messages
        system_instruction_for_config = system_prompt

        if system_prompt:
            if GEMINI_IMAGE_GEN_SYSTEM_MODE == "SKIP":
                # Remove system messages entirely
                processed_messages = [
                    msg for msg in messages if msg["role"] != "system"
                ]
                system_instruction_for_config = None
            elif GEMINI_IMAGE_GEN_SYSTEM_MODE == "PREPEND":
                # Prepend system to first user message and remove system messages
                modified_messages = []
                first_user_found = False
                for msg in messages:
                    if msg["role"] == "system":
                        continue  # Skip system messages
                    elif msg["role"] == "user" and not first_user_found:
                        # Prepend system to first user message
                        if isinstance(msg["content"], str):
                            content = f"{system_prompt}\n\n---\n\n{msg['content']}"
                        else:
                            # Handle list content by prepending to first text part
                            content = (
                                msg["content"].copy()
                                if isinstance(msg["content"], list)
                                else [msg["content"]]
                            )
                            for i, part in enumerate(content):
                                if (
                                    isinstance(part, dict)
                                    and part.get("type") == "text"
                                ):
                                    content[i] = {
                                        **part,
                                        "text": f"{system_prompt}\n\n---\n\n{part['text']}",
                                    }
                                    break
                            else:
                                # No text part found, add system as first text part
                                content.insert(
                                    0,
                                    {
                                        "type": "text",
                                        "text": f"{system_prompt}\n\n---\n\n",
                                    },
                                )

                        modified_messages.append({"role": "user", "content": content})
                        first_user_found = True
                    else:
                        modified_messages.append(msg)
                processed_messages = modified_messages
                system_instruction_for_config = None

        # Convert messages using litellm's proven conversion function
        contents = _gemini_convert_messages_with_history(processed_messages)

        # Create config with system instruction if available
        config_kwargs = {"response_modalities": ["IMAGE", "TEXT"]}
        if system_instruction_for_config:
            config_kwargs["system_instruction"] = system_instruction_for_config

        generate_content_config = types.GenerateContentConfig(**config_kwargs)

        response_text = ""
        has_image = False
        file_index = 0

        # Get streaming delay for updating message
        model_in_use, _ = _get_effective_model_and_service(
            event.chat_id, event.sender_id
        )
        edit_interval = get_streaming_delay(model_in_use)
        last_edit_time = asyncio.get_event_loop().time()

        # Stream the response
        try:
            async for chunk in await client.aio.models.generate_content_stream(
                model=re.sub(r"^gemini/", "", model),  # Remove prefix for native API
                contents=contents,
                config=generate_content_config,
            ):
                if (
                    chunk.candidates is None
                    or chunk.candidates[0].content is None
                    or chunk.candidates[0].content.parts is None
                ):
                    continue

                # Handle image data
                if (
                    chunk.candidates[0].content.parts[0].inline_data
                    and chunk.candidates[0].content.parts[0].inline_data.data
                ):

                    inline_data = chunk.candidates[0].content.parts[0].inline_data
                    data_buffer = inline_data.data
                    file_extension = (
                        mimetypes.guess_extension(inline_data.mime_type) or ".png"
                    )

                    # Send image using shared utility function
                    image_sent = await _send_image_to_telegram(
                        event,
                        data_buffer,
                        filename_base="generated_image",
                        file_extension=file_extension,
                        file_index=file_index,
                    )

                    if image_sent:
                        has_image = True
                        file_index += 1

                # Handle text data
                if hasattr(chunk, "text") and chunk.text:
                    response_text += chunk.text

                    # Update message periodically during streaming
                    current_time = asyncio.get_event_loop().time()
                    if (current_time - last_edit_time) > edit_interval:
                        try:
                            await util.edit_message(
                                response_message, f"{response_text}▌", parse_mode="md"
                            )
                            last_edit_time = current_time
                        except errors.rpcerrorlist.MessageNotModifiedError:
                            pass
                        except Exception as e:
                            print(f"Error during message edit: {e}")

        except (
            httpx.HTTPStatusError,
            litellm.exceptions.MidStreamFallbackError,
            litellm.exceptions.InternalServerError,
        ) as e:
            is_500_error = False

            # Check if it's a 500 error from different exception types
            if isinstance(e, httpx.HTTPStatusError):
                is_500_error = e.response.status_code == 500
            elif isinstance(
                e,
                (
                    litellm.exceptions.MidStreamFallbackError,
                    litellm.exceptions.InternalServerError,
                ),
            ):
                # These litellm exceptions typically indicate 500-type server errors
                is_500_error = True

            if is_500_error and max_retries > 0:
                print(
                    f"Gemini image generation failed with 500 error. Retrying (attempt {MAX_RETRIES - max_retries + 1}/{MAX_RETRIES})..."
                )
                await asyncio.sleep(1)  # Small delay before retrying
                return await _handle_native_gemini_image_generation(
                    event,
                    messages,
                    api_key,
                    model,
                    response_message,
                    model_capabilities,
                    max_retries=max_retries - 1,
                )
            elif is_500_error:
                traceback.print_exc()
                print(f"Gemini image generation failed after {MAX_RETRIES} attempts.")
                raise llm_util.TelegramUserReplyException(
                    "The Gemini model's server is currently unavailable (500 Internal Server Error). This is likely an upstream issue. Please try again later."
                )
            else:
                # Re-raise other errors
                raise

        return response_text.strip(), has_image

    except Exception as e:
        raise


def get_model_capabilities(model: str) -> Dict[str, bool]:
    """Get model capabilities for vision, audio input, video input, audio output, PDF input, and image generation support."""
    capabilities = {
        "vision": False,
        "audio_input": False,
        "video_input": False,
        "audio_output": False,
        "pdf_input": False,
        "image_generation": False,
    }
    try:
        capabilities["vision"] = litellm.supports_vision(model)
    except Exception as e:
        print(f"Error checking vision support for {model}: {e}")
    try:
        capabilities["audio_input"] = (
            litellm.supports_audio_input(model)
            or is_gemini_model(model)
            or model
            in (
                "gemini/gemini-2.5-flash-lite",
                "gemini/gemini-2.5-flash",
            )
        )
        #: hardcoding some models because of upstream bugs
    except Exception as e:
        print(f"Error checking audio input support for {model}: {e}")
    try:
        video_input_p = False
        if hasattr(litellm, "supports_video_input"):
            video_input_p = litellm.supports_video_input(model)
        elif hasattr(litellm.utils, "supports_video_input"):
            video_input_p = litellm.utils.supports_video_input(model)
        #: This function seems to not have been written yet in LiteLLM.

        video_input_p = video_input_p or is_gemini_model(model)

        capabilities["video_input"] = video_input_p
    except Exception as e:
        print(f"Error checking video input support for {model}: {e}")
    try:
        capabilities["audio_output"] = litellm.supports_audio_output(model)
    except Exception as e:
        print(f"Error checking audio output support for {model}: {e}")

    try:
        capabilities["pdf_input"] = litellm.utils.supports_pdf_input(model)

    except Exception as e:
        print(f"Error checking PDF input support for {model}: {e}")
        capabilities["pdf_input"] = False

    capabilities["image_generation"] = is_image_generation_model(model)
    return capabilities


async def _get_and_cache_media_info(message, file_id, temp_dir):
    """
    Downloads media if not cached, determines its type, and caches it in a
    text-safe format (raw text or Base64).

    Returns a tuple of:
    (storage_type, content, filename, mime_type)
    - storage_type: 'text' or 'base64'
    - content: content (string for text, base64 string for base64)
    - filename: name of the file
    - mime_type: detected mime type
    Returns (None, None, None, None) on failure.
    """
    cached_file_info = await history_util.get_cached_file(file_id)
    if cached_file_info:
        return (
            cached_file_info["data_storage_type"],
            cached_file_info["data"],
            cached_file_info.get("filename"),
            cached_file_info.get("mime_type"),
        )

    # File not cached, download and process
    file_path_str = await message.download_media(file=temp_dir)
    if not file_path_str:
        return None, None, None, None

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
        return "text", text_content, original_filename, mime_type
    else:
        b64_content = base64.b64encode(file_bytes).decode("utf-8")
        await history_util.cache_file(
            file_id,
            data=b64_content,
            data_storage_type="base64",
            filename=original_filename,
            mime_type=mime_type,
        )
        return "base64", b64_content, original_filename, mime_type


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
    is_private = event.is_private
    chat_id = event.chat_id
    prefs = user_manager.get_prefs(user_id)

    effective_last_n_limit = _get_effective_last_n_limit(
        chat_id,
        user_id,
    )

    # Determine the base mode and its source
    chat_context_mode = chat_manager.get_context_mode(chat_id)
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

    # Dynamically generate mode name
    mode_name_base = CONTEXT_MODE_NAMES.get(effective_mode, effective_mode)
    mode_name = mode_name_base
    if effective_mode == "last_N":
        mode_name = f"{mode_name_base} (Limit: {effective_last_n_limit})"

    # Build the response message
    response_parts = [
        f"∙ **Current Mode:** `{mode_name}`",
        f"∙ **Source:** This is using {source_text}.",
    ]

    # If the effective mode is 'smart', add the current state
    if effective_mode == "smart":
        current_smart_state = get_smart_context_mode(user_id)
        smart_state_name_base = CONTEXT_MODE_NAMES.get(
            current_smart_state, current_smart_state
        )
        smart_state_name = smart_state_name_base
        if current_smart_state == "last_N":
            smart_state_name = (
                f"{smart_state_name_base} (Limit: {effective_last_n_limit})"
            )

        response_parts.append(
            f"∙ **Smart State:** The bot is currently using the `{smart_state_name}` method."
        )

    return "\n".join(response_parts)


def _check_media_capability(
    media_type: str,
    model_capabilities: Dict[str, bool],
    issued_warnings: set,
    *,
    private_p: bool,
) -> MediaCapabilityCheckResult:
    """
    Checks if the model supports the given media type and returns a consolidated
    warning if not, tracking which warnings have been issued.
    """
    result = MediaCapabilityCheckResult()

    if media_type == "image" and not model_capabilities.get("vision", False):
        if "image" not in issued_warnings:
            issued_warnings.add("image")
            result.has_warning = True
            result.warnings.append(
                "Images were skipped because the current model does not support vision."
            )
    elif media_type == "audio" and not model_capabilities.get("audio_input", False):
        if "audio" not in issued_warnings:
            issued_warnings.add("audio")
            result.has_warning = True
            result.warnings.append(
                "Audio files were skipped because the current model does not support audio input."
            )
    elif media_type == "video" and not model_capabilities.get("video_input", False):
        if "video" not in issued_warnings:
            issued_warnings.add("video")
            result.has_warning = True
            result.warnings.append(
                "Video files were skipped because the current model does not support video input."
            )
    elif media_type == "pdf" and not model_capabilities.get("pdf_input", False):
        if "pdf" not in issued_warnings:
            issued_warnings.add("pdf")
            result.has_warning = True
            result.warnings.append(
                "PDF files were skipped because the current model does not support PDF input."
            )
    elif media_type is None:
        if "unknown" not in issued_warnings:
            issued_warnings.add("unknown")
            result.has_warning = True
            if private_p:
                result.warnings.append(
                    "Files with unknown or unsupported media types were skipped."
                )
    return result


async def _process_media(
    message: Message,
    temp_dir: Path,
    model_capabilities: Dict[str, bool],
    issued_warnings: set,
    sender_id: int,
    api_key: str,
    model_in_use: str,
    *,
    is_private: bool,
    check_gemini_cached_files_p: bool = DEFAULT_CHECK_GEMINI_CACHED_FILES_P,
) -> ProcessMediaResult:
    """
    Downloads or retrieves media from cache, prepares it for litellm,
    and ensures it's cached in a text-safe format (raw text or Base64).
    Uses the Gemini Files API for native models if configured.
    """
    if not message or not message.media:
        return ProcessMediaResult(media_part=None, warnings=[])

    try:
        file_id = (
            f"{message.chat_id}_{message.id}_{getattr(message.media, 'id', 'unknown')}"
        )

        # --- Branch 1: Gemini Files API Mode ---
        if is_native_gemini_files_mode(model_in_use):
            gemini_client = None

            cached_info = await history_util.get_cached_gemini_file_info(
                file_id, sender_id
            )

            if cached_info and "name" in cached_info and "uri" in cached_info:
                # New: check media capability before proceeding
                cached_mime_type = cached_info.get("mime_type")
                media_type = common_util.get_media_type(cached_mime_type)
                check_result = _check_media_capability(
                    media_type,
                    model_capabilities,
                    issued_warnings,
                    private_p=is_private,
                )
                if check_result.has_warning:
                    return ProcessMediaResult(
                        media_part=None, warnings=check_result.warnings
                    )

                # If we need to check, verify the file still exists on Gemini's servers
                if check_gemini_cached_files_p:
                    try:
                        if gemini_client is None:
                            gemini_client = llm_util.create_genai_client(
                                api_key=api_key, user_id=sender_id
                            )

                        # This API call verifies the file's existence.
                        await gemini_client.aio.files.get(name=cached_info["name"])
                        # If it exists, we can use the cached info.
                        part = {
                            "type": "file",
                            "file": {
                                "file_id": cached_info["uri"],
                                "filename": "some_file",
                                "format": cached_info.get("mime_type"),
                            },
                        }
                        return ProcessMediaResult(media_part=part, warnings=[])
                    except google_exceptions.NotFound:
                        # File has expired or was deleted from Gemini, so we'll proceed to re-upload.
                        pass
                    except Exception as e:
                        print(
                            f"Error validating Gemini file {cached_info['name']}: {e}"
                        )
                        # If validation fails for another reason, don't proceed with this file.
                        return ProcessMediaResult(
                            media_part=None,
                            warnings=["Failed to verify cached Gemini file."],
                        )
                else:
                    # If checking is disabled, we trust the cache is valid.
                    part = {
                        "type": "file",
                        "file": {
                            "file_id": cached_info["uri"],
                            "filename": "some_file",
                            "format": cached_info.get("mime_type"),
                        },
                    }
                    return ProcessMediaResult(media_part=part, warnings=[])

            # --- File not cached in Gemini format, proceed to upload ---
            storage_type, content, filename, mime_type = (
                await _get_and_cache_media_info(message, file_id, temp_dir)
            )

            if not storage_type:
                return ProcessMediaResult(media_part=None, warnings=[])

            if storage_type == "text":
                part = {
                    "type": "text",
                    "text": f"\n--- Attachment: {filename} ---\n{content}",
                }
                return ProcessMediaResult(media_part=part, warnings=[])

            # It must be 'base64' type. 'content' is a b64 string.
            # Check capability before uploading.
            media_type = common_util.get_media_type(mime_type)
            check_result = _check_media_capability(
                media_type, model_capabilities, issued_warnings, private_p=is_private
            )
            if check_result.has_warning:
                return ProcessMediaResult(
                    media_part=None, warnings=check_result.warnings
                )

            # Upload to Gemini Files API
            if gemini_client is None:
                gemini_client = llm_util.create_genai_client(
                    api_key=api_key, user_id=sender_id
                )
            with tempfile.NamedTemporaryFile(
                delete=False, suffix=Path(filename).suffix
            ) as temp_f:
                # The helper returns a base64 string for binary files,
                # so we decode it back to raw bytes before writing to a temp file.
                raw_bytes = base64.b64decode(content)
                temp_f.write(raw_bytes)
                temp_path = temp_f.name
            try:
                gemini_file = await gemini_client.aio.files.upload(
                    file=temp_path,
                )
                while gemini_file.state.name in (
                    "PROCESSING",
                    "STATE_UNSPECIFIED",
                ):
                    await asyncio.sleep(1)
                    gemini_file = await gemini_client.aio.files.get(
                        name=gemini_file.name
                    )
                if gemini_file.state.name == "FAILED":
                    raise Exception("Gemini file processing failed.")

                # Cache the name, URI and mime_type
                await history_util.cache_gemini_file_info(
                    file_id,
                    sender_id,
                    gemini_file.name,
                    gemini_file.uri,
                    gemini_file.mime_type,
                )

                part = {
                    "type": "file",
                    "file": {
                        "file_id": gemini_file.uri,
                        "filename": filename,
                        "format": gemini_file.mime_type,
                    },
                }
                return ProcessMediaResult(media_part=part, warnings=[])
            finally:
                os.unlink(temp_path)

        # --- Branch 2: Base64 Mode ---
        storage_type, content, filename, mime_type = await _get_and_cache_media_info(
            message, file_id, temp_dir
        )

        if not storage_type:
            return ProcessMediaResult(media_part=None, warnings=[])

        if storage_type == "text":
            part = {
                "type": "text",
                "text": f"\n--- Attachment: {filename} ---\n{content}",
            }
            return ProcessMediaResult(media_part=part, warnings=[])
        elif storage_type == "base64":
            media_type = common_util.get_media_type(mime_type)
            check_result = _check_media_capability(
                media_type, model_capabilities, issued_warnings, private_p=is_private
            )
            if check_result.has_warning:
                return ProcessMediaResult(
                    media_part=None, warnings=check_result.warnings
                )

            if not mime_type or (
                not mime_type.startswith(("image/", "audio/", "video/"))
                and mime_type != "application/pdf"
            ):
                print(
                    f"Unsupported binary media type '{mime_type}' for file {filename}"
                )
                return ProcessMediaResult(media_part=None, warnings=[])

            # Handle PDF files with the new file format for litellm
            if mime_type == "application/pdf":
                part = {
                    "type": "file",
                    "file": {
                        "file_id": f"data:{mime_type};base64,{content}",
                        "format": "application/pdf",
                    },
                }
            else:
                # Handle other media types (images, audio, video) with existing format
                part = {
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime_type};base64,{content}"},
                }
            return ProcessMediaResult(media_part=part, warnings=[])

    except Exception as e:
        print(f"Error processing media from message {message.id}: {e}")
        traceback.print_exc()
        return ProcessMediaResult(media_part=None, warnings=[])


async def _log_conversation(
    event, prefs: UserPrefs, model_in_use: str, messages: list, final_response: str
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
            f"Model: {model_in_use}",
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

    if hasattr(message, "_role"):
        #: allows forcefully setting a role programmatically
        return message._role

    # Check for magic pattern to force user role
    if message.text and MAGIC_PATTERN_AS_USER.search(message.text):
        return "user"

    if message.forward and message.forward.from_id:
        # from_id is a Peer object; we only care about user-to-user forwards for role assignment.
        original_sender_id = getattr(message.forward.from_id, "user_id", None)

    # A message is from the assistant if it was sent by the bot OR if it's a forward of a message originally from the bot.
    if message.sender_id == BOT_ID or original_sender_id == BOT_ID:
        role = "assistant"

    return role


async def _process_message_content(
    message: Message,
    role: str,
    temp_dir: Path,
    model_capabilities: Dict[str, bool],
    issued_warnings: set,
    api_key: str,
    model_in_use: str,
    sender_id,
    *,
    metadata_prefix: str = "",
    check_gemini_cached_files_p: bool = DEFAULT_CHECK_GEMINI_CACHED_FILES_P,
    is_private: bool,
) -> ProcessContentResult:
    """Processes a single message's text and media into litellm content parts."""
    text_buffer, media_parts, warnings = [], [], []

    # ic(message, message.id, message.text)

    # Filter out meta-info messages and commands from history
    if (
        role == "assistant"
        and message.text
        and message.text.startswith(BOT_META_INFO_PREFIX)
    ):
        return ProcessContentResult(text_parts=[], media_parts=[], warnings=[])
    if role == "user" and _is_known_command(message.text):
        return ProcessContentResult(text_parts=[], media_parts=[], warnings=[])

    processed_text = message.text

    if processed_text:
        processed_text = processed_text.split(BOT_META_INFO_LINE, 1)[0].strip()
    if role == "user" and processed_text:
        if re.match(r"^\.s\b", processed_text):
            processed_text = processed_text[2:].strip()

        # Strip model selection prefixes from all user messages in history
        prefix_detection = _detect_and_process_message_prefix(processed_text)
        processed_text = prefix_detection.processed_text

        # Remove magic pattern to force user role
        processed_text = MAGIC_PATTERN_AS_USER.sub("", processed_text).strip()

        # Apply prompt replacements with simple regex substitutions.
        # Apply each pattern at most once per message.
        for pattern, replacement in PROMPT_REPLACEMENTS.items():
            processed_text = pattern.sub(
                replacement,
                processed_text,
                # count=1,
                #: By default, `re.sub` replaces all occurrences of the pattern in the string.
            )

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

    media_result = await _process_media(
        message,
        temp_dir,
        model_capabilities,
        issued_warnings,
        sender_id,
        api_key,
        model_in_use,
        is_private=is_private,
        check_gemini_cached_files_p=check_gemini_cached_files_p,
    )
    warnings.extend(media_result.warnings)
    if media_result.media_part:
        media_parts.append(media_result.media_part)

    return ProcessContentResult(
        text_parts=text_buffer, media_parts=media_parts, warnings=warnings
    )


async def _finalize_content_parts(text_buffer: list, media_parts: list) -> list:
    """Combines text and media parts into a final list for a history entry."""
    content_parts = []

    text_from_files = []
    for part in media_parts:
        if part.get("type") == "text":
            text_from_files.append(part["text"])
        else:
            content_parts.append(part)

    #: It seems that if the media parts are put first, the model will also see them first? I am not really sure, but it seems to help the model not say it doesn't have access to files when a file and its textual instructions are in the same message.

    if text_buffer:
        content_parts.append({"type": "text", "text": "\n".join(text_buffer)})

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
    event,
    message_list: List[Message],
    temp_dir: Path,
    model_capabilities: Dict[str, bool],
    api_key: str,
    model_in_use: str,
    check_gemini_cached_files_p: bool = DEFAULT_CHECK_GEMINI_CACHED_FILES_P,
    *,
    is_private: bool,
) -> tuple[List[dict], List[str]]:
    """
    Processes a final, sorted list of messages into litellm history format,
    respecting the user's chosen metadata and context settings.
    Returns: (history, warnings)
    """
    sender_id = event.sender_id
    history = []
    all_warnings = []
    issued_warnings = set()  # Track issued warnings for this turn processing.
    if not message_list:
        return history, all_warnings

    user_prefs = user_manager.get_prefs(sender_id)
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
                content_result = await _process_message_content(
                    turn_msg,
                    role,
                    temp_dir,
                    model_capabilities,
                    issued_warnings,
                    api_key,
                    model_in_use,
                    sender_id,
                    metadata_prefix="",
                    check_gemini_cached_files_p=check_gemini_cached_files_p,
                    is_private=is_private,
                )
                text_buffer.extend(content_result.text_parts)
                media_parts.extend(content_result.media_parts)
                all_warnings.extend(content_result.warnings)

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
            elif active_metadata_mode == "only_forwarded" and message.forward:
                prefix_parts.append(await _get_forward_metadata_prefix(message))

            metadata_prefix = " ".join(filter(None, prefix_parts))
            #: Return an iterator yielding those items of iterable for which function(item) is true. If function is None, return the items that are true.
            content_result = await _process_message_content(
                message,
                role,
                temp_dir,
                model_capabilities,
                issued_warnings,
                api_key,
                model_in_use,
                sender_id,
                metadata_prefix=metadata_prefix,
                check_gemini_cached_files_p=check_gemini_cached_files_p,
                is_private=is_private,
            )
            all_warnings.extend(content_result.warnings)

            # Skip messages that have no original text and no processable media.
            if not message.text and not content_result.media_parts:
                continue

            if not content_result.text_parts and not content_result.media_parts:
                continue

            final_content_parts = await _finalize_content_parts(
                content_result.text_parts, content_result.media_parts
            )
            if final_content_parts:
                try:
                    final_content = (
                        final_content_parts[0]["text"]
                        if len(final_content_parts) == 1
                        and final_content_parts[0]["type"] == "text"
                        else final_content_parts
                    )
                except:
                    ic(final_content_parts[0])
                    raise

                history.append({"role": role, "content": final_content})

    return history, all_warnings


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


async def build_conversation_history(
    event,
    context_mode: str,
    temp_dir: Path,
    model_capabilities: Dict[str, bool],
    api_key: str,
    model_in_use: str,
    check_gemini_cached_files_p: bool = DEFAULT_CHECK_GEMINI_CACHED_FILES_P,
    *,
    is_private: bool,
) -> ConversationHistoryResult:
    """
    Orchestrates the construction of a conversation history based on the user's
    selected context mode, using the appropriate method for a bot or userbot.
    """
    messages_to_process = []
    chat_id = event.chat_id
    user_id = event.sender_id

    # Get effective LAST_N limit using the new helper function
    effective_last_n_limit = _get_effective_last_n_limit(chat_id, user_id)

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
            history, warnings = await _process_turns_to_history(
                event,
                expanded_messages,
                temp_dir,
                model_capabilities,
                api_key,
                model_in_use,
                check_gemini_cached_files_p=check_gemini_cached_files_p,
                is_private=is_private,
            )
            return ConversationHistoryResult(history=history, warnings=warnings)

        elif context_mode == "last_N":
            message_ids = await history_util.get_last_n_ids(
                chat_id, effective_last_n_limit
            )
        elif context_mode == "until_separator":
            message_ids = await history_util.get_all_ids(chat_id)
        elif context_mode == "recent":
            now = datetime.now(timezone.utc)
            five_seconds_ago = now - timedelta(seconds=5)
            message_ids = await history_util.get_ids_since(chat_id, five_seconds_ago)
            # ic(message_ids)

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
                chat_id, limit=effective_last_n_limit
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

    history, warnings = await _process_turns_to_history(
        event,
        expanded_messages,
        temp_dir,
        model_capabilities,
        api_key,
        model_in_use,
        check_gemini_cached_files_p=check_gemini_cached_files_p,
        is_private=is_private,
    )
    return ConversationHistoryResult(history=history, warnings=warnings)


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
            pattern=rf"(?i)^/stop{bot_username_suffix_re}\s*$",
            func=lambda e: e.is_private,
        )
    )(stop_handler)
    borg.on(
        events.NewMessage(
            pattern=rf"(?i)^/log{bot_username_suffix_re}(?:\s+(\d+))?\s*$",
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
            pattern=rf"(?i)^/setdeepseekkey{bot_username_suffix_re}(?:\s+(.*))?\s*$",
            func=lambda e: e.is_private,
        )
    )(set_deepseek_key_handler)
    borg.on(
        events.NewMessage(
            pattern=rf"(?i)^/setmistralkey{bot_username_suffix_re}(?:\s+(.*))?\s*$",
            func=lambda e: e.is_private,
        )
    )(set_mistral_key_handler)
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
        events.NewMessage(
            pattern=rf"(?i)^/setmodelhere{bot_username_suffix_re}(?:\s+(.*))?$"
        )
    )(set_model_here_handler)
    borg.on(
        events.NewMessage(pattern=rf"(?i)^/getmodelhere{bot_username_suffix_re}\s*$")
    )(get_model_here_handler)
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
            pattern=rf"(?i)^/setlastn{bot_username_suffix_re}(?:\s+(.*))?$",
            func=lambda e: e.is_private,
        )
    )(set_last_n_handler)
    borg.on(
        events.NewMessage(
            pattern=rf"(?i)^/getlastn{bot_username_suffix_re}\s*$",
            func=lambda e: e.is_private,
        )
    )(get_last_n_handler)
    borg.on(
        events.NewMessage(
            pattern=rf"(?i)^/setlastnhere{bot_username_suffix_re}(?:\s+(.*))?$"
        )
    )(set_last_n_here_handler)
    borg.on(
        events.NewMessage(pattern=rf"(?i)^/getlastnhere{bot_username_suffix_re}\s*$")
    )(get_last_n_here_handler)
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

    # Load smart context states from Redis on startup (both bot and userbot)
    await load_smart_context_states()

    # Populate callback hash map for persistent button handling
    bot_util.populate_callback_hash_map(MODEL_CHOICES)

    register_handlers()

    #: Registering the history event handlers last, as uniborg monkey patches `._event_builders` to be a ReverseList.
    #: [[zf:~\[borg\]/uniborg/uniborg.py::self._event_builders = hacks.ReverseList()]]
    if IS_BOT:
        await history_util.initialize_history_handler()
        print("LLM_Chat: Running as a BOT. History utility initialized.")
    else:
        print(
            "LLM_Chat: Running as a USERBOT. History utility and bot commands skipped."
        )

    # Notify log chat that the plugin initialized successfully
    try:
        chat = getattr(borg, "log_chat", None)
        if chat:
            mode = "BOT" if IS_BOT else "USERBOT"
            await borg.send_message(
                chat,
                f"{BOT_META_INFO_PREFIX}LLM_Chat initialized successfully. Running as {mode}.",
            )
    except Exception:
        # Silently ignore logging errors to avoid breaking startup
        pass

    if IS_BOT:
        #: This doesn't really matter, so we put it after the successful initialization message.
        await bot_util.register_bot_commands(borg, BOT_COMMANDS)


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
**Hello! I am a Telegram chat bot powered by third-party AI providers.** It's like ChatGPT but in Telegram!

To get started, you'll need an API key. Send me /setgeminikey for Gemini models, /setopenrouterkey for OpenRouter, /setdeepseekkey for DeepSeek models, or /setmistralkey for Mistral models.

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
  - `Last N Messages`: The most recent messages in the chat.

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
- /stop: Stop all in-progress chat requests.
- /log: Get your last {LOG_COUNT_LIMIT} conversation logs as files.
- /setgeminikey: Sets or updates your Gemini API key.
- /setModel: Change the AI model. Current: `{prefs.model}`.
- /setSystemPrompt: Change my core instructions or reset to default.
- /setModelHere: Set the AI model for the current chat only.
- /getModelHere: View the effective AI model for the current chat.
- /setLastN: Set your default 'Last N' message limit.
- /getLastN: View your default 'Last N' message limit.
- /setLastNHere: Set 'Last N' message limit for this chat.
- /getLastNHere: View 'Last N' message limit for this chat.
- /contextMode: Change how **private** chat history is gathered.
- /groupContextMode: Change how **group** chat history is gathered.
- /metadataMode: Change how **private** chat metadata is handled.
- /groupMetadataMode: Change how **group** chat metadata is handled.
- /groupActivationMode: Change how I am triggered in groups.
- /setthink: Adjust the model's reasoning effort for complex tasks.
- /tools: Enable/disable tools like Google Search and Code Execution.
- /json: Toggle JSON-only output mode for structured data needs.

**Quick Model Selection Shortcuts**
Start your messages with these shortcuts to use specific models:
- `.c` → GPT-5 Chat (OpenRouter): Latest Non-reasoning OpenAI model
- `.f` → Gemini 2.5 Flash Lite: Ultra-fast responses
- `.ff` → Gemini 2.5 Flash: Fast responses
- `.g` → Gemini 2.5 Pro: Google's flagship model
- `.d` → DeepSeek Reasoner
"""
    await event.reply(
        f"{BOT_META_INFO_PREFIX}{help_text}", link_preview=False, parse_mode="md"
    )


async def stop_handler(event):
    """Stop all in-progress chat requests for the user."""
    user_id = event.sender_id

    # Cancel any pending input flows first
    if llm_db.is_awaiting_key(user_id):
        llm_db.cancel_key_flow(user_id)
    cancel_input_flow(user_id)

    # Cancel live mode if active
    chat_id = event.chat_id
    if gemini_live_util.live_session_manager.is_live_mode_active(chat_id):
        session = gemini_live_util.live_session_manager.get_session(chat_id)
        if session and hasattr(session, "_response_task") and session._response_task:
            session._response_task.cancel()
        gemini_live_util.live_session_manager.end_session(chat_id)

    # Cancel all active LLM tasks
    cancelled_count = cancel_all_llm_tasks(user_id)

    if cancelled_count > 0:
        await event.reply(
            f"{BOT_META_INFO_PREFIX}✅ Stopped {cancelled_count} active chat request(s)."
        )
    else:
        await event.reply(f"{BOT_META_INFO_PREFIX}No active chat requests to stop.")


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

    # Determine model status - check for chat-specific model
    model_status = f"`{prefs.model}`"
    chat_model = chat_manager.get_model(chat_id)
    if chat_model:
        model_status += f" (overridden in this chat)"

    # 'Last N' limit status
    user_last_n_limit = (
        f"`{prefs.last_n_messages_limit}`"
        if prefs.last_n_messages_limit is not None
        else f"Default (`{LAST_N_MESSAGES_LIMIT}`)"
    )
    effective_last_n_limit = _get_effective_last_n_limit(
        chat_id,
        user_id,
    )
    chat_last_n_limit = chat_manager.get_last_n_messages_limit(chat_id)
    chat_last_n_status = (
        f"`{chat_last_n_limit}`" if chat_last_n_limit is not None else "Not set"
    )

    # Get context mode names and handle smart/last_N modes
    context_mode_name = CONTEXT_MODE_NAMES.get(
        prefs.context_mode, prefs.context_mode.replace("_", " ").title()
    )
    if prefs.context_mode == "last_N":
        context_mode_name += f" (Limit: {effective_last_n_limit})"

    smart_mode_status_str = ""
    if prefs.context_mode == "smart":
        current_smart_state = get_smart_context_mode(user_id)
        smart_state_name_base = CONTEXT_MODE_NAMES.get(
            current_smart_state, current_smart_state
        )
        smart_state_name = smart_state_name_base
        if current_smart_state == "last_N":
            smart_state_name = (
                f"{smart_state_name_base} (Limit: {effective_last_n_limit})"
            )
        smart_mode_status_str = f" (State: `{smart_state_name}`)"

    group_context_mode_name = CONTEXT_MODE_NAMES.get(
        prefs.group_context_mode, prefs.group_context_mode.replace("_", " ").title()
    )
    if prefs.group_context_mode == "last_N":
        group_context_mode_name += f" (Limit: {effective_last_n_limit})"

    group_smart_mode_status_str = ""
    if prefs.group_context_mode == "smart":
        current_smart_state = get_smart_context_mode(user_id)
        smart_state_name_base = CONTEXT_MODE_NAMES.get(
            current_smart_state, current_smart_state
        )
        smart_state_name = smart_state_name_base
        if current_smart_state == "last_N":
            group_smart_mode_status_str = (
                f" (State: `{smart_state_name}`, Limit: `{effective_last_n_limit}`)"
            )

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
        f"• **Model:** {model_status}\n"
        f"• **Reasoning Level:** `{thinking_level}`\n"
        f"• **Enabled Tools:** `{enabled_tools_str}`\n"
        f"• **JSON Mode:** `{'Enabled' if prefs.json_mode else 'Disabled'}`\n"
        f"• **Personal System Prompt:** `{user_system_prompt_status}`\n"
        f"• **Personal 'Last N' Limit:** {user_last_n_limit}\n\n"
        f"**This Chat's Settings**\n"
        f"• **Chat Model:** `{chat_model or 'Not set'}`\n"
        f"• **Chat System Prompt:** `{chat_system_prompt_status}`\n"
        f"• **Chat 'Last N' Limit:** {chat_last_n_status}\n\n"
        f"**TTS Settings (This Chat)**\n"
        f"• **TTS Model:** `{tts_model_display}`\n"
        f"• **Voice:** {effective_voice_display}\n\n"
        f"**Private Chat Context**\n"
        f"• **Context Mode:** `{context_mode_name}`{smart_mode_status_str}\n"
        f"• **Metadata Mode:** `{metadata_mode_name}`\n\n"
        f"**Group Chat Context**\n"
        f"• **Context Mode:** `{group_context_mode_name}`{group_smart_mode_status_str}\n"
        f"• **Metadata Mode:** `{group_metadata_mode_name}`\n"
        f"• **Activation:** `{group_activation_mode_name}`\n"
    )
    await event.reply(f"{BOT_META_INFO_PREFIX}{status_message}", parse_mode="md")


async def log_handler(event):
    """Sends the last few conversation logs to the user."""
    user_id = event.sender_id
    user_log_dir = LOG_DIR / str(user_id)

    # Check if a number parameter was provided
    match = event.pattern_match
    custom_count = None
    if match and match.group(1):
        # Admin check required for custom count
        if await util.isAdmin(event):
            custom_count = int(match.group(1))
            if custom_count >= 20:
                custom_count = 20
                event.reply(
                    f"{BOT_META_INFO_PREFIX}Custom log count limited to 20 even for admins for privacy reasons."
                )

        else:
            if False:
                #: let us fall back gracefully
                await event.reply(
                    f"{BOT_META_INFO_PREFIX}Admin access required for custom log count."
                )
                return

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

        count_to_use = custom_count if custom_count is not None else LOG_COUNT_LIMIT
        logs_to_send = log_files[:count_to_use]

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


async def set_deepseek_key_handler(event):
    """Delegates /setdeepseekkey command logic to the shared module."""
    await llm_db.handle_set_key_command(event, "deepseek")


async def set_mistral_key_handler(event):
    """Delegates /setmistralkey command logic to the shared module."""
    await llm_db.handle_set_key_command(event, "mistral")


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


async def set_model_here_handler(event):
    """Sets a model for the current chat only, now with an interactive flow."""
    is_bot_admin = await util.isAdmin(event)
    is_group_admin = await util.is_group_admin(event)

    if not event.is_private and not (is_bot_admin or is_group_admin):
        await event.reply(
            f"{BOT_META_INFO_PREFIX}You must be a group admin or bot admin to use this command in a group."
        )
        return

    model_match = event.pattern_match.group(1)
    chat_id = event.chat_id
    user_id = event.sender_id
    current_chat_model = chat_manager.get_model(chat_id)

    if model_match and model_match.strip():
        model = model_match.strip()
        chat_manager.set_model(chat_id, model)
        cancel_input_flow(user_id)
        await event.reply(
            f"{BOT_META_INFO_PREFIX}✅ This chat's model has been set to: `{model}`"
        )
    else:
        await bot_util.present_options(
            event,
            title="Set Chat Model",
            options=CHAT_MODEL_OPTIONS,
            current_value=current_chat_model or "",
            callback_prefix="chatmodel_",
            awaiting_key="chatmodel_selection",
            n_cols=2,
        )
        # Also prompt for custom model
        await event.reply(
            f"{BOT_META_INFO_PREFIX}Or, send a custom model ID below."
            "\n(Type `cancel` or `not set` to stop/clear.)"
        )
        AWAITING_INPUT_FROM_USERS[user_id] = {"type": "chatmodel", "chat_id": chat_id}


async def get_model_here_handler(event):
    """Gets and displays the effective model for the current chat."""
    user_id = event.sender_id
    chat_id = event.chat_id
    effective_model, _ = _get_effective_model_and_service(chat_id, user_id)
    chat_model = chat_manager.get_model(chat_id)

    if chat_model:
        await event.reply(
            f"{BOT_META_INFO_PREFIX}**Current chat model:** `{effective_model}`",
            parse_mode="md",
        )
    else:
        user_prefs = user_manager.get_prefs(user_id)
        source_text = "your personal model" if user_prefs.model else "the default model"
        await event.reply(
            f"{BOT_META_INFO_PREFIX}This chat has no custom model set. Using {source_text}: `{effective_model}`",
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
    options_for_menu = _build_context_mode_menu_options(event.chat_id, event.sender_id)
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


async def set_last_n_handler(event):
    """Sets a user's personal default for 'Last N Messages' limit."""
    user_id = event.sender_id
    limit_match = event.pattern_match.group(1)

    if not limit_match or not limit_match.strip():
        await event.reply(
            f"{BOT_META_INFO_PREFIX}**Usage:** `/setLastN <number>` or `/setLastN reset`"
        )
        return

    limit_str = limit_match.strip()
    if limit_str.lower() in RESET_KEYWORDS:
        user_manager.set_last_n_messages_limit(user_id, None)
        await event.reply(
            f"{BOT_META_INFO_PREFIX}✅ Your personal 'Last N' limit has been reset. "
            f"The global default of `{LAST_N_MESSAGES_LIMIT}` will be used."
        )
        return

    try:
        limit = int(limit_str)
        if not (1 < limit <= LAST_N_MAX):
            raise ValueError("Limit out of range.")
        user_manager.set_last_n_messages_limit(user_id, limit)
        await event.reply(
            f"{BOT_META_INFO_PREFIX}✅ Your personal default for 'Last N Messages' is now **{limit}**."
        )
    except ValueError:
        await event.reply(
            f"{BOT_META_INFO_PREFIX}❌ Please provide a valid number between 2 and {LAST_N_MAX}."
        )


async def get_last_n_handler(event):
    """Gets the user's personal default for 'Last N Messages' limit."""
    user_id = event.sender_id
    user_limit = user_manager.get_last_n_messages_limit(user_id)

    if user_limit is not None:
        await event.reply(
            f"{BOT_META_INFO_PREFIX}Your personal 'Last N Messages' limit is set to **{user_limit}**."
        )
    else:
        await event.reply(
            f"{BOT_META_INFO_PREFIX}You have not set a personal 'Last N Messages' limit. "
            f"The global default is **{LAST_N_MESSAGES_LIMIT}**."
        )


async def set_last_n_here_handler(event):
    """Sets a chat-specific limit for the 'Last N Messages' context mode."""
    is_bot_admin = await util.isAdmin(event)
    is_group_admin = await util.is_group_admin(event)

    if not event.is_private and not (is_bot_admin or is_group_admin):
        await event.reply(
            f"{BOT_META_INFO_PREFIX}You must be a group admin or bot admin to use this command in a group."
        )
        return

    limit_match = event.pattern_match.group(1)
    if not limit_match or not limit_match.strip():
        await event.reply(
            f"{BOT_META_INFO_PREFIX}**Usage:** `/setLastNHere <number>` or `/setLastNHere reset`"
        )
        return

    limit_str = limit_match.strip()
    if limit_str.lower() in RESET_KEYWORDS:
        chat_manager.set_last_n_messages_limit(event.chat_id, None)
        await event.reply(
            f"{BOT_META_INFO_PREFIX}✅ Chat-specific 'Last N' limit has been reset. "
            f"Your personal or the global default will be used."
        )
        return

    try:
        limit = int(limit_str)
        if not (1 < limit <= LAST_N_MAX):
            raise ValueError("Limit out of range.")
        chat_manager.set_last_n_messages_limit(event.chat_id, limit)
        await event.reply(
            f"{BOT_META_INFO_PREFIX}✅ This chat will now use the last **{limit}** messages for context when in 'Last N' mode."
        )
    except ValueError:
        await event.reply(
            f"{BOT_META_INFO_PREFIX}❌ Please provide a valid number between 2 and {LAST_N_MAX}."
        )


async def get_last_n_here_handler(event):
    """Gets the chat-specific limit for the 'Last N Messages' context mode."""
    effective_limit = _get_effective_last_n_limit(event.chat_id, event.sender_id)
    chat_limit = chat_manager.get_last_n_messages_limit(event.chat_id)

    if chat_limit is not None:
        await event.reply(
            f"{BOT_META_INFO_PREFIX}The 'Last N Messages' limit for this chat is set to **{chat_limit}**."
        )
    else:
        user_limit = user_manager.get_last_n_messages_limit(event.sender_id)
        if user_limit is not None:
            source = f"your personal default of **{user_limit}**"
        else:
            source = f"the global default of **{LAST_N_MESSAGES_LIMIT}**"

        await event.reply(
            f"{BOT_META_INFO_PREFIX}This chat has no specific 'Last N' limit and uses {source}."
        )


# --- New Feature Handlers ---


async def context_mode_handler(event):
    prefs = user_manager.get_prefs(event.sender_id)

    options = _build_context_mode_menu_options(event.chat_id, event.sender_id)

    await bot_util.present_options(
        event,
        title="Set Private Chat Context Mode",
        options=options,
        current_value=prefs.context_mode,
        callback_prefix="context_",
        awaiting_key="context_mode_selection",
        n_cols=1,
    )


async def group_context_mode_handler(event):
    prefs = user_manager.get_prefs(event.sender_id)

    options = _build_context_mode_menu_options(event.chat_id, event.sender_id)

    await bot_util.present_options(
        event,
        title="Set Group Chat Context Mode",
        options=options,
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

    elif data_str.startswith("chatmodel_"):
        model_id = bot_util.unsanitize_callback_data(data_str.split("_", 1)[1])
        chat_id = event.chat_id
        # Handle "Not Set" option (empty string means remove chat-specific model)
        if model_id == "":
            chat_manager.set_model(chat_id, None)
            feedback_msg = "Chat model cleared (using personal default)"
        else:
            chat_manager.set_model(chat_id, model_id)
            feedback_msg = f"Chat model set to {MODEL_CHOICES[model_id]}"
        cancel_input_flow(user_id)  # Cancel the custom input flow

        # Update the menu to show the new selection
        current_chat_model = chat_manager.get_model(chat_id)

        buttons = [
            KeyboardButtonCallback(
                f"✅ {name}" if key == (current_chat_model or "") else name,
                data=f"chatmodel_{bot_util.sanitize_callback_data(key)}",
            )
            for key, name in CHAT_MODEL_OPTIONS.items()
        ]
        await event.edit(buttons=util.build_menu(buttons, n_cols=2))
        await event.answer(feedback_msg)

    elif data_str.startswith("think_"):
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

        options = _build_context_mode_menu_options(event.chat_id, event.sender_id)
        buttons = [
            KeyboardButtonCallback(
                f"✅ {name}" if key == prefs.context_mode else name,
                data=f"context_{key}",
            )
            for key, name in options.items()
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

        # Prepare options for the menu, including a "Not Set" option for resetting
        options_for_menu = _build_context_mode_menu_options(
            event.chat_id, event.sender_id
        )
        options_for_menu["not_set"] = NOT_SET_HERE_DISPLAY_NAME

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
        user_last_n_limit = prefs.last_n_messages_limit or LAST_N_MESSAGES_LIMIT
        options = CONTEXT_MODE_NAMES.copy()
        if "last_N" in options:
            options["last_N"] += f" (Limit: {user_last_n_limit})"
        buttons = [
            KeyboardButtonCallback(
                f"✅ {name}" if key == prefs.group_context_mode else name,
                data=f"groupcontext_{key}",
            )
            for key, name in options.items()
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

    if text.lower() in CANCEL_KEYWORDS:
        cancel_input_flow(user_id)
        await event.reply(f"{BOT_META_INFO_PREFIX}Process cancelled.")
        return

    # Handle simple text inputs
    if input_type == "model":
        user_manager.set_model(user_id, text)
        await event.reply(f"{BOT_META_INFO_PREFIX}✅ Model updated to: `{text}`")
    elif input_type == "chatmodel":
        chat_id = flow_data.get("chat_id", event.chat_id)
        if text.lower() in RESET_KEYWORDS:
            chat_manager.set_model(chat_id, None)
            await event.reply(
                f"{BOT_META_INFO_PREFIX}✅ This chat's model cleared (using personal default)"
            )
        else:
            chat_manager.set_model(chat_id, text)
            await event.reply(
                f"{BOT_META_INFO_PREFIX}✅ This chat's model updated to: `{text}`"
            )
    elif input_type == "system_prompt":
        if text.lower() in RESET_KEYWORDS:
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

        # Create client using the shared helper function
        client = llm_util.create_genai_client(
            api_key=api_key, user_id=user_id, proxy_p=True
        )
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
            await event.reply(
                f"{BOT_META_INFO_PREFIX}❌ API key validation failed: {str(api_error)}"
            )
            return

        await event.reply(
            f"{BOT_META_INFO_PREFIX}🔗 Attempting WebSocket connection..."
        )
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

    if event.text.strip() == ".":
        #: allow single dot for simply bookmarking a message
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

            async with borg.action(event.chat, "audio") as action:
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

        gemini_api = gemini_live_util.GeminiLiveAPI(api_key, user_id=event.sender_id)

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
                        async with borg.action(session.chat_id, "audio") as action:
                            await borg.send_file(
                                session.chat_id,
                                ogg_data,
                                attributes=[],
                                voice_note=True,
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


def get_streaming_delay(model_name: str) -> float:
    """Get streaming delay based on current model name."""
    if any(
        keyword in model_name.lower()
        for keyword in [
            ":free",
        ]
    ):
        return 2.0

    elif any(
        keyword in model_name.lower()
        for keyword in [
            "gemini-2.5-pro",
        ]
    ):
        return 2.0

    return 0.8


async def chat_handler(event):
    """Main handler for all non-command messages in a private chat."""
    user_id = event.sender_id
    chat_id = event.chat_id
    # ic(user_id, chat_id)

    # Clean up any completed tasks from previous requests
    cleanup_completed_tasks(user_id)

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

    # Check for override context mode first
    override_mode = override_chat_context_mode.get(event.chat_id)
    if override_mode:
        if override_mode == "recent":
            return  # Early return - first message handles the rest
        context_mode_to_use = override_mode
    else:
        # Check for chat-specific context mode
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

    # Detect model prefix and process message text
    prefix_result = _detect_and_process_message_prefix(event.text)

    # Audio URL Magic: Check if message contains only a URL pointing to audio
    if (
        AUDIO_URL_MAGIC_P
        and await util.isAdmin(event)
        and not event.file
        and not group_id
    ):

        # Use processed text from prefix detection (in case prefixes were stripped)
        text_to_check = prefix_result.processed_text or event.text
        url = _is_url_only_message(text_to_check)

        # ic(url)
        if url:
            media_info = await common_util.url_audio_p(url)
            if media_info.audio_p:
                # Process the audio URL and return early if successful
                if await _process_audio_url_magic(event, url):
                    return

    # Determine effective model and service (with prefix model override)
    model_in_use, service_needed = _get_effective_model_and_service(
        chat_id, user_id, prefix_model=prefix_result.model
    )
    model_capabilities = get_model_capabilities(model_in_use)
    api_key = llm_db.get_api_key(user_id=user_id, service=service_needed)

    if not api_key:
        await llm_db.request_api_key_message(event, service_needed)
        return

    if group_id:
        bot_util.PROCESSED_GROUP_IDS.add(group_id)

    if event.text and re.match(r"^\.s\b", event.text):
        RECENT_WAIT_TIME = 1
        override_chat_context_mode[event.chat_id] = "recent"
        await asyncio.sleep(RECENT_WAIT_TIME)
        # Pop the recent mode after waiting
        override_chat_context_mode.pop(event.chat_id, None)
        context_mode_to_use = "recent"
        event.message.text = event.text[2:].strip()
        event.text = event.message.text  #: might be redundant

        response_message = await event.reply(
            f"{BOT_META_INFO_PREFIX}**Recent Context Mode:** I'll use only the recent messages to form the conversation context. I have waited {RECENT_WAIT_TIME} second(s) to receive all your messages.\n\nProcessing ... "
        )

    else:
        response_message = await event.reply(f"{BOT_META_INFO_PREFIX}...")

    import tempfile

    temp_dir = Path(tempfile.gettempdir()) / f"temp_llm_chat_{event.id}"
    try:
        temp_dir.mkdir(exist_ok=True)

        if group_id:
            await asyncio.sleep(0.1)  # Allow album messages to arrive

        history_result = await build_conversation_history(
            event,
            context_mode_to_use,
            temp_dir,
            model_capabilities,
            api_key,
            model_in_use,
            is_private=event.is_private,
        )
        messages = history_result.history
        warnings = history_result.warnings

        # ic(messages)
        if not messages:
            unique_warnings = sorted(list(set(warnings)))
            warning_text = "\n".join(f"• {w}" for w in unique_warnings)

            error_message = f"{BOT_META_INFO_PREFIX}I couldn't find any valid text or supported media to process."
            if warning_text:
                error_message += f"\n\n**Notes:**\n{warning_text}"

            await util.edit_message(response_message, error_message, parse_mode="md")
            return

        # --- System Prompt Selection Logic ---
        prompt_info = get_system_prompt_info(event)
        system_prompt_to_use = prompt_info.effective_prompt

        system_message = {"role": "system", "content": system_prompt_to_use}
        # Add context caching for native Gemini models
        if is_native_gemini(model_in_use):
            # Add cache_control to system message
            system_message["cache_control"] = {"type": "ephemeral"}

            # Add cache_control to ALL conversation messages for full context caching
            for message in messages:
                message["cache_control"] = {"type": "ephemeral"}

        messages.insert(0, system_message)

        # --- Construct API call arguments ---
        is_gemini_model_p = is_gemini_model(model_in_use)

        # Image generation models don't support streaming
        # Note: Native Gemini image generation has separate handling with its own streaming
        use_streaming = not model_capabilities.get("image_generation", False)

        api_kwargs = {
            "model": model_in_use,
            "messages": messages,
            "api_key": api_key,
            "stream": use_streaming,
        }

        if prefs.json_mode:
            api_kwargs["response_format"] = {"type": "json_object"}
            if prefs.enabled_tools:
                warnings.append("Tools are disabled (not supported in JSON mode).")

        if is_gemini_model_p:
            api_kwargs["safety_settings"] = SAFETY_SETTINGS
            # Upstream Gemini Limitation: Only enable tools if JSON mode is OFF.
            if prefs.enabled_tools and not prefs.json_mode:
                api_kwargs["tools"] = [{t: {}} for t in prefs.enabled_tools]
            if prefs.thinking and "2.5-pro" not in model_in_use:
                #: 2.5-pro thinks automatically
                api_kwargs["reasoning_effort"] = prefs.thinking
            # Add modalities for image generation models
            if model_capabilities.get("image_generation", False):
                api_kwargs["modalities"] = ["image", "text"]
        else:
            # Add warnings if user has Gemini-specific settings enabled
            if prefs.enabled_tools and WARN_UNAVAILABLE_TOOLS_P:
                warnings.append("Tools are disabled (Gemini-only feature).")
            if prefs.thinking and WARN_UNAVAILABLE_THINKING_P:
                warnings.append("Reasoning effort is disabled (Gemini-only feature).")

        # Make the API call
        response_text = ""
        has_image = False

        # Check if this is a native Gemini image generation model
        if is_native_gemini_image_generation(model_in_use):
            # Use native Gemini API for image generation with streaming
            response_text, has_image = await _handle_native_gemini_image_generation(
                event,
                messages,
                api_key,
                model_in_use,
                response_message,
                model_capabilities,
            )
            finish_reason = (
                None  # Native Gemini image generation doesn't provide finish_reason
            )
        elif use_streaming:
            edit_interval = get_streaming_delay(model_in_use)
            llm_response = await _retry_on_no_response_with_reasons(
                user_id,
                event,
                response_message,
                api_kwargs,
                edit_interval,
                model_capabilities,
                streaming_p=True,
            )
        else:
            llm_response = await _retry_on_no_response_with_reasons(
                user_id,
                event,
                response_message,
                api_kwargs,
                None,
                model_capabilities,
                streaming_p=False,
            )

        response_text = llm_response.text
        finish_reason = llm_response.finish_reason
        has_image = getattr(llm_response, "has_image", False)

        # Final text processing (now handles both success and failure cases)
        final_text = response_text.strip()

        should_warn = WARN_UNSUPPORTED_TO_USER_P == "always" or (
            WARN_UNSUPPORTED_TO_USER_P == "private_only"
            and getattr(event, "is_private", False)
        )

        if should_warn and warnings:
            unique_warnings = sorted(list(set(warnings)))
            warning_text = f"\n\n{BOT_META_INFO_LINE}\n**Note:**\n" + "\n".join(
                f"- {w}" for w in unique_warnings
            )
            final_text += warning_text

        # Only send text message if there's actual content
        if final_text.strip():
            await util.edit_message(
                response_message,
                final_text,
                parse_mode="md",
                link_preview=False,
                file_length_threshold=util.DEFAULT_FILE_LENGTH_THRESHOLD,
            )
        else:
            # If we sent an image but have no text, delete the "..." message
            try:
                await response_message.delete()
            except Exception as e:
                print(f"Error deleting placeholder message: {e}")

        # TTS Integration Hook
        await _handle_tts_response(event, final_text)

        await _log_conversation(event, prefs, model_in_use, messages, final_text)

    except asyncio.CancelledError:
        # print(f"chat_handler: Task was cancelled for event: {event.id}")
        pass

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
