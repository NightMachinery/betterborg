import asyncio
import ast
import base64
import binascii
import hashlib
import io
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable, Optional

from urllib.parse import urlparse

import openai
from PIL import Image

from uniborg import util
from uniborg.constants import OPENAI_CODEX_LUNA_RESERVE


CODEX_MODEL_PREFIX = "openai-codex/"

#: The `error.type` (bodies) or `error.code` (stream events) that marks a
#: ChatGPT plan allowance as spent, as opposed to an ordinary rate limit.
CODEX_USAGE_LIMIT_TYPE = "usage_limit_reached"
#: No plan window legitimately runs longer than this; beyond it the reported
#: deadline is nonsense and is discarded rather than trusted.
CODEX_USAGE_LIMIT_MAX_WINDOW_SECONDS = 45 * 24 * 3600
#: Bounds how much of an exception string is handed to `ast.literal_eval`.
CODEX_ERROR_STRING_MAX_CHARS = 8192
_CODEX_ERROR_STRING_RE = re.compile(r"^Error code: \d+ - ")
#: Fields read off an SDK stream event when it is not a plain mapping.
_USAGE_LIMIT_FIELDS = (
    "type",
    "code",
    "message",
    "plan_type",
    "resets_at",
    "resets_in_seconds",
)


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


@dataclass(frozen=True)
class CodexUsageLimit:
    """A parsed `usage_limit_reached` rejection from the Codex backend."""

    message: Optional[str] = None
    plan_type: Optional[str] = None
    #: Timezone-aware UTC. None when the payload carried no usable deadline.
    resets_at: Optional[datetime] = None


class CodexStreamError(RuntimeError):
    def __init__(
        self,
        message: str,
        response: CodexResponse,
        *,
        usage_limit: Optional[CodexUsageLimit] = None,
    ):
        self.response = response
        self.usage_limit = usage_limit
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


def _positive_number(value) -> Optional[float]:
    """A finite, strictly positive number, rejecting `bool` and non-numerics."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")) or number <= 0:
        return None
    return number


def _epoch_seconds(value) -> Optional[float]:
    seconds = _positive_number(value)
    if seconds is None:
        return None
    #: Some backends report milliseconds; no second-scale timestamp reaches 1e11.
    return seconds / 1000 if seconds > 1e11 else seconds


def _plausible_deadline(value: datetime, *, now: datetime) -> bool:
    return now < value <= now + timedelta(seconds=CODEX_USAGE_LIMIT_MAX_WINDOW_SECONDS)


def _usage_limit_deadline(fields: dict, *, now: datetime) -> Optional[datetime]:
    """`resets_at` wins over `resets_in_seconds`; each is range-checked alone.

    Validating them independently means a bogus absolute timestamp degrades to
    the relative one instead of losing the deadline altogether.
    """
    absolute = _epoch_seconds(fields.get("resets_at"))
    if absolute is not None:
        try:
            candidate = datetime.fromtimestamp(absolute, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            candidate = None
        if candidate is not None and _plausible_deadline(candidate, now=now):
            return candidate

    relative = _positive_number(fields.get("resets_in_seconds"))
    if relative is not None:
        try:
            candidate = now + timedelta(seconds=relative)
        except OverflowError:
            return None
        if _plausible_deadline(candidate, now=now):
            return candidate
    return None


def _literal_body(text: str) -> Optional[dict]:
    """The mapping inside openai's `Error code: N - {...}` message, else None.

    That message interpolates the decoded body with `repr`, so it is a Python
    literal with `'` quotes and `None` rather than JSON.
    """
    if len(text) > CODEX_ERROR_STRING_MAX_CHARS:
        return None
    stripped = _CODEX_ERROR_STRING_RE.sub("", text.strip(), count=1)
    try:
        value = ast.literal_eval(stripped)
    except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def _fields_from_object(payload) -> dict:
    """Best-effort extraction from an SDK event object rather than a mapping."""
    fields = {}
    for name in _USAGE_LIMIT_FIELDS:
        value = _field(payload, name)
        if value is not None:
            fields[name] = value
    return fields


def _usage_limit_mapping(payload) -> Optional[dict]:
    """The mapping that may carry usage-limit fields, from any accepted payload."""
    body = getattr(payload, "body", None)
    if body is None:
        if isinstance(payload, (dict, str)):
            body = payload
        elif isinstance(payload, BaseException):
            body = str(payload)
        else:
            body = _fields_from_object(payload)
    if isinstance(body, str):
        body = _literal_body(body)
    if not isinstance(body, dict):
        return None
    #: `.body` arrives already unwrapped; the message string keeps the wrapper,
    #: and this backend also uses `detail` (sometimes holding a bare string).
    for key in ("error", "detail"):
        nested = body.get(key)
        if isinstance(nested, dict):
            return nested
    return body


def _is_usage_limit(fields: dict) -> bool:
    #: Response bodies carry `type`; stream `error` events carry `code`.
    return CODEX_USAGE_LIMIT_TYPE in (fields.get("type"), fields.get("code"))


def parse_usage_limit(
    payload, *, now: Optional[datetime] = None
) -> Optional[CodexUsageLimit]:
    """Read a ChatGPT usage-limit rejection out of `payload`, or return None.

    `payload` may be an `openai.APIStatusError` (its decoded `.body` is used), a
    mapping such as a response body or a `response.*` stream `error` event, or
    the `Error code: 429 - {...}` string openai builds. An ordinary rate limit
    is not a usage limit and yields None. Never raises.
    """
    try:
        fields = _usage_limit_mapping(payload)
    except Exception:
        return None
    if not isinstance(fields, dict) or not _is_usage_limit(fields):
        return None

    now = now or datetime.now(timezone.utc)
    message = fields.get("message")
    plan_type = fields.get("plan_type")
    return CodexUsageLimit(
        message=message if isinstance(message, str) else None,
        plan_type=plan_type if isinstance(plan_type, str) else None,
        resets_at=_usage_limit_deadline(fields, now=now),
    )


def _stream_error_message(stream_event) -> str:
    """Keep the backend's own code and message instead of discarding them."""
    detail = " ".join(
        str(part)
        for part in (_field(stream_event, "code"), _field(stream_event, "message"))
        if part
    )
    base = "Codex backend reported a streaming error."
    return f"{base} {detail}" if detail else base


def is_codex_model(model: str) -> bool:
    return bool(model and model.startswith(CODEX_MODEL_PREFIX))


def codex_model_name(model: str) -> str:
    return model.removeprefix(CODEX_MODEL_PREFIX)


def is_luna_reserve_model(model: str) -> bool:
    """Whether `model` routes to the Luna Reserve rather than the plan allowance."""
    return bool(model) and codex_model_name(model) == codex_model_name(
        OPENAI_CODEX_LUNA_RESERVE
    )


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


async def _create_async_client(
    *, max_retries: Optional[int] = None
) -> openai.AsyncOpenAI:
    token, headers, base_url = await asyncio.to_thread(_get_codex_auth)
    kwargs = {
        "api_key": token,
        "base_url": base_url,
        "default_headers": headers,
    }
    #: Left to the SDK default unless a caller caps it. A usage-limit 429 is not
    #: worth retrying, and the default of 2 triples the wait before we see it.
    if max_retries is not None:
        kwargs["max_retries"] = max_retries
    return openai.AsyncOpenAI(**kwargs)


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
    max_retries: Optional[int] = None,
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
    usage_limit = None

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
        client = await _create_async_client(max_retries=max_retries)
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
                #: A plain RuntimeError here; raising CodexStreamError inside the
                #: try would be re-wrapped below and duplicate the message.
                usage_limit = parse_usage_limit(stream_event)
                raise RuntimeError(_stream_error_message(stream_event))
        if not completed:
            raise RuntimeError("Codex stream ended before response completion.")
        if not result.text.strip() and not result.has_image:
            raise RuntimeError("Codex returned an empty result (no text or images).")
        return result
    except Exception as exc:
        raise CodexStreamError(
            str(exc), result, usage_limit=usage_limit or parse_usage_limit(exc)
        ) from exc
    finally:
        # Cleanup errors must not mask generation failures or cancellation.
        for name, resource in (("stream", stream), ("client", client)):
            if resource is not None:
                try:
                    await resource.close()
                except Exception as exc:
                    print(f"Error closing Codex {name}: {exc}")
