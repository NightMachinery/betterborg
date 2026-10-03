"""Showing text while it is produced, by editing a message at a measured pace.

A *streaming loop* reads a growing text (a model's answer) and shows it in a
message as it grows. `PacedEditor` is the step each loop repeats: when an
edit is due, edit the message to the text so far, plus a cursor that says
more is coming. How often an edit is due is the *pace*, chosen per message by
a pace function: by default `draft_stream.streaming_pace`, which slows edits
down as the answer ages and keeps a draft's own pace.

The editor only edits when it is asked to show new text (the leading edge),
and it creates no task or timer, so a streaming loop stays a plain loop.
A producer that can go quiet, such as a shell command, needs the trailing
edge too: its last text must show even when nothing follows it. `follow` is
that pump: it waits for changes and shows the latest text once it is due.

A user picks per *scope* (private chats, or groups) whether answers stream
as drafts or by edits (`StreamMode`). The settings live in the plugin's own
preferences, any object with a `stream_private` and a `stream_groups` field;
`stream_mode` and `set_stream_mode` read and write them.

A *stream target* is the message a reply streams into: a draft stand-in
(`draft_stream.DraftAnswerMessage`) where the user streams drafts and
Telegram shows one, otherwise a sent placeholder message.
`open_stream_target` picks it, `stop_wired` and `run_stoppable` connect a
draft's Stop button to the work that fills it, and `flush_draft` sends what
a draft last showed once the work is over. `register_draft_stop` lets a
plugin receive the Stop button's presses.

This is a core module, so a plugin reload never re-executes it. How the chat
bot uses it is in docs/draft_streaming.md.
"""

import asyncio
from contextlib import asynccontextmanager
from enum import Enum
import logging
from typing import Any, AsyncIterator, Awaitable, Callable, Optional, TypeVar

from telethon import errors, events
from telethon.tl.types import UpdateUserTyping

from uniborg import draft_stream, util

_log = logging.getLogger(__name__)

_T = TypeVar("_T")

#: A pace function: `(message, *, elapsed, edit_interval) -> StreamingPace`.
PaceFunction = Callable[..., draft_stream.StreamingPace]
#: `follow` waits this much past an edit's due time, since an edit is due
#: only strictly after the interval.
FOLLOW_SLACK = 0.05


class StreamMode(str, Enum):
    """How an answer shows while it is written (/stream)."""

    #: Telegram's live draft, with a Stop button; private chats only.
    DRAFTS = "drafts"
    #: A message edited as the answer grows.
    EDITS = "edits"


STREAM_SCOPE_PRIVATE = "private"
STREAM_SCOPE_GROUPS = "groups"
STREAM_SCOPE_NAMES = {
    STREAM_SCOPE_PRIVATE: "Private chats",
    STREAM_SCOPE_GROUPS: "Groups",
}
STREAM_MODE_NAMES = {StreamMode.DRAFTS: "Drafts", StreamMode.EDITS: "Edits"}


def stream_scope(event: Any) -> str:
    """The scope of the chat EVENT happened in."""
    return STREAM_SCOPE_PRIVATE if event.is_private else STREAM_SCOPE_GROUPS


def _stream_field(scope: str) -> str:
    if scope == STREAM_SCOPE_PRIVATE:
        return "stream_private"
    elif scope == STREAM_SCOPE_GROUPS:
        return "stream_groups"
    else:
        raise ValueError(f"Unknown stream scope: {scope!r}")


def stream_mode(prefs: Any, *, scope: str) -> StreamMode:
    """The mode PREFS set for SCOPE."""
    return StreamMode(getattr(prefs, _stream_field(scope)))


def set_stream_mode(prefs: Any, *, scope: str, mode: StreamMode) -> None:
    """Sets PREFS's mode for SCOPE to MODE."""
    setattr(prefs, _stream_field(scope), mode)


async def _reply(event: Any, text: str) -> Any:
    return await event.reply(text)


async def open_stream_target(
    event: Any,
    *,
    client: Any,
    drafts: bool,
    placeholder_text: str,
    draft_text: Optional[str] = None,
    send_placeholder: Callable[[Any, str], Awaitable[Any]] = _reply,
    top_msg_id: Optional[int] = None,
    logger: Optional[logging.Logger] = None,
) -> Any:
    """The stream target of a reply to EVENT.

    With DRAFTS, a draft stand-in that first shows DRAFT_TEXT (by default
    PLACEHOLDER_TEXT; "" shows Telegram's own "Thinking…"), in the private
    topic TOP_MSG_ID when given. Without DRAFTS, or when the first draft
    fails or Telegram refuses one in this chat, the message that
    `send_placeholder(event, PLACEHOLDER_TEXT)` sends, by default a reply.
    """
    if drafts:
        draft = draft_stream.DraftAnswerMessage(
            client, event=event, top_msg_id=top_msg_id, logger=logger
        )
        if await draft.start(placeholder_text if draft_text is None else draft_text):
            return draft
    return await send_placeholder(event, placeholder_text)


@asynccontextmanager
async def stop_wired(target: Any, *, on_stop: Callable[[], Any]) -> AsyncIterator[None]:
    """Inside, a press of draft TARGET's Stop button calls ON_STOP.

    On leaving, by a return or an error, the draft stream ends, so no draft
    can arrive after the reply. A TARGET that is not a draft is left alone.
    """
    if not isinstance(target, draft_stream.DraftAnswerMessage):
        yield
        return
    target.on_stop = on_stop
    try:
        yield
    finally:
        await target.end_stream()


async def run_stoppable(target: Any, work: Awaitable[_T]) -> _T:
    """Awaits WORK, which fills TARGET; a draft's Stop button cancels it.

    For a draft TARGET, WORK runs as its own task, so that Stop can cancel
    it, and the draft stream ends when it returns or fails (`stop_wired`).
    """
    if not isinstance(target, draft_stream.DraftAnswerMessage):
        return await work
    task = asyncio.ensure_future(work)
    async with stop_wired(target, on_stop=task.cancel):
        return await task


async def flush_draft(target: Any, *, logger: Optional[logging.Logger] = None) -> None:
    """Sends what draft TARGET last showed and nothing replaced, for real.

    Such as an error or a cancelled partial answer. Call it once the work is
    over, whatever its outcome. A failure is logged to LOGGER, by default the
    draft's own, and not raised. A TARGET that is not a draft is left alone.
    """
    if not isinstance(target, draft_stream.DraftAnswerMessage):
        return
    try:
        await target.flush()
    except Exception:
        (logger or target.logger).warning(
            "Could not send a draft's last text", exc_info=True
        )


def register_draft_stop(client: Any, *, module: str) -> bool:
    """Handles presses of drafts' Stop buttons on CLIENT, for the plugin MODULE.

    MODULE is the plugin's `__name__`: the handler counts as the plugin's
    own, so reloading or removing the plugin removes it
    (`Uniborg.remove_events_of_mod`). False, registering nothing, where this
    Telethon cannot build a Stop button (`draft_stream.STOP_SUPPORTED`).
    Plugins that each register one are safe, since a press stops its stream
    only once.
    """
    if not draft_stream.STOP_SUPPORTED:
        return False

    async def draft_stop_handler(update):
        await draft_stream.on_typing_update(update)

    draft_stop_handler.__module__ = module
    client.on(events.Raw(types=UpdateUserTyping))(draft_stop_handler)
    return True


class ShowResult(Enum):
    """What `PacedEditor.show` did."""

    #: The message now shows the text.
    SHOWN = "shown"
    #: Too soon after the last edit; nothing was sent.
    NOT_DUE = "not_due"
    #: There is no message to edit.
    NO_TARGET = "no_target"
    #: The edit failed, or changed nothing; the last-edit time is unchanged.
    FAILED = "failed"


def fixed_pace(*, cursor: str = "▌") -> PaceFunction:
    """A pace function that keeps EDIT_INTERVAL and CURSOR at any age."""

    def pace(
        response_message: Any, *, elapsed: float, edit_interval: float
    ) -> draft_stream.StreamingPace:
        return draft_stream.StreamingPace(interval=edit_interval, cursor=cursor)

    return pace


def _default_render(text: str, pace: draft_stream.StreamingPace) -> str:
    return f"{text}{pace.cursor}"


def _loop_time() -> float:
    return asyncio.get_running_loop().time()


class PacedEditor:
    """Edits MESSAGE to the text it is shown, at most once per pace interval.

    The pace is `pace(message, elapsed=…, edit_interval=EDIT_INTERVAL)`, where
    `elapsed` counts from the editor's creation. An edit is due once strictly
    more than the pace's interval has passed since the last successful edit
    (or since creation). `render(text, pace)` builds what is sent; by default
    the text and the pace's cursor. CLOCK defaults to the running loop's time,
    read on every call.

    MESSAGE may be None, for a caller that only collects the text; `show` then
    does nothing.
    """

    def __init__(
        self,
        message: Any,
        *,
        edit_interval: float,
        parse_mode: Any = "md",
        pace: PaceFunction = draft_stream.streaming_pace,
        render: Optional[Callable[[str, draft_stream.StreamingPace], str]] = None,
        clock: Optional[Callable[[], float]] = None,
        logger: Optional[logging.Logger] = None,
    ):
        self.message = message
        self.edit_interval = edit_interval
        self.parse_mode = parse_mode
        self._pace = pace
        self._render = render or _default_render
        self._clock = clock or _loop_time
        self._log = logger or _log
        self.started_at = self._clock()
        self.last_edit_at = self.started_at

    def _pace_at(self, now: float) -> draft_stream.StreamingPace:
        return self._pace(
            self.message,
            elapsed=now - self.started_at,
            edit_interval=self.edit_interval,
        )

    def due_in(self) -> float:
        """Seconds until an edit is due; 0 when one is due now."""
        now = self._clock()
        return max(0.0, self.last_edit_at + self._pace_at(now).interval - now)

    async def show(self, text: str) -> ShowResult:
        """Edits the message to TEXT when an edit is due."""
        if self.message is None:
            return ShowResult.NO_TARGET
        now = self._clock()
        pace = self._pace_at(now)
        if not now - self.last_edit_at > pace.interval:
            return ShowResult.NOT_DUE
        try:
            await util.edit_message(
                self.message, self._render(text, pace), parse_mode=self.parse_mode
            )
        except errors.rpcerrorlist.MessageNotModifiedError:
            return ShowResult.FAILED
        except Exception as e:
            #: A failed edit never stops the stream; the next one tries again.
            self._log.warning("Could not edit a streaming message: %r", e)
            return ShowResult.FAILED
        self.last_edit_at = now
        return ShowResult.SHOWN


async def _wait_first(*futures, timeout: Optional[float] = None) -> None:
    await asyncio.wait(futures, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)


async def follow(
    editor: PacedEditor,
    *,
    render: Callable[[], str],
    changed: asyncio.Event,
    done: asyncio.Future,
    retry_after: float = 1.0,
) -> None:
    """Shows `render()` through EDITOR whenever CHANGED is set, until DONE.

    A change that arrives before an edit is due is shown once it is due, so
    the last text shows even when nothing follows it; a burst of changes
    makes one edit. After a failed edit, the next waits RETRY_AFTER seconds,
    so a broken message cannot make this spin. The final text is the
    caller's to deliver: this returns as soon as DONE completes, without
    showing what changed since the last edit.
    """
    #: A change not yet shown.
    behind = False
    while not done.done():
        waiter = asyncio.ensure_future(changed.wait())
        try:
            await _wait_first(
                waiter,
                done,
                timeout=editor.due_in() + FOLLOW_SLACK if behind else None,
            )
        finally:
            waiter.cancel()
        if done.done():
            return
        if changed.is_set():
            changed.clear()
            behind = True
        if not behind or editor.due_in() > 0:
            continue
        result = await editor.show(render())
        if result == ShowResult.SHOWN:
            behind = False
        elif result == ShowResult.NOT_DUE:
            pass
        elif result == ShowResult.FAILED:
            await _wait_first(done, timeout=retry_after)
        elif result == ShowResult.NO_TARGET:
            await _wait_first(done)
        else:
            raise ValueError(f"Unknown show result: {result!r}")
