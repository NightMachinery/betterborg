"""Callback-query answers that a handler's own edits cannot swallow.

Telethon's ``CallbackQuery.Event.edit`` (and ``delete``, ``respond`` and
``reply``) schedules a bare ``self.answer()`` before its own request. That
task answers the press with no text, and ``answer()`` ignores every later
call, so a toast or alert sent after an edit never shows. See
docs/callback_answers.md.
"""

import asyncio
import functools
import logging
from typing import Callable, Optional

_log = logging.getLogger(__name__)

#: How long a held bare answer waits for the handler to send a toast. The
#: client spins until the press is answered, so a slow handler should not keep
#: it spinning past this.
FALLBACK_ANSWER_SECONDS = 5.0


class _HeldAnswers:
    """Replaces one event's ``answer`` until the handler finishes.

    A bare call (no arguments: Telethon's automatic answer, or the handler's
    own) is held back. Any call with arguments goes straight through. The held
    answer is sent when the handler finishes, or after `fallback_seconds`,
    unless an answer with arguments went out first.
    """

    def __init__(self, event, *, fallback_seconds: float):
        self._event = event
        self._real_answer = event.answer
        self._fallback_seconds = fallback_seconds
        self._answered = False
        self._finished = False
        self._fallback: Optional[asyncio.Task] = None
        event.answer = self.answer

    async def answer(self, *args, **kwargs):
        if args or kwargs:
            self._answered = True
            self._cancel_fallback()
            return await self._real_answer(*args, **kwargs)
        if not (self._answered or self._finished or self._fallback):
            self._fallback = asyncio.get_running_loop().create_task(
                self._answer_later()
            )
        return None

    async def finish(self) -> None:
        self._finished = True
        self._cancel_fallback()
        await self._answer_bare()
        self._event.answer = self._real_answer

    async def _answer_later(self) -> None:
        await asyncio.sleep(self._fallback_seconds)
        #: Detached first, so a handler finishing meanwhile cannot cancel the
        #: request below.
        self._fallback = None
        await self._answer_bare()

    async def _answer_bare(self) -> None:
        if self._answered:
            return
        self._answered = True
        try:
            await self._real_answer()
        except Exception:
            #: Usually a press too old to answer; nothing is left to tell.
            _log.warning("Could not answer a callback query", exc_info=True)

    def _cancel_fallback(self) -> None:
        if self._fallback is not None:
            self._fallback.cancel()
            self._fallback = None


def hold_bare_answers(
    handler: Optional[Callable] = None,
    *,
    fallback_seconds: float = FALLBACK_ANSWER_SECONDS,
):
    """Decorates a CallbackQuery handler so its toasts and alerts always show.

    Every press is answered exactly once: by the handler's first answer with
    arguments, or else with no text when the handler finishes (also when it
    raises) or after `fallback_seconds`. Use it bare (``@hold_bare_answers``)
    or with options (``@hold_bare_answers(fallback_seconds=2)``). The wrapper
    keeps the handler's ``__module__``, which plugin reloads match on.
    """
    if handler is None:
        return functools.partial(hold_bare_answers, fallback_seconds=fallback_seconds)

    @functools.wraps(handler)
    async def wrapper(event, *args, **kwargs):
        answers = _HeldAnswers(event, fallback_seconds=fallback_seconds)
        try:
            return await handler(event, *args, **kwargs)
        finally:
            await answers.finish()

    return wrapper
