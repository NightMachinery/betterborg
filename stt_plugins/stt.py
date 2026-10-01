from icecream import ic
from uniborg import util
from uniborg import llm_util
from uniborg import llm_db
from uniborg import guest_util
from uniborg import redis_util
from uniborg import tg_compat
from uniborg import tg_format
from uniborg import tg_raw
from uniborg import callback_util
from uniborg import bot_util
from uniborg import stt_models
from uniborg.storage import UserStorage
from uniborg.constants import (
    GEMINI_FLASH_LATEST,
    GEMINI_STT_LATEST,
    STT_FILE_LENGTH_THRESHOLD,
    STT_FILE_ONLY_LENGTH_THRESHOLD,
    GEMINI_STT_ROTATE_KEYS_P,
    ADMIN_ONLY_COMMAND_IGNORED,
    STT_MODELS,
    STT_MODEL_UNAVAILABLE_SECONDS,
    STT_RETRIES_PER_MODEL,
    STT_RETRY_SLEEP,
    STT_RETRY_MAX_DELAY,
)
import os
import traceback
import llm
import uuid
import asyncio
import json
from datetime import datetime
from pathlib import Path
from telethon import errors, events
from telethon.extensions import markdown as telethon_markdown
from telethon.tl.functions.bots import SetBotCommandsRequest
from telethon.tl.types import (
    BotCommand,
    BotCommandScopeDefault,
    MessageMediaUnsupported,
    MessageMediaWebPage,
)
from pydantic import BaseModel, Field
from typing import List, Optional
from dataclasses import dataclass

# --- Bot Commands Registration ---
BOT_COMMANDS = [
    {"command": "start", "description": "Onboard and set API key"},
    {"command": "help", "description": "Show help and instructions"},
    {"command": "setgeminikey", "description": "Set or update your Gemini API key"},
    {"command": "model", "description": "Choose the transcription model"},
]
KNOWN_STRICT_COMMANDS = {".rot"}  # Undocumented admin-only command; do not add to help.

# --- Pydantic Schema and Prompt for Transcription ---


class TranscriptionResult(BaseModel):
    """The synthesized processing result for ALL provided media files."""

    transcription: str = Field(
        description="The combined verbatim audio transcription or OCR text from all files. Separate content from different files with '---'. Empty if no speech/text is found."
    )
    visual_description: Optional[str] = Field(
        None,
        description="For video(s) ONLY, a combined narrative of the key visual scenes from all videos. MUST be null otherwise.",
    )
    output_type: str = Field(
        "none",
        description="The dominant output type. One of: 'transcript' (if any audio/video), 'ocr' (if only images), or 'none'.",
    )
    error_message: Optional[str] = Field(
        None,
        description="If processing failed entirely, provide a brief error message here.",
    )


# --- New System Prompt for a Single, Synthesized JSON Output ---

TRANSCRIPTION_PROMPT_V6 = r"""
Your mission is to act as a media processing engine. Analyze ALL attached media files and synthesize their content into a SINGLE structured JSON object.

Your entire output MUST be a single, valid JSON object that conforms to the `TranscriptionResult` schema provided.

Follow these synthesis rules:
1.  **`transcription` field**:
    - For all files, combine their meaningful content into this single field.
    - **Content Rules:**
        - **Language:**
            - **Farsi/English:** Transcribe these languages accurately as you hear them.
            - **Other Languages & Translation:** If you are SURE the language is not Farsi or English, you **MUST** provide two things:
                1. The transcription in the original language.
                2. The English translation of that transcription. Separate the translation from the original using clear formatting.
            - **CRITICAL:** You **MUST NOT** translate Farsi transcriptions.
        - **Inclusion:** Transcribe spoken words from audio/video, formatted lyrics from songs (if they are the primary content, not background music), and text from images (OCR).
        - **Exclusion:** Skip filler words (um, uh, er), false starts, repetitions, non-speech sounds (music/effects if speech is present), and discourse markers (well, I mean). Omit words when in doubt.
        - **Formatting:**
            - **Readability:** Use standard punctuation (commas, periods, new lines) and create new paragraphs for different topics or speakers to make the text easy to read. Maintain the spatial structure of the text when doing OCR or transcribing lyrics or dialogue using appropriate whitespace etc. You can use custom markdown markup: `**bold**`, `` `code` ``, or `__italic__` are available. In addition you can send `[links](https://example.com)` and ```` ```pre``` ```` blocks with three backticks.

            - **Emoji Use in Audio/Video Transcription:** use emojis liberally to reflect the body language, tone and emotions of the speakers. Be especially generous when romance is involved! (This directive does NOT apply when doing OCR on images.)

            - **Speaker Identification:** When MULTIPLE people are speaking, each speaker label must be on its own line (after a line break) in bold, followed by a colon, then their dialogue (either on the same line or after a line break, depending on your own judgement). Use the original language of the dialogue for all labels.

              Choose appropriate labels based on context:
              • Person's name: "**María:**", "**سپیده:**"
              • Job title or role: "**Detective:**", "**آرایشگر:**"
              • Generic labels when unsure: "**Speaker 1:**", "**گوینده ۱:**"

              **CRITICAL:** All labels must match the language being spoken, even when guessing names or roles. For Persian/Farsi dialogue, you MUST use matching Persian labels.

            - **Separators:** If you process multiple files, you MUST place `---` on its own line to separate the content from each distinct file.

            - When in doubt, use more whitespace, new lines (line breaks), and new paragraphs. This improves readability and reduces clutter.

            - **Prohibited Content:** You MUST NOT include timestamps, explanatory notes (e.g., "[music playing]"), or any commentary in the transcription text.

2.  **`visual_description` field**:
    - If any of the files are videos, provide a combined, flowing description of their key visual elements in this field. Ignore the visuals for all other file types.
    - If there are NO videos, this field MUST be `null`.

3.  **`output_type` field**:
    - Set to "transcript" if any audio or video files are present.
    - Set to "ocr" if ONLY image files are present.
    - Set to "none" if no text/speech can be extracted from any file.

4.  **Failures**:
    - If all files are unintelligible or empty, set `transcription` to an empty string, `output_type` to "none", and optionally provide a reason in `error_message`.

Do not add any commentary, apologies, or text outside of the final JSON object.
"""

# Set this as the active prompt
TRANSCRIPTION_PROMPT = TRANSCRIPTION_PROMPT_V6
print(f"STT Prompt Loaded:\n\n{TRANSCRIPTION_PROMPT}\n---\n\n")

# Route llm-library Gemini calls through GEMINI_SPECIAL_HTTP_PROXY (no-op if unset).
llm_util.install_llm_gemini_proxy_patch()


# --- Core Transcription Logic ---


def get_effective_gemini_api_key(user_id: int) -> str | None:
    return llm_db.get_gemini_api_key(
        user_id=user_id,
        rotate_keys_p=GEMINI_STT_ROTATE_KEYS_P,
        service="gemini",
        scope="stt",
    )


# Retry config lives in uniborg/constants.py:
#   STT_MODELS            — Auto's ordered model list; first is default, rest are fallbacks
#   STT_RETRIES_PER_MODEL — attempts per model before cycling to the next
#   STT_RETRY_SLEEP       — base sleep between attempts (seconds)
#   STT_RETRY_MAX_DELAY   — upper cap on sleep (seconds)


def _is_retriable_stt_error(exception) -> bool:
    """Whether an error from the transcription call is transient and worth retrying.

    Retries upstream-capacity / rate-limit style errors. Does NOT retry permanent
    failures (proxy restriction, unknown model, bad request, auth).
    """
    # Permanent: never retry (proxy gate and other user-facing permanent failures).
    if isinstance(exception, llm_util.TelegramUserReplyException):
        return False

    text = str(exception).lower()
    retriable_markers = (
        "high demand",
        "overloaded",
        "try again later",
        "temporarily",
        "rate limit",
        "resource_exhausted",
        "quota",
        "429",
        "503",
        "500",
        "unavailable",
        "deadline",
        "timeout",
        "timed out",
        "connection",
        "readerror",
        "remoteprotocolerror",
    )
    return any(marker in text for marker in retriable_markers)


def _is_model_unavailable_error(exception) -> bool:
    """Whether the API refused the model itself for this key.

    Google answers 404 "This model models/gemini-2.5-flash is no longer
    available to new users" once a model is closed to new keys. Retrying the
    model cannot help, but the next one may work.
    """
    text = str(exception).lower()
    return "no longer available" in text or "is not found for api version" in text


async def _model_unavailable_p(api_key: str, model_name: str) -> bool:
    key = redis_util.model_unavailable_key(redis_util.api_key_hash(api_key), model_name)
    return await redis_util.get_and_renew(key, renew=False) is not None


async def _mark_model_unavailable(api_key: str, model_name: str) -> None:
    key = redis_util.model_unavailable_key(redis_util.api_key_hash(api_key), model_name)
    await redis_util.set_with_expiry(
        key, "1", expire_seconds=STT_MODEL_UNAVAILABLE_SECONDS
    )


async def _show_stt_status(status_message, text: str) -> None:
    try:
        await util.edit_message(
            status_message,
            text,
            parse_mode="md",
            link_preview=False,
        )
    except Exception:
        pass  # Progress edit is best-effort.


@dataclass
class SttAnswer:
    #: The model that answered.
    model_name: str
    #: Its answer: JSON from a media model, plain text from a speech model.
    raw: str
    #: Files a speech model could not hear (images, silent videos).
    skipped: int = 0


async def _transcribe_with_retry(
    *,
    models: List[str],
    attachments,
    api_key,
    status_message,
    italics_marker,
) -> SttAnswer:
    """Transcribes ATTACHMENTS with the first of MODELS that answers.

    Each model gets STT_RETRIES_PER_MODEL attempts on transient errors before
    the next one is tried. Sleeps STT_RETRY_SLEEP seconds between every attempt
    (capped at STT_RETRY_MAX_DELAY). Edits ``status_message`` to show retry
    progress. A user's explicit choice is a list of one, so nothing else is
    tried.

    A model that refuses the API key is skipped at once, and for that key it
    stays skipped for STT_MODEL_UNAVAILABLE_SECONDS. When it was the last
    model, `SttModelRefusedError` says so.

    Raises on the last failure.
    """
    models_to_try = list(models)
    available = [
        name for name in models_to_try if not await _model_unavailable_p(api_key, name)
    ]
    #: When every model refused this key before, try them all again.
    models_to_try = available or models_to_try

    total_attempts = len(models_to_try) * STT_RETRIES_PER_MODEL
    global_attempt = 0
    last_exception = None
    sound = None

    for current_idx, current_model_name in enumerate(models_to_try):
        speech_model = None
        if stt_models.is_speech_model(current_model_name):
            speech_model = stt_models.model_for_id(current_model_name)
        else:
            try:
                current_model = stt_models.load_media_model(current_model_name)
            except Exception as e:
                print(f"STT: could not load model {current_model_name!r}: {e}")
                last_exception = e
                global_attempt += STT_RETRIES_PER_MODEL
                continue
        if speech_model is not None and sound is None:
            try:
                sound = await stt_models.audio_inputs(attachments)
            except stt_models.NoSoundError:
                raise SttRequestError(_no_sound_text(current_model_name)) from None
        next_model_name = (
            models_to_try[current_idx + 1]
            if current_idx + 1 < len(models_to_try)
            else None
        )
        model_label = stt_models.label_for(current_model_name)
        next_label = stt_models.label_for(next_model_name) if next_model_name else None

        for model_attempt in range(1, STT_RETRIES_PER_MODEL + 1):
            global_attempt += 1
            try:
                if speech_model is not None:
                    text = await stt_models.transcribe(
                        speech_model, audio=sound.audio, api_key=api_key
                    )
                    return SttAnswer(
                        model_name=current_model_name, raw=text, skipped=sound.skipped
                    )
                response = await current_model.prompt(
                    prompt=TRANSCRIPTION_PROMPT,
                    attachments=attachments,
                    schema=TranscriptionResult,
                    key=api_key,
                    temperature=0,
                )
                return SttAnswer(
                    model_name=current_model_name, raw=await response.text()
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                last_exception = e
                if _is_model_unavailable_error(e):
                    await _mark_model_unavailable(api_key, current_model_name)
                    if next_model_name is None:
                        raise SttModelRefusedError(
                            f"{model_label} is not available for this API key. "
                            "Choose another model with /model."
                        ) from e
                    #: This model's remaining attempts are skipped.
                    global_attempt += STT_RETRIES_PER_MODEL - model_attempt
                    print(
                        f"STT: {current_model_name} refused this API key "
                        f"({type(e).__name__}: {e}); switching to {next_model_name}."
                    )
                    await _show_stt_status(
                        status_message,
                        f"{italics_marker}{model_label} is not available for this "
                        f"API key; switching to {next_label}…{italics_marker}",
                    )
                    break
                if not _is_retriable_stt_error(e):
                    raise

                is_last = global_attempt >= total_attempts
                if is_last:
                    raise

                delay = min(STT_RETRY_SLEEP, STT_RETRY_MAX_DELAY)
                switching = model_attempt >= STT_RETRIES_PER_MODEL

                print(
                    f"STT: attempt {global_attempt}/{total_attempts} "
                    f"(model={current_model_name}, try={model_attempt}/{STT_RETRIES_PER_MODEL}) "
                    f"failed ({type(e).__name__}: {e}); "
                    + (
                        f"switching to {next_model_name} after {delay:.0f}s."
                        if switching
                        else f"retrying in {delay:.0f}s."
                    )
                )
                if switching:
                    status = (
                        f"{italics_marker}High demand on {model_label} — "
                        f"switching to {next_label} "
                        f"({global_attempt}/{total_attempts}), "
                        f"waiting {delay:.0f}s…{italics_marker}"
                    )
                else:
                    status = (
                        f"{italics_marker}High demand — retrying with {model_label} "
                        f"({global_attempt}/{total_attempts}), "
                        f"waiting {delay:.0f}s…{italics_marker}"
                    )
                await _show_stt_status(status_message, status)
                await asyncio.sleep(delay)

    if last_exception is not None:
        raise last_exception
    raise SttModelLoadError("No STT model could be tried.")


class SttRequestError(Exception):
    """A request that cannot be transcribed; the message is for the user."""


class MissingSttKeyError(SttRequestError):
    pass


class SttModelRefusedError(SttRequestError):
    """The last model to try refused the API key."""


class SttModelLoadError(Exception):
    """Loading the model failed unexpectedly; the cause is chained."""


def _no_sound_text(model_name: str) -> str:
    return (
        f"{stt_models.label_for(model_name)} transcribes speech only, and none of "
        "these files has sound. Choose a Flash model with /model to read images."
    )


# --- Model choice ---
#: Each user's choice from /model: a model id, or `stt_models.AUTO`.

STT_PREFS_PURPOSE = "stt_preferences"
_prefs_storage = None


def _prefs() -> UserStorage:
    #: Made on first use, so importing the plugin creates no directory.
    global _prefs_storage
    if _prefs_storage is None:
        _prefs_storage = UserStorage(purpose=STT_PREFS_PURPOSE)
    return _prefs_storage


def get_model_choice(user_id: int) -> str:
    """USER_ID's model, or Auto when they chose none or one no longer offered."""
    data = _prefs().get(user_id) or {}
    return stt_models.normalize_choice(data.get("model"))


def set_model_choice(user_id: int, choice: str) -> None:
    data = _prefs().get(user_id) or {}
    data["model"] = stt_models.normalize_choice(choice)
    _prefs().set(user_id, data)


@dataclass
class SttJob:
    """A transcription that passed its checks: the key, models and files."""

    #: The models to try, in order: Auto's list, or the user's choice alone.
    models: List[str]
    api_key: str
    attachments: list


@dataclass
class Transcription:
    #: Markdown for the user.
    text: str
    #: What the model returned, for the log.
    raw: str
    #: The model that answered.
    model_name: str = ""


def prepare_stt_job(cwd, *, user_id: int, model_choice: Optional[str] = None) -> SttJob:
    """Checks the key, the model and the files in CWD before any status message.

    MODEL_CHOICE defaults to the user's own (`get_model_choice`).

    Raises `MissingSttKeyError`, `SttRequestError` (with a message for the
    user) or `SttModelLoadError`.
    """
    api_key = get_effective_gemini_api_key(user_id)
    if not api_key:
        raise MissingSttKeyError("No Gemini API key is set.")
    if model_choice is None:
        model_choice = get_model_choice(user_id)
    models = stt_models.models_for_choice(model_choice, auto=STT_MODELS)
    first = models[0]
    if not stt_models.is_speech_model(first):
        try:
            model = stt_models.load_media_model(first)
        except llm.UnknownModelError:
            raise SttRequestError(
                f"Error: '{first}' model not found. Perhaps the relevant LLM plugin has not been installed."
            ) from None
        except Exception as e:
            raise SttModelLoadError(first) from e
        if not getattr(model, "supports_schema", False):
            raise SttRequestError(
                f"Error: The model '{first}' does not support structured output (schemas)."
            )
    attachments = llm_util.create_attachments_from_dir(Path(cwd))
    if not attachments:
        raise SttRequestError("No valid media files found to transcribe.")
    if stt_models.is_speech_model(first) and not any(
        stt_models.has_sound(attachment) for attachment in attachments
    ):
        raise SttRequestError(_no_sound_text(first))
    return SttJob(models=models, api_key=api_key, attachments=attachments)


def format_speech_transcript(
    text: str, *, model_name: str, skipped: int, italics_marker: str
) -> str:
    """A speech model's plain transcript as the message the user gets."""
    parts = [text.strip() or f"{italics_marker}[No speech detected]{italics_marker}"]
    if skipped:
        files = f"{skipped} files without sound were"
        if skipped == 1:
            files = "1 file without sound was"
        parts.append(
            f"{italics_marker}({stt_models.label_for(model_name)} hears speech "
            f"only, so {files} skipped.){italics_marker}"
        )
    return "\n\n".join(parts)


def format_transcription(json_response_text: str, *, italics_marker: str) -> str:
    """The model's JSON answer as the message the user gets."""
    final_output_message = ""
    try:
        clean_json_text = (
            json_response_text.strip()
            .removeprefix("```json")
            .removesuffix("```")
            .strip()
        )
        # The entire data blob is our result object
        result_data = json.loads(clean_json_text)
        result = TranscriptionResult.model_validate(result_data)

        output_parts = []
        if result.transcription:
            # output_parts.append(f"**Transcription:**\n{result.transcription}")
            output_parts.append(f"{result.transcription}")

        if result.visual_description:
            output_parts.append(f"\n**Visuals:**\n{result.visual_description}")

        if not output_parts:
            message = result.error_message or "[No speech or text detected]"
            output_parts.append(f"{italics_marker}{message}{italics_marker}")

        final_output_message = "\n\n".join(output_parts)

    except (json.JSONDecodeError, Exception) as parse_error:
        print(f"Error parsing model's JSON response: {parse_error}")
        final_output_message = f"**Could not parse structured response, showing raw output:**\n\n```json\n{json_response_text}\n```"

    return (
        final_output_message
        or f"{italics_marker}No content was generated.{italics_marker}"
    )


async def run_stt_job(
    job: SttJob, *, user_id: int, status_message, italics_marker: str = "__"
) -> Transcription:
    """Transcribes JOB, showing retry progress on STATUS_MESSAGE."""
    # Route this request's Gemini traffic through GEMINI_SPECIAL_HTTP_PROXY if configured.
    # Honors the admin-only gate (may raise ProxyRestrictedException for blocked users).
    proxy_url, _ = llm_util.get_proxy_config_or_error(user_id)
    proxy_token = llm_util.set_llm_gemini_proxy(proxy_url)
    try:
        # Transcribe, cycling through fallback models on transient upstream errors.
        answer = await _transcribe_with_retry(
            models=job.models,
            attachments=job.attachments,
            api_key=job.api_key,
            status_message=status_message,
            italics_marker=italics_marker,
        )
    finally:
        llm_util.reset_llm_gemini_proxy(proxy_token)
    if stt_models.is_speech_model(answer.model_name):
        text = format_speech_transcript(
            answer.raw,
            model_name=answer.model_name,
            skipped=answer.skipped,
            italics_marker=italics_marker,
        )
    else:
        text = format_transcription(answer.raw, italics_marker=italics_marker)
    return Transcription(text=text, raw=answer.raw, model_name=answer.model_name)


def _log_transcription(event, *, model_name: str, raw: str) -> None:
    try:
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        unique_id = str(uuid.uuid4())
        log_filename = f"{timestamp}_{unique_id}.txt"

        user = event.sender
        user_id = user.id
        first_name = user.first_name or ""
        last_name = user.last_name or ""
        username = user.username or "N/A"
        full_name = f"{first_name} {last_name}".strip()

        log_content = (
            f"Date: {timestamp}\n"
            f"User ID: {user_id}\n"
            f"Name: {full_name}\n"
            f"Username: @{username}\n"
            f"model: {model_name}\n"
        )
        print(f"\n{log_content}\n---")

        log_content += f"--- Transcription ---\n" f"{raw}"

        log_dir = os.path.expanduser(f"~/.borg/stt/log/{user_id}")
        os.makedirs(log_dir, exist_ok=True)
        log_file_path = os.path.join(log_dir, log_filename)

        with open(log_file_path, "w", encoding="utf-8") as f:
            f.write(log_content)

    except Exception as log_e:
        print(f"Failed to write transcription log: {log_e}")
        print(traceback.format_exc())


async def llm_stt(*, cwd, event, model_choice=None, log=True):
    """
    Performs speech-to-text on media with the sender's model (`/model`), or
    MODEL_CHOICE when given.
    """
    parse_mode = "md"
    italics_marker = "__"

    try:
        job = prepare_stt_job(cwd, user_id=event.sender_id, model_choice=model_choice)
    except MissingSttKeyError:
        await llm_db.request_api_key_message(event, "gemini")
        return
    except SttRequestError as e:
        await event.reply(str(e))
        return
    except SttModelLoadError as e:
        await llm_util.handle_llm_error(
            event=event,
            exception=e.__cause__,
            base_error_message="An unexpected error occurred while loading the model.",
            error_id_p=True,
        )
        return

    status_message = await event.reply("Transcribing...")

    try:
        transcription = await run_stt_job(
            job,
            user_id=event.sender_id,
            status_message=status_message,
            italics_marker=italics_marker,
        )
        await util.edit_message(
            status_message,
            transcription.text,
            reply_to=event.message,
            link_preview=False,
            parse_mode=parse_mode,
            file_name_mode="llm",
            send_file_mode=util.SendFileMode.ALSO_IF_LESS_THAN,
            file_length_threshold=STT_FILE_LENGTH_THRESHOLD,
            file_only_threshold=STT_FILE_ONLY_LENGTH_THRESHOLD,
            api_keys={
                "gemini": job.api_key,
            },
            #: The transcript must survive a status message it can no longer edit.
            send_new_on_head_failure=True,
        )

        if log:
            _log_transcription(
                event, model_name=transcription.model_name, raw=transcription.raw
            )

    except SttRequestError as e:
        await _show_stt_status(status_message, str(e))
    except Exception as e:
        await llm_util.handle_llm_error(
            event=event,
            exception=e,
            response_message=status_message,
            service="gemini",
            base_error_message="An error occurred during the API call.",
            error_id_p=True,
        )


# --- Bot Command Setup ---


async def set_bot_menu_commands():
    """
    Sets the bot's command menu in Telegram's UI.
    This should be called once after the client has started.
    """
    print("STT: setting bot commands ...")
    try:
        await asyncio.sleep(5)
        await borg(
            SetBotCommandsRequest(
                scope=BotCommandScopeDefault(),
                lang_code="en",
                commands=[
                    BotCommand(c["command"], c["description"]) for c in BOT_COMMANDS
                ],
            )
        )
        print("STT: Bot command menu has been updated.")
    except Exception as e:
        print(f"STT: Failed to set bot commands: {e}")


# --- Telethon Event Handlers ---

PROCESSED_GROUP_IDS = set()


@borg.on(events.NewMessage(pattern="/start", func=lambda e: e.is_private))
async def start_handler(event):
    """Handles the /start command to onboard new users."""
    user_id = event.sender_id
    if llm_db.is_awaiting_key(user_id):
        llm_db.cancel_key_flow(user_id)
    if get_effective_gemini_api_key(user_id):
        await event.reply(
            "Welcome back! Your Gemini API key is already configured. You can send me media files to transcribe."
        )
    else:
        await llm_db.request_api_key_message(event, "gemini")


@borg.on(events.NewMessage(pattern="/help"))
async def help_handler(event):
    """Provides help information."""
    if llm_db.is_awaiting_key(event.sender_id):
        llm_db.cancel_key_flow(event.sender_id)
        await event.reply("API key setup cancelled.")

    help_text = """
**Hello! I am a transcription bot powered by Google's Gemini.**

Here's how to use me:

1.  **Get a Gemini API Key:**
    You need a free API key to use my services. Get one from Google AI Studio:
    ➡️ **https://aistudio.google.com/app/apikey**

2.  **Set Your API Key:**
    Use the /setGeminiKey command to save your key.
    - Provide the key directly: `/setGeminiKey YOUR_API_KEY`
    - Or, just type /setGeminiKey and I will guide you.
3.  **Transcribe Media:**
    Simply send any audio file, voice message, or video. If you send multiple files as an album, I will process them in a single request.
4.  **In Other Chats:**
    Reply to a voice note, audio, video or image in any chat, even one I am not in, with a mention of me, and I post the transcript there. It uses your API key, and everyone in that chat sees it.
5.  **Choose a Model:**
    /model picks the Gemini model. Auto (the default) falls back to another model when one is busy or unavailable for your key; any other choice uses that model alone. 3.5 Transcribe is Google's dedicated speech-to-text model: audio only, plain text.
**Available Commands:**
- `/start`: Onboard and set up your API key.
- `/help`: Shows this help message.
- `/setGeminiKey [API_KEY]`: Sets or updates your Gemini API key.
- `/model`: Chooses the transcription model.
"""
    await event.reply(help_text, link_preview=False)


@borg.on(
    events.NewMessage(func=lambda e: e.text and e.text.strip() in KNOWN_STRICT_COMMANDS)
)
async def rotate_keys_handler(event):
    """Toggle Gemini API key rotation (admin-only, undocumented)."""
    user_id = event.sender_id
    if not llm_db.user_gemini_rotate_keys_p(
        user_id,
        rotate_keys_p=GEMINI_STT_ROTATE_KEYS_P,
        require_enabled_p=False,
        require_global_p=False,
        scope="stt",
    ):
        await event.reply(ADMIN_ONLY_COMMAND_IGNORED)
        return
    enabled = llm_db.toggle_gemini_rotate_keys_enabled(user_id=user_id, scope="stt")
    state = "enabled" if enabled else "disabled"
    await event.reply(f"Gemini key rotation {state}.")


@borg.on(events.NewMessage(pattern=llm_db.gemini_api_key_command_pattern()))
async def set_key_handler(event):
    """Delegates /setgeminikey command logic to the shared module."""
    await llm_db.handle_set_key_command(event, "gemini")


MODEL_CALLBACK_PREFIX = "sttmodel_"
MODEL_MENU_COLUMNS = 2


def _model_menu_title() -> str:
    auto = ", then ".join(stt_models.label_for(name) for name in STT_MODELS)
    return (
        "**Choose the transcription model**\n\n"
        f"Auto tries {auto}, and skips a model your API key cannot use. "
        "Any other choice uses that model alone.\n\n"
        "3.5 Transcribe is Google's speech-to-text model: it hears audio "
        "only, and writes plain text without speaker labels or emoji."
    )


@borg.on(events.NewMessage(pattern=r"(?i)^/model(?:@\w+)?\s*$"))
async def model_handler(event):
    """Presents the transcription model menu."""
    choice = get_model_choice(event.sender_id)
    await bot_util.present_options(
        event,
        title=_model_menu_title(),
        options=stt_models.menu_options(),
        current_value=stt_models.slug_for_choice(choice),
        callback_prefix=MODEL_CALLBACK_PREFIX,
        awaiting_key="stt_model_selection",
        n_cols=MODEL_MENU_COLUMNS,
        is_bot=True,
    )


@borg.on(events.CallbackQuery(pattern=MODEL_CALLBACK_PREFIX.encode()))
@callback_util.hold_bare_answers
async def model_callback_handler(event):
    """Saves the model the user pressed, for the presser."""
    slug = bot_util.unsanitize_callback_data(
        event.data.decode("utf-8").removeprefix(MODEL_CALLBACK_PREFIX)
    )
    if slug == stt_models.AUTO:
        choice, label = stt_models.AUTO, stt_models.AUTO_LABEL
    else:
        model = stt_models.model_for_slug(slug)
        if model is None:
            await event.answer(
                "That model is no longer offered. Send /model again.", alert=True
            )
            return
        choice, label = model.model_id, model.label
    set_model_choice(event.sender_id, choice)
    await event.answer(f"Transcription model: {label}")
    buttons = bot_util.option_buttons(
        stt_models.menu_options(),
        current_value=slug,
        callback_prefix=MODEL_CALLBACK_PREFIX,
    )
    try:
        await event.edit(buttons=util.build_menu(buttons, n_cols=MODEL_MENU_COLUMNS))
    except Exception:
        pass  # The menu is unchanged, or too old to edit.


@borg.on(
    events.NewMessage(
        func=lambda e: e.is_private
        and llm_db.is_awaiting_key(e.sender_id)
        and not e.text.startswith("/")
    )
)
async def key_submission_handler(event):
    """Delegates plain-text key submission logic to the shared module."""
    await llm_db.handle_key_submission(event)


#: Media with nothing to transcribe. On layer 224 a rich message arrives as
#: empty text plus `MessageMediaUnsupported`, and a link preview would have its
#: preview photo downloaded and OCR'd.
NON_TRANSCRIBABLE_MEDIA_TYPES = (MessageMediaUnsupported, MessageMediaWebPage)


def is_transcribable_media(message) -> bool:
    return message.media is not None and not isinstance(
        message.media, NON_TRANSCRIBABLE_MEDIA_TYPES
    )


def is_transcribable_media_event(event) -> bool:
    """Whether EVENT carries media `media_handler` should transcribe."""
    return (
        is_transcribable_media(event)
        and bool(event.sender)
        and not guest_util.is_guest_answer(getattr(event, "message", None))
    )


@borg.on(events.NewMessage(func=is_transcribable_media_event))
async def media_handler(event):
    """
    Handles incoming messages with media. If the user is being prompted for an
    API key, this will cancel the prompt and attempt to process the media.
    """
    user_id = event.sender_id

    # If user sends media while being prompted for a key, cancel the flow.
    if llm_db.is_awaiting_key(user_id):
        llm_db.cancel_key_flow(user_id)
        await event.reply("API key setup cancelled. Processing your media instead...")

    group_id = event.grouped_id
    if group_id:
        if group_id in PROCESSED_GROUP_IDS:
            return  # Already processing this group

        PROCESSED_GROUP_IDS.add(group_id)
        try:
            # util.run_and_upload is assumed to handle downloading files from the event
            # into a temporary directory `cwd` and passing it to the awaited function.
            await util.run_and_upload(event=event, to_await=llm_stt)
        finally:
            await asyncio.sleep(
                5
            )  # Give some grace time for all messages to be processed
            PROCESSED_GROUP_IDS.remove(group_id)
    else:
        await util.run_and_upload(event=event, to_await=llm_stt)


# --- Guest mode ---
#: Transcripts of media that a mention replies to, in chats the bot is not in.
#: The protocol and its safety rules are in docs/guest_mode.md.

GUEST_MAX_AGE_SECONDS = 120
GUEST_MAX_CALLS_PER_HOUR = 30
GUEST_CLASSIC_LIMIT_UNITS = 4096
#: A rich message allows 32768 UTF-8 characters; counting bytes is the safe
#: reading.
GUEST_RICH_LIMIT_BYTES = 32000
GUEST_TRUNCATED_NOTE = (
    "\n\n_(Truncated; send me the media privately for the whole transcript.)_"
)
GUEST_PLACEHOLDER = "🎙 Transcribing…"
GUEST_NO_MEDIA_TEXT = (
    "No voice note, audio, video or image in your message or the one you " "replied to."
)
GUEST_INVITE_TEXT = (
    "To use me here, start me in a private chat and set a Gemini API key "
    "first (it is free)."
)

_guest_claims = guest_util.QueryClaims(
    backend=guest_util.redis_claim_backend(redis_util.get_redis)
)
_guest_limiter = guest_util.CallLimiter(
    backend=guest_util.redis_counter_backend(redis_util.get_redis)
)


def _guest_title() -> str:
    #: Required by Telegram but never shown.
    return getattr(borg.me, "first_name", None) or "Transcriber"


async def _guest_note(query, text, *, buttons=None):
    await guest_util.answer_note(
        borg, query, text, title=_guest_title(), buttons=buttons, logger=logger
    )


async def _finalize_guest_transcript(answer, text: str) -> None:
    """Classic Markdown when it fits one message, else rich, else cut short.

    The transcript prompt asks for Telegram Markdown (`__italic__`), which
    rich Markdown reads as bold, so classic is preferred.
    """
    parsed, _entities = telethon_markdown.parse(text)
    if tg_format.utf16_len(parsed) <= GUEST_CLASSIC_LIMIT_UNITS:
        await answer.finalize(text=text, parse_mode="md")
        return
    try:
        await answer.finalize(
            markdown=tg_format.truncate_utf8(
                text, GUEST_RICH_LIMIT_BYTES, suffix=GUEST_TRUNCATED_NOTE
            )
        )
        return
    except errors.FloodWaitError:
        raise
    except errors.RPCError as e:
        logger.warning("Telegram refused a rich transcript (%s); cutting it", e)
    #: The raw Markdown is never shorter than what it renders.
    await answer.finalize(
        text=tg_format.truncate_utf16(
            text, GUEST_CLASSIC_LIMIT_UNITS - 96, suffix=GUEST_TRUNCATED_NOTE
        ),
        parse_mode="md",
    )


async def guest_stt_handler(query):
    """Transcribes the media of a guest mention and its reference, if any.

    Only an explicit mention is a request; a reply to our transcript is not.
    Transcripts of guest chats are not logged.
    """
    username = borg.me.username
    if not (username and guest_util.mentions(query.text, username=username)):
        return
    caller_id = query.caller_id
    if caller_id is None:
        await _guest_note(query, "I can only answer people, not channels.")
        return
    media = [m for m in query.messages if is_transcribable_media(m)]
    if not media:
        await _guest_note(query, GUEST_NO_MEDIA_TEXT)
        return
    event = guest_util.GuestEvent(query)
    if not await util.isAdmin(event) and not await _guest_limiter.allow(
        f"stt:{borg.me.id}:{caller_id}", limit=GUEST_MAX_CALLS_PER_HOUR
    ):
        await _guest_note(
            query,
            f"You have had {GUEST_MAX_CALLS_PER_HOUR} transcripts this hour; "
            "try again later, or send me the media privately.",
        )
        return
    if not get_effective_gemini_api_key(caller_id):
        await _guest_note(
            query,
            GUEST_INVITE_TEXT,
            buttons=[
                [
                    tg_compat.url_button(
                        "Start a private chat",
                        f"https://t.me/{username}?start=guest",
                    )
                ]
            ],
        )
        return

    try:
        inline_id = await tg_raw.answer_guest(
            borg, query_id=query.query_id, title=_guest_title(), text=GUEST_PLACEHOLDER
        )
    except Exception:
        #: Not retried: a second answer could post twice.
        logger.exception("Could not answer guest query %s", query.query_id)
        return

    async with tg_raw.InlineEditor(borg, inline_id) as editor:
        answer = guest_util.GuestAnswerMessage(editor, logger=logger)

        async def transcribe(*, cwd, event):
            try:
                job = prepare_stt_job(cwd, user_id=caller_id)
            except SttRequestError as e:
                await answer.finalize(text=str(e))
                return
            except SttModelLoadError as e:
                raise e.__cause__
            try:
                transcription = await run_stt_job(
                    job, user_id=caller_id, status_message=answer
                )
            except SttRequestError as e:
                await answer.finalize(text=str(e))
                return
            await _finalize_guest_transcript(answer, transcription.text)

        cwd = f"{util.dl_base}{uuid.uuid4()}/"
        try:
            await util.run_and_get(None, transcribe, cwd, messages=media)
        except Exception as e:
            await llm_util.handle_llm_error(
                event=event,
                exception=e,
                response_message=answer,
                service="gemini",
                base_error_message="An error occurred during the API call.",
                error_id_p=True,
            )
        finally:
            await util.remove_potential_file(cwd)


async def register_guest_mode():
    """Answers guest queries; a no-op on a user account or Telethon 1.43."""
    guest_util.register_guest_handler(
        borg,
        guest_stt_handler,
        claims=_guest_claims,
        max_age_seconds=GUEST_MAX_AGE_SECONDS,
        logger=logger,
    )


# --- Initialization ---
# Schedule the command menu setup to run on the bot's event loop upon loading.
borg.loop.create_task(set_bot_menu_commands())
borg.loop.create_task(register_guest_mode())
