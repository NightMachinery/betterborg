"""The shared streaming core (`uniborg/stream_driver.py`).

`PacedEditor` runs on a *scripted clock*: a callable whose time only moves
when the test says so, so each edit decision sees an exact timestamp.
`util.edit_message` is replaced by a mock that records its calls.
"""

import asyncio
import logging
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from telethon import errors

from uniborg import draft_stream, stream_driver, util
from uniborg.stream_driver import PacedEditor, ShowResult


class _Clock:
    def __init__(self, now: float = 0.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


class PacedEditorTests(unittest.TestCase):
    def setUp(self):
        self.clock = _Clock()
        self.edit = AsyncMock()
        patcher = patch.object(util, "edit_message", self.edit)
        patcher.start()
        self.addCleanup(patcher.stop)

    def editor(self, message="message", **kwargs):
        kwargs.setdefault("edit_interval", 1.0)
        return PacedEditor(message, clock=self.clock, **kwargs)

    def show(self, editor, text, *, at):
        self.clock.now = at
        return asyncio.run(editor.show(text))

    def test_an_edit_is_due_only_strictly_after_the_interval(self):
        editor = self.editor()

        self.assertEqual(self.show(editor, "a", at=1.0), ShowResult.NOT_DUE)
        self.assertEqual(self.show(editor, "ab", at=1.01), ShowResult.SHOWN)
        self.assertEqual(self.show(editor, "abc", at=2.01), ShowResult.NOT_DUE)
        self.assertEqual(self.show(editor, "abcd", at=2.02), ShowResult.SHOWN)
        self.assertEqual(
            [call.args[1] for call in self.edit.await_args_list], ["ab▌", "abcd▌"]
        )

    def test_the_text_is_positional_and_parse_mode_the_only_keyword(self):
        editor = self.editor()

        self.show(editor, "hello", at=2)

        self.edit.assert_awaited_once_with("message", "hello▌", parse_mode="md")

    def test_the_parse_mode_is_injected(self):
        editor = self.editor(parse_mode=None)

        self.show(editor, "hello", at=2)

        self.edit.assert_awaited_once_with("message", "hello▌", parse_mode=None)

    def test_a_failed_edit_keeps_the_last_edit_time(self):
        editor = self.editor(logger=logging.getLogger("test.stream_driver"))
        self.edit.side_effect = [RuntimeError("flood"), None]

        with self.assertLogs("test.stream_driver", level="WARNING"):
            self.assertEqual(self.show(editor, "a", at=2), ShowResult.FAILED)
        #: Due again at once, since the failed edit did not count.
        self.assertEqual(self.show(editor, "ab", at=2.5), ShowResult.SHOWN)
        self.assertEqual(editor.last_edit_at, 2.5)

    def test_not_modified_fails_silently(self):
        editor = self.editor(logger=logging.getLogger("test.stream_driver"))
        self.edit.side_effect = errors.MessageNotModifiedError(request=None)

        with self.assertNoLogs("test.stream_driver"):
            self.assertEqual(self.show(editor, "a", at=2), ShowResult.FAILED)
        self.assertEqual(editor.last_edit_at, 0)

    def test_no_message_is_a_no_op(self):
        editor = self.editor(message=None)

        self.assertEqual(self.show(editor, "a", at=5), ShowResult.NO_TARGET)
        self.edit.assert_not_awaited()

    def test_the_default_pace_slows_with_age(self):
        editor = self.editor(edit_interval=0.8)

        self.assertEqual(self.show(editor, "a", at=31), ShowResult.SHOWN)
        #: Past 30 s the interval is 15 s and the cursor sleepier.
        self.assertEqual(self.show(editor, "ab", at=45), ShowResult.NOT_DUE)
        self.assertEqual(self.show(editor, "abc", at=46.5), ShowResult.SHOWN)
        self.assertEqual(
            [call.args[1] for call in self.edit.await_args_list],
            ["a▌💤", "abc▌💤"],
        )

    def test_fixed_pace_keeps_the_interval_and_cursor_past_30_seconds(self):
        #: The native Gemini image loop's pace: no tiers, at any age.
        editor = self.editor(edit_interval=0.8, pace=stream_driver.fixed_pace())

        for at in (31, 31.9, 125, 125.5):
            self.show(editor, "x", at=at)

        self.assertEqual(self.edit.await_count, 3)
        self.assertEqual({call.args[1] for call in self.edit.await_args_list}, {"x▌"})

    def test_a_draft_keeps_the_draft_pace(self):
        draft = draft_stream.DraftAnswerMessage(
            object(), event=SimpleNamespace(chat_id=1, sender_id=1)
        )
        editor = self.editor(message=draft, edit_interval=2.0)

        self.assertEqual(self.show(editor, "a", at=1.5), ShowResult.SHOWN)

    def test_render_builds_the_text(self):
        editor = self.editor(render=lambda text, pace: f"[{text}]")

        self.show(editor, "a", at=2)

        self.assertEqual(self.edit.await_args.args[1], "[a]")

    def test_due_in_counts_down_to_the_next_edit(self):
        editor = self.editor()

        self.clock.now = 0.25
        self.assertEqual(editor.due_in(), 0.75)
        self.clock.now = 3
        self.assertEqual(editor.due_in(), 0)

    def test_the_default_clock_is_the_running_loop_time(self):
        async def run():
            loop = asyncio.get_running_loop()
            loop.time = lambda: 100.0
            try:
                editor = PacedEditor("message", edit_interval=1.0)
                first = await editor.show("a")
                loop.time = lambda: 101.5
                return first, await editor.show("ab")
            finally:
                del loop.time

        self.assertEqual(asyncio.run(run()), (ShowResult.NOT_DUE, ShowResult.SHOWN))


if __name__ == "__main__":
    unittest.main()
