"""Short LLM titles: file names and summaries of long answers, and topic names.

A user's title choice is a model id or "auto". "auto" is the Codex Luna
Reserve for a user with Codex access, and the latest Gemini Flash Lite
otherwise. Any other model falls back to Flash Lite when it fails. See
docs/title_model.md.
"""

import asyncio
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable, Optional, Type, TypeVar

import litellm
from pydantic import BaseModel

from uniborg import codex_util, llm_db, llm_util, util
from uniborg.constants import GEMINI_FLASH_LITE_LATEST, OPENAI_CODEX_LUNA_RESERVE

AUTO_TITLE_MODEL = "auto"
FALLBACK_TITLE_MODEL = GEMINI_FLASH_LITE_LATEST
#: Titles need little thought, and a lower effort answers sooner.
CODEX_TITLE_REASONING_EFFORT = "low"
TITLE_TIMEOUT_SECONDS = 30
#: How long Codex titles stay off after a usage limit that names no reset.
CODEX_PAUSE_WITHOUT_RESET = timedelta(hours=1)

FILE_TITLE_PROMPT = "Generate a title and filename for this text content:\n\n{text}"

SchemaT = TypeVar("SchemaT", bound=BaseModel)


class TitleUnavailableError(RuntimeError):
    """Every candidate model failed or could not be tried."""


def is_codex_model(model: str) -> bool:
    return llm_util.get_service_from_model(model) == "codex"


def resolve_title_model(choice: Optional[str], *, codex_p: bool) -> str:
    """The model a title choice means for a user with (or without) Codex."""
    if not choice or choice == AUTO_TITLE_MODEL:
        return OPENAI_CODEX_LUNA_RESERVE if codex_p else GEMINI_FLASH_LITE_LATEST
    if is_codex_model(choice) and not codex_p:
        return GEMINI_FLASH_LITE_LATEST
    return choice


class CodexTitlePause:
    """Keeps Codex titles off until a spent meter resets, in this process.

    Without it, every long answer would spend a round trip on a meter that is
    known to be empty.
    """

    def __init__(self, *, clock: Optional[Callable[[], datetime]] = None):
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.until: Optional[datetime] = None

    def active(self) -> bool:
        return self.until is not None and self._clock() < self.until

    def pause(self, usage_limit: codex_util.CodexUsageLimit) -> None:
        self.until = usage_limit.resets_at or (
            self._clock() + CODEX_PAUSE_WITHOUT_RESET
        )


CODEX_PAUSE = CodexTitlePause()


def _json_instructions(schema: Type[BaseModel]) -> str:
    return (
        "Reply with only a JSON object, without code fences, that matches this "
        f"JSON schema:\n{json.dumps(schema.model_json_schema())}"
    )


def parse_json_reply(text: str, schema: Type[SchemaT]) -> SchemaT:
    """Validate the JSON object in a model's plain-text reply."""
    stripped = text.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", stripped, flags=re.DOTALL)
    if fenced:
        stripped = fenced.group(1)
    start, end = stripped.find("{"), stripped.rfind("}")
    if start == -1 or end < start:
        raise ValueError("The reply holds no JSON object.")
    return schema.model_validate_json(stripped[start : end + 1])


async def complete_structured(
    prompt: str,
    schema: Type[SchemaT],
    *,
    model: str,
    api_key: Optional[str] = None,
    api_user_id: Optional[int] = None,
) -> SchemaT:
    """One request to `model`, validated against `schema`.

    Codex models take the schema in their instructions, since the Codex
    backend has no structured-output option here. The rest go through litellm.
    """
    if is_codex_model(model):
        text = await codex_util.complete_codex_text(
            model=model,
            instructions=_json_instructions(schema),
            text=prompt,
            reasoning_effort=CODEX_TITLE_REASONING_EFFORT,
            prompt_cache_key=codex_util.codex_prompt_cache_key(
                model=model, chat_id="titles"
            ),
        )
        return parse_json_reply(text, schema)

    completion_kwargs = dict(
        api_key=api_key,
        model=model,
        messages=[{"role": "user", "content": prompt}],
        response_format=schema,
    )
    #: Native Gemini (Google API) traffic goes through the special proxy.
    if model.startswith("gemini/"):
        proxy_client = llm_util.create_litellm_proxy_client(api_user_id)
        if proxy_client is not None:
            completion_kwargs["client"] = proxy_client
    response = await litellm.acompletion(**completion_kwargs)
    return schema.model_validate_json(response.choices[0].message.content)


def _api_key_for(
    model: str, *, api_keys: Optional[dict], api_user_id: Optional[int]
) -> Optional[str]:
    service = llm_util.get_service_from_model(model)
    api_key = (api_keys or {}).get(service)
    if not api_key and api_user_id is not None:
        api_key = llm_db.get_api_key(api_user_id, service=service)
    return api_key


async def generate_title(
    prompt: str,
    schema: Type[SchemaT],
    *,
    choice: Optional[str],
    codex_p: bool,
    api_keys: Optional[dict] = None,
    api_user_id: Optional[int] = None,
    timeout: float = TITLE_TIMEOUT_SECONDS,
    complete: Optional[Callable[..., Awaitable[BaseModel]]] = None,
    codex_pause: Optional[CodexTitlePause] = None,
) -> SchemaT:
    """A title from the model `choice` means, or else from Flash Lite.

    `api_keys` maps a service to a key and wins over the user's stored keys.
    Raises `TitleUnavailableError` when no candidate produced a title.
    """
    complete = complete or complete_structured
    codex_pause = codex_pause or CODEX_PAUSE
    model = resolve_title_model(choice, codex_p=codex_p)
    candidates = [model]
    if model != FALLBACK_TITLE_MODEL:
        candidates.append(FALLBACK_TITLE_MODEL)

    failures = []
    for candidate in candidates:
        api_key = None
        if is_codex_model(candidate):
            if codex_pause.active():
                failures.append(f"{candidate}: paused until {codex_pause.until}")
                continue
        else:
            api_key = _api_key_for(
                candidate, api_keys=api_keys, api_user_id=api_user_id
            )
            if not api_key:
                failures.append(f"{candidate}: no API key")
                continue
        try:
            return await asyncio.wait_for(
                complete(
                    prompt,
                    schema,
                    model=candidate,
                    api_key=api_key,
                    api_user_id=api_user_id,
                ),
                timeout,
            )
        except codex_util.CodexStreamError as e:
            if e.usage_limit is not None:
                codex_pause.pause(e.usage_limit)
            failures.append(f"{candidate}: {e}")
        except Exception as e:
            failures.append(f"{candidate}: {type(e).__name__}: {e}")
    raise TitleUnavailableError("; ".join(failures))


def file_title_generator(
    *,
    choice: Optional[str],
    codex_p: bool,
    api_keys: Optional[dict] = None,
    api_user_id: Optional[int] = None,
) -> Callable[[str], Awaitable[util.FilenameGeneration]]:
    """A `title_generator` for `util.edit_message` and `send_as_file_with_filename`."""

    async def generate(text: str) -> util.FilenameGeneration:
        return await generate_title(
            FILE_TITLE_PROMPT.format(text=text),
            util.FilenameGeneration,
            choice=choice,
            codex_p=codex_p,
            api_keys=api_keys,
            api_user_id=api_user_id,
        )

    return generate
