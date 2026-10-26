"""Where native Gemini (`gemini/...`) requests go: Google AI Studio or Vertex AI.

Vertex AI is used in Express mode: an API key alone, no project or region. Its
`generateContent` endpoints take the same request body as AI Studio's, so litellm's
`gemini/` provider reaches it with `api_base=VERTEX_API_BASE`.
"""

from uniborg.constants import (
    GEMINI_FLASH_2_5,
    GEMINI_FLASH_3,
    GEMINI_FLASH_LATEST,
    GEMINI_FLASH_LITE_LATEST,
)

#: Also the llm_db service names of their API keys.
AI_STUDIO = "gemini"
VERTEX = "vertex"

LABELS = {
    AI_STUDIO: "Google AI Studio",
    VERTEX: "Vertex AI",
}

VERTEX_API_BASE = "https://aiplatform.googleapis.com/v1/publishers/google"


def _bare(model: str) -> str:
    return model.removeprefix("gemini/")


#: The newest Flash and Flash Lite on Vertex AI when last checked by hand
#: (2026-10-16). `vertex_latest` finds any newer one.
VERTEX_FLASH_BASELINE = "gemini/gemini-3.8-flash"
VERTEX_FLASH_LITE_BASELINE = "gemini/gemini-3.5-flash-lite"

#: What the `-latest` aliases, which Vertex AI lacks, are sent as there.
#: `vertex_latest` updates it in place with the newest models it finds.
VERTEX_LATEST_IDS = {
    GEMINI_FLASH_LATEST: VERTEX_FLASH_BASELINE,
    GEMINI_FLASH_LITE_LATEST: VERTEX_FLASH_LITE_BASELINE,
}

#: Chat models Vertex AI serves (checked 2026-10-16). `gemini-3-pro-preview`
#: exists only on AI Studio, and the `-latest` aliases are sent as
#: `VERTEX_LATEST_IDS` says.
VERTEX_CHAT_MODELS = (
    GEMINI_FLASH_LATEST,
    GEMINI_FLASH_LITE_LATEST,
    GEMINI_FLASH_2_5,
    GEMINI_FLASH_3,
    "gemini/gemini-2.5-flash-lite",
    "gemini/gemini-2.5-pro",
    "gemini/gemini-3.1-pro-preview",
    "gemini/gemini-3.5-flash",
    VERTEX_FLASH_LITE_BASELINE,
    VERTEX_FLASH_BASELINE,
)

#: The Vertex stand-in for a title model Vertex does not serve.
VERTEX_TITLE_MODEL = "gemini/gemini-2.5-flash-lite"


#: AI Studio model ids that Vertex AI serves under another id (checked
#: 2026-10-16): the same TTS and image models, out of preview.
VERTEX_MODEL_IDS = {
    "gemini-2.5-flash-preview-tts": "gemini-2.5-flash-tts",
    "gemini-2.5-pro-preview-tts": "gemini-2.5-pro-tts",
    "gemini-2.5-flash-image-preview": "gemini-2.5-flash-image",
}


def normalize(provider) -> str:
    return provider if provider in LABELS else AI_STUDIO


def vertex_model_id(model: str) -> str:
    """MODEL as Vertex AI names it, keeping any `gemini/` prefix."""
    bare = _bare(model)
    latest = VERTEX_LATEST_IDS.get(f"gemini/{bare}")
    vertex = _bare(latest) if latest else VERTEX_MODEL_IDS.get(bare, bare)
    return model[: len(model) - len(bare)] + vertex


def vertex_serves_p(model: str) -> bool:
    return model in VERTEX_CHAT_MODELS or _bare(model) in VERTEX_MODEL_IDS
