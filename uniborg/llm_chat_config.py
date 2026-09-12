"""Reloadable access policy for the llm_chat plugin."""

from __future__ import annotations

import logging
import os
import fcntl
import json
import stat as stat_module
import tempfile
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
  codex_users: [],
}
"""

PolicyEntry = Union[int, str]


@dataclass(frozen=True)
class CodexUser:
    id: int
    name: Optional[str]
    codex_enabled: bool
    imagegen_enabled: bool


@dataclass(frozen=True)
class LLMChatConfig:
    codex_allowed_users: Tuple[PolicyEntry, ...]
    codex_imagegen_allowed_users: Tuple[PolicyEntry, ...]
    valid: bool = True
    codex_users: Tuple[CodexUser, ...] = ()


DENY_ALL_CONFIG = LLMChatConfig((), (), valid=False)


def config_path() -> Path:
    override = os.environ.get("LLM_CHAT_CONFIG_PATH")
    return Path(override).expanduser() if override else Path.home() / ".borg" / "llm_chat_config.json5"


def _parse_json5(text: str) -> dict:
    return json5.loads(text, allow_duplicate_keys=False)


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
    users_value = data.get("codex_users", [])
    if not isinstance(users_value, list):
        raise ValueError("codex_users must be an array")
    users = []
    seen_ids = set()
    for index, value in enumerate(users_value):
        if not isinstance(value, dict):
            raise ValueError(f"codex_users[{index}] must be an object")
        allowed_keys = {"id", "name", "codex_enabled", "imagegen_enabled"}
        extra = set(value) - allowed_keys
        if extra:
            raise ValueError(f"codex_users[{index}] has unknown key(s): {', '.join(sorted(extra))}")
        user_id = value.get("id")
        if isinstance(user_id, bool) or not isinstance(user_id, int):
            raise ValueError(f"codex_users[{index}].id must be an integer")
        if user_id in seen_ids:
            raise ValueError(f"duplicate codex_users id: {user_id}")
        name = value.get("name")
        if name is not None and not isinstance(name, str):
            raise ValueError(f"codex_users[{index}].name must be a string")
        for key in ("codex_enabled", "imagegen_enabled"):
            if type(value.get(key)) is not bool:
                raise ValueError(f"codex_users[{index}].{key} must be a boolean")
        seen_ids.add(user_id)
        users.append(CodexUser(user_id, name, value["codex_enabled"], value["imagegen_enabled"]))
    return LLMChatConfig(
        codex_allowed_users=_validate_policy(data[required[0]], required[0]),
        codex_imagegen_allowed_users=_validate_policy(data[required[1]], required[1]),
        codex_users=tuple(users),
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


def configured_users(config: LLMChatConfig) -> Tuple[CodexUser, ...]:
    """Return roster records plus synthetic records for legacy numeric grants."""
    if not config.valid:
        return ()
    users = list(config.codex_users)
    roster_ids = {user.id for user in users}
    legacy_ids = []
    for entry in config.codex_allowed_users + config.codex_imagegen_allowed_users:
        if isinstance(entry, int) and not isinstance(entry, bool) and entry not in legacy_ids:
            legacy_ids.append(entry)
    for user_id in legacy_ids:
        if user_id not in roster_ids:
            users.append(CodexUser(
                user_id,
                None,
                user_id in config.codex_allowed_users,
                user_id in config.codex_imagegen_allowed_users,
            ))
    return tuple(users)


async def _sentinel_allows(event, policy: Tuple[PolicyEntry, ...]) -> bool:
    if MAGIC_ADMINS not in policy:
        return False
    from uniborg import util
    return await util.isAdmin(event)


def _manual_flag(sender_id, config: LLMChatConfig, capability: str) -> bool:
    for user in config.codex_users:
        if user.id == sender_id:
            return getattr(user, capability)
    policy = config.codex_allowed_users if capability == "codex_enabled" else config.codex_imagegen_allowed_users
    return sender_id in policy


async def can_use_codex(event, config: LLMChatConfig) -> bool:
    if not config.valid:
        return False
    sender_id = getattr(event, "sender_id", None)
    return _manual_flag(sender_id, config, "codex_enabled") or await _sentinel_allows(event, config.codex_allowed_users)


async def can_use_codex_imagegen(event, config: LLMChatConfig) -> bool:
    if not config.valid:
        return False
    sender_id = getattr(event, "sender_id", None)
    codex = _manual_flag(sender_id, config, "codex_enabled") or await _sentinel_allows(event, config.codex_allowed_users)
    imagegen = _manual_flag(sender_id, config, "imagegen_enabled") or await _sentinel_allows(event, config.codex_imagegen_allowed_users)
    return codex and imagegen


class ConfigUpdateError(RuntimeError):
    """Raised when an access-policy update cannot be safely persisted."""


@dataclass(frozen=True)
class _Token:
    value: str
    start: int
    end: int


def _tokens(text: str):
    """Tokenize enough JSON5 structure to safely find object and array spans."""
    result = []
    i = 0
    punctuation = "{}[]:,"
    while i < len(text):
        if text[i].isspace():
            i += 1
            continue
        if text.startswith("//", i):
            endings = [text.find(char, i + 2) for char in ("\n", "\r", "\u2028", "\u2029")]
            endings = [ending for ending in endings if ending >= 0]
            i = len(text) if not endings else min(endings) + 1
            continue
        if text.startswith("/*", i):
            end = text.find("*/", i + 2)
            if end < 0:
                raise ConfigUpdateError("unterminated comment in configuration")
            i = end + 2
            continue
        start = i
        if text[i] in "'\"":
            quote = text[i]
            i += 1
            while i < len(text):
                if text[i] == "\\":
                    i += 2
                elif text[i] == quote:
                    i += 1
                    break
                else:
                    i += 1
            else:
                raise ConfigUpdateError("unterminated string in configuration")
        elif text[i] in punctuation:
            i += 1
        else:
            while i < len(text) and not text[i].isspace() and text[i] not in punctuation and not text.startswith("//", i) and not text.startswith("/*", i):
                i += 1
        result.append(_Token(text[start:i], start, i))
    return result


def _matching(tokens, start_index, opening, closing):
    depth = 0
    for index in range(start_index, len(tokens)):
        if tokens[index].value == opening:
            depth += 1
        elif tokens[index].value == closing:
            depth -= 1
            if depth == 0:
                return index
    raise ConfigUpdateError(f"unclosed {opening} in configuration")


def _key_value_span(text: str, tokens, object_start: int, object_end: int, key: str):
    depth = 0
    i = object_start + 1
    while i < object_end:
        token = tokens[i]
        if token.value in ("{", "["):
            depth += 1
        elif token.value in ("}", "]"):
            depth -= 1
        elif depth == 0 and i + 2 < object_end and tokens[i + 1].value == ":":
            raw = token.value
            if raw[:1] in ("'", '"'):
                decoded = json5.loads(raw, allow_duplicate_keys=False)
            else:
                try:
                    decoded = next(iter(json5.loads(
                        "{" + raw + ":null}", allow_duplicate_keys=False
                    )))
                except Exception as exc:
                    raise ConfigUpdateError(f"unsupported JSON5 property name {raw!r}") from exc
            if decoded == key:
                value_index = i + 2
                value_token = tokens[value_index]
                if value_token.value == "[":
                    end_index = _matching(tokens, value_index, "[", "]")
                elif value_token.value == "{":
                    end_index = _matching(tokens, value_index, "{", "}")
                else:
                    end_index = value_index
                return value_index, end_index
        i += 1
    return None


def _updated_source(text: str, user_id: int, capability: str, enabled: bool, config: LLMChatConfig) -> str:
    tokens = _tokens(text)
    if not tokens or tokens[0].value != "{":
        raise ConfigUpdateError("configuration root span could not be located")
    root_end = _matching(tokens, 0, "{", "}")
    roster = _key_value_span(text, tokens, 0, root_end, "codex_users")
    desired = "true" if enabled else "false"
    if roster is not None:
        array_start, array_end = roster
        if tokens[array_start].value != "[":
            raise ConfigUpdateError("codex_users source is not an array")
        i = array_start + 1
        while i < array_end:
            if tokens[i].value == "{":
                obj_end = _matching(tokens, i, "{", "}")
                id_span = _key_value_span(text, tokens, i, obj_end, "id")
                if id_span and json5.loads(tokens[id_span[0]].value) == user_id:
                    flag_span = _key_value_span(text, tokens, i, obj_end, capability)
                    if not flag_span:
                        raise ConfigUpdateError(f"existing roster record lacks {capability}")
                    flag_token = tokens[flag_span[0]]
                    if flag_token.value == desired:
                        return text
                    return text[:flag_token.start] + desired + text[flag_token.end:]
                i = obj_end
            i += 1
        legacy = next(user for user in configured_users(config) if user.id == user_id)
        codex = enabled if capability == "codex_enabled" else legacy.codex_enabled
        imagegen = enabled if capability == "imagegen_enabled" else legacy.imagegen_enabled
        empty = array_end == array_start + 1
        has_trailing_comma = not empty and tokens[array_end - 1].value == ","
        prefix = "" if empty or has_trailing_comma else ","
        insertion = f'{prefix}\n    {{id: {user_id}, codex_enabled: {str(codex).lower()}, imagegen_enabled: {str(imagegen).lower()}}},'
        position = tokens[array_end].start
        return text[:position] + insertion + text[position:]
    legacy = next(user for user in configured_users(config) if user.id == user_id)
    codex = enabled if capability == "codex_enabled" else legacy.codex_enabled
    imagegen = enabled if capability == "imagegen_enabled" else legacy.imagegen_enabled
    position = tokens[root_end].start
    empty = root_end == 1
    has_trailing_comma = not empty and tokens[root_end - 1].value == ","
    prefix = "" if empty or has_trailing_comma else ","
    insertion = f'{prefix}\n  codex_users: [{{id: {user_id}, codex_enabled: {str(codex).lower()}, imagegen_enabled: {str(imagegen).lower()}}}],\n'
    return text[:position] + insertion + text[position:]


def _added_user_source(text: str, user_id: int, name: Optional[str]) -> str:
    tokens = _tokens(text)
    if not tokens or tokens[0].value != "{":
        raise ConfigUpdateError("configuration root span could not be located")
    root_end = _matching(tokens, 0, "{", "}")
    roster = _key_value_span(text, tokens, 0, root_end, "codex_users")
    name_source = "" if name is None else f", name: {json.dumps(name, ensure_ascii=False)}"
    record = (
        f"{{id: {user_id}{name_source}, codex_enabled: false, "
        "imagegen_enabled: false}"
    )
    if roster is not None:
        array_start, array_end = roster
        if tokens[array_start].value != "[":
            raise ConfigUpdateError("codex_users source is not an array")
        empty = array_end == array_start + 1
        has_trailing_comma = not empty and tokens[array_end - 1].value == ","
        prefix = "" if empty or has_trailing_comma else ","
        insertion = f"{prefix}\n    {record},"
        position = tokens[array_end].start
        return text[:position] + insertion + text[position:]
    position = tokens[root_end].start
    empty = root_end == 1
    has_trailing_comma = not empty and tokens[root_end - 1].value == ","
    prefix = "" if empty or has_trailing_comma else ","
    insertion = f"{prefix}\n  codex_users: [{record}],\n"
    return text[:position] + insertion + text[position:]


def _write_config_update(transform) -> LLMChatConfig:
    try:
        requested_path = config_path()
        path = requested_path.resolve(strict=True)
        lock_path = path.with_name(path.name + ".lock")
        with lock_path.open("a+") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            before_stat = path.stat()
            original = path.read_text(encoding="utf-8")
            config = parse_config(original)
            updated, expected = transform(original, config)
            if updated == original:
                return config
            candidate = parse_config(updated)
            if candidate != expected:
                raise ConfigUpdateError("generated configuration did not preserve policy semantics")
            fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as temp_file:
                    temp_file.write(updated)
                    temp_file.flush()
                    os.fsync(temp_file.fileno())
                os.chmod(temp_name, stat_module.S_IMODE(before_stat.st_mode))
                identity_before = (
                    before_stat.st_dev,
                    before_stat.st_ino,
                    before_stat.st_mode,
                    before_stat.st_mtime_ns,
                    before_stat.st_ctime_ns,
                    before_stat.st_size,
                )
                current_stat = path.stat()
                current = path.read_text(encoding="utf-8")
                final_stat = path.stat()
                identity_current = (
                    current_stat.st_dev,
                    current_stat.st_ino,
                    current_stat.st_mode,
                    current_stat.st_mtime_ns,
                    current_stat.st_ctime_ns,
                    current_stat.st_size,
                )
                identity_final = (
                    final_stat.st_dev,
                    final_stat.st_ino,
                    final_stat.st_mode,
                    final_stat.st_mtime_ns,
                    final_stat.st_ctime_ns,
                    final_stat.st_size,
                )
                if identity_current != identity_before or identity_final != identity_current or current != original:
                    raise ConfigUpdateError("configuration changed while the update was being prepared")
                if requested_path.resolve(strict=True) != path:
                    raise ConfigUpdateError("configuration symlink target changed during update")
                os.replace(temp_name, path)
            finally:
                try:
                    os.unlink(temp_name)
                except FileNotFoundError:
                    pass
            return candidate
    except ConfigUpdateError:
        raise
    except Exception as exc:
        target = locals().get("path", locals().get("requested_path", config_path()))
        raise ConfigUpdateError(f"could not update {target}: {exc}") from exc


def update_user_access(user_id: int, capability: str, enabled: bool) -> LLMChatConfig:
    if isinstance(user_id, bool) or not isinstance(user_id, int):
        raise ConfigUpdateError("user_id must be an integer")
    if capability not in ("codex_enabled", "imagegen_enabled"):
        raise ConfigUpdateError("capability must be codex_enabled or imagegen_enabled")
    if type(enabled) is not bool:
        raise ConfigUpdateError("enabled must be a boolean")

    def transform(original, config):
        if not any(user.id == user_id for user in configured_users(config)):
            raise ConfigUpdateError(f"user {user_id} is not configured")
        updated = _updated_source(original, user_id, capability, enabled, config)
        roster = list(config.codex_users)
        for index, user in enumerate(roster):
            if user.id == user_id:
                roster[index] = CodexUser(
                    user.id,
                    user.name,
                    enabled if capability == "codex_enabled" else user.codex_enabled,
                    enabled if capability == "imagegen_enabled" else user.imagegen_enabled,
                )
                break
        else:
            legacy = next(user for user in configured_users(config) if user.id == user_id)
            roster.append(CodexUser(
                user_id,
                None,
                enabled if capability == "codex_enabled" else legacy.codex_enabled,
                enabled if capability == "imagegen_enabled" else legacy.imagegen_enabled,
            ))
        expected = LLMChatConfig(
            config.codex_allowed_users,
            config.codex_imagegen_allowed_users,
            valid=True,
            codex_users=tuple(roster),
        )
        return updated, expected

    return _write_config_update(transform)


def add_user(user_id: int, *, name: Optional[str] = None) -> LLMChatConfig:
    """Append a disabled roster record unless the user is already configured."""
    if (
        isinstance(user_id, bool)
        or not isinstance(user_id, int)
        or not 0 < user_id <= 2**63 - 1
    ):
        raise ConfigUpdateError("user_id must be a positive signed 64-bit integer")
    if name is not None and not isinstance(name, str):
        raise ConfigUpdateError("name must be a string or None")

    def transform(original, config):
        if any(user.id == user_id for user in configured_users(config)):
            return original, config
        user = CodexUser(user_id, name, False, False)
        updated = _added_user_source(original, user_id, name)
        expected = LLMChatConfig(
            config.codex_allowed_users,
            config.codex_imagegen_allowed_users,
            valid=True,
            codex_users=config.codex_users + (user,),
        )
        return updated, expected

    return _write_config_update(transform)
