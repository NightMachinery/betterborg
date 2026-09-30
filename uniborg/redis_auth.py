# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

"""Which credentials the bots present to Redis, and the ACL user they own.

Terms, as in docs/redis.md:

- **Admin secret**: the password of Redis's default user, which may run any
  command. It comes from ``REDISCLI_AUTH``, else ``~/.redis-auth``: the same
  places redis-cli and the host's own scripts read it from.
- **Bot user**: a Redis ACL user, ``borg`` by default, limited to the
  ``borg:*`` keys and to the command categories in ``BOT_ACL_RULES``.
- **Bot secret**: the bot user's password, kept in ``~/.borg/redis-auth``.
- **Provisioning**: creating the bot user, or resetting it to
  ``BOT_ACL_RULES``, through a client that holds the admin secret.

Nothing here persists the bot user: no ``ACL SAVE`` and no ``CONFIG REWRITE``,
since the latter would write ``requirepass`` in plaintext into the server's
config file. A Redis restart therefore forgets the user, and the next
``connect`` provisions it again.

Keep this module import-light. In particular it must not import
``uniborg.util``, which starts shell servers when imported.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
import os
from pathlib import Path
import secrets
import tempfile
from typing import Any, Callable, Iterable, Mapping, Optional

try:
    from redis.exceptions import AuthenticationError
except ImportError:

    class AuthenticationError(Exception):
        """Stands in for redis-py's, so that this module imports without it."""


DEFAULT_REDIS_URL = "redis://localhost:6379"
DEFAULT_BOT_USER = "borg"
BOT_KEY_PATTERN = "borg:*"
DEFAULT_BOT_AUTH_FILE = Path.home() / ".borg" / "redis-auth"
DEFAULT_ADMIN_AUTH_FILE = Path.home() / ".redis-auth"

#: Applied as ``ACL SETUSER <user> reset >PASSWORD <rules...>``. Redis applies
#: the rules left to right, so the order matters: the final ``-@dangerous``
#: takes back what the categories before it granted, such as KEYS, FLUSHDB,
#: FLUSHALL, SWAPDB and CLIENT KILL.
BOT_ACL_RULES = (
    "on",
    f"~{BOT_KEY_PATTERN}",
    "resetchannels",
    "-@all",
    "+@read",
    "+@write",
    "+@keyspace",
    "+@transaction",
    "+@connection",
    "-@dangerous",
)

#: ``RedisConnection.source`` values. A connection as the bot user has the
#: source ``bot_user_source(user)``.
SOURCE_EXPLICIT_URL = "REDIS_URL"
SOURCE_ADMIN_SECRET = "admin secret"
SOURCE_NO_PASSWORD = "no password"

_REDACTED = "<redacted>"
_FLAG_TRUE = frozenset({"1", "y", "yes", "true", "on"})
_FLAG_FALSE = frozenset({"0", "n", "no", "false", "off"})


class RedisAuthError(RuntimeError):
    """A credential file is in a state this module refuses to repair."""


@dataclass(frozen=True)
class RedisAuthSettings:
    #: ``REDIS_URL``. When set, it is used verbatim and nothing is
    #: provisioned. Hidden from ``repr`` because it may carry a password.
    explicit_url: Optional[str] = field(default=None, repr=False)
    #: Where to connect otherwise. It must not carry credentials: redis-py
    #: lets credentials in the URL override the ones passed alongside it.
    base_url: str = DEFAULT_REDIS_URL
    bot_user: str = DEFAULT_BOT_USER
    bot_auth_file: Path = DEFAULT_BOT_AUTH_FILE
    admin_auth_file: Path = DEFAULT_ADMIN_AUTH_FILE
    #: The admin secret as found in the environment, if it was.
    admin_secret_env: Optional[str] = field(default=None, repr=False)
    provision: bool = True


@dataclass(frozen=True)
class RedisConnection:
    client: Any
    #: How the client authenticated; see the ``SOURCE_*`` constants.
    source: str
    #: Why a preferred route was not taken, with both secrets scrubbed out.
    warning: Optional[str] = None


def bot_user_source(user: str) -> str:
    return f"ACL user {user}"


def settings_from_env(env: Optional[Mapping[str, str]] = None) -> RedisAuthSettings:
    """Read ``RedisAuthSettings`` from ``env`` (default: ``os.environ``).

    An empty variable counts as unset. ``BORG_REDIS_PROVISION`` must be a
    yes/no value; anything else raises ValueError rather than guessing.
    """
    env = os.environ if env is None else env
    auth_file = env.get("BORG_REDIS_AUTH_FILE")
    return RedisAuthSettings(
        explicit_url=env.get("REDIS_URL") or None,
        bot_user=env.get("BORG_REDIS_USER") or DEFAULT_BOT_USER,
        bot_auth_file=(
            Path(auth_file).expanduser() if auth_file else DEFAULT_BOT_AUTH_FILE
        ),
        admin_secret_env=env.get("REDISCLI_AUTH") or None,
        provision=_env_flag(env, "BORG_REDIS_PROVISION", default=True),
    )


def _env_flag(env: Mapping[str, str], name: str, *, default: bool) -> bool:
    raw = env.get(name) or ""
    value = raw.strip().lower()
    if not value:
        return default
    elif value in _FLAG_TRUE:
        return True
    elif value in _FLAG_FALSE:
        return False
    else:
        raise ValueError(
            f"{name}={raw!r} is not a yes/no value"
            f" (expected one of {sorted(_FLAG_TRUE | _FLAG_FALSE)})"
        )


def _secret_read(path: Path) -> Optional[str]:
    """The secret in ``path``, or None when it is missing, unreadable or empty."""
    try:
        #: .strip() because the file may end in a newline, or CRLF. Secrets
        #: are hex, so no legitimate character can be stripped.
        secret = Path(path).read_text().strip()
    except (OSError, UnicodeError):
        return None
    return secret or None


def admin_secret(settings: RedisAuthSettings) -> Optional[str]:
    """The admin secret: the environment's first, else the admin auth file's."""
    return settings.admin_secret_env or _secret_read(settings.admin_auth_file)


def bot_secret_ensure(
    path: Path,
    *,
    token: Callable[[int], str] = secrets.token_hex,
) -> str:
    """The bot secret in ``path``, creating the file if it does not exist.

    Creation writes ``token(32)`` to a temporary file (mode 600) beside the
    target and hard-links it into place. ``link`` fails when the target
    exists, atomically, so processes racing to create the file cannot end up
    believing in two different secrets: each loser adopts the winner's file.

    A target that exists but holds no secret raises RedisAuthError: it may be
    someone's file, and this never overwrites one.
    """
    path = Path(path)
    secret = _secret_read(path)
    if secret:
        return secret

    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w") as temp_file:
            temp_file.write(f"{token(32)}\n")
        with contextlib.suppress(FileExistsError):
            os.link(temp_path, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temp_path)

    secret = _secret_read(path)
    if not secret:
        raise RedisAuthError(
            f"{path} exists but holds no readable secret; refusing to overwrite it"
        )
    return secret


async def provision_bot_user(
    admin: Any,
    *,
    user: str,
    password: str,
    rules: Iterable[str] = BOT_ACL_RULES,
) -> bool:
    """Create ``user``, or reset it to ``rules``; return whether that changed it.

    ``reset`` first, so that a rule dropped from ``rules`` is dropped from
    Redis too. ``ACL GETUSER`` before and after tells a first start (or a start
    after a Redis restart) apart from a routine re-assertion. It reports
    password hashes, never passwords.
    """
    before = await admin.execute_command("ACL", "GETUSER", user)
    await admin.execute_command("ACL", "SETUSER", user, "reset", f">{password}", *rules)
    after = await admin.execute_command("ACL", "GETUSER", user)
    return before != after


async def close_quietly(client: Any) -> None:
    """Close ``client``, ignoring errors: it is being discarded anyway."""
    with contextlib.suppress(Exception):
        await client.aclose()


async def _open(from_url: Callable[..., Any], url: str, **credentials: Any) -> Any:
    """A client for ``url`` that has answered PING. A failed client is closed."""
    client = from_url(url, decode_responses=True, **credentials)
    try:
        await client.ping()
    except Exception:
        await close_quietly(client)
        raise
    return client


def _describe(error: BaseException, *, hidden: Iterable[Optional[str]]) -> str:
    text = f"{type(error).__name__}: {error}"
    for secret in hidden:
        if secret:
            text = text.replace(secret, _REDACTED)
    return text


def _warning(problems: list) -> Optional[str]:
    return "; ".join(problems) or None


async def connect(
    settings: RedisAuthSettings,
    *,
    from_url: Callable[..., Any],
    log: Callable[[str], Any] = print,
) -> RedisConnection:
    """Connect to Redis with the best credentials available.

    In order:

    1. ``explicit_url``, verbatim, with nothing provisioned.
    2. With the admin secret and ``provision`` on: ensure the bot secret file
       and provision the bot user. A failure here only adds to the warning.
    3. With a bot secret: connect as the bot user. If Redis rejects it, raise,
       unless there is an admin secret to fall back to.
    4. With the admin secret: connect with it, and warn.
    5. Otherwise, or when Redis rejected the admin secret because it asks for
       no password at all: connect without a password.

    ``from_url`` is ``redis.asyncio.from_url`` or a stand-in; this uses only a
    client's ``execute_command``, ``ping`` and ``aclose``.
    """
    if settings.explicit_url:
        client = await _open(from_url, settings.explicit_url)
        return RedisConnection(client=client, source=SOURCE_EXPLICIT_URL)

    user = settings.bot_user
    admin_password = admin_secret(settings)
    bot_password = None
    problems = []

    if admin_password and settings.provision:
        try:
            bot_password = bot_secret_ensure(settings.bot_auth_file)
            admin = from_url(
                settings.base_url, decode_responses=True, password=admin_password
            )
            try:
                changed = await provision_bot_user(
                    admin, user=user, password=bot_password
                )
            finally:
                await close_quietly(admin)
            if changed:
                log(f"RedisAuth: Provisioned Redis ACL user {user!r}")
        except Exception as e:
            problems.append(
                f"could not provision ACL user {user!r}"
                f" ({_describe(e, hidden=(admin_password, bot_password))})"
            )

    if bot_password is None:
        bot_password = _secret_read(settings.bot_auth_file)
    if bot_password:
        try:
            client = await _open(
                from_url, settings.base_url, username=user, password=bot_password
            )
        except AuthenticationError as e:
            if not admin_password:
                raise
            problems.append(
                f"Redis rejected ACL user {user!r}"
                f" ({_describe(e, hidden=(admin_password, bot_password))})"
            )
        else:
            return RedisConnection(
                client=client,
                source=bot_user_source(user),
                warning=_warning(problems),
            )

    admin_error = None
    if admin_password:
        try:
            client = await _open(from_url, settings.base_url, password=admin_password)
        except AuthenticationError as e:
            admin_error = e
            problems.append(
                "Redis rejected the admin secret"
                f" ({_describe(e, hidden=(admin_password, bot_password))})"
            )
        else:
            problems.append(f"connected with the admin secret instead of {user!r}")
            return RedisConnection(
                client=client,
                source=SOURCE_ADMIN_SECRET,
                warning=_warning(problems),
            )

    try:
        client = await _open(from_url, settings.base_url)
    except Exception:
        if admin_error is not None:
            raise admin_error
        raise
    if admin_error is not None:
        problems.append(
            "connected without a password: this Redis requires none,"
            " so it is not protected"
        )
    return RedisConnection(
        client=client, source=SOURCE_NO_PASSWORD, warning=_warning(problems)
    )
