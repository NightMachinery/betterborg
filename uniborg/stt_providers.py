"""Provider metadata and the key-only Vertex transport for STT.

No project, region or service-account credentials are needed for Express mode.
HTTP clients are created inside the STT proxy context, including key validation.
"""

import base64
from dataclasses import dataclass

import httpx

from uniborg import stt_models

GEMINI = "gemini"
VERTEX = "vertex"


@dataclass(frozen=True)
class Provider:
    label: str
    key_url: str
    api_base: str
    model_ids: tuple[str, ...] = ()
    auto_models: tuple[str, ...] = ()


PROVIDERS = {
    GEMINI: Provider(
        "Google AI Studio",
        "https://aistudio.google.com/app/apikey",
        "https://generativelanguage.googleapis.com/v1beta/models",
    ),
    VERTEX: Provider(
        "Vertex AI",
        "https://console.cloud.google.com/vertex-ai/studio/overview",
        "https://aiplatform.googleapis.com/v1/publishers/google/models",
        (
            "gemini/gemini-3.5-flash",
            "gemini/gemini-3-flash-preview",
            "gemini/gemini-2.5-flash",
            "gemini/gemini-2.5-flash-lite",
        ),
        (
            "gemini/gemini-3.5-flash",
            "gemini/gemini-3-flash-preview",
            "gemini/gemini-2.5-flash-lite",
        ),
    ),
}


def normalize_provider(provider: str | None) -> str:
    return provider if provider in PROVIDERS else GEMINI


def model_options(provider: str) -> dict[str, str]:
    options = stt_models.menu_options()
    supported = PROVIDERS[provider].model_ids
    if not supported:
        return options
    return {
        slug: label
        for slug, label in options.items()
        if slug == stt_models.AUTO
        or stt_models.model_for_slug(slug).model_id in supported
    }


def normalize_model(provider: str, choice: str | None) -> str:
    choice = stt_models.normalize_choice(choice)
    return (
        choice
        if stt_models.slug_for_choice(choice) in model_options(provider)
        else stt_models.AUTO
    )


class ProviderError(Exception):
    """A safe API error: no request URL, key, or raw Google error body."""

    def __init__(self, provider: str, status_code: int):
        self.status_code = status_code
        label = PROVIDERS[provider].label
        if status_code in (401, 403):
            detail = "The key was rejected or lacks access. Check it with /provider."
        elif status_code == 400:
            detail = "The key or request was rejected. Check the key and media format."
        elif status_code == 404:
            detail = "The model is not found for API version or this key."
        elif status_code == 429:
            detail = "Rate limit or quota exceeded. Try again later."
        elif status_code == 413:
            detail = "The media is too large. Try a smaller file or another provider."
        else:
            detail = "The API request failed. Try again later."
        super().__init__(f"{label} API error {status_code}: {detail}")


async def _post(
    provider: str, model: str, action: str, key: str, body: dict, *, timeout=600
):
    url = f"{PROVIDERS[provider].api_base}/{model.removeprefix('gemini/')}:{action}"
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(url, headers={"x-goog-api-key": key}, json=body)
    if response.status_code != 200:
        raise ProviderError(provider, response.status_code)
    try:
        payload = response.json()
    except ValueError:
        raise RuntimeError(
            f"{PROVIDERS[provider].label} returned an invalid response."
        ) from None
    if not isinstance(payload, dict):
        raise RuntimeError(f"{PROVIDERS[provider].label} returned an invalid response.")
    return payload


async def validate_key(provider: str, key: str) -> None:
    """Check the intended endpoint with token counting, without generating text."""
    payload = await _post(
        provider,
        "gemini-2.5-flash-lite",
        "countTokens",
        key,
        {"contents": [{"role": "user", "parts": [{"text": "test"}]}]},
        timeout=30,
    )
    if not isinstance(payload.get("totalTokens"), int):
        raise RuntimeError(
            "The provider could not validate this key. Please try again."
        )


async def transcribe_vertex(
    *, model: str, attachments, key: str, prompt: str, schema
) -> str:
    parts = [{"text": prompt}]
    for attachment in attachments:
        parts.append(
            {
                "inlineData": {
                    "mimeType": attachment.resolve_type() or "application/octet-stream",
                    "data": base64.b64encode(attachment.content_bytes()).decode(
                        "ascii"
                    ),
                }
            }
        )
    payload = await _post(
        VERTEX,
        model,
        "generateContent",
        key,
        {
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {
                "temperature": 0,
                "responseMimeType": "application/json",
                "responseJsonSchema": schema.model_json_schema(),
            },
        },
    )
    text = "".join(
        part.get("text", "")
        for candidate in (payload.get("candidates") or [])[:1]
        for part in (candidate.get("content") or {}).get("parts", [])
        if not part.get("thought")
    )
    if not text.strip():
        raise RuntimeError(
            "Vertex AI returned no transcription. Try another model with /model."
        )
    return text
