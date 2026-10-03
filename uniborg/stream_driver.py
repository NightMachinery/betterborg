"""Showing text while it is produced, by editing a message at a measured pace.

A *streaming loop* reads a growing text (a model's answer) and shows it in a
message as it grows. `PacedEditor` is the step each loop repeats: when an
edit is due, edit the message to the text so far, plus a cursor that says
more is coming. How often an edit is due is the *pace*, chosen per message by
a pace function: by default `draft_stream.streaming_pace`, which slows edits
down as the answer ages and keeps a draft's own pace.

The editor only edits when it is asked to show new text (the leading edge),
and it creates no task or timer, so a streaming loop stays a plain loop.

This is a core module, so a plugin reload never re-executes it. How the chat
bot uses it is in docs/draft_streaming.md.
"""

import asyncio
from enum import Enum
import logging
from typing import Any, Callable, Optional

from telethon import errors

from uniborg import draft_stream, util

_log = logging.getLogger(__name__)

#: A pace function: `(message, *, elapsed, edit_interval) -> StreamingPace`.
PaceFunction = Callable[..., draft_stream.StreamingPace]


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
