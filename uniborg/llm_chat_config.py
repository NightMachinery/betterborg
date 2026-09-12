"""Reloadable access policy for the llm_chat plugin."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple, Union

import json5


logger = logging.getLogger(__name__)

MAGIC_ADMINS = "MAGIC_ADMINS"
DEFAULT_CONFIG_TEXT = """// Betterborg llm_chat access policy.
// Numeric entries are Telegram user IDs. MAGIC_ADMINS uses util.isAdmin(),
// which also includes trusted chats.
{
  codex_allowed_users: ["MAGIC_ADMINS"],
  codex_imagegen_allowed_users: ["MAGIC_ADMINS"],
}
"""

PolicyEntry = Union[int, str]


@dataclass(frozen=True)
class LLMChatConfig:
    codex_allowed_users: Tuple[PolicyEntry, ...]
    codex_imagegen_allowed_users: Tuple[PolicyEntry, ...]
    valid: bool = True


DENY_ALL_CONFIG = LLMChatConfig((), (), valid=False)


def config_path() -> Path:
    override = os.environ.get("LLM_CHAT_CONFIG_PATH")
    return Path(override).expanduser() if override else Path.home() / ".borg" / "llm_chat_config.json5"


def _parse_json5(text: str) -> dict:
    return json5.loads(text)


def _validate_policy(value, key: str) -> Tuple[PolicyEntry, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{key} must be an array")
    result = []
    for entry in value:
        if isinstance(entry, bool) or not (
            isinstance(entry, int) or entry == MAGIC_ADMINS
        ):
            raise ValueError(
                f'{key} entries must be numeric Telegram IDs or "{MAGIC_ADMINS}"'
            )
        result.append(entry)
    return tuple(result)


def parse_config(text: str) -> LLMChatConfig:
    data = _parse_json5(text)
    if not isinstance(data, dict):
        raise ValueError("configuration root must be an object")
    required = ("codex_allowed_users", "codex_imagegen_allowed_users")
    missing = [key for key in required if key not in data]
    if missing:
        raise ValueError(f"missing required key(s): {', '.join(missing)}")
    return LLMChatConfig(
        codex_allowed_users=_validate_policy(data[required[0]], required[0]),
        codex_imagegen_allowed_users=_validate_policy(data[required[1]], required[1]),
    )


class LLMChatConfigLoader:
    """Load on demand and reload whenever the file identity changes."""

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path is not None else config_path()
        self._signature = None
        self._text: Optional[str] = None
        self._config: Optional[LLMChatConfig] = None

    def _ensure_file(self) -> None:
        if self.path.exists():
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self.path.open("x", encoding="utf-8") as config_file:
                config_file.write(DEFAULT_CONFIG_TEXT)
        except FileExistsError:
            pass

    def load(self) -> LLMChatConfig:
        text = None
        try:
            self._ensure_file()
            stat = self.path.stat()
            signature = (
                stat.st_dev,
                stat.st_ino,
                stat.st_mode,
                stat.st_mtime_ns,
                stat.st_ctime_ns,
                stat.st_size,
            )
            text = self.path.read_text(encoding="utf-8")
            if (
                signature == self._signature
                and text == self._text
                and self._config is not None
            ):
                return self._config
            config = parse_config(text)
        except Exception as exc:
            logger.error("Invalid or unreadable llm_chat config %s: %s", self.path, exc)
            config = DENY_ALL_CONFIG
            try:
                stat = self.path.stat()
                signature = (
                    stat.st_dev,
                    stat.st_ino,
                    stat.st_mode,
                    stat.st_mtime_ns,
                    stat.st_ctime_ns,
                    stat.st_size,
                )
            except OSError:
                signature = None
        self._signature = signature
        self._text = text
        self._config = config
        return config


_loader: Optional[LLMChatConfigLoader] = None


def load_config() -> LLMChatConfig:
    global _loader
    path = config_path()
    if _loader is None or _loader.path != path:
        _loader = LLMChatConfigLoader(path)
    return _loader.load()


async def policy_allows(event, policy: Tuple[PolicyEntry, ...]) -> bool:
    sender_id = getattr(event, "sender_id", None)
    if sender_id in policy:
        return True
    if MAGIC_ADMINS in policy:
        from uniborg import util

        return await util.isAdmin(event)
    return False


async def can_use_codex(event, config: LLMChatConfig) -> bool:
    return config.valid and await policy_allows(event, config.codex_allowed_users)


async def can_use_codex_imagegen(event, config: LLMChatConfig) -> bool:
    return (
        config.valid
        and await policy_allows(event, config.codex_allowed_users)
        and await policy_allows(event, config.codex_imagegen_allowed_users)
    )
