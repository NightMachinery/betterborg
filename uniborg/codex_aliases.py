"""Codex model aliases, such as "the newest GPT Sol", resolved from the catalog.

Codex has no `latest` alias of its own: every model in its catalog is a
pinned slug (docs/codex_models.md). So the bot registers one stable id per
family, such as `openai-codex/gpt-sol-latest`, and `refresh` points each at
the newest slug of that family that the catalog offers through the API.
Saved settings keep the stable id, so an upgrade needs no commit, and the
admins get a message whenever an alias moves.

An alias starts at its family's pinned slug. What `refresh` finds is kept in
Redis, so a restart neither forgets it nor alerts again, and every process
sharing the Redis agrees.
"""

import asyncio
from dataclasses import dataclass
import json
import logging
import re
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from uniborg import redis_util
from uniborg.constants import (
    OPENAI_CODEX_ASTRA,
    OPENAI_CODEX_LUNA,
    OPENAI_CODEX_SOL,
)

_log = logging.getLogger(__name__)

#: The reasoning levels the menus offer for OpenAI models. The Responses API
#: also takes `minimal`, which no menu offers. The catalog also lists
#: `ultra`, a Codex-app subagent mode that the API rejects.
OPENAI_REASONING_LEVELS = ("none", "low", "medium", "high", "xhigh", "max")
#: GPT-6 Astra and GPT-6.1 Sol reject `none` (probed live, 2026-10-01).
OPENAI_NO_NONE_REASONING_LEVELS = OPENAI_REASONING_LEVELS[1:]

#: Where the resolved aliases are kept.
REDIS_KEY = "borg:codex_aliases"
#: How often `refresh_forever` reads the catalog.
REFRESH_SECONDS = 3600

_SLUG_RE = re.compile(r"^gpt-(?P<version>\d+(?:\.\d+)*)-(?P<name>[a-z]+)$")


@dataclass(frozen=True)
class Family:
    #: The stable model id users select.
    alias_id: str
    #: Its catalog slugs are `gpt-<version>-<name>`.
    name: str
    #: The slug used until the catalog has been read.
    pinned_slug: str
    #: The pinned slug's levels. The catalog never lists `none`, so a family
    #: offers `none` only when it is here (probed live).
    pinned_levels: Tuple[str, ...]


FAMILIES = (
    Family(OPENAI_CODEX_SOL, "sol", "gpt-6.1-sol", OPENAI_NO_NONE_REASONING_LEVELS),
    Family(OPENAI_CODEX_ASTRA, "astra", "gpt-6-astra", OPENAI_NO_NONE_REASONING_LEVELS),
    Family(OPENAI_CODEX_LUNA, "luna", "gpt-6-luna", OPENAI_REASONING_LEVELS),
)
_FAMILIES_BY_ID = {family.alias_id: family for family in FAMILIES}


@dataclass(frozen=True)
class Resolution:
    """The model an alias points at."""

    slug: str
    reasoning_levels: Tuple[str, ...]

    @property
    def display_name(self) -> str:
        """`GPT-6.1 Sol` for `gpt-6.1-sol`."""
        match = _SLUG_RE.match(self.slug)
        if match is None:
            return self.slug
        return f"GPT-{match['version']} {match['name'].title()}"


@dataclass(frozen=True)
class AliasChange:
    alias_id: str
    old: Resolution
    #: None when the catalog no longer offers any model of the family.
    new: Optional[Resolution]

    def message(self) -> str:
        if self.new is None:
            return (
                f"Codex alias `{self.alias_id}`: the catalog no longer offers any "
                f"model of its family, so it stays on `{self.old.slug}`."
            )
        text = f"Codex alias `{self.alias_id}`"
        if self.new.slug != self.old.slug:
            text += (
                f" now points at `{self.new.slug}` ({self.new.display_name}), "
                f"was `{self.old.slug}`."
            )
        else:
            text += f" still points at `{self.new.slug}`."
        if self.new.reasoning_levels != self.old.reasoning_levels:
            text += (
                f" Reasoning levels: {', '.join(self.new.reasoning_levels)}"
                f" (were {', '.join(self.old.reasoning_levels)})."
            )
        return text


#: The aliases `refresh` resolved; an absent one is at its pinned slug.
_resolved: Dict[str, Resolution] = {}
#: Aliases whose family the catalog no longer offers, so that is said once.
_missing = set()


def family_for(model: Optional[str]) -> Optional[Family]:
    return _FAMILIES_BY_ID.get(model)


def pinned(family: Family) -> Resolution:
    return Resolution(family.pinned_slug, family.pinned_levels)


def resolution_for(model: Optional[str]) -> Optional[Resolution]:
    """What the alias MODEL points at now; None when MODEL is not an alias."""
    family = family_for(model)
    if family is None:
        return None
    return _resolved.get(model) or pinned(family)


def slug_for(model: Optional[str]) -> Optional[str]:
    resolution = resolution_for(model)
    return resolution.slug if resolution else None


# --- The catalog ---


def _version(slug: str) -> Tuple[int, ...]:
    return tuple(int(part) for part in _SLUG_RE.match(slug)["version"].split("."))


def latest_entry(models: List[dict], family: Family) -> Optional[dict]:
    """The catalog entry of FAMILY with the highest version that the API serves.

    Only listed entries count: hidden ones (`visibility: hide`) are internal
    or routing slugs.
    """
    candidates = []
    for entry in models:
        match = _SLUG_RE.match(entry.get("slug") or "")
        if match is None or match["name"] != family.name:
            continue
        if not entry.get("supported_in_api") or entry.get("visibility") != "list":
            continue
        candidates.append(entry)
    return max(
        candidates,
        key=lambda entry: (_version(entry["slug"]), -(entry.get("priority") or 0)),
        default=None,
    )


def _entry_levels(entry: dict, family: Family) -> Tuple[str, ...]:
    listed = set()
    for level in entry.get("supported_reasoning_levels") or ():
        listed.add(level.get("effort") if isinstance(level, dict) else level)
    levels = tuple(level for level in OPENAI_REASONING_LEVELS if level in listed)
    if not levels:
        return family.pinned_levels
    if "none" in family.pinned_levels:
        levels = ("none",) + levels
    return levels


def resolve_entry(entry: dict, family: Family) -> Resolution:
    return Resolution(entry["slug"], _entry_levels(entry, family))


# --- State ---


async def load() -> None:
    """Reads the aliases that `refresh` last saved, from any process."""
    raw = await redis_util.get_and_renew(REDIS_KEY, renew=False)
    if not raw:
        return
    try:
        state = json.loads(raw)
        resolved = {
            alias_id: Resolution(item["slug"], tuple(item["levels"]))
            for alias_id, item in state.get("aliases", {}).items()
            if alias_id in _FAMILIES_BY_ID
        }
        missing = {alias_id for alias_id in state.get("missing", ())}
    except (ValueError, KeyError, TypeError, AttributeError):
        _log.warning("Ignoring unreadable Codex aliases in Redis", exc_info=True)
        return
    _resolved.clear()
    _resolved.update(resolved)
    _missing.clear()
    _missing.update(missing & set(_FAMILIES_BY_ID))


async def _save() -> None:
    state = {
        "aliases": {
            alias_id: {"slug": res.slug, "levels": list(res.reasoning_levels)}
            for alias_id, res in _resolved.items()
        },
        "missing": sorted(_missing),
    }
    await redis_util.set_with_expiry(
        REDIS_KEY,
        json.dumps(state),
        expire_seconds=redis_util.get_very_long_expire_duration(),
    )


async def _fetch_catalog() -> List[dict]:
    from uniborg import codex_util

    return await codex_util.fetch_codex_catalog()


async def refresh(
    *,
    fetch_catalog: Optional[Callable[[], Awaitable[List[dict]]]] = None,
    notify: Optional[Callable[[str], Awaitable[Any]]] = None,
) -> List[AliasChange]:
    """Points each alias at its family's newest model, and reports what moved.

    Starts from what Redis holds, so a change another process already
    announced is not announced again. NOTIFY gets one message per change.
    """
    models = await (fetch_catalog or _fetch_catalog)()
    await load()
    missing_before = set(_missing)
    changes = []
    for family in FAMILIES:
        old = resolution_for(family.alias_id)
        entry = latest_entry(models, family)
        if entry is None:
            if family.alias_id not in _missing:
                _missing.add(family.alias_id)
                changes.append(AliasChange(family.alias_id, old=old, new=None))
            continue
        _missing.discard(family.alias_id)
        new = resolve_entry(entry, family)
        if new != old:
            _resolved[family.alias_id] = new
            changes.append(AliasChange(family.alias_id, old=old, new=new))
    if changes or _missing != missing_before:
        await _save()
    for change in changes:
        _log.warning(change.message())
        if notify is None:
            continue
        try:
            await notify(change.message())
        except Exception:
            _log.warning("Could not announce a Codex alias change", exc_info=True)
    return changes


async def refresh_forever(
    *,
    interval_seconds: float = REFRESH_SECONDS,
    fetch_catalog: Optional[Callable[[], Awaitable[List[dict]]]] = None,
    notify: Optional[Callable[[str], Awaitable[Any]]] = None,
    on_change: Optional[Callable[[], Any]] = None,
) -> None:
    """`refresh` now and every INTERVAL_SECONDS; ON_CHANGE runs after any change."""
    while True:
        try:
            if await refresh(fetch_catalog=fetch_catalog, notify=notify):
                if on_change is not None:
                    on_change()
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.warning("Could not refresh the Codex aliases", exc_info=True)
        await asyncio.sleep(interval_seconds)
