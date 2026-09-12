import asyncio
import base64
import binascii
import hashlib
import io
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

from urllib.parse import urlparse

import openai
from PIL import Image

from uniborg import util


CODEX_MODEL_PREFIX = "openai-codex/"


@dataclass
class CodexResponse:
    text: str
    finish_reason: Optional[str] = None
    previews_delivered: int = 0
    images_delivered: int = 0

    @property
    def has_image(self) -> bool:
        return bool(self.previews_delivered or self.images_delivered)


@dataclass(frozen=True)
class CodexImage:
    """One validated output; preview indices are zero-based and local to an item."""

    data: bytes
    item_id: str
    preview_index: Optional[int] = None
    file_extension: str = ".png"

    @property
    def is_preview(self) -> bool:
        return self.preview_index is not None


class CodexStreamError(RuntimeError):
    def __init__(self, message: str, response: CodexResponse):
        self.response = response
        super().__init__(
            message
            + (
                " Already-delivered images have been kept; no regeneration was attempted."
                if response.has_image
                else ""
            )
        )


def _field(value, name, default=None):
    return (
        value.get(name, default)
        if isinstance(value, dict)
        else getattr(value, name, default)
    )


def _decode_image(encoded: str) -> tuple[bytes, str]:
    try:
        data = base64.b64decode(encoded, validate=True)
        with Image.open(io.BytesIO(data)) as img:
            extension = {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp"}.get(img.format)
            if extension is None:
                raise ValueError("Unsupported image format")
            img.verify()
        return data, extension
    except (TypeError, ValueError, binascii.Error, OSError, SyntaxError) as exc:
        raise ValueError("Codex returned malformed image data.") from exc


def is_codex_model(model: str) -> bool:
    return bool(model and model.startswith(CODEX_MODEL_PREFIX))


def codex_model_name(model: str) -> str:
    return model.removeprefix(CODEX_MODEL_PREFIX)


def codex_prompt_cache_key(*, model: str, chat_id=None, user_id=None) -> str:
    """Build a stable, non-secret cache routing key for Codex Responses calls.

    ``user_id`` is accepted for backward compatibility with existing callers, but
    cache affinity is intentionally scoped only by model and chat.
    """
    raw_key = f"betterborg:codex:{codex_model_name(model)}:chat:{chat_id}"
    return "bb-codex-" + hashlib.sha256(raw_key.encode("utf-8")).hexdigest()[:32]


def _get_codex_auth():
    try:
        from llm_openai_via_codex import CODEX_BASE_URL, borrow_codex_key
    except ImportError as e:
        raise RuntimeError(
            "Missing dependency: llm-openai-via-codex. Install requirements.txt in "
            "the bot runtime environment."
        ) from e

    token, account_id = borrow_codex_key()
    headers = {}
    if account_id:
        headers["ChatGPT-Account-ID"] = account_id
    return token, headers, CODEX_BASE_URL


def _is_image_data_url(url: str) -> bool:
    """Return True when a URL is an inline data URL with an image MIME type."""
    if not isinstance(url, str) or not url.startswith("data:"):
        return False

    metadata = url.split(",", 1)[0]
    mime_type = metadata.removeprefix("data:").split(";", 1)[0].lower()
    return mime_type.startswith("image/")


def _is_supported_codex_image_url(url: str) -> bool:
    """Codex image inputs support image data URLs and remote URLs.

    Betterborg currently produces inline data URLs for Telegram attachments;
    validate those strictly so video/audio/file parts cannot be forwarded as
    OpenAI Responses `input_image` content. Remote URLs are preserved for any
    future callers that pass them through.
    """
    if not isinstance(url, str) or not url:
        return False
    if url.startswith("data:"):
        return _is_image_data_url(url)

    scheme = urlparse(url).scheme.lower()
    return scheme in {"http", "https"}


async def _create_async_client() -> openai.AsyncOpenAI:
    token, headers, base_url = await asyncio.to_thread(_get_codex_auth)
    return openai.AsyncOpenAI(
        api_key=token,
        base_url=base_url,
        default_headers=headers,
    )


def _content_part_to_codex(part: dict) -> Optional[dict]:
    part_type = part.get("type")
    if part_type == "text":
        text = part.get("text") or ""
        return {"type": "input_text", "text": text} if text else None

    if part_type == "image_url":
        image_url = (part.get("image_url") or {}).get("url")
        if _is_supported_codex_image_url(image_url):
            return {
                "type": "input_image",
                "image_url": image_url,
                "detail": "low",
            }

    return None


def prepare_codex_response_kwargs(
    *,
    model: str,
    instructions: str,
    input_messages: list[dict],
    reasoning_effort: Optional[str] = None,
    tools: Optional[list[dict]] = None,
    prompt_cache_key: Optional[str] = None,
) -> dict:
    kwargs = {
        "model": codex_model_name(model),
        "input": input_messages,
        "store": False,
        "stream": True,
        "instructions": instructions or "You are a helpful assistant.",
    }
    if reasoning_effort:
        kwargs["reasoning"] = {"effort": reasoning_effort}
    if prompt_cache_key:
        kwargs["prompt_cache_key"] = prompt_cache_key
    if tools:
        kwargs["tools"] = tools
    return kwargs


def messages_to_codex(messages: list[dict]) -> tuple[str, list[dict]]:
    instructions = []
    codex_messages = []

    for message in messages:
        role = message.get("role")
        content = message.get("content")

        if role == "system":
            if content:
                instructions.append(str(content))
            continue

        if role not in ("user", "assistant"):
            continue

        if isinstance(content, str):
            if content:
                codex_messages.append({"role": role, "content": content})
            continue

        if isinstance(content, list):
            converted_parts = []
            for part in content:
                if not isinstance(part, dict):
                    continue
                converted_part = _content_part_to_codex(part)
                if converted_part:
                    converted_parts.append(converted_part)

            if converted_parts:
                if role == "assistant":
                    text = "\n".join(
                        part["text"]
                        for part in converted_parts
                        if part.get("type") == "input_text" and part.get("text")
                    )
                    if text:
                        codex_messages.append({"role": role, "content": text})
                else:
                    codex_messages.append({"role": role, "content": converted_parts})

    return "\n\n".join(instructions), codex_messages


async def stream_codex_response(
    *,
    event,
    response_message,
    model: str,
    messages: list[dict],
    reasoning_effort: Optional[str] = None,
    tools: Optional[list[dict]] = None,
    edit_interval: float = 0.8,
    prompt_cache_key: Optional[str] = None,
    image_callback: Optional[Callable[[CodexImage], Awaitable[None]]] = None,
) -> CodexResponse:
    """Deliver images serially; callback success means Telegram accepted the image.

    Callback failures stop this request without retrying generation. Cancellation
    propagates after resource cleanup; already-delivered images remain intact.
    """
    instructions, input_messages = messages_to_codex(messages)
    kwargs = prepare_codex_response_kwargs(
        model=model,
        instructions=instructions,
        input_messages=input_messages,
        reasoning_effort=reasoning_effort,
        tools=tools,
        prompt_cache_key=prompt_cache_key,
    )
    result = CodexResponse(text="")
    previews_seen = set()
    images_seen = set()
    # Track individual text parts so terminal events only fill missing parts.
    text_parts = {}
    last_edit_time = asyncio.get_running_loop().time()
    streaming_start_time = last_edit_time
    client = None
    stream = None
    completed = False

    def text_key(item_id, output_index, content_index):
        return (item_id if item_id is not None else output_index, content_index)

    def add_text(key, text, *, delta=False):
        if not text:
            return
        if delta:
            text_parts[key] = text_parts.get(key, "") + text
        elif key not in text_parts:
            text_parts[key] = text
        result.text = "".join(text_parts.values())

    async def deliver_image(item_id, encoded, preview_index=None):
        if not isinstance(item_id, str) or not item_id:
            raise ValueError("Codex image output is missing its item identity.")
        if preview_index is not None:
            if type(preview_index) is not int or preview_index < 0:
                raise ValueError("Codex returned an invalid image preview index.")
            identity = (item_id, preview_index)
            seen = previews_seen
        else:
            identity = item_id
            seen = images_seen
        if identity in seen:
            return
        data, extension = _decode_image(encoded)
        if image_callback is None:
            raise RuntimeError(
                "Codex returned an image without an image delivery callback."
            )
        try:
            await image_callback(CodexImage(data, item_id, preview_index, extension))
        except Exception as exc:
            raise RuntimeError("Failed to deliver a Codex image to Telegram.") from exc
        seen.add(identity)
        if preview_index is None:
            result.images_delivered += 1
        else:
            result.previews_delivered += 1

    async def consume_item(item, output_index):
        item_id = _field(item, "id")
        if _field(item, "type") == "image_generation_call":
            if _field(item, "status") not in (None, "completed"):
                raise RuntimeError("Codex image generation did not complete.")
            await deliver_image(item_id, _field(item, "result"))
        elif _field(item, "type") == "message":
            for content_index, part in enumerate(_field(item, "content", []) or []):
                if _field(part, "type") in ("output_text", "refusal"):
                    add_text(
                        text_key(item_id, output_index, content_index),
                        _field(part, "text") or _field(part, "refusal"),
                    )

    try:
        client = await _create_async_client()
        stream = await client.responses.create(**kwargs)
        async for stream_event in stream:
            event_type = _field(stream_event, "type")
            if event_type in ("response.output_text.delta", "response.refusal.delta"):
                key = text_key(
                    _field(stream_event, "item_id"),
                    _field(stream_event, "output_index"),
                    _field(stream_event, "content_index", 0),
                )
                add_text(key, _field(stream_event, "delta"), delta=True)
                current_time = asyncio.get_running_loop().time()
                elapsed = current_time - streaming_start_time
                current_edit_interval = (
                    60 if elapsed > 120 else 15 if elapsed > 30 else edit_interval
                )
                cursor = "▌💤💤" if elapsed > 120 else "▌💤" if elapsed > 30 else "▌"
                if current_time - last_edit_time > current_edit_interval:
                    try:
                        await util.edit_message(
                            response_message, result.text + cursor, parse_mode="md"
                        )
                        last_edit_time = current_time
                    except Exception as exc:
                        print(f"Error during Codex message edit: {exc}")
            elif event_type in ("response.output_text.done", "response.refusal.done"):
                key = text_key(
                    _field(stream_event, "item_id"),
                    _field(stream_event, "output_index"),
                    _field(stream_event, "content_index", 0),
                )
                add_text(
                    key, _field(stream_event, "text") or _field(stream_event, "refusal")
                )
            elif event_type == "response.image_generation_call.partial_image":
                preview_index = _field(stream_event, "partial_image_index")
                if preview_index is None:
                    raise ValueError("Codex image preview is missing its index.")
                await deliver_image(
                    _field(stream_event, "item_id"),
                    _field(stream_event, "partial_image_b64"),
                    preview_index,
                )
            elif event_type == "response.output_item.done":
                await consume_item(
                    _field(stream_event, "item"), _field(stream_event, "output_index")
                )
            elif event_type in (
                "response.completed",
                "response.failed",
                "response.incomplete",
            ):
                response = _field(stream_event, "response")
                result.finish_reason = _field(response, "status")
                # Preserve any completed outputs even when the response later fails.
                for index, item in enumerate(_field(response, "output", []) or []):
                    if _field(item, "status") in (None, "completed"):
                        await consume_item(item, index)
                if event_type != "response.completed" or result.finish_reason not in (
                    None,
                    "completed",
                ):
                    raise RuntimeError(
                        f"Codex response ended with {result.finish_reason or event_type}."
                    )
                completed = True
            elif event_type == "error":
                raise RuntimeError("Codex backend reported a streaming error.")
        if not completed:
            raise RuntimeError("Codex stream ended before response completion.")
        if not result.text.strip() and not result.has_image:
            raise RuntimeError("Codex returned an empty result (no text or images).")
        return result
    except Exception as exc:
        raise CodexStreamError(str(exc), result) from exc
    finally:
        # Cleanup errors must not mask generation failures or cancellation.
        for name, resource in (("stream", stream), ("client", client)):
            if resource is not None:
                try:
                    await resource.close()
                except Exception as exc:
                    print(f"Error closing Codex {name}: {exc}")
