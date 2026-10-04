"""Showing text while it is produced, by editing a message at a measured pace.

A *streaming loop* reads a growing text (a model's answer) and shows it in a
message as it grows. `PacedEditor` is the step each loop repeats: when an
edit is due, edit the message to the text so far, plus a cursor that says
more is coming. How often an edit is due is the *pace*, chosen per message by
a pace function: by default `draft_stream.streaming_pace`, which slows edits
down as the answer ages and keeps a draft's own pace; `fixed_pace` and
`tiered_pace` build others.

The editor only edits when it is asked to show new text (the leading edge),
and it creates no task or timer, so a streaming loop stays a plain loop.
A producer that can go quiet, such as a shell command, needs the trailing
edge too: its last text must show even when nothing follows it. `follow` is
that pump: it waits for changes and shows the latest text once it is due.

A user picks per *scope* (private chats, or groups) whether answers stream
as drafts or by edits (`StreamMode`). The settings live in the plugin's own
preferences, any object with a `stream_private` and a `stream_groups` field;
`stream_mode` and `set_stream_mode` read and write them, and
`stream_mode_lines`, `stream_mode_rows`, `stream_choice` and
`stream_choice_of_args` draw and read the menu that changes them (the chat
bot's /stream, the shell's /settings).

A *stream target* is the message a reply streams into: a draft stand-in
(`draft_stream.DraftAnswerMessage`) where the user streams drafts and
Telegram shows one, otherwise a sent placeholder message.
`open_stream_target` picks it, `stop_wired` and `run_stoppable` connect a
draft's Stop button to the work that fills it, `flush_draft` sends what a
draft last showed once the work is over, and `show_final` makes a target
show its final text instead. `register_draft_stop` lets a plugin receive
the Stop button's presses.

This is a core module, so a plugin reload never re-executes it. How the chat
bot uses it is in docs/draft_streaming.md.
"""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import Enum
import logging
from typing import Any, AsyncIterator, Awaitable, Callable, Optional, TypeVar

from telethon import errors, events
from telethon.tl.types import UpdateUserTyping

from uniborg import draft_stream, tg_compat, util

_log = logging.getLogger(__name__)

_T = TypeVar("_T")

#: A pace function: `(message, *, elapsed, edit_interval) -> StreamingPace`.
PaceFunction = Callable[..., draft_stream.StreamingPace]
#: `follow` waits this much past an edit's due time, since an edit is due
#: only strictly after the interval.
FOLLOW_SLACK = 0.05
#: The longest `follow` waits after failed edits in a row.
FOLLOW_MAX_RETRY_AFTER = 60.0


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


@dataclass(frozen=True)
class StreamChoice:
    """A mode for a scope, as a menu button or a command names it."""

    scope: str
    mode: StreamMode


def stream_mode_lines(prefs: Any) -> list[str]:
    """A Markdown line per scope that names the mode PREFS set for it."""
    return [
        f"{name}: **{STREAM_MODE_NAMES[stream_mode(prefs, scope=scope)]}**"
        for scope, name in STREAM_SCOPE_NAMES.items()
    ]


def stream_mode_rows(prefs: Any, *, callback_prefix: str) -> list[list]:
    """A row of buttons per scope, one per mode, with PREFS's mode checked.

    A button sends CALLBACK_PREFIX, then `scope:mode`, which `stream_choice`
    reads back.
    """
    rows = []
    for scope, name in STREAM_SCOPE_NAMES.items():
        current = stream_mode(prefs, scope=scope)
        rows.append(
            [
                tg_compat.callback_button(
                    f"{'✅ ' if mode == current else ''}{name}: "
                    f"{STREAM_MODE_NAMES[mode]}",
                    data=f"{callback_prefix}{scope}:{mode.value}",
                )
                for mode in StreamMode
            ]
        )
    return rows


def stream_choice(data: str) -> StreamChoice:
    """The choice a `stream_mode_rows` button sends, as DATA after its prefix.

    Raises ValueError for data no such button sends.
    """
    scope, mode = data.split(":", 1)
    if scope not in STREAM_SCOPE_NAMES:
        raise ValueError(f"Unknown stream scope: {scope!r}")
    return StreamChoice(scope=scope, mode=StreamMode(mode))


def stream_choice_of_args(args: list[str]) -> Optional[StreamChoice]:
    """The choice that ARGS (`["groups", "drafts"]`, any case) name, or None."""
    words = [arg.lower() for arg in args]
    modes = {mode.value: mode for mode in StreamMode}
    if len(words) != 2 or words[0] not in STREAM_SCOPE_NAMES or words[1] not in modes:
        return None
    return StreamChoice(scope=words[0], mode=modes[words[1]])


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
    parse_mode: Any = (),
    logger: Optional[logging.Logger] = None,
) -> Any:
    """The stream target of a reply to EVENT.

    With DRAFTS, a draft stand-in that first shows DRAFT_TEXT (by default
    PLACEHOLDER_TEXT; "" shows Telegram's own "Thinking…") in the private
    topic TOP_MSG_ID when given, parsed with PARSE_MODE (by default the
    stand-in's own, Markdown; None is plain text). Without DRAFTS, or when the
    first draft fails or Telegram refuses one in this chat, the message that
    `send_placeholder(event, PLACEHOLDER_TEXT)` sends, by default a reply.
    """
    if drafts:
        draft = draft_stream.DraftAnswerMessage(
            client, event=event, top_msg_id=top_msg_id, logger=logger
        )
        first = placeholder_text if draft_text is None else draft_text
        start_kwargs = {} if parse_mode == () else {"parse_mode": parse_mode}
        if await draft.start(first, **start_kwargs):
            return draft
    return await send_placeholder(event, placeholder_text)


async def show_final(target: Any, text: str) -> Any:
    """Makes stream TARGET show TEXT for good, as plain text with no buttons.

    A message is edited in place, and loses any buttons: `Message.edit`
    keeps the old ones unless told otherwise. A draft's stream ends, and TEXT
    is sent as a real reply to the draft's event, after a sync draft so that
    the client adopts the draft into it. Returns the message that shows TEXT.
    Errors propagate.
    """
    kwargs = {"parse_mode": None, "link_preview": False, "buttons": None}
    if isinstance(target, draft_stream.DraftAnswerMessage):
        await target.end_stream()
        await target.edit(text, **kwargs)
        return target.message
    return await target.edit(text, **kwargs) or target


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
    #: Telegram says the message already shows the text; the last-edit time
    #: is unchanged.
    UNCHANGED = "unchanged"
    #: The edit failed; the last-edit time is unchanged. Only an editor built
    #: with `report_failures` sees a failed edit.
    FAILED = "failed"


def fixed_pace(*, cursor: str = "▌") -> PaceFunction:
    """A pace function that keeps EDIT_INTERVAL and CURSOR at any age."""

    def pace(
        response_message: Any, *, elapsed: float, edit_interval: float
    ) -> draft_stream.StreamingPace:
        return draft_stream.StreamingPace(interval=edit_interval, cursor=cursor)

    return pace


def tiered_pace(
    *, slow_after: float, slow_interval: float, cursor: str = "▌"
) -> PaceFunction:
    """A pace function: EDIT_INTERVAL, then SLOW_INTERVAL after SLOW_AFTER seconds.

    A draft keeps the draft pace (`draft_stream.streaming_pace`).
    """

    def pace(
        response_message: Any, *, elapsed: float, edit_interval: float
    ) -> draft_stream.StreamingPace:
        if isinstance(response_message, draft_stream.DraftAnswerMessage):
            return draft_stream.streaming_pace(
                response_message, elapsed=elapsed, edit_interval=edit_interval
            )
        interval = slow_interval if elapsed > slow_after else edit_interval
        return draft_stream.StreamingPace(interval=interval, cursor=cursor)

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

    `util.edit_message` prints a failed edit of MESSAGE and returns, so by
    default a failed edit counts as made: a deleted message costs one failed
    edit per interval, however often `show` is called. With REPORT_FAILURES
    it raises the failure instead, and `show` logs it and returns FAILED
    without moving the last-edit time, so the caller decides when to retry.
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
        report_failures: bool = False,
    ):
        self.message = message
        self.edit_interval = edit_interval
        self.parse_mode = parse_mode
        self.report_failures = report_failures
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

    def interval(self) -> float:
        """The pace's interval now: the least time between two edits."""
        return self._pace_at(self._clock()).interval

    async def show(self, text: str) -> ShowResult:
        """Edits the message to TEXT when an edit is due."""
        if self.message is None:
            return ShowResult.NO_TARGET
        now = self._clock()
        pace = self._pace_at(now)
        if not now - self.last_edit_at > pace.interval:
            return ShowResult.NOT_DUE
        edit_kwargs = {"parse_mode": self.parse_mode}
        if self.report_failures:
            edit_kwargs["raise_on_head_failure"] = True
        try:
            await util.edit_message(
                self.message, self._render(text, pace), **edit_kwargs
            )
        except errors.rpcerrorlist.MessageNotModifiedError:
            return ShowResult.UNCHANGED
        except Exception as e:
            #: A failed edit never stops the stream; the next one tries again.
            self._log.warning("Could not edit a streaming message: %r", e)
            return ShowResult.FAILED
        self.last_edit_at = now
        return ShowResult.SHOWN


def retry_wait(failures: int, *, first: float, longest: float) -> float:
    """How long to wait after a failed edit that follows FAILURES failed
    edits in a row: FIRST, doubled per earlier failure, at most LONGEST (or
    FIRST, when that is longer)."""
    return min(first * 2 ** min(failures, 32), max(first, longest))


async def _wait_first(*futures, timeout: Optional[float] = None) -> None:
    await asyncio.wait(futures, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)


async def follow(
    editor: PacedEditor,
    *,
    render: Callable[[], str],
    changed: asyncio.Event,
    done: asyncio.Future,
    retry_after: float = 1.0,
    max_retry_after: float = FOLLOW_MAX_RETRY_AFTER,
) -> None:
    """Shows `render()` through EDITOR whenever CHANGED is set, until DONE.

    A change that arrives before an edit is due is shown once it is due, so
    the last text shows even when nothing follows it; a burst of changes
    makes one edit. After a failed edit, the next waits RETRY_AFTER seconds,
    or the pace's interval when that is longer, and each further failure in
    a row doubles the wait, up to MAX_RETRY_AFTER: a message that is gone
    (deleted, or the bot left the chat) cannot make this spin, nor cost an
    edit a second for as long as the command runs. A shown edit starts the
    count again. EDITOR must be built with `report_failures`, since
    otherwise it never sees a failed edit. A message that Telegram says
    already shows the text counts as shown. The final text is the caller's
    to deliver: this returns as soon as DONE completes, without showing what
    changed since the last edit.
    """
    if not editor.report_failures:
        raise ValueError("follow needs an editor built with report_failures=True")
    #: A change not yet shown.
    behind = False
    #: Failed edits in a row.
    failures = 0
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
        if result in (ShowResult.SHOWN, ShowResult.UNCHANGED):
            behind = False
            failures = 0
        elif result == ShowResult.NOT_DUE:
            pass
        elif result == ShowResult.FAILED:
            wait = retry_wait(
                failures,
                first=max(retry_after, editor.interval()),
                longest=max_retry_after,
            )
            failures += 1
            await _wait_first(done, timeout=wait)
        elif result == ShowResult.NO_TARGET:
            await _wait_first(done)
        else:
            raise ValueError(f"Unknown show result: {result!r}")
