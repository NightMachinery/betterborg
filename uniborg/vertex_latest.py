"""Keep the Vertex AI stand-ins for the `-latest` Gemini aliases current.

Vertex AI serves neither `gemini-flash-latest` nor `gemini-flash-lite-latest`,
so `gemini_providers.VERTEX_LATEST_IDS` maps them to fixed models. This module
finds the newest Flash and Flash Lite by probing the model ids after the known
newest ones with `countTokens`, which is free and known to work with Express
keys (listing models with one was not checked). The answer is cached in a file
shared by every bot process for `CACHE_TTL`, and probed again as soon as it
expires.
"""

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable, Optional

import httpx

from uniborg import gemini_providers, llm_db, llm_util, util
from uniborg.constants import GEMINI_FLASH_LATEST, GEMINI_FLASH_LITE_LATEST

_log = logging.getLogger(__name__)

CACHE_PATH = os.path.expanduser("~/.borg/vertex_latest_models.json")
CACHE_TTL = timedelta(hours=24)
#: How soon a refresh that could not finish is tried again.
RETRY_DELAY = timedelta(hours=1)
PROBE_TIMEOUT_SECONDS = 30
#: Parallel probes; more risk rate limits, which leave the refresh incomplete.
PROBE_CONCURRENCY = 6


@dataclass(frozen=True)
class Family:
    alias: str
    baseline: str
    #: The id's tail after the version, as in `gemini-3.8-flash-lite`.
    suffix: str


FAMILIES = (
    Family(GEMINI_FLASH_LATEST, gemini_providers.VERTEX_FLASH_BASELINE, "flash"),
    Family(
        GEMINI_FLASH_LITE_LATEST,
        gemini_providers.VERTEX_FLASH_LITE_BASELINE,
        "flash-lite",
    ),
)


@dataclass(frozen=True)
class Cache:
    checked_at: datetime
    #: Alias to the Vertex model it is sent as.
    ids: dict


@dataclass(frozen=True)
class RefreshResult:
    ids: dict
    #: False when some probe failed for a reason other than "no such model",
    #: so a newer model may have been missed.
    complete: bool


def version_of(model: str, *, suffix: str) -> Optional[tuple]:
    """(major, minor, released) of a `gemini-X.Y-SUFFIX[-preview]` id, or None.

    `released` is 0 for a preview and 1 otherwise, so a version's release ranks
    above its preview.
    """
    match = re.fullmatch(
        rf"(?:gemini/)?gemini-(\d+)(?:\.(\d+))?-{re.escape(suffix)}(-preview)?",
        model,
    )
    if not match:
        return None
    return int(match.group(1)), int(match.group(2) or 0), int(not match.group(3))


def candidates_after(version: tuple, *, suffix: str) -> list:
    """Ids ranking above VERSION, through the next major version's .9.

    Each version is tried as a release and as a preview, and a major version's
    first both as `gemini-4-flash` and `gemini-4.0-flash`, since Google has used
    both forms.
    """
    major = version[0]
    names = []
    for each in (major, major + 1):
        names += [str(each)] + [f"{each}.{minor}" for minor in range(10)]
    ids = [
        f"gemini/gemini-{name}-{suffix}{tail}"
        for name in names
        for tail in ("", "-preview")
    ]
    return [model for model in ids if version_of(model, suffix=suffix) > version]


def _newest(models, *, suffix: str) -> Optional[str]:
    versioned = [m for m in models if version_of(m, suffix=suffix)]
    return max(versioned, key=lambda m: version_of(m, suffix=suffix), default=None)


async def probe(client: httpx.AsyncClient, model: str, *, api_key: str):
    """True when Vertex serves MODEL, False when it does not, None when unsure."""
    url = (
        f"{gemini_providers.VERTEX_API_BASE}/models/"
        f"{model.removeprefix('gemini/')}:countTokens"
    )
    try:
        response = await client.post(
            url,
            headers={"x-goog-api-key": api_key},
            json={"contents": [{"role": "user", "parts": [{"text": "test"}]}]},
        )
    except httpx.HTTPError:
        return None
    if response.status_code == 200:
        return True
    if response.status_code == 404:
        return False
    return None


async def refresh(
    api_key: str,
    *,
    current: Optional[dict] = None,
    client: Optional[httpx.AsyncClient] = None,
) -> RefreshResult:
    """The newest model of each family that Vertex serves.

    Probing starts after both the baseline and CURRENT's model, so a model
    Vertex stops serving falls back to the newest one it still does.
    """
    current = current or {}
    owned = client is None
    if owned:
        client = httpx.AsyncClient(
            timeout=PROBE_TIMEOUT_SECONDS, proxy=llm_util.gemini_proxy_url()
        )
    try:
        plans = {}
        for family in FAMILIES:
            starts = {family.baseline, current.get(family.alias, family.baseline)}
            ids = []
            for start in starts:
                version = version_of(start, suffix=family.suffix)
                if version:
                    ids += candidates_after(version, suffix=family.suffix)
            plans[family] = sorted(set(ids) - {family.baseline})
        everything = sorted({model for ids in plans.values() for model in ids})
        slots = asyncio.Semaphore(PROBE_CONCURRENCY)

        async def limited(model):
            async with slots:
                return await probe(client, model, api_key=api_key)

        answers = dict(zip(everything, await asyncio.gather(*map(limited, everything))))
    finally:
        if owned:
            await client.aclose()

    ids = {}
    for family, models in plans.items():
        served = [model for model in models if answers[model]]
        ids[family.alias] = (
            _newest(served + [family.baseline], suffix=family.suffix) or family.baseline
        )
    return RefreshResult(ids, all(answer is not None for answer in answers.values()))


def load_cache(path: str = CACHE_PATH) -> Optional[Cache]:
    try:
        with open(path) as file:
            data = json.load(file)
        checked_at = datetime.fromisoformat(data["checked_at"])
        ids = {
            family.alias: data["ids"][family.alias]
            for family in FAMILIES
            if isinstance(data["ids"].get(family.alias), str)
        }
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None
    if checked_at.tzinfo is None:
        return None
    return Cache(checked_at, ids)


def save_cache(cache: Cache, path: str = CACHE_PATH) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = f"{path}.{os.getpid()}.tmp"
    with open(temporary, "w") as file:
        json.dump({"checked_at": cache.checked_at.isoformat(), "ids": cache.ids}, file)
    os.replace(temporary, path)


def apply(ids: dict) -> None:
    for family in FAMILIES:
        if family.alias in ids:
            gemini_providers.VERTEX_LATEST_IDS[family.alias] = ids[family.alias]


def admin_vertex_key() -> Optional[str]:
    """The first bot admin's Vertex AI key."""
    for admin in util.admins:
        if isinstance(admin, int):
            key = llm_db.get_api_key(admin, service=gemini_providers.VERTEX)
            if key:
                return key
    return None


async def keep_fresh(
    *,
    api_key: Callable[[], Optional[str]] = admin_vertex_key,
    cache_path: str = CACHE_PATH,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    sleep: Callable[[float], Awaitable] = asyncio.sleep,
    refresher: Callable[..., Awaitable[RefreshResult]] = refresh,
) -> None:
    """Apply the cached models, and probe again whenever the cache expires.

    The cache is re-read before each probe, so a refresh done by another bot
    process is used instead of repeated.
    """
    while True:
        cache = load_cache(cache_path)
        if cache:
            apply(cache.ids)
            due = cache.checked_at + CACHE_TTL
            if due > now():
                await sleep((due - now()).total_seconds())
                continue
        key = api_key()
        if not key:
            _log.warning(
                "No admin Vertex AI key; the Vertex -latest models stay %s.",
                gemini_providers.VERTEX_LATEST_IDS,
            )
            await sleep(RETRY_DELAY.total_seconds())
            continue
        try:
            result = await refresher(
                key, current=dict(gemini_providers.VERTEX_LATEST_IDS)
            )
        except Exception:
            _log.exception("Probing the newest Vertex AI models failed.")
            await sleep(RETRY_DELAY.total_seconds())
            continue
        apply(result.ids)
        if not result.complete:
            _log.warning("Some Vertex AI model probes failed; retrying soon.")
            await sleep(RETRY_DELAY.total_seconds())
            continue
        if not cache or cache.ids != result.ids:
            _log.info("Vertex AI -latest models: %s", result.ids)
        save_cache(Cache(now(), result.ids), cache_path)


_task: Optional[asyncio.Task] = None


def start(loop: Optional[asyncio.AbstractEventLoop] = None, **kwargs) -> asyncio.Task:
    """Run `keep_fresh` once per process, however often a plugin is reloaded."""
    global _task
    if _task is None or _task.done():
        loop = loop or asyncio.get_event_loop()
        _task = loop.create_task(keep_fresh(**kwargs))
    return _task
