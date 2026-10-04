# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""Safety nets for Telegram objects that Telethon cannot parse.

Telegram sometimes sends a constructor from a newer layer than the session
declared (see docs/telethon_upgrade.md). TL objects carry no length prefix, so
Telethon cannot skip an object it does not know, and one unknown object costs
everything around it:

- inside a pushed ``msg_container``, the whole container is dropped without an
  acknowledgement, siblings included;
- inside a ``gzip_packed`` container entry, the entries after it are never
  processed;
- inside ``getDifference`` or ``getChannelDifference``, the update loop
  disconnects the client, which ends the bot.

The nets below shrink the loss to the one object, count every event in
`SafetyStats`, and send rate-limited alerts through an injected callable.

Two more nets fix Telethon behaviour that loses or repeats updates and
requests rather than failing to parse them:

- the qts re-dispatch hands on the qts updates (guest queries, bot stops) that
  ``getDifference`` recovers after a gap, which Telethon drops;
- the at-most-once guard keeps a reconnect from re-sending requests marked
  with `mark_at_most_once`, such as a guest answer.

This module imports only the standard library, Telethon and
``uniborg.env_switch`` (itself standard library only), so tests and tools can
load it without ``uniborg.util``.
"""
import asyncio
from collections import Counter
from dataclasses import dataclass, field
import enum
import functools
import logging
import time
from typing import Any, Awaitable, Callable, Iterable, Optional
import weakref

import telethon
from telethon import errors

#: Loaded so `_patch_telethon` can reach them as attributes of the package.
import telethon._updates.messagebox
import telethon.network.mtprotosender
import telethon.tl.core.messagecontainer
import telethon.tl.core.tlmessage
from telethon.tl.tlobject import TLObject

from uniborg import env_switch

SAFETY_NETS_ENV = "borg_tg_safety_nets"

#: The patches replace private Telethon internals. Both versions ship
#: byte-identical `MessageContainer.from_reader`, `MTProtoSender`'s
#: `_process_message`/`_handle_container`/`_handle_gzip_packed`/`_handle_update`,
#: the re-queueing step of `MTProtoSender._reconnect`, and `_updates/messagebox.py`.
SUPPORTED_TELETHON_VERSIONS = frozenset({"1.43.2", "1.45.0"})

#: The qts updates the re-dispatch hands on. Only idempotent consumers belong
#: here: session state is saved about once a minute, so after a hard stop the
#: next difference can repeat updates that were already dispatched. Guest
#: queries are deduplicated by query id; reaction updates are not listed
#: because `history_util` merges their counts.
REDISPATCH_UPDATE_NAMES = ("UpdateBotGuestChatQuery", "UpdateBotStopped")

ALERT_INTERVAL_SECONDS = 10 * 60

#: The record `MTProtoSender._recv_loop` logs when it drops a whole message.
TELETHON_TYPE_NOT_FOUND_MSG = "Type %08x not found, remaining data %r"

_log = logging.getLogger(__name__)

AlertFn = Callable[[str], Awaitable[None]]
ReportFn = Callable[..., None]


class SafetyKind(enum.Enum):
    """Where an unparseable object was caught, and what that cost."""

    CONTAINER_ENTRY = "container_entry"
    PROCESSING = "processing"
    DROPPED_MESSAGE = "dropped_message"
    DIFFERENCE = "difference"
    CHANNEL_DIFFERENCE = "channel_difference"
    RPC_RESULT = "rpc_result"

    @property
    def description(self) -> str:
        return _KIND_DESCRIPTIONS[self]


_KIND_DESCRIPTIONS = {
    SafetyKind.CONTAINER_ENTRY: (
        "skipped an unknown object inside a message container; "
        "it was acknowledged and its siblings were kept"
    ),
    SafetyKind.PROCESSING: (
        "an unknown object surfaced while a received message was processed "
        "(usually gzip-packed); that message is lost"
    ),
    SafetyKind.DROPPED_MESSAGE: (
        "Telethon dropped a received message it could not parse, "
        "without acknowledging it"
    ),
    SafetyKind.DIFFERENCE: (
        "skipped an unparseable getDifference window; account updates in it are lost"
    ),
    SafetyKind.CHANNEL_DIFFERENCE: (
        "forgot a channel's update state after an unparseable "
        "getChannelDifference; its missed updates are lost"
    ),
    SafetyKind.RPC_RESULT: (
        "a request's result contained an unknown object; "
        "the call raised, although Telegram may have executed it"
    ),
}


class SafetyNet(enum.Enum):
    """The individual nets `install_safety_nets` can put in place."""

    CONTAINER_SKIP = "container_skip"
    MESSAGE_GUARD = "message_guard"
    LOG_COUNTER = "log_counter"
    DIFFERENCE_FALLBACK = "difference_fallback"
    QTS_REDISPATCH = "qts_redispatch"
    AT_MOST_ONCE = "at_most_once"


_PATCH_NETS = frozenset(
    {
        SafetyNet.CONTAINER_SKIP,
        SafetyNet.MESSAGE_GUARD,
        SafetyNet.QTS_REDISPATCH,
        SafetyNet.AT_MOST_ONCE,
    }
)


def _format_constructor(constructor_id: Optional[int]) -> str:
    if constructor_id is None:
        return "unknown"
    return f"0x{constructor_id:08x}"


@dataclass
class SafetyStats:
    """Counts of safety-net events, per kind and per unknown constructor."""

    by_kind: Counter = field(default_factory=Counter)
    by_constructor: Counter = field(default_factory=Counter)
    installed: set = field(default_factory=set)
    #: Updates the qts re-dispatch recovered, by type name. They are
    #: recoveries, not failures, so they stay out of `by_kind` and `total`.
    recovered: Counter = field(default_factory=Counter)

    def record(self, kind: SafetyKind, *, constructor_id: Optional[int] = None) -> int:
        self.by_kind[kind] += 1
        if constructor_id is not None:
            self.by_constructor[constructor_id] += 1
        return self.by_kind[kind]

    @property
    def total(self) -> int:
        return sum(self.by_kind.values())

    def summary(self) -> str:
        nets = ", ".join(sorted(net.value for net in self.installed)) or "none"
        kinds = (
            ", ".join(
                f"{kind.value}={count}"
                for kind, count in sorted(
                    self.by_kind.items(), key=lambda item: item[0].value
                )
            )
            or "none"
        )
        constructors = (
            ", ".join(
                f"{_format_constructor(constructor_id)}={count}"
                for constructor_id, count in self.by_constructor.most_common()
            )
            or "none"
        )
        recovered = (
            ", ".join(
                f"{name}={count}" for name, count in sorted(self.recovered.items())
            )
            or "none"
        )
        return (
            f"nets: {nets}; events: {kinds}; constructors: {constructors}; "
            f"recovered: {recovered}"
        )


class SafetyMonitor:
    """Records safety-net events: counts them, logs them, and alerts.

    An alert goes out at most once per kind per `alert_interval_seconds`; the
    next one mentions how many events were held back in between. `alert` is an
    async callable taking the alert text, and may be replaced later.
    """

    def __init__(
        self,
        *,
        stats: Optional[SafetyStats] = None,
        alert: Optional[AlertFn] = None,
        clock: Optional[Callable[[], float]] = None,
        alert_interval_seconds: float = ALERT_INTERVAL_SECONDS,
        context: str = "",
        logger: Optional[logging.Logger] = None,
    ):
        self.stats = stats if stats is not None else SafetyStats()
        self.alert = alert
        self._clock = clock or time.monotonic
        self._alert_interval_seconds = alert_interval_seconds
        self._context = context
        self._log = logger or _log
        self._last_alert_at = {}
        self._held_back = Counter()
        self._alert_tasks = set()

    def report(
        self,
        kind: SafetyKind,
        *,
        constructor_id: Optional[int] = None,
        detail: str = "",
    ) -> None:
        count = self.stats.record(kind, constructor_id=constructor_id)
        self._log.warning(
            "Telegram safety net (%s): %s; constructor %s%s; %d so far",
            kind.value,
            kind.description,
            _format_constructor(constructor_id),
            f"; {detail}" if detail else "",
            count,
        )
        self._maybe_alert(kind, constructor_id=constructor_id, count=count)

    def record_recovered(self, updates: Iterable[Any]) -> None:
        """Counts and logs updates the qts re-dispatch handed on. No alert."""
        names = [type(update).__name__ for update in updates]
        self.stats.recovered.update(names)
        self._log.info(
            "Telegram safety net (%s): re-dispatched %d qts update(s) that "
            "getDifference recovered: %s",
            SafetyNet.QTS_REDISPATCH.value,
            len(names),
            ", ".join(names),
        )

    def _maybe_alert(
        self, kind: SafetyKind, *, constructor_id: Optional[int], count: int
    ) -> None:
        if self.alert is None:
            return

        now = self._clock()
        last = self._last_alert_at.get(kind)
        if last is not None and now - last < self._alert_interval_seconds:
            self._held_back[kind] += 1
            return

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            #: Only reachable from a thread without an event loop; the
            #: warning above already recorded the event.
            self._held_back[kind] += 1
            return

        self._last_alert_at[kind] = now
        held_back = self._held_back.pop(kind, 0)
        text = self.alert_text(
            kind, constructor_id=constructor_id, count=count, held_back=held_back
        )
        task = loop.create_task(self._send_alert(text))
        self._alert_tasks.add(task)
        task.add_done_callback(self._alert_tasks.discard)

    def alert_text(
        self,
        kind: SafetyKind,
        *,
        constructor_id: Optional[int],
        count: int,
        held_back: int = 0,
    ) -> str:
        parts = [
            f"Telegram safety net [{kind.value}]: {kind.description}.",
            f"Constructor {_format_constructor(constructor_id)}, {count} so far",
        ]
        if held_back:
            parts[-1] += f", {held_back} more since the last alert"
        parts[-1] += "."
        if self._context:
            parts.append(f"{self._context}.")
        return " ".join(parts)

    async def _send_alert(self, text: str) -> None:
        try:
            await self.alert(text)
        except Exception:
            self._log.warning("Could not deliver a safety-net alert", exc_info=True)


class TypeNotFoundLogHandler(logging.Handler):
    """Counts the unknown-constructor records Telethon's sender logs itself.

    Attached to the ``telethon.network.mtprotosender`` logger. It sees:

    - the INFO record `_recv_loop` logs when a whole message is dropped
      (`SafetyKind.DROPPED_MESSAGE`);
    - any record whose exception is a `TypeNotFoundError`, which without the
      message guard is how a gzip-packed failure surfaces
      (`SafetyKind.PROCESSING`).

    A logger level above INFO on that logger hides the first kind from it.
    """

    def __init__(
        self,
        *,
        report: ReportFn,
        not_found_error: type = errors.TypeNotFoundError,
    ):
        super().__init__()
        self._report = report
        self._not_found_error = not_found_error

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if (
                record.msg == TELETHON_TYPE_NOT_FOUND_MSG
                and isinstance(record.args, tuple)
                and record.args
            ):
                self._report(SafetyKind.DROPPED_MESSAGE, constructor_id=record.args[0])
            elif record.exc_info and isinstance(
                record.exc_info[1], self._not_found_error
            ):
                self._report(
                    SafetyKind.PROCESSING,
                    constructor_id=record.exc_info[1].invalid_constructor_id,
                )
        except Exception:
            self.handleError(record)


class SkippedObject(TLObject):
    """Stands in for a container entry whose body could not be parsed.

    `MTProtoSender._process_message` still acknowledges its msg_id, then hands
    it to `_handle_update`, which logs that it is not an update and drops it.
    """

    #: None matches no `MTProtoSender._handlers` key, and is not the Updates
    #: SUBCLASS_OF_ID (0x8af52aac), so nothing reads the missing fields.
    CONSTRUCTOR_ID = None
    SUBCLASS_OF_ID = None

    def __init__(
        self,
        *,
        outer_constructor_id: int,
        invalid_constructor_id: int,
        length: int,
    ):
        self.outer_constructor_id = outer_constructor_id
        self.invalid_constructor_id = invalid_constructor_id
        self.length = length

    def to_dict(self):
        return {
            "_": "SkippedObject",
            "outer_constructor_id": _format_constructor(self.outer_constructor_id),
            "invalid_constructor_id": _format_constructor(self.invalid_constructor_id),
            "length": self.length,
        }


def _skipping_container_reader(
    *,
    container_cls: type,
    message_cls: type,
    not_found_error: type,
    report: ReportFn,
):
    """A `MessageContainer.from_reader` that skips unknown entries by length.

    Mirrors Telethon's own reader, plus one step: an entry whose body raises
    `TypeNotFoundError` becomes a `SkippedObject`, and reading resumes at the
    next entry, which the entry's length prefix locates exactly.
    """

    def from_reader(cls, reader):
        messages = []
        for _ in range(reader.read_int()):
            msg_id = reader.read_long()
            seq_no = reader.read_int()
            length = reader.read_int()
            before = reader.tell_position()
            try:
                #: May over-read, e.g. RpcResult; the seek below corrects it.
                obj = reader.tgread_object()
            except not_found_error as e:
                reader.set_position(before)
                outer_constructor_id = reader.read_int(signed=False)
                obj = SkippedObject(
                    outer_constructor_id=outer_constructor_id,
                    invalid_constructor_id=e.invalid_constructor_id,
                    length=length,
                )
                report(
                    SafetyKind.CONTAINER_ENTRY,
                    constructor_id=e.invalid_constructor_id,
                    detail=(
                        f"entry {_format_constructor(outer_constructor_id)}, "
                        f"{length} bytes, msg_id {msg_id}"
                    ),
                )
            reader.set_position(before + length)
            messages.append(message_cls(msg_id, seq_no, obj))
        return container_cls(messages)

    return classmethod(from_reader)


def _guarded_process_message(
    *,
    original: Callable,
    not_found_error: type,
    report: ReportFn,
):
    """Wraps `MTProtoSender._process_message` so one message cannot sink others.

    `_handle_container` calls `_process_message` once per entry, so guarding
    it here is the same as guarding each turn of that loop. `_handle_container`
    itself cannot be patched usefully: `MTProtoSender.__init__` binds it into
    `_handlers`, so a sender built before the patch keeps the original.
    `_process_message` acknowledges the msg_id before dispatching, so a
    swallowed failure is still acked. At the top level, Telethon's
    `_recv_loop` only logs an escaping exception and carries on, which is what
    this does too.
    """

    @functools.wraps(original)
    async def _process_message(self, message):
        try:
            await original(self, message)
        except not_found_error as e:
            report(
                SafetyKind.PROCESSING,
                constructor_id=e.invalid_constructor_id,
                detail=f"msg_id {message.msg_id}",
            )
        except Exception:
            self._log.exception("Unhandled error while processing msgs")

    return _process_message


def _recovered_qts_updates(
    box: Any,
    other_updates: list,
    *,
    pts_info_cls: type,
    secret_entry: Any,
    update_types: tuple,
) -> tuple:
    """Splits a difference's `other_updates` into (re-dispatch, rest).

    An update is re-dispatched when it carries a qts, is newer than the qts the
    box held before the difference, and is one of `update_types`. Without a
    known qts nothing can be shown to be new, so nothing is re-dispatched.
    """
    state = box.map.get(secret_entry)
    if state is None or not update_types:
        return [], list(other_updates)

    recovered, rest = [], []
    for update in other_updates:
        info = pts_info_cls.from_update(update)
        if (
            info is not None
            and info.entry is secret_entry
            and info.pts > state.pts
            and isinstance(update, update_types)
        ):
            recovered.append(update)
        else:
            rest.append(update)
    recovered.sort(key=lambda update: pts_info_cls.from_update(update).pts)
    return recovered, rest


def _redispatching_apply_difference_type(
    *,
    original: Callable,
    pts_info_cls: type,
    secret_entry: Any,
    update_types: tuple,
    on_recovered: Callable[[list], None],
):
    """Wraps `MessageBox.apply_difference_type` to keep recovered qts updates.

    Telethon applies the difference's final state first, then runs
    `other_updates` through `process_updates`, which then sees every qts
    update as already handled (or skips it while the secret entry is still
    being fetched) and drops it. TDLib applies them. The wrapper runs the
    original on the other updates only, then appends the recovered qts
    updates, which `_update_loop` dispatches like any other. If the split
    itself fails, the original runs unchanged: an exception here would make
    `_update_loop` disconnect the client.
    """

    @functools.wraps(original)
    def apply_difference_type(self, diff, chat_hashes):
        try:
            recovered, rest = _recovered_qts_updates(
                self,
                diff.other_updates,
                pts_info_cls=pts_info_cls,
                secret_entry=secret_entry,
                update_types=update_types,
            )
        except Exception:
            _log.exception(
                "Telegram safety net (%s) could not split a difference; "
                "applying it unchanged",
                SafetyNet.QTS_REDISPATCH.value,
            )
            return original(self, diff, chat_hashes)

        if not recovered:
            return original(self, diff, chat_hashes)

        other_updates = diff.other_updates
        diff.other_updates = rest
        try:
            updates, users, chats = original(self, diff, chat_hashes)
        finally:
            diff.other_updates = other_updates

        for update in recovered:
            #: `process_updates` sets this on everything it sees; the loop
            #: that reads it has already run, but keep the attribute uniform.
            update._self_outgoing = False
        updates.extend(recovered)
        try:
            on_recovered(recovered)
        except Exception:
            _log.exception("Could not record re-dispatched updates")
        return updates, users, chats

    return apply_difference_type


class DeliveryUnknownError(ConnectionError):
    """An at-most-once request was in flight when the connection dropped.

    Telegram may or may not have executed it, and it was not re-sent.
    """


#: Futures of requests that must never be sent twice. Weak, so a finished
#: request leaves no trace.
_AT_MOST_ONCE_FUTURES = weakref.WeakSet()


def mark_at_most_once(future: asyncio.Future) -> None:
    """Marks the future `MTProtoSender.send` returned as at-most-once.

    Call it before awaiting, while the request can at most be queued. A
    reconnect then fails the future with `DeliveryUnknownError` instead of
    re-sending the request, once it has gone out unanswered.
    """
    _AT_MOST_ONCE_FUTURES.add(future)


def at_most_once_installed(*, telethon_module: Any = None) -> bool:
    """Whether the at-most-once guard is active for this Telethon module."""
    installation = _INSTALLATIONS.get(id(telethon_module or telethon))
    return (
        installation is not None
        and SafetyNet.AT_MOST_ONCE in installation.monitor.stats.installed
    )


def drop_at_most_once_requests(sender: Any) -> int:
    """Removes sent, unanswered at-most-once requests from `sender`.

    `MTProtoSender._reconnect` re-queues every pending request under a fresh
    session, which Telegram treats as a new call. Requests still in the send
    queue were never sent, so they stay. Returns how many were dropped.
    """
    dropped = 0
    for msg_id, state in list(sender._pending_state.items()):
        if state.future not in _AT_MOST_ONCE_FUTURES:
            continue
        del sender._pending_state[msg_id]
        if not state.future.done():
            state.future.set_exception(
                DeliveryUnknownError(
                    f"{type(state.request).__name__} was in flight when the "
                    "connection dropped; it may or may not have been executed, "
                    "and it was not re-sent"
                )
            )
        dropped += 1
    return dropped


def _at_most_once_reconnect(*, original: Callable):
    """Wraps `MTProtoSender._reconnect` to drop at-most-once requests first.

    Dropping them before the original runs means a late answer on the old
    connection finds no state and is ignored; the caller has already been told
    the delivery is unknown, which is the safe reading.
    """

    @functools.wraps(original)
    async def _reconnect(self, last_error):
        try:
            dropped = drop_at_most_once_requests(self)
            if dropped:
                self._log.warning(
                    "Telegram safety net (%s): not re-sending %d request(s) "
                    "after the connection dropped",
                    SafetyNet.AT_MOST_ONCE.value,
                    dropped,
                )
        except Exception:
            self._log.exception("Could not drop at-most-once requests")
        return await original(self, last_error)

    return _reconnect


@dataclass
class Backoff:
    """Exponential delay for a fallback that keeps firing.

    The first fallback after a quiet spell runs at once; each repeat within
    `quiet_seconds` of the previous one waits twice as long, up to
    `max_seconds`. Repeated getDifference calls otherwise risk FLOOD_WAIT.
    """

    base_seconds: float = 1.0
    max_seconds: float = 60.0
    quiet_seconds: float = 600.0
    streak: int = 0
    last_at: Optional[float] = None

    def next_delay(self, *, now: float) -> float:
        if self.last_at is not None and now - self.last_at >= self.quiet_seconds:
            self.streak = 0
        if self.streak == 0:
            delay = 0.0
        else:
            exponent = min(self.streak - 1, 32)
            delay = min(self.max_seconds, self.base_seconds * 2**exponent)
        self.streak += 1
        self.last_at = now
        return delay


class DifferenceFallback:
    """Substitutes for calls that failed with `TypeNotFoundError`.

    - `GetDifferenceRequest`: an empty `updates.Difference` carrying a fresh
      `GetStateRequest` state. `MessageBox.apply_difference` then jumps the
      account state past the unparseable window and ends the difference.
    - `GetChannelDifferenceRequest`: `ChannelPrivateError`, which
      `_update_loop` handles as BANNED: it forgets the channel's state, and the
      next update from that channel starts it afresh. Returning
      `ChannelDifferenceEmpty` instead would keep the gap, so every later
      update from the channel would fail the same way.
    - Anything else is counted as `SafetyKind.RPC_RESULT` and re-raised.

    The backoff waits inside `_update_loop`, so it pauses update processing.
    Channels back off one by one: forgetting a channel sends no request of
    its own, and a shared streak would make every other failing channel of a
    deadline sweep wait behind the first.
    """

    def __init__(
        self,
        *,
        report: ReportFn,
        telethon_module: Any = None,
        clock: Optional[Callable[[], float]] = None,
        sleep: Optional[Callable[[float], Awaitable[None]]] = None,
        backoff_factory: Callable[[], Backoff] = Backoff,
    ):
        module = telethon_module or telethon
        self._report = report
        self._clock = clock or time.monotonic
        self._sleep = sleep or asyncio.sleep
        self._backoff_factory = backoff_factory
        self._get_difference = module.functions.updates.GetDifferenceRequest
        self._get_channel_difference = (
            module.functions.updates.GetChannelDifferenceRequest
        )
        self._get_state = module.functions.updates.GetStateRequest
        self._difference = module.types.updates.Difference
        self._channel_private_error = module.errors.ChannelPrivateError
        #: Keyed by (kind, channel id), with None as the account's channel id.
        self.backoffs = {}

    async def recover(self, request: Any, *, error: Exception, call: Callable):
        """Returns a stand-in result for `request`, or raises.

        `call` invokes a request without going through this fallback again.
        """
        constructor_id = error.invalid_constructor_id
        if isinstance(request, self._get_difference):
            self._report(SafetyKind.DIFFERENCE, constructor_id=constructor_id)
            await self._back_off(SafetyKind.DIFFERENCE)
            state = await call(self._get_state())
            return self._difference(
                new_messages=[],
                new_encrypted_messages=[],
                other_updates=[],
                chats=[],
                users=[],
                state=state,
            )

        if isinstance(request, self._get_channel_difference):
            channel_id = getattr(request.channel, "channel_id", request.channel)
            self._report(
                SafetyKind.CHANNEL_DIFFERENCE,
                constructor_id=constructor_id,
                detail=f"channel {channel_id}",
            )
            await self._back_off(SafetyKind.CHANNEL_DIFFERENCE, channel_id=channel_id)
            raise self._channel_private_error(request=request) from error

        self._report(
            SafetyKind.RPC_RESULT,
            constructor_id=constructor_id,
            detail=type(request).__name__,
        )
        raise error

    async def _back_off(self, kind: SafetyKind, *, channel_id: Any = None) -> None:
        now = self._clock()
        #: Streaks past their quiet spell would restart anyway; dropping them
        #: keeps one entry per recently failing channel.
        self.backoffs = {
            key: backoff
            for key, backoff in self.backoffs.items()
            if now - backoff.last_at < backoff.quiet_seconds
        }
        key = (kind, channel_id)
        backoff = self.backoffs.get(key)
        if backoff is None:
            backoff = self.backoffs[key] = self._backoff_factory()
        delay = backoff.next_delay(now=now)
        if delay > 0:
            _log.info(
                "Backing off %.1fs before the next %s fallback", delay, kind.value
            )
            await self._sleep(delay)


class DifferenceFallbackMixin:
    """Keeps an unparseable (channel) difference from disconnecting the client.

    Put it before `TelegramClient` in the bases. Telethon's `_update_loop`
    fetches differences with ``await self(request)``, so this `__call__` sees
    each one, and Telethon's own handling of the substitute does the rest (see
    `DifferenceFallback`). Without a fallback attached it changes nothing.
    """

    #: None keeps Telethon's behaviour; `install_safety_nets(client=...)` sets it.
    difference_fallback: Optional[DifferenceFallback] = None

    #: The signature matches `TelegramClient.__call__`, which callers may use
    #: positionally.
    async def __call__(self, request, ordered=False, flood_sleep_threshold=None):
        call = super().__call__
        try:
            return await call(
                request, ordered=ordered, flood_sleep_threshold=flood_sleep_threshold
            )
        except errors.TypeNotFoundError as error:
            fallback = self.difference_fallback
            if fallback is None:
                raise
            return await fallback.recover(request, error=error, call=call)


def safety_nets_enabled(*, environ=None) -> bool:
    """Reads `SAFETY_NETS_ENV` (`env_switch`); unset means on."""
    return env_switch.env_switch(SAFETY_NETS_ENV, environ=environ)


@dataclass
class _Patch:
    owner: Any
    name: str
    original: Any

    @classmethod
    def apply(cls, owner: Any, name: str, *, replacement: Any) -> "_Patch":
        patch = cls(owner=owner, name=name, original=owner.__dict__[name])
        setattr(owner, name, replacement)
        return patch

    def restore(self) -> None:
        setattr(self.owner, self.name, self.original)


@dataclass
class _Installation:
    module: Any
    monitor: SafetyMonitor
    handler: logging.Handler
    sender_logger: logging.Logger
    patches: list


#: Keyed by id() of the Telethon module; the entry keeps the module alive, so
#: the id cannot be reused while it is registered.
_INSTALLATIONS = {}


def _telethon_layer(module: Any) -> Optional[int]:
    try:
        return module.tl.alltlobjects.LAYER
    except AttributeError:
        return None


def _redispatch_update_types(module: Any) -> tuple:
    return tuple(
        getattr(module.types, name)
        for name in REDISPATCH_UPDATE_NAMES
        if hasattr(module.types, name)
    )


def _patch_telethon(
    module: Any, *, report: ReportFn, on_recovered: Callable[[list], None]
) -> list:
    not_found_error = module.errors.TypeNotFoundError
    container_cls = module.tl.core.messagecontainer.MessageContainer
    sender_cls = module.network.mtprotosender.MTProtoSender
    messagebox = module._updates.messagebox
    return [
        _Patch.apply(
            container_cls,
            "from_reader",
            replacement=_skipping_container_reader(
                container_cls=container_cls,
                message_cls=module.tl.core.tlmessage.TLMessage,
                not_found_error=not_found_error,
                report=report,
            ),
        ),
        _Patch.apply(
            sender_cls,
            "_process_message",
            replacement=_guarded_process_message(
                original=sender_cls.__dict__["_process_message"],
                not_found_error=not_found_error,
                report=report,
            ),
        ),
        _Patch.apply(
            sender_cls,
            "_reconnect",
            replacement=_at_most_once_reconnect(
                original=sender_cls.__dict__["_reconnect"]
            ),
        ),
        _Patch.apply(
            messagebox.MessageBox,
            "apply_difference_type",
            replacement=_redispatching_apply_difference_type(
                original=messagebox.MessageBox.__dict__["apply_difference_type"],
                pts_info_cls=messagebox.PtsInfo,
                secret_entry=messagebox.ENTRY_SECRET,
                update_types=_redispatch_update_types(module),
                on_recovered=on_recovered,
            ),
        ),
    ]


def _install(
    module: Any,
    *,
    stats: Optional[SafetyStats],
    alert: Optional[AlertFn],
    clock: Optional[Callable[[], float]],
) -> _Installation:
    version = getattr(module, "__version__", None)
    monitor = SafetyMonitor(
        stats=stats,
        alert=alert,
        clock=clock,
        context=f"Telethon {version}, layer {_telethon_layer(module)}",
    )

    patches = []
    if version in SUPPORTED_TELETHON_VERSIONS:
        patches = _patch_telethon(
            module, report=monitor.report, on_recovered=monitor.record_recovered
        )
        monitor.stats.installed.update(_PATCH_NETS)
    else:
        _log.warning(
            "Telethon %s is not one of the versions the Telethon patches were "
            "checked against (%s); not installing them",
            version,
            ", ".join(sorted(SUPPORTED_TELETHON_VERSIONS)),
        )

    handler = TypeNotFoundLogHandler(
        report=monitor.report,
        not_found_error=module.errors.TypeNotFoundError,
    )
    sender_logger = logging.getLogger(f"{module.__name__}.network.mtprotosender")
    sender_logger.addHandler(handler)
    monitor.stats.installed.add(SafetyNet.LOG_COUNTER)

    return _Installation(
        module=module,
        monitor=monitor,
        handler=handler,
        sender_logger=sender_logger,
        patches=patches,
    )


def install_safety_nets(
    *,
    stats: Optional[SafetyStats] = None,
    alert: Optional[AlertFn] = None,
    telethon_module: Any = None,
    client: Optional[DifferenceFallbackMixin] = None,
    environ=None,
    clock: Optional[Callable[[], float]] = None,
    sleep: Optional[Callable[[float], Awaitable[None]]] = None,
) -> SafetyStats:
    """Installs the Telegram safety nets and returns their shared stats.

    - The container skip, the message guard, the qts re-dispatch and the
      at-most-once guard patch Telethon's classes, so they apply process-wide,
      only on `SUPPORTED_TELETHON_VERSIONS`.
    - The log counter is a handler on Telethon's sender logger.
    - `client`, which must use `DifferenceFallbackMixin`, gets a
      `DifferenceFallback` reporting into the same stats.

    Idempotent per Telethon module: a repeat call keeps the first call's stats
    and patches, replaces the alert when one is given, and attaches `client`
    if it has no fallback yet. `SAFETY_NETS_ENV` switches all of it off; an
    unrecognised value raises `ValueError`.
    """
    if not safety_nets_enabled(environ=environ):
        _log.info("Telegram safety nets are disabled by %s", SAFETY_NETS_ENV)
        return stats if stats is not None else SafetyStats()

    if client is not None and not isinstance(client, DifferenceFallbackMixin):
        raise TypeError(
            f"{type(client).__name__} does not use DifferenceFallbackMixin, "
            "so a difference fallback would never run"
        )

    module = telethon_module or telethon
    installation = _INSTALLATIONS.get(id(module))
    if installation is None:
        installation = _install(module, stats=stats, alert=alert, clock=clock)
        _INSTALLATIONS[id(module)] = installation
    elif alert is not None:
        installation.monitor.alert = alert

    monitor = installation.monitor
    if client is not None:
        if client.difference_fallback is None:
            client.difference_fallback = DifferenceFallback(
                report=monitor.report,
                telethon_module=module,
                clock=clock,
                sleep=sleep,
            )
        monitor.stats.installed.add(SafetyNet.DIFFERENCE_FALLBACK)
    return monitor.stats


def uninstall_safety_nets(*, telethon_module: Any = None) -> bool:
    """Restores Telethon's classes and removes the log counter.

    Clients keep any `DifferenceFallback` already attached; set their
    `difference_fallback` to None to drop it. Returns whether anything was
    installed.
    """
    module = telethon_module or telethon
    installation = _INSTALLATIONS.pop(id(module), None)
    if installation is None:
        return False

    for patch in reversed(installation.patches):
        patch.restore()
    installation.sender_logger.removeHandler(installation.handler)
    installation.monitor.stats.installed -= _PATCH_NETS | {SafetyNet.LOG_COUNTER}
    return True
