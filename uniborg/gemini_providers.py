"""Where native Gemini (`gemini/...`) requests go: Google AI Studio or Vertex AI.

Vertex AI is used in Express mode: an API key alone, no project or region. Its
`generateContent` endpoints take the same request body as AI Studio's, so litellm's
`gemini/` provider reaches it with `api_base=VERTEX_API_BASE`.
"""

from uniborg.constants import GEMINI_FLASH_2_5, GEMINI_FLASH_3

#: Also the llm_db service names of their API keys.
AI_STUDIO = "gemini"
VERTEX = "vertex"

LABELS = {
    AI_STUDIO: "Google AI Studio",
    VERTEX: "Vertex AI",
}

VERTEX_API_BASE = "https://aiplatform.googleapis.com/v1/publishers/google"

#: Chat models Vertex AI serves (checked 2026-10-12). The `-latest` aliases and
#: `gemini-3-pro-preview` exist only on AI Studio.
VERTEX_CHAT_MODELS = (
    GEMINI_FLASH_2_5,
    GEMINI_FLASH_3,
    "gemini/gemini-2.5-flash-lite",
    "gemini/gemini-2.5-pro",
    "gemini/gemini-3.1-pro-preview",
    "gemini/gemini-3.5-flash",
)

#: Titles default to an AI Studio-only alias; this is their Vertex stand-in.
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
    bare = model.removeprefix("gemini/")
    return model[: len(model) - len(bare)] + VERTEX_MODEL_IDS.get(bare, bare)


def vertex_serves_p(model: str) -> bool:
    return (
        model in VERTEX_CHAT_MODELS or model.removeprefix("gemini/") in VERTEX_MODEL_IDS
    )
