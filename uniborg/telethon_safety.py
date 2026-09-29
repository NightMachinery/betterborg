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

The nets below shrink the loss to the one object and count every event in
`SafetyStats`. This module imports only the standard library and Telethon, so
tests and tools can load it without ``uniborg.util``.
"""
import asyncio
from collections import Counter
from dataclasses import dataclass, field
import enum
import functools
import logging
import os
import time
from typing import Any, Awaitable, Callable, Optional

import telethon
from telethon import errors

#: Loaded so `_patch_telethon` can reach them as attributes of the package.
import telethon.network.mtprotosender
import telethon.tl.core.messagecontainer
import telethon.tl.core.tlmessage
from telethon.tl.tlobject import TLObject

SAFETY_NETS_ENV = "borg_tg_safety_nets"

#: The container patches replace private Telethon internals. Both versions ship
#: byte-identical `MessageContainer.from_reader` and `MTProtoSender`'s
#: `_process_message`/`_handle_container`/`_handle_gzip_packed`/`_handle_update`.
SUPPORTED_TELETHON_VERSIONS = frozenset({"1.43.2", "1.45.0"})

_ENABLED_VALUES = frozenset({"", "1", "true", "yes", "on"})
_DISABLED_VALUES = frozenset({"0", "false", "no", "off"})

_log = logging.getLogger(__name__)

ReportFn = Callable[..., None]


class SafetyKind(enum.Enum):
    """Where an unparseable object was caught, and what that cost."""

    CONTAINER_ENTRY = "container_entry"
    PROCESSING = "processing"
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
    DIFFERENCE_FALLBACK = "difference_fallback"


_PATCH_NETS = frozenset({SafetyNet.CONTAINER_SKIP, SafetyNet.MESSAGE_GUARD})


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
        return f"nets: {nets}; events: {kinds}; constructors: {constructors}"


class SafetyMonitor:
    """Records safety-net events: counts them and logs them."""

    def __init__(
        self,
        *,
        stats: Optional[SafetyStats] = None,
        logger: Optional[logging.Logger] = None,
    ):
        self.stats = stats if stats is not None else SafetyStats()
        self._log = logger or _log

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
    """Reads `SAFETY_NETS_ENV`; unset means on. Unknown values raise."""
    environ = os.environ if environ is None else environ
    raw = environ.get(SAFETY_NETS_ENV, "")
    value = raw.strip().lower()
    if value in _ENABLED_VALUES:
        return True
    if value in _DISABLED_VALUES:
        return False
    raise ValueError(
        f"{SAFETY_NETS_ENV}={raw!r} is not a recognised switch; "
        "use 1/true/yes/on or 0/false/no/off"
    )


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
    patches: list


#: Keyed by id() of the Telethon module; the entry keeps the module alive, so
#: the id cannot be reused while it is registered.
_INSTALLATIONS = {}


def _patch_telethon(module: Any, *, report: ReportFn) -> list:
    not_found_error = module.errors.TypeNotFoundError
    container_cls = module.tl.core.messagecontainer.MessageContainer
    sender_cls = module.network.mtprotosender.MTProtoSender
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
    ]


def _install(module: Any, *, stats: Optional[SafetyStats]) -> _Installation:
    version = getattr(module, "__version__", None)
    monitor = SafetyMonitor(stats=stats)

    patches = []
    if version in SUPPORTED_TELETHON_VERSIONS:
        patches = _patch_telethon(module, report=monitor.report)
        monitor.stats.installed.update(_PATCH_NETS)
    else:
        _log.warning(
            "Telethon %s is not one of the versions the container patches were "
            "checked against (%s); not installing them",
            version,
            ", ".join(sorted(SUPPORTED_TELETHON_VERSIONS)),
        )

    return _Installation(module=module, monitor=monitor, patches=patches)


def install_safety_nets(
    *,
    stats: Optional[SafetyStats] = None,
    telethon_module: Any = None,
    client: Optional[DifferenceFallbackMixin] = None,
    environ=None,
    clock: Optional[Callable[[], float]] = None,
    sleep: Optional[Callable[[float], Awaitable[None]]] = None,
) -> SafetyStats:
    """Installs the Telegram safety nets and returns their shared stats.

    - The container skip and the message guard patch Telethon's classes, so
      they apply process-wide, only on `SUPPORTED_TELETHON_VERSIONS`.
    - `client`, which must use `DifferenceFallbackMixin`, gets a
      `DifferenceFallback` reporting into the same stats.

    Idempotent per Telethon module: a repeat call keeps the first call's stats
    and patches, and attaches `client` if it has no fallback yet. `SAFETY_NETS_ENV` switches all of it off; an
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
        installation = _install(module, stats=stats)
        _INSTALLATIONS[id(module)] = installation

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
    """Restores Telethon's classes.

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
    installation.monitor.stats.installed -= _PATCH_NETS
    return True
