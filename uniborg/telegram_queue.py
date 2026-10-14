"""Client-owned pacing and recovery for the chat bot's Telegram operations.

Only explicit flood rejections are retried. In particular, this does not retry
an ambiguous network failure or the at-most-once guest-result transport.
"""

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import IntEnum
import logging
from typing import Optional

from telethon import errors, utils
from telethon.tl import TLRequest

log = logging.getLogger(__name__)


class Priority(IntEnum):
    FINAL = 0
    NORMAL = 10
    METADATA = 20
    STREAM = 30


_priority = ContextVar("telegram_priority", default=Priority.NORMAL)
_before_send = ContextVar("telegram_before_send", default=None)


@contextmanager
def operation(priority, *, before_send=None):
    token = _priority.set(priority)
    guard_token = _before_send.set(before_send)
    try:
        yield
    finally:
        _priority.reset(token)
        _before_send.reset(guard_token)


class StreamingDeferred(Exception):
    """An optional edit was skipped; the model must keep generating."""


class OperationSuperseded(Exception):
    """Queued metadata no longer applies when it reaches the front."""


def _note(event, request, **fields):
    details = " ".join(f"{key}={value}" for key, value in fields.items())
    log.info(
        "%s Telegram queue %s request=%s %s",
        datetime.now(timezone.utc).isoformat(),
        event,
        type(request).__name__,
        details,
    )


async def _wait(event, seconds):
    if seconds is None:
        await event.wait()
    else:
        try:
            await asyncio.wait_for(event.wait(), max(seconds, 0.001))
        except asyncio.TimeoutError:
            pass


@dataclass(eq=False)
class _Ticket:
    request: object
    priority: Priority
    kind: int
    chat: Optional[int]
    lane: object
    interval: float
    guard: object


class TelegramQueue:
    """A scheduler for one Telegram client, with interruptible, shared waits.

    No lock is held while sleeping or making an RPC. State changes between
    awaits are atomic on the client's event loop. Queued work owns its request
    object, including Telegram's random_id, across explicit flood retries.
    """

    MESSAGE_TYPES = {
        "SendMessageRequest",
        "SendMediaRequest",
        "SendMultiMediaRequest",
        "EditMessageRequest",
        "SendInlineBotResultRequest",
    }
    EXCLUDED = {
        "SetTypingRequest",
        "SetBotCallbackAnswerRequest",
        "SetBotGuestChatResultRequest",
        "EditInlineBotMessageRequest",
    }

    def __init__(self, *, clock=None, wait=None):
        self.clock = clock or (lambda: asyncio.get_running_loop().time())
        self.wait = wait or _wait
        self.pending = []
        self.busy = set()
        self.cooldowns = {}
        self.next_at = {}
        self.changed = asyncio.Event()

    @classmethod
    def handles(cls, request):
        return (
            isinstance(request, TLRequest)
            and type(request).__name__ not in cls.EXCLUDED
        )

    def _notify(self):
        changed, self.changed = self.changed, asyncio.Event()
        changed.set()

    def _ticket(self, request):
        chat = None
        peer = getattr(request, "peer", None)
        if peer is not None:
            try:
                chat = utils.get_peer_id(peer)
            except (TypeError, ValueError):
                pass
        name = type(request).__name__
        if name in self.MESSAGE_TYPES and chat is not None and chat > 0:
            lane, interval = ("private", chat), 1.0
        elif name == "EditForumTopicRequest":
            lane, interval = ("topic",), 1.0
        else:
            lane, interval = None, 0.0
        return _Ticket(
            request,
            _priority.get(),
            request.CONSTRUCTOR_ID,
            chat,
            lane,
            interval,
            _before_send.get(),
        )

    def _due(self, ticket):
        return max(
            self.cooldowns.get((ticket.kind, None), 0),
            self.cooldowns.get((ticket.kind, ticket.chat), 0),
            self.next_at.get(ticket.lane, 0) if ticket.lane else 0,
        )

    def _available(self, ticket, now):
        return ticket.kind not in self.busy and self._due(ticket) <= now

    def _winner(self, now):
        return min(
            (t for t in self.pending if self._available(t, now)),
            key=lambda t: t.priority,
            default=None,
        )

    async def run(self, request, call):
        if asyncio.current_task().cancelling():
            raise asyncio.CancelledError()
        ticket = self._ticket(request)
        self.pending.append(ticket)
        self._notify()
        _note("queued", request, priority=ticket.priority.name)
        try:
            while True:
                now = self.clock()
                if self._winner(now) is not ticket:
                    if ticket.priority == Priority.STREAM:
                        raise StreamingDeferred()
                    # Wake at the first ready operation or when an RPC finishes.
                    deadlines = [
                        self._due(t) for t in self.pending if t.kind not in self.busy
                    ]
                    future = [due for due in deadlines if due > now]
                    delay = min(future) - now if future else None
                    await self.wait(self.changed, delay)
                    continue
                self.pending.remove(ticket)
                self.busy.add(ticket.kind)
                try:
                    if ticket.guard is not None:
                        with operation(ticket.priority):
                            if not await ticket.guard():
                                raise OperationSuperseded()
                    if ticket.lane:
                        self.next_at[ticket.lane] = self.clock() + ticket.interval
                    result = await call(request)
                except (
                    errors.FloodWaitError,
                    errors.FloodPremiumWaitError,
                    errors.SlowModeWaitError,
                ) as exc:
                    key = (
                        ticket.kind,
                        (
                            ticket.chat
                            if isinstance(exc, errors.SlowModeWaitError)
                            else None
                        ),
                    )
                    self.cooldowns[key] = max(
                        self.cooldowns.get(key, 0), self.clock() + max(1, exc.seconds)
                    )
                    _note("cooldown", request, seconds=exc.seconds)
                    if ticket.priority == Priority.STREAM:
                        raise StreamingDeferred() from exc
                    self.pending.append(ticket)
                except OperationSuperseded:
                    _note("superseded", request)
                    raise
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    _note("failed", request, error=type(exc).__name__)
                    raise
                else:
                    _note("delivered", request)
                    return result
                finally:
                    self.busy.discard(ticket.kind)
                    self._notify()
        finally:
            if ticket in self.pending:
                self.pending.remove(ticket)
            self._notify()


class TelegramQueueMixin:
    """Placed inside the outgoing guard and topic-placement wrappers."""

    telegram_queue = None

    async def __call__(self, request, ordered=False, flood_sleep_threshold=None):
        call = super().__call__
        queue = self.telegram_queue
        if queue is None or not queue.handles(request):
            return await call(
                request, ordered=ordered, flood_sleep_threshold=flood_sleep_threshold
            )
        await request.resolve(self, utils)

        async def invoke(request):
            return await call(request, ordered=ordered, flood_sleep_threshold=0)

        return await queue.run(request, invoke)
