# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""Receiving guest queries and answering them safely.

Guest mode (Bot API 10.0) lets a bot answer where it was mentioned, in a chat
it is not a member of; see docs/guest_mode.md for the whole flow. The rules
this module enforces:

- **The caller is the trigger's sender.** In a private chat the trigger's
  `peer_id` is the *other* participant, so anything keyed by, read from or sent
  to the chat id would reach the bot's own DM with someone else. Guest
  messages are bound to a `GuestClient` that refuses such calls, and
  `GuestEvent` has no chat to reply to.
- **One answer per query.** Queries are claimed by id before any handler runs,
  in Redis when available, so a redelivered query is dropped after a restart.
- **Echoes are not commands.** In groups the bot receives its own guest answer
  back as a new outgoing message; `is_guest_answer` recognises it.
- **Automation does not summon the shell.** Telegram finds mentions in plain
  text, so a userbot relaying text that starts with ``@somebot .a …`` would run
  it as its owner; `OutgoingTriggerGuardMixin` defangs such text on its way out.

This module imports only the standard library, Telethon and `tg_format`,
never ``uniborg.util``, so ``util`` can import it.
"""
import asyncio
from dataclasses import dataclass, field
import enum
import itertools
import json
import logging
import os
import re
import time
from typing import Any, Awaitable, Callable, Optional

from telethon import errors, events, functions, types, utils

from uniborg import tg_format

_log = logging.getLogger(__name__)

#: Set on every message built from a guest query.
GUEST_MESSAGE_ATTR = "_borg_guest_query_id"

#: Where `redis_claim_backend` keeps its keys. The bots' Redis user may only
#: touch `borg:*` keys.
REDIS_CLAIM_PREFIX = "borg:guest:claim:"
REDIS_COUNTER_PREFIX = "borg:guest:count:"
REDIS_THREAD_PREFIX = "borg:guest:thread:"

DEFAULT_CLAIM_TTL_SECONDS = 24 * 60 * 60


class GuestContextError(ValueError):
    """Something tried to read or send by the chat of a guest query.

    A `ValueError`, because Telethon's own fallbacks (such as
    `Message._reload_message`) catch that and give up quietly.
    """


class ChatKind(str, enum.Enum):
    PRIVATE = "private"
    GROUP = "group"

    @classmethod
    def of_peer(cls, peer: Any) -> "ChatKind":
        if isinstance(peer, types.PeerUser):
            return cls.PRIVATE
        if isinstance(peer, (types.PeerChat, types.PeerChannel)):
            return cls.GROUP
        raise ValueError(f"Unknown guest chat peer {peer!r}")


def is_guest_message(message: Any) -> bool:
    """Whether `message` was built from a guest query (trigger or reference)."""
    return getattr(message, GUEST_MESSAGE_ATTR, None) is not None


def is_guest_answer(message: Any) -> bool:
    """Whether `message` is this account's own guest answer, echoed back.

    In groups the bot receives each guest answer it posts as a new message
    with `out` set and `guestchat_via_from` naming the caller. Its text can be
    steered by the caller (an LLM answer, command output), so it must never
    be treated as coming from an admin, or as a command.
    """
    return (
        message is not None
        and getattr(message, "guestchat_via_from", None) is not None
        and bool(getattr(message, "out", False))
    )


def is_guest_event(event: Any) -> bool:
    return bool(getattr(event, "is_guest", False))


def caller_id_of(message: Any) -> Optional[int]:
    """The user id in `message.from_id`, or None for a channel or no sender."""
    from_id = getattr(message, "from_id", None)
    if isinstance(from_id, types.PeerUser):
        return from_id.user_id
    return None


#: What `GuestClient` lets guest messages reach on the real client. Reading a
#: user's profile is harmless; everything addressed by chat is not.
GUEST_CLIENT_ALLOWED = frozenset(
    {
        "download_media",
        "download_file",
        "iter_download",
        "get_entity",
        "get_input_entity",
        "get_peer_id",
        "parse_mode",
        "session",
        "loop",
        "build_reply_markup",
        "_self_id",
        "_mb_entity_cache",
        "_parse_message_text",
    }
)


class GuestClient:
    """The client that guest messages are bound to: an allow-list proxy.

    Telethon's `Message` methods reach the chat through `message._client`
    (`get_reply_message`, `reply`, `get_chat`, `_reload_message`, ...). With
    this proxy the ones on `GUEST_CLIENT_ALLOWED` work (downloads, entity
    lookups, parse modes), and every other attribute raises
    `GuestContextError`.
    """

    def __init__(self, client: Any):
        object.__setattr__(self, "_guest_wrapped", client)

    @property
    def wrapped(self) -> Any:
        return object.__getattribute__(self, "_guest_wrapped")

    def __getattr__(self, name: str) -> Any:
        if name in GUEST_CLIENT_ALLOWED:
            return getattr(self.wrapped, name)
        raise GuestContextError(
            f"{name} is not available in a guest chat: the bot may only answer "
            "the guest query, never read or send by its chat"
        )

    def __setattr__(self, name: str, value: Any) -> None:
        raise GuestContextError(f"GuestClient is read-only (tried to set {name})")


async def _no_reload() -> None:
    """Stands in for `Message._reload_message` on guest messages.

    Telethon reloads a message to find a sender it has not cached (always the
    case for "min" users), through `get_messages(chat_id, ids=...)`. In a
    guest chat that id belongs to another chat, so the reload must not happen.
    """
    return None


def _bind_guest_message(
    message: Any, *, client: GuestClient, entities: dict, query_id: int
) -> Any:
    message._finish_init(client, entities, None)
    setattr(message, GUEST_MESSAGE_ATTR, query_id)
    message._reload_message = _no_reload
    rich = getattr(message, "rich_message", None)
    if not getattr(message, "message", None) and rich is not None:
        #: A rich message (our own answers, the Premium rich editor) has empty
        #: text; read it as the Markdown it renders.
        message.message = tg_format.flatten_rich_message(rich)
        message.entities = None
        message._text = None
    return message


@dataclass
class GuestQuery:
    """One `UpdateBotGuestChatQuery`, with its messages bound for safe use."""

    query_id: int
    trigger: Any
    references: list
    caller_id: Optional[int]
    chat_kind: ChatKind
    thread_key: str
    received_at: float
    client: GuestClient = field(repr=False)

    @property
    def messages(self) -> list:
        """The references, then the trigger: the conversation in order."""
        return [*self.references, self.trigger]

    @property
    def text(self) -> str:
        return self.trigger.message or ""


def _thread_key(*, chat_kind: ChatKind, peer: Any, caller_id: Optional[int]) -> str:
    #: In a private chat the peer is the other participant as the caller sees
    #: it, so it flips with who summons; the unordered pair does not.
    match chat_kind:
        case ChatKind.PRIVATE:
            lo, hi = sorted([caller_id or 0, peer.user_id])
            return f"pair:{lo}:{hi}"
        case ChatKind.GROUP:
            return f"chat:{utils.get_peer_id(peer)}"
        case _:
            raise ValueError(f"Unknown chat kind {chat_kind!r}")


def guest_query_from_update(
    update: Any,
    *,
    client: Any,
    clock: Callable[[], float] = time.time,
) -> GuestQuery:
    """Builds a `GuestQuery`, binding its messages to a `GuestClient`."""
    guest_client = GuestClient(client)
    entities = getattr(update, "_entities", None) or {}
    bind = lambda m: _bind_guest_message(
        m, client=guest_client, entities=entities, query_id=update.query_id
    )
    trigger = bind(update.message)
    references = [bind(m) for m in (update.reference_messages or [])]
    caller_id = caller_id_of(trigger)
    chat_kind = ChatKind.of_peer(trigger.peer_id)
    return GuestQuery(
        query_id=update.query_id,
        trigger=trigger,
        references=references,
        caller_id=caller_id,
        chat_kind=chat_kind,
        thread_key=_thread_key(
            chat_kind=chat_kind, peer=trigger.peer_id, caller_id=caller_id
        ),
        received_at=clock(),
        client=guest_client,
    )


def _mention_patterns(username: str) -> tuple:
    name = re.escape(username.lstrip("@"))
    return (
        re.compile(rf"(?<![\w@])@{name}(?!\w)", re.IGNORECASE),
        re.compile(rf"^\s*@{name}(?!\w)[\s,:]*", re.IGNORECASE),
        re.compile(rf"[\s,]*(?<![\w@])@{name}(?!\w)\s*$", re.IGNORECASE),
    )


def mentions(text: Optional[str], *, username: str) -> bool:
    """Whether `text` mentions `@username` anywhere (case-insensitive)."""
    anywhere, _leading, _trailing = _mention_patterns(username)
    return bool(text) and anywhere.search(text) is not None


def text_after_leading_mention(text: Optional[str], *, username: str) -> Optional[str]:
    """What follows a leading `@username`, or None when the text does not start
    with it. Only whitespace may come before the mention."""
    _anywhere, leading, _trailing = _mention_patterns(username)
    match = leading.match(text or "")
    if match is None:
        return None
    return text[match.end() :]


def _utf16_offset(text: str, index: int) -> int:
    return tg_format.utf16_len(text[:index])


def _cut_entities(entities: Optional[list], *, start: int, end: int) -> list:
    """Keeps the parts of `entities` inside [start, end) UTF-16 units, shifted."""
    kept = []
    for entity in entities or []:
        begin = max(entity.offset, start)
        finish = min(entity.offset + entity.length, end)
        if finish <= begin:
            continue
        entity.offset = begin - start
        entity.length = finish - begin
        kept.append(entity)
    return kept


def strip_mention(message: Any, *, username: str) -> bool:
    """Removes one leading or one trailing `@username` from `message` in place.

    Entities are shifted (in UTF-16 units) or dropped to match. Returns
    whether the text mentioned the bot anywhere, before stripping. A reply to
    a guest answer re-triggers the bot without a mention, so each caller
    decides what an unmentioned query means.
    """
    text = getattr(message, "message", None) or ""
    anywhere, leading, trailing = _mention_patterns(username)
    mentioned = anywhere.search(text) is not None
    if not mentioned:
        return False

    match = leading.match(text)
    if match is not None:
        start, end = match.end(), len(text)
    else:
        match = trailing.search(text)
        if match is None:
            return True
        start, end = 0, match.start()
    message.entities = _cut_entities(
        message.entities,
        start=_utf16_offset(text, start),
        end=_utf16_offset(text, end),
    )
    message.message = text[start:end]
    message._text = None
    return True


ClaimBackend = Callable[[str, int], Awaitable[Optional[bool]]]


class QueryClaims:
    """First-come claims on keys, such as a guest query id.

    `claim` returns True exactly once per key within `ttl_seconds`. The memory
    set covers this process; `backend` (see `redis_claim_backend`) makes it
    hold across restarts. A backend that fails, or has no connection (it
    returns None), falls back to the memory answer.
    """

    def __init__(
        self,
        *,
        backend: Optional[ClaimBackend] = None,
        ttl_seconds: float = DEFAULT_CLAIM_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        logger: Optional[logging.Logger] = None,
    ):
        self._backend = backend
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._log = logger or _log
        self._expiries = {}

    def _prune(self, now: float) -> None:
        self._expiries = {
            key: expires for key, expires in self._expiries.items() if expires > now
        }

    async def claim(self, key: str, *, ttl_seconds: Optional[float] = None) -> bool:
        ttl = self._ttl_seconds if ttl_seconds is None else ttl_seconds
        now = self._clock()
        self._prune(now)
        if key in self._expiries:
            return False
        self._expiries[key] = now + ttl

        if self._backend is None:
            return True
        try:
            claimed = await self._backend(key, int(max(1, ttl)))
        except Exception:
            self._log.warning(
                "Guest claim backend failed for %s; using memory", key, exc_info=True
            )
            return True
        return True if claimed is None else bool(claimed)


def redis_claim_backend(
    get_redis: Callable[[], Awaitable[Any]], *, prefix: str = REDIS_CLAIM_PREFIX
) -> ClaimBackend:
    """A `QueryClaims` backend doing `SET key 1 NX EX ttl` on `get_redis()`."""

    async def claim(key: str, ttl_seconds: int) -> Optional[bool]:
        client = await get_redis()
        if client is None:
            return None
        return bool(await client.set(f"{prefix}{key}", "1", nx=True, ex=ttl_seconds))

    return claim


CounterBackend = Callable[[str, int], Awaitable[Optional[int]]]


class CallLimiter:
    """At most `limit` calls per key in each fixed window of `window_seconds`.

    `allow(key, limit=…)` counts the call and says whether it is within the
    limit; the limit is an argument so a reloaded config applies at once.
    Windows are aligned to the wall clock, so `backend` (see
    `redis_counter_backend`) shares the counts across restarts. A backend that
    fails, or has no connection, falls back to the memory count.
    """

    def __init__(
        self,
        *,
        backend: Optional[CounterBackend] = None,
        window_seconds: int = 3600,
        clock: Callable[[], float] = time.time,
        logger: Optional[logging.Logger] = None,
    ):
        self._backend = backend
        self._window_seconds = window_seconds
        self._clock = clock
        self._log = logger or _log
        self._counts = {}

    async def allow(self, key: str, *, limit: int) -> bool:
        window = int(self._clock() // self._window_seconds)
        self._counts = {k: n for k, n in self._counts.items() if k[1] == window}
        count = self._counts[(key, window)] = self._counts.get((key, window), 0) + 1
        if self._backend is not None:
            try:
                shared = await self._backend(f"{key}:{window}", self._window_seconds)
            except Exception:
                self._log.warning(
                    "Guest counter backend failed for %s; using memory",
                    key,
                    exc_info=True,
                )
            else:
                if shared is not None:
                    count = shared
        return count <= limit


def redis_counter_backend(
    get_redis: Callable[[], Awaitable[Any]], *, prefix: str = REDIS_COUNTER_PREFIX
) -> CounterBackend:
    """A `CallLimiter` backend doing `INCR` and `EXPIRE` on `get_redis()`."""

    async def count(key: str, ttl_seconds: int) -> Optional[int]:
        client = await get_redis()
        if client is None:
            return None
        name = f"{prefix}{key}"
        value = await client.incr(name)
        await client.expire(name, ttl_seconds)
        return int(value)

    return count


class GuestThreadStore:
    """Recent guest answers per thread, so that a reply to one can continue it.

    A record is a JSON-able dict with at least `id` and `answered_at` (epoch
    seconds); `parent` names the record it continued. The newest
    `max_records` of a thread are kept for `ttl_seconds` after its last
    write: in Redis (a list under `borg:guest:thread:`) when `get_redis`
    gives a connection, else in this process's memory.
    """

    def __init__(
        self,
        *,
        get_redis: Optional[Callable[[], Awaitable[Any]]] = None,
        prefix: str = REDIS_THREAD_PREFIX,
        ttl_seconds: int = 7 * 24 * 60 * 60,
        max_records: int = 50,
        clock: Callable[[], float] = time.time,
        logger: Optional[logging.Logger] = None,
    ):
        self._get_redis = get_redis
        self._prefix = prefix
        self._ttl_seconds = ttl_seconds
        self._max_records = max_records
        self._clock = clock
        self._log = logger or _log
        self._memory = {}

    async def _redis(self) -> Any:
        if self._get_redis is None:
            return None
        try:
            return await self._get_redis()
        except Exception:
            self._log.warning("Guest thread store has no Redis", exc_info=True)
            return None

    async def add(self, thread: str, record: dict) -> None:
        now = self._clock()
        _expires, records = self._memory.get(thread, (now, []))
        self._memory[thread] = (
            now + self._ttl_seconds,
            ([record] + records)[: self._max_records],
        )
        client = await self._redis()
        if client is None:
            return
        name = f"{self._prefix}{thread}"
        try:
            await client.lpush(name, json.dumps(record))
            await client.ltrim(name, 0, self._max_records - 1)
            await client.expire(name, self._ttl_seconds)
        except Exception:
            self._log.warning("Could not store a guest answer", exc_info=True)

    async def records(self, thread: str) -> list:
        """The thread's records, newest first."""
        client = await self._redis()
        if client is not None:
            try:
                raw = await client.lrange(
                    f"{self._prefix}{thread}", 0, self._max_records - 1
                )
                return [json.loads(item) for item in raw]
            except Exception:
                self._log.warning("Could not read guest answers", exc_info=True)
        expires, records = self._memory.get(thread, (0, []))
        return list(records) if expires > self._clock() else []


def find_answer(
    records: list, *, answered_at: float, tolerance_seconds: float = 5.0
) -> Optional[dict]:
    """The record answered closest to `answered_at`, if within the tolerance.

    A guest answer's message is dated when Telegram posted it, which is when
    the answer call returned, so its date identifies the record.
    """
    best = None
    for record in records:
        gap = abs(record["answered_at"] - answered_at)
        if gap <= tolerance_seconds and (best is None or gap < best[0]):
            best = (gap, record)
    return None if best is None else best[1]


def answer_chain(records: list, record: dict, *, limit: int = 10) -> list:
    """`record` and the records it continued, oldest first, at most `limit`."""
    by_id = {r["id"]: r for r in records}
    chain = [record]
    while len(chain) < limit:
        parent = by_id.get(chain[-1].get("parent"))
        if parent is None or parent in chain:
            break
        chain.append(parent)
    return chain[::-1]


GuestHandler = Callable[[GuestQuery], Awaitable[None]]


def register_guest_handler(
    client: Any,
    handler: GuestHandler,
    *,
    claims: QueryClaims,
    max_age_seconds: float = 120,
    allow_forwarded: bool = False,
    clock: Callable[[], float] = time.time,
    logger: Optional[logging.Logger] = None,
) -> Optional[Callable]:
    """Calls `handler` for each fresh, unclaimed guest query this bot receives.

    Returns the registered callback, or None when the account is not a bot or
    Telethon has no guest types (1.43.2). Call it from a plugin's module body:
    the callback takes the handler's `__module__`, so a plugin reload removes
    it together with the plugin's other handlers. Before `handler` runs, a
    query is dropped when its trigger is forwarded or sent via a bot (unless
    `allow_forwarded`), older than `max_age_seconds` (a late command should
    not run, and Telegram rejects late answers anyway), or already claimed.
    """
    log = logger or _log
    update_cls = getattr(types, "UpdateBotGuestChatQuery", None)
    if update_cls is None:
        log.info("Guest mode needs Telethon 1.44 or later; not registering")
        return None
    if not getattr(getattr(client, "me", None), "bot", False):
        log.info("Guest mode is for bot accounts; not registering")
        return None

    async def on_guest_query(update):
        try:
            query = guest_query_from_update(update, client=client, clock=clock)
        except Exception:
            log.exception("Could not read guest query %s", update.query_id)
            return

        trigger = query.trigger
        if not allow_forwarded and (trigger.fwd_from or trigger.via_bot_id):
            log.info("Ignoring guest query %s: forwarded trigger", query.query_id)
            return
        age = query.received_at - trigger.date.timestamp()
        if age > max_age_seconds:
            log.info(
                "Ignoring guest query %s: trigger is %.0fs old", query.query_id, age
            )
            return
        if not await claims.claim(f"q:{query.query_id}"):
            log.info("Ignoring guest query %s: already claimed", query.query_id)
            return

        try:
            await handler(query)
        except Exception:
            log.exception("Guest handler failed for query %s", query.query_id)

    on_guest_query.__module__ = handler.__module__
    on_guest_query.__qualname__ = f"{handler.__qualname__}.<guest>"
    client.add_event_handler(on_guest_query, events.Raw(update_cls))
    return on_guest_query


def _refuse(what: str):
    async def refused(*args, **kwargs):
        raise GuestContextError(
            f"{what} is not available in a guest chat; answer through the guest "
            "answer instead"
        )

    return refused


class GuestEvent:
    """Stands in for a `NewMessage` event when a guest query runs a pipeline.

    `chat_id` is a synthetic ``guest:<thread_key>`` string, so per-chat
    settings, caches and prompt-cache keys get their own namespace and never
    match a real chat. There is no chat to reply to: every method that would
    send or read by chat raises `GuestContextError`.
    """

    is_guest = True
    is_private = False
    is_channel = False
    out = False
    grouped_id = None
    forward = None
    pattern_match = None

    reply = _refuse("reply")
    respond = _refuse("respond")
    edit = _refuse("edit")
    delete = _refuse("delete")
    get_reply_message = _refuse("get_reply_message")
    get_chat = _refuse("get_chat")
    get_input_chat = _refuse("get_input_chat")
    mark_read = _refuse("mark_read")
    pin = _refuse("pin")
    forward_to = _refuse("forward_to")

    def __init__(self, query: GuestQuery, *, text: Optional[str] = None):
        self.query = query
        self.message = query.trigger
        self.client = query.client
        self.sender_id = query.caller_id
        self.chat_id = f"guest:{query.thread_key}"
        self.id = query.trigger.id
        self.is_group = query.chat_kind is ChatKind.GROUP
        self.text = self.raw_text = text if text is not None else query.text

    @property
    def sender(self) -> Any:
        return self.message.sender

    @property
    def file(self) -> Any:
        return self.message.file

    @property
    def media(self) -> Any:
        return self.message.media

    @property
    def chat(self) -> Any:
        raise GuestContextError("A guest event has no chat")

    input_chat = chat

    async def get_sender(self) -> Any:
        return await self.message.get_sender()

    async def get_input_sender(self) -> Any:
        return await self.message.get_input_sender()

    def action(self, *args, **kwargs):
        raise GuestContextError("A guest event cannot show chat actions")


_ANSWER_IDS = itertools.count(-1, -1)


class GuestAnswerMessage:
    """Stands in for the response `Message` that streaming code edits.

    `util.edit_message` and the backends' streaming loops only need `id`,
    `chat_id`, `text`, `reply_to_msg_id` and `edit(...)`. Edits go to the
    guest answer through `editor` (a `tg_raw.InlineEditor`), at least
    `min_interval` seconds apart. A flood wait Telethon did not sleep through
    blocks partial edits until it ends; `finalize` waits it out instead.
    `reply`, `respond`, `get_chat` and `delete` raise `GuestContextError`, so
    text past the first 4096 units is dropped rather than posted anywhere.
    """

    reply_to_msg_id = None
    out = True

    reply = _refuse("reply")
    respond = _refuse("respond")
    get_chat = _refuse("get_chat")
    delete = _refuse("delete")

    def __init__(
        self,
        editor: Any,
        *,
        min_interval: float = 1.2,
        max_final_wait: float = 300,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        logger: Optional[logging.Logger] = None,
    ):
        self.editor = editor
        self.id = next(_ANSWER_IDS)
        self.chat_id = f"guest-answer:{self.id}"
        self.text = ""
        self.blocked_until = 0.0
        self._min_interval = min_interval
        self._max_final_wait = max_final_wait
        self._clock = clock
        self._sleep = sleep
        self._log = logger or _log
        self._last_edit_at = None
        self._lock = asyncio.Lock()

    @property
    def raw_text(self) -> str:
        return self.text

    def _wait_seconds(self) -> float:
        now = self._clock()
        wait = self.blocked_until - now
        if self._last_edit_at is not None:
            wait = max(wait, self._last_edit_at + self._min_interval - now)
        return max(0.0, wait)

    async def _edit(self, **kwargs) -> bool:
        try:
            return await self.editor.edit(**kwargs)
        finally:
            self._last_edit_at = self._clock()

    async def edit(
        self,
        text: str,
        parse_mode: Any = None,
        link_preview: bool = False,
        buttons: Any = None,
        **_ignored,
    ) -> "GuestAnswerMessage":
        """A streaming edit; skipped while a flood wait is running."""
        async with self._lock:
            if self._clock() < self.blocked_until:
                return self
            wait = self._wait_seconds()
            if wait:
                await self._sleep(wait)
            try:
                await self._edit(
                    text=text,
                    parse_mode=parse_mode,
                    link_preview=link_preview,
                    buttons=buttons,
                )
            except errors.FloodWaitError as e:
                self.blocked_until = self._clock() + e.seconds
                self._log.warning(
                    "Guest answer flood wait of %ss; skipping edits", e.seconds
                )
                return self
            self.text = text
            return self

    async def finalize(self, **kwargs) -> bool:
        """The last edit (`tg_raw.InlineEditor.edit` arguments); never skipped.

        Waits out a flood wait of up to `max_final_wait` seconds, then tries
        once more. Other errors propagate, so the caller can fall back.
        """
        async with self._lock:
            for attempt in range(2):
                wait = self._wait_seconds()
                if wait > self._max_final_wait:
                    raise errors.FloodWaitError(request=None, capture=int(wait))
                if wait:
                    await self._sleep(wait)
                try:
                    changed = await self._edit(**kwargs)
                except errors.FloodWaitError as e:
                    self.blocked_until = self._clock() + e.seconds
                    if attempt:
                        raise
                    continue
                self.text = kwargs.get("markdown") or kwargs.get("text") or ""
                return changed
        raise AssertionError("unreachable")


TRIGGER_GUARD_ENV = "borg_guest_trigger_guard"

_ENABLED_VALUES = frozenset({"", "1", "true", "yes", "on"})
_DISABLED_VALUES = frozenset({"0", "false", "no", "off"})

#: A leading bot mention followed by `.a`: the guest shell's trigger shape.
#: Every bot username ends in "bot".
_SHELL_TRIGGER = re.compile(r"^(\s*)@(\w*bot)(?=\s+\.a)", re.IGNORECASE)

#: U+FF20 FULLWIDTH COMMERCIAL AT: looks like "@", is not a mention, and is one
#: UTF-16 unit, so no entity moves.
FULLWIDTH_AT = "\uff20"

_MENTION_ENTITIES = (types.MessageEntityMention, types.InputMessageEntityMentionName)


def trigger_guard_enabled(*, environ=None) -> bool:
    """Reads `TRIGGER_GUARD_ENV`; unset means on. Unknown values raise."""
    environ = os.environ if environ is None else environ
    raw = environ.get(TRIGGER_GUARD_ENV, "")
    value = raw.strip().lower()
    if value in _ENABLED_VALUES:
        return True
    if value in _DISABLED_VALUES:
        return False
    raise ValueError(
        f"{TRIGGER_GUARD_ENV}={raw!r} is not a recognised switch; "
        "use 1/true/yes/on or 0/false/no/off"
    )


def defang_guest_trigger(text: Optional[str], entities: Optional[list] = None):
    """Returns (text, entities) with a leading ``@…bot .a`` made inert.

    The "@" becomes `FULLWIDTH_AT`, and a mention entity starting there is
    dropped. Other text is returned unchanged.
    """
    match = _SHELL_TRIGGER.match(text or "")
    if match is None:
        return text, entities
    at = match.start(2) - 1
    offset = tg_format.utf16_len(text[:at])
    kept = [
        entity
        for entity in entities or []
        if not (isinstance(entity, _MENTION_ENTITIES) and entity.offset == offset)
    ]
    return text[:at] + FULLWIDTH_AT + text[at + 1 :], (kept if entities else entities)


_GUARDED_REQUESTS = (
    functions.messages.SendMessageRequest,
    functions.messages.EditMessageRequest,
    functions.messages.SendMediaRequest,
    functions.messages.SendMultiMediaRequest,
)


def defang_request(request: Any) -> bool:
    """Defangs the text of an outgoing message request in place.

    Returns whether anything changed. Other requests are left alone.
    """
    if not isinstance(request, _GUARDED_REQUESTS):
        return False
    changed = False
    parts = [request, *(getattr(request, "multi_media", None) or [])]
    for part in parts:
        text = getattr(part, "message", None)
        if not isinstance(text, str):
            continue
        new_text, new_entities = defang_guest_trigger(text, part.entities)
        if new_text != text:
            part.message, part.entities = new_text, new_entities
            changed = True
    return changed


class OutgoingTriggerGuardMixin:
    """Keeps a user account's automation from summoning a guest shell.

    Put it first in the client's bases. Everything a userbot process sends is
    automation (its owner types in a Telegram app, which bypasses it), yet
    Telegram would run text such as an LLM answer or command output starting
    with ``@shellbot .a …`` as a guest query from the owner, who is admin. The
    guard only acts on user accounts, and only on that trigger shape.
    """

    #: `Uniborg.create` sets it from `trigger_guard_enabled()`.
    trigger_guard = True

    async def __call__(self, request, ordered=False, flood_sleep_threshold=None):
        if self.trigger_guard and getattr(self, "_is_bot", None) is False:
            for part in request if utils.is_list_like(request) else [request]:
                if defang_request(part):
                    _log.info(
                        "Defanged a guest shell trigger in an outgoing %s",
                        type(part).__name__,
                    )
        return await super().__call__(
            request, ordered=ordered, flood_sleep_threshold=flood_sleep_threshold
        )
