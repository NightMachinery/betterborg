"""Telegram usernames, including active collectible aliases."""

from collections.abc import Iterable
import re
from typing import Any

Usernames = str | Iterable[str]


def normalize_usernames(usernames: Usernames | None) -> tuple[str, ...]:
    """Bare names in input order, deduplicated without regard to case."""
    if isinstance(usernames, str):
        usernames = (usernames,)
    names = {}
    for name in usernames or ():
        if isinstance(name, str):
            name = name.strip().lstrip("@")
            if name:
                names.setdefault(name.casefold(), name)
    return tuple(names.values())


def active_usernames(entity: Any) -> tuple[str, ...]:
    """The primary name first, followed by active aliases from Telegram."""
    return normalize_usernames(
        [getattr(entity, "username", None)]
        + [
            getattr(alias, "username", None)
            for alias in getattr(entity, "usernames", None) or ()
            if getattr(alias, "active", False)
        ]
    )


def username_pattern(usernames: Usernames) -> str:
    """An escaped regex alternative; an empty set matches nothing."""
    names = normalize_usernames(usernames)
    if not names:
        return r"(?!)"
    return "(?:" + "|".join(re.escape(name) for name in names) + ")"
