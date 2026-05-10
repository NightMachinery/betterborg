import asyncio
from dataclasses import dataclass
from typing import Optional

import openai

from uniborg import util


CODEX_MODEL_PREFIX = "openai-codex/"


@dataclass
class CodexResponse:
    text: str
    finish_reason: Optional[str] = None


def is_codex_model(model: str) -> bool:
    return bool(model and model.startswith(CODEX_MODEL_PREFIX))


def codex_model_name(model: str) -> str:
    return model.removeprefix(CODEX_MODEL_PREFIX)


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
        if image_url:
            return {
                "type": "input_image",
                "image_url": image_url,
                "detail": "low",
            }

    return None


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
    edit_interval: float = 0.8,
) -> CodexResponse:
    client = await _create_async_client()
    instructions, input_messages = messages_to_codex(messages)

    kwargs = {
        "model": codex_model_name(model),
        "input": input_messages,
        "store": False,
        "stream": True,
        "instructions": instructions or "You are a helpful assistant.",
    }
    if reasoning_effort:
        kwargs["reasoning"] = {"effort": reasoning_effort}

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
                    print(f"Error during Codex message edit: {e}")

        elif event_type == "response.completed":
            response = getattr(stream_event, "response", None)
            if response is not None:
                finish_reason = getattr(response, "status", None)

    return CodexResponse(text=response_text, finish_reason=finish_reason)
