"""The models the STT bot can transcribe with (docs/stt_models.md).

Each user picks one with /model, or Auto: the fallthrough list `STT_MODELS`.
There are two kinds of model:

- A *media model* is a general Gemini model. It gets the bot's transcription
  prompt and a JSON schema, and reads audio, video and images.
- A *speech model* (Gemini 3.5 Transcribe) only turns speech into text. It
  takes audio and nothing else: no system instruction, no schema, and it
  ignores a text prompt. It answers in an `audioTranscription` part, which
  llm-gemini does not read, so `transcribe` calls the API directly.
"""

import asyncio
import base64
from dataclasses import dataclass
from pathlib import Path
import tempfile
from typing import Dict, List, Optional, Sequence

import httpx
import llm

from uniborg.constants import (
    GEMINI_FLASH_2_5,
    GEMINI_FLASH_3,
    GEMINI_FLASH_LATEST,
    GEMINI_FLASH_LITE_LATEST,
)

#: The choice that tries `STT_MODELS` in order.
AUTO = "auto"
AUTO_LABEL = "Auto"

MEDIA = "media"
SPEECH = "speech"


@dataclass(frozen=True)
class SttModel:
    #: The menu's key for it, short enough for callback data.
    slug: str
    #: `gemini/<Gemini model id>`, as llm names it.
    model_id: str
    label: str
    kind: str = MEDIA

    @property
    def gemini_model_id(self) -> str:
        return self.model_id.removeprefix("gemini/")


STT_MODEL_CHOICES = (
    SttModel("flash-latest", GEMINI_FLASH_LATEST, "Flash Latest"),
    SttModel("flash-lite-latest", GEMINI_FLASH_LITE_LATEST, "Flash Lite Latest"),
    SttModel("3.8-flash", "gemini/gemini-3.8-flash", "3.8 Flash"),
    SttModel("3.7-flash", "gemini/gemini-3.7-flash", "3.7 Flash"),
    SttModel("3.6-flash", "gemini/gemini-3.6-flash", "3.6 Flash"),
    SttModel("3.5-flash", "gemini/gemini-3.5-flash", "3.5 Flash"),
    SttModel("3.5-flash-lite", "gemini/gemini-3.5-flash-lite", "3.5 Flash Lite"),
    SttModel("3.1-flash-lite", "gemini/gemini-3.1-flash-lite", "3.1 Flash Lite"),
    SttModel("3-flash", GEMINI_FLASH_3, "3 Flash Preview"),
    SttModel("2.5-flash", GEMINI_FLASH_2_5, "2.5 Flash"),
    SttModel("2.5-flash-lite", "gemini/gemini-2.5-flash-lite", "2.5 Flash Lite"),
    SttModel(
        "3.5-transcribe", "gemini/gemini-3.5-transcribe", "3.5 Transcribe", SPEECH
    ),
)
_BY_SLUG = {model.slug: model for model in STT_MODEL_CHOICES}
_BY_ID = {model.model_id: model for model in STT_MODEL_CHOICES}


def model_for_slug(slug: str) -> Optional[SttModel]:
    return _BY_SLUG.get(slug)


def model_for_id(model_id: Optional[str]) -> Optional[SttModel]:
    return _BY_ID.get(model_id)


def label_for(model_id: str) -> str:
    model = model_for_id(model_id)
    return model.label if model else model_id.split("/")[-1]


def is_speech_model(model_id: str) -> bool:
    model = model_for_id(model_id)
    return model is not None and model.kind == SPEECH


def menu_options() -> Dict[str, str]:
    """{slug: button text}, Auto first."""
    return {AUTO: AUTO_LABEL, **{m.slug: m.label for m in STT_MODEL_CHOICES}}


def normalize_choice(choice: Optional[str]) -> str:
    """CHOICE when the menu still offers it, else Auto."""
    return choice if choice in _BY_ID else AUTO


def slug_for_choice(choice: str) -> str:
    model = model_for_id(choice)
    return model.slug if model else AUTO


def models_for_choice(choice: str, *, auto: Sequence[str]) -> List[str]:
    """The models to try for CHOICE: AUTO's list, or that model alone."""
    choice = normalize_choice(choice)
    if choice == AUTO:
        return list(auto)
    return [choice]


# --- Media models ---

_media_models: Dict[str, object] = {}


def load_media_model(model_id: str):
    """The llm model for MODEL_ID, also when the installed llm-gemini predates it.

    The bot's llm-gemini need not know every Gemini model on the menu, so a
    known one it lacks is built from llm-gemini's own class.
    """
    try:
        return llm.get_async_model(model_id)
    except llm.UnknownModelError:
        model = model_for_id(model_id)
        if model is None or model.kind != MEDIA:
            raise
    built = _media_models.get(model_id)
    if built is None:
        import llm_gemini

        built = llm_gemini.AsyncGeminiPro(model.gemini_model_id, can_schema=True)
        _media_models[model_id] = built
    return built


# --- Speech models ---

GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta"
TRANSCRIBE_TIMEOUT_SECONDS = 600
#: Telegram voice notes are Ogg Opus already; anything else is converted.
PASSTHROUGH_AUDIO_TYPES = {"audio/ogg"}
AUDIO_BITRATE = "48k"


class GeminiApiError(Exception):
    """An error answer from the Gemini API.

    Its text keeps the HTTP status and Google's message, which is what
    `stt._is_retriable_stt_error` and `stt._is_model_unavailable_error` read.
    """

    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        super().__init__(f"Gemini API error {status_code}: {message}")


class NoSoundError(Exception):
    """None of the files has sound a speech model could hear."""


@dataclass
class AudioInput:
    mime_type: str
    data: bytes


@dataclass
class AudioInputs:
    audio: List[AudioInput]
    #: Files without sound: images, and videos without an audio track.
    skipped: int


def _mime_type(attachment) -> str:
    try:
        return attachment.resolve_type() or ""
    except Exception:
        #: puremagic cannot name every file.
        return ""


def has_sound(attachment) -> bool:
    """Whether ATTACHMENT is audio or video, judged by its type."""
    return _mime_type(attachment).startswith(("audio/", "video/"))


async def _to_ogg_opus(source: Path, target: Path) -> bool:
    """Converts SOURCE's audio track to mono Ogg Opus; False when it has none."""
    try:
        process = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-nostdin",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-vn",
            "-ac",
            "1",
            "-c:a",
            "libopus",
            "-b:a",
            AUDIO_BITRATE,
            str(target),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        raise RuntimeError("ffmpeg is not installed, so audio cannot be converted.")
    _, stderr = await process.communicate()
    if process.returncode == 0:
        return True
    error = stderr.decode(errors="replace")
    if "does not contain any stream" in error or "matches no streams" in error:
        return False
    raise RuntimeError(f"ffmpeg could not convert {source.name}: {error.strip()}")


async def audio_inputs(attachments) -> AudioInputs:
    """The sound of ATTACHMENTS, ready for a speech model.

    Ogg audio is sent as it is. Other audio and the sound of videos are
    converted to Ogg Opus. Raises `NoSoundError` when nothing has sound.
    """
    audio = []
    skipped = 0
    with tempfile.TemporaryDirectory(prefix="stt_audio_") as workdir:
        for index, attachment in enumerate(attachments):
            mime_type = _mime_type(attachment)
            if not mime_type.startswith(("audio/", "video/")):
                skipped += 1
                continue
            if mime_type in PASSTHROUGH_AUDIO_TYPES:
                audio.append(AudioInput(mime_type, attachment.content_bytes()))
                continue
            if attachment.path:
                source = Path(attachment.path)
            else:
                source = Path(workdir) / f"{index}.in"
                source.write_bytes(attachment.content_bytes())
            target = Path(workdir) / f"{index}.ogg"
            if not await _to_ogg_opus(source, target):
                skipped += 1
                continue
            audio.append(AudioInput("audio/ogg", target.read_bytes()))
    if not audio:
        raise NoSoundError()
    return AudioInputs(audio=audio, skipped=skipped)


def transcript_text(payload: dict) -> str:
    """The transcript in a generateContent answer of a speech model.

    Several files in one request come back as one text.
    """
    block_reason = (payload.get("promptFeedback") or {}).get("blockReason")
    if block_reason:
        raise GeminiApiError(200, f"the request was blocked ({block_reason})")
    texts = []
    for candidate in (payload.get("candidates") or ())[:1]:
        for part in (candidate.get("content") or {}).get("parts") or ():
            text = (part.get("audioTranscription") or {}).get("text") or part.get(
                "text"
            )
            if text:
                texts.append(text)
    return "\n\n".join(texts)


async def transcribe(model: SttModel, *, audio: List[AudioInput], api_key: str) -> str:
    """The transcript of AUDIO by the speech model MODEL, in one request.

    The client is made here, so a proxy set for this request applies
    (`llm_util.set_llm_gemini_proxy`).
    """
    body = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {
                        "inline_data": {
                            "mime_type": item.mime_type,
                            "data": base64.b64encode(item.data).decode("ascii"),
                        }
                    }
                    for item in audio
                ],
            }
        ]
    }
    url = f"{GEMINI_API_BASE}/models/{model.gemini_model_id}:generateContent"
    async with httpx.AsyncClient(timeout=TRANSCRIBE_TIMEOUT_SECONDS) as client:
        response = await client.post(
            url, headers={"x-goog-api-key": api_key}, json=body
        )
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if response.status_code != 200:
        error = (payload or {}).get("error") if isinstance(payload, dict) else None
        message = (error or {}).get("message") or response.text[:500]
        status = (error or {}).get("status")
        if status:
            message = f"{status}: {message}"
        raise GeminiApiError(response.status_code, message)
    if not isinstance(payload, dict):
        raise GeminiApiError(response.status_code, "the answer was not JSON")
    return transcript_text(payload)
