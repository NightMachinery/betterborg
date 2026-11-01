"""Where GPT models run: Codex, the OpenAI API, or OpenRouter (/openaiProvider).

The pickers offer one provider-neutral id per GPT family, such as
`openai/gpt-sol-latest`. The user's provider decides which backend answers each
request, so switching providers keeps every saved model and effort. See
docs/openai_provider.md.
"""

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional, Tuple

from uniborg import codex_aliases
from uniborg.constants import (
    GPT_ASTRA,
    GPT_LUNA,
    GPT_SOL,
    OPENAI_CODEX_ASTRA,
    OPENAI_CODEX_LUNA,
    OPENAI_CODEX_SOL,
    OR_OPENAI_ASTRA,
    OR_OPENAI_LUNA,
    OR_OPENAI_SOL,
)

_log = logging.getLogger(__name__)

#: Providers. The first three are also the backends a request can run on.
CODEX = "codex"
OPENAI = "openai"
OPENROUTER = "openrouter"
AUTO_OPENAI_FIRST = "auto-openai-first"
AUTO_CODEX_FIRST = "auto-codex-first"

DEFAULT = OPENAI

LABELS = {
    OPENAI: "OpenAI API key",
    CODEX: "Codex (ChatGPT plan)",
    OPENROUTER: "OpenRouter",
    AUTO_OPENAI_FIRST: "Auto: OpenAI key, then Codex",
    AUTO_CODEX_FIRST: "Auto: Codex, then OpenAI key",
}
BACKENDS = (CODEX, OPENAI, OPENROUTER)

#: `/openaiProvider ARG` names a provider without the menu.
ARGS = {
    "openai": OPENAI,
    "api": OPENAI,
    "codex": CODEX,
    "openrouter": OPENROUTER,
    "or": OPENROUTER,
    "auto-openai-first": AUTO_OPENAI_FIRST,
    "auto": AUTO_OPENAI_FIRST,
    "auto-codex-first": AUTO_CODEX_FIRST,
}

#: Concrete OpenAI API model ids. The suffix is a Codex alias name or a slug.
OPENAI_API_PREFIX = "openai-api/"


@dataclass(frozen=True)
class GptFamily:
    neutral_id: str
    codex_id: str
    openrouter_id: str


FAMILIES = (
    GptFamily(GPT_SOL, OPENAI_CODEX_SOL, OR_OPENAI_SOL),
    GptFamily(GPT_ASTRA, OPENAI_CODEX_ASTRA, OR_OPENAI_ASTRA),
    GptFamily(GPT_LUNA, OPENAI_CODEX_LUNA, OR_OPENAI_LUNA),
)
_FAMILIES_BY_NEUTRAL: Dict[str, GptFamily] = {f.neutral_id: f for f in FAMILIES}
_FAMILIES_BY_CODEX: Dict[str, GptFamily] = {f.codex_id: f for f in FAMILIES}

#: OpenRouter accepts only these reasoning levels for OpenAI models.
OPENROUTER_LEVELS: Tuple[str, ...] = ("low", "medium", "high")


def normalize(provider: Optional[str]) -> str:
    """PROVIDER, or the default for a missing or unknown one."""
    return provider if provider in LABELS else DEFAULT


def is_neutral_gpt(model: Optional[str]) -> bool:
    return bool(model) and model in _FAMILIES_BY_NEUTRAL


def is_openai_api_model(model: Optional[str]) -> bool:
    return bool(model) and model.startswith(OPENAI_API_PREFIX)


def neutral_for_codex(model: Optional[str]) -> Optional[str]:
    """The neutral id of the Codex alias MODEL, or None."""
    family = _FAMILIES_BY_CODEX.get(model)
    return family.neutral_id if family else None


def family_for(model: str) -> GptFamily:
    family = _FAMILIES_BY_NEUTRAL.get(model)
    if family is None:
        raise ValueError(f"Not a provider-neutral GPT model: {model!r}")
    return family


def backend_model(model: str, backend: str) -> str:
    """The concrete model id that runs the neutral MODEL on BACKEND."""
    family = family_for(model)
    if backend == CODEX:
        return family.codex_id
    if backend == OPENAI:
        return OPENAI_API_PREFIX + model.split("/", 1)[1]
    if backend == OPENROUTER:
        return family.openrouter_id
    raise ValueError(f"Unknown GPT backend: {backend!r}")


def openai_api_slug(model: str) -> str:
    """The model name the OpenAI API is sent for the concrete id MODEL.

    A family alias goes out as the slug its Codex alias points at now: the two
    catalogs name models alike, and only Codex's is read (`codex_aliases`).
    """
    name = model.removeprefix(OPENAI_API_PREFIX)
    neutral = "openai/" + name
    if neutral in _FAMILIES_BY_NEUTRAL:
        return codex_aliases.slug_for(_FAMILIES_BY_NEUTRAL[neutral].codex_id) or name
    return name


def openrouter_level(level: Optional[str]) -> Optional[str]:
    """LEVEL clamped to what OpenRouter accepts for OpenAI models."""
    if level is None:
        return None
    if level in OPENROUTER_LEVELS:
        return level
    if level in ("xhigh", "max"):
        return "high"
    if level in ("none", "minimal"):
        return "low"
    return None


@dataclass(frozen=True)
class Route:
    """The backend one request starts on."""

    backend: str
    #: A note for the user about why this backend was chosen.
    note: Optional[str] = None
    #: Where to retry when this backend turns out to be spent mid-request.
    fallback: Optional[str] = None


NO_KEY_NOTE = (
    "You have no OpenAI API key, so Codex answered. "
    "Set one with /setOpenAIKey, or choose a provider with /openaiProvider."
)
OPENAI_SPENT_NOTE = "Your OpenAI keys are out of quota, so Codex answered."
CODEX_SPENT_NOTE = "Codex is over its usage limit, so your OpenAI key answered."


def choose_backend(
    provider: str,
    *,
    codex_access: bool,
    openai_any: bool,
    openai_usable: bool,
    codex_exhausted: bool,
) -> Route:
    """Pick the backend for one request.

    OPENAI_ANY: the user has some OpenAI key at all. OPENAI_USABLE: one of them
    is not known to be out of quota. CODEX_EXHAUSTED: the Codex account is known
    to be over its usage limit.
    """
    codex_ready = codex_access and not codex_exhausted
    if provider == CODEX:
        return Route(CODEX)
    if provider == OPENROUTER:
        return Route(OPENROUTER)
    if provider == OPENAI:
        if not openai_any and codex_access:
            #: Never strand someone who has a working backend.
            return Route(CODEX, note=NO_KEY_NOTE)
        return Route(OPENAI)
    if provider == AUTO_OPENAI_FIRST:
        if openai_usable:
            return Route(OPENAI, fallback=CODEX if codex_access else None)
        if codex_ready:
            return Route(CODEX, note=OPENAI_SPENT_NOTE if openai_any else None)
        if openai_any:
            return Route(OPENAI, fallback=CODEX if codex_access else None)
        return Route(CODEX) if codex_access else Route(OPENAI)
    if provider == AUTO_CODEX_FIRST:
        if codex_ready:
            return Route(CODEX, fallback=OPENAI if openai_usable else None)
        if openai_any:
            return Route(OPENAI, note=CODEX_SPENT_NOTE if codex_access else None)
        return Route(CODEX) if codex_access else Route(OPENAI)
    raise ValueError(f"Unknown OpenAI provider: {provider!r}")


# --- Quota memory ---
#: How long an OpenAI key that ran out of credit is skipped (owner: 72h).
OPENAI_QUOTA_TTL = timedelta(hours=72)
#: When a Codex usage limit names no reset.
CODEX_EXHAUSTED_DEFAULT_TTL = timedelta(hours=1)

_OPENAI_QUOTA_PREFIX = "borg:openai_quota:"
_CODEX_EXHAUSTED_KEY = "borg:codex_exhausted"


class QuotaMemory:
    """Which OpenAI keys are out of credit, and whether Codex is spent.

    Kept in Redis, so every process and restart agrees; a process without
    Redis remembers in memory. Keys are stored by hash, never raw. Each record
    holds its deadline as epoch seconds.
    """

    def __init__(self, *, get_redis=None, clock=time.time):
        self._get_redis = get_redis
        self._clock = clock
        self._memory: Dict[str, float] = {}

    async def _redis(self):
        if self._get_redis is None:
            return None
        try:
            return await self._get_redis()
        except Exception as exc:
            _log.warning("Quota memory: Redis unavailable: %s", exc)
            return None

    async def _set(self, name: str, until: float) -> None:
        self._memory[name] = until
        redis = await self._redis()
        if redis is None:
            return
        ttl = max(1, int(until - self._clock()))
        try:
            await redis.set(name, str(until), ex=ttl)
        except Exception as exc:
            _log.warning("Quota memory: could not save %s: %s", name, exc)

    async def _get(self, name: str) -> Optional[float]:
        now = self._clock()
        redis = await self._redis()
        until = None
        if redis is not None:
            try:
                value = await redis.get(name)
                until = float(value) if value is not None else None
            except Exception as exc:
                _log.warning("Quota memory: could not read %s: %s", name, exc)
                until = self._memory.get(name)
        else:
            until = self._memory.get(name)
        if until is None or until <= now:
            self._memory.pop(name, None)
            return None
        return until

    async def _delete(self, name: str) -> None:
        self._memory.pop(name, None)
        redis = await self._redis()
        if redis is None:
            return
        try:
            await redis.delete(name)
        except Exception as exc:
            _log.warning("Quota memory: could not clear %s: %s", name, exc)

    @staticmethod
    def _key_name(api_key: str) -> str:
        from uniborg.redis_util import api_key_hash

        return _OPENAI_QUOTA_PREFIX + api_key_hash(api_key)

    async def mark_key_spent(self, api_key: str) -> datetime:
        until = self._clock() + OPENAI_QUOTA_TTL.total_seconds()
        await self._set(self._key_name(api_key), until)
        return datetime.fromtimestamp(until, tz=timezone.utc)

    async def key_spent_until(self, api_key: str) -> Optional[datetime]:
        until = await self._get(self._key_name(api_key))
        return datetime.fromtimestamp(until, tz=timezone.utc) if until else None

    async def clear_key(self, api_key: str) -> None:
        await self._delete(self._key_name(api_key))

    async def mark_codex_spent(self, until: Optional[datetime] = None) -> None:
        deadline = (
            until.timestamp()
            if until is not None
            else self._clock() + CODEX_EXHAUSTED_DEFAULT_TTL.total_seconds()
        )
        if deadline > self._clock():
            await self._set(_CODEX_EXHAUSTED_KEY, deadline)

    async def codex_spent_until(self) -> Optional[datetime]:
        until = await self._get(_CODEX_EXHAUSTED_KEY)
        return datetime.fromtimestamp(until, tz=timezone.utc) if until else None

    async def clear_codex(self) -> None:
        await self._delete(_CODEX_EXHAUSTED_KEY)


def _exception_chain(exc: BaseException):
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        yield exc
        exc = exc.__cause__ or exc.__context__


def _error_code(exc: BaseException) -> Optional[str]:
    code = getattr(exc, "code", None)
    if isinstance(code, str):
        return code
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        error = body.get("error", body)
        if isinstance(error, dict) and isinstance(error.get("code"), str):
            return error["code"]
    return None


def is_insufficient_quota(exc: BaseException) -> bool:
    """Whether EXC says an OpenAI key has no credit left (not a rate limit)."""
    for item in _exception_chain(exc):
        if _error_code(item) == "insufficient_quota":
            return True
        if "insufficient_quota" in str(item):
            return True
    return False
