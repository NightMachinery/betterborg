import asyncio
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

import openai

from uniborg import util
from uniborg.constants import PIONEER_BASE_URL


PIONEER_MODEL_PREFIX = "pioneer/"


@dataclass
class PioneerResponse:
    text: str
    finish_reason: Optional[str] = None


def is_pioneer_model(model: str) -> bool:
    return bool(model and model.startswith(PIONEER_MODEL_PREFIX))


def pioneer_model_name(model: str) -> str:
    return model.removeprefix(PIONEER_MODEL_PREFIX)


def _data_url_mime_type(url: str) -> Optional[str]:
    if not isinstance(url, str) or not url.startswith("data:"):
        return None
    metadata = url.split(",", 1)[0]
    return metadata.removeprefix("data:").split(";", 1)[0].lower()


def _is_supported_image_url(url: str) -> bool:
    if not isinstance(url, str) or not url:
        return False
    mime_type = _data_url_mime_type(url)
    if mime_type is not None:
        return mime_type.startswith("image/")
    return urlparse(url).scheme.lower() in {"http", "https"}


def _content_part_to_pioneer(part: dict) -> Optional[dict]:
    part_type = part.get("type")
    if part_type == "text":
        text = part.get("text") or ""
        return {"type": "input_text", "text": text} if text else None

    if part_type == "image_url":
        image_url = (part.get("image_url") or {}).get("url")
        if _is_supported_image_url(image_url):
            return {
                "type": "input_image",
                "image_url": image_url,
                "detail": "low",
            }
        return None

    if part_type == "file":
        file_info = part.get("file") or {}
        file_id = file_info.get("file_id")
        filename = file_info.get("filename") or "attachment"
        mime_type = (file_info.get("format") or "").lower()
        data_url_mime = _data_url_mime_type(file_id)

        if data_url_mime is not None:
            if (
                data_url_mime != "application/pdf"
                and not data_url_mime.startswith("text/")
            ):
                return None
            converted = {
                "type": "input_file",
                "file_data": file_id,
                "filename": filename,
            }
            return converted

        if isinstance(file_id, str) and file_id:
            if urlparse(file_id).scheme.lower() in {"http", "https"}:
                return {
                    "type": "input_file",
                    "file_url": file_id,
                    "filename": filename,
                }
            if mime_type == "application/pdf" or file_id.startswith("file-"):
                return {
                    "type": "input_file",
                    "file_id": file_id,
                    "filename": filename,
                }

    return None


def prepare_pioneer_response_kwargs(
    *,
    model: str,
    instructions: str,
    input_messages: list[dict],
    reasoning_effort: Optional[str] = None,
    tools: Optional[list[dict]] = None,
) -> dict:
    kwargs = {
        "model": pioneer_model_name(model),
        "input": input_messages,
        "store": False,
        "stream": True,
        "instructions": instructions or "You are a helpful assistant.",
    }
    if reasoning_effort:
        kwargs["reasoning"] = {"effort": reasoning_effort}
    if tools:
        kwargs["tools"] = tools
    return kwargs


def messages_to_pioneer_responses(messages: list[dict]) -> tuple[str, list[dict]]:
    instructions = []
    response_messages = []

    for message in messages:
        role = message.get("role")
        content = message.get("content")

        if role == "system":
            if isinstance(content, str) and content:
                instructions.append(content)
            elif isinstance(content, list):
                text = "\n".join(
                    part.get("text") or ""
                    for part in content
                    if (
                        isinstance(part, dict)
                        and part.get("type") == "text"
                        and part.get("text")
                    )
                )
                if text:
                    instructions.append(text)
            continue

        if role not in ("user", "assistant"):
            continue

        if isinstance(content, str):
            if content:
                response_messages.append({"role": role, "content": content})
            continue

        if isinstance(content, list):
            converted_parts = []
            for part in content:
                if not isinstance(part, dict):
                    continue
                converted_part = _content_part_to_pioneer(part)
                if converted_part:
                    converted_parts.append(converted_part)

            if not converted_parts:
                continue

            if role == "assistant":
                text = "\n".join(
                    part["text"]
                    for part in converted_parts
                    if part.get("type") == "input_text" and part.get("text")
                )
                if text:
                    response_messages.append({"role": role, "content": text})
            else:
                response_messages.append({"role": role, "content": converted_parts})

    return "\n\n".join(instructions), response_messages


async def _create_async_client(api_key: str) -> openai.AsyncOpenAI:
    if not api_key:
        raise RuntimeError("Missing Pioneer API key.")
    return openai.AsyncOpenAI(
        api_key=api_key,
        base_url=PIONEER_BASE_URL,
        default_headers={"X-API-Key": api_key},
    )


async def stream_pioneer_response(
    *,
    event,
    response_message,
    model: str,
    messages: list[dict],
    api_key: str,
    reasoning_effort: Optional[str] = None,
    tools: Optional[list[dict]] = None,
    edit_interval: float = 0.8,
) -> PioneerResponse:
    client = await _create_async_client(api_key)
    instructions, input_messages = messages_to_pioneer_responses(messages)

    kwargs = prepare_pioneer_response_kwargs(
        model=model,
        instructions=instructions,
        input_messages=input_messages,
        reasoning_effort=reasoning_effort,
        tools=tools,
    )

    response_text = ""
    finish_reason = None
    last_edit_time = asyncio.get_event_loop().time()
    streaming_start_time = last_edit_time

    async for stream_event in await client.responses.create(**kwargs):
        event_type = getattr(stream_event, "type", None)

        if event_type == "response.output_text.delta":
            delta = getattr(stream_event, "delta", None)
            if not delta:
                continue
            response_text += delta
            current_time = asyncio.get_event_loop().time()
            current_edit_interval = edit_interval
            cursor = "▌"

            if (current_time - streaming_start_time) > 120:
                current_edit_interval = 60
                cursor = "▌💤💤"
            elif (current_time - streaming_start_time) > 30:
                current_edit_interval = 15
                cursor = "▌💤"

            if (current_time - last_edit_time) > current_edit_interval:
                try:
                    await util.edit_message(
                        response_message,
                        f"{response_text}{cursor}",
                        parse_mode="md",
                    )
                    last_edit_time = current_time
                except Exception as e:
                    print(f"Error during Pioneer message edit: {e}")

        elif event_type == "response.completed":
            response = getattr(stream_event, "response", None)
            if response is not None:
                finish_reason = getattr(response, "status", None)

    return PioneerResponse(text=response_text, finish_reason=finish_reason)
