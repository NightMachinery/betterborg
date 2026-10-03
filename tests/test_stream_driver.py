"""The shared streaming core (`uniborg/stream_driver.py`).

`PacedEditor` runs on a *scripted clock*: a callable whose time only moves
when the test says so, so each edit decision sees an exact timestamp.
`follow` runs on a real loop with intervals of a few hundredths of a second.
`util.edit_message` is replaced by a mock that records its calls.
"""

import asyncio
import logging
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from telethon import errors

from uniborg import draft_stream, stream_driver, util
from uniborg.stream_driver import PacedEditor, ShowResult, StreamMode


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


#: The edit interval of `follow`'s tests, in seconds.
_INTERVAL = 0.05


class FollowTests(unittest.TestCase):
    """`follow` with a producer that sets `text` and `changed` by hand."""

    def setUp(self):
        self.edits = []

        async def edit_message(message, text, **kwargs):
            self.edits.append(text)

        patcher = patch.object(util, "edit_message", edit_message)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.text = ""

    def run_follow(self, producer, *, retry_after=1.0):
        async def main():
            editor = PacedEditor(
                "message",
                edit_interval=_INTERVAL,
                logger=logging.getLogger("test.stream_driver"),
            )
            changed = asyncio.Event()
            done = asyncio.get_running_loop().create_future()
            pump = asyncio.ensure_future(
                stream_driver.follow(
                    editor,
                    render=lambda: self.text,
                    changed=changed,
                    done=done,
                    retry_after=retry_after,
                )
            )
            await producer(changed)
            done.set_result(None)
            await asyncio.wait_for(pump, 1)

        asyncio.run(main())

    async def until(self, predicate, *, timeout=1.0):
        deadline = asyncio.get_running_loop().time() + timeout
        while not predicate():
            self.assertLess(asyncio.get_running_loop().time(), deadline)
            await asyncio.sleep(0.005)

    def test_the_last_text_shows_after_a_quiet_spell(self):
        async def producer(changed):
            #: Before the first edit is due, so it must wait for the pace.
            self.text = "a"
            changed.set()
            await self.until(lambda: self.edits)
            self.text = "ab"
            changed.set()
            await self.until(lambda: len(self.edits) == 2)

        self.run_follow(producer)

        self.assertEqual(self.edits, ["a▌", "ab▌"])

    def test_a_burst_makes_one_edit_of_the_last_text(self):
        async def producer(changed):
            for text in ("a", "ab", "abc"):
                self.text = text
                changed.set()
            await self.until(lambda: self.edits)
            await asyncio.sleep(3 * _INTERVAL)

        self.run_follow(producer)

        self.assertEqual(self.edits, ["abc▌"])

    def test_a_spread_burst_edits_at_most_once_per_interval(self):
        async def producer(changed):
            await asyncio.sleep(2 * _INTERVAL)
            for index in range(10):
                self.text = str(index)
                changed.set()
                await asyncio.sleep(_INTERVAL / 10)
            await self.until(lambda: self.edits[-1:] == ["9▌"])

        self.run_follow(producer)

        #: One leading edit, then the trailing one, and maybe one between.
        self.assertLessEqual(len(self.edits), 3)
        self.assertEqual(self.edits[-1], "9▌")

    def test_a_failed_edit_backs_off(self):
        async def producer(changed):
            for index in range(10):
                self.text = str(index)
                changed.set()
                await asyncio.sleep(0.02)

        attempts = []

        async def failing(message, text, **kwargs):
            attempts.append(text)
            raise RuntimeError("the message is gone")

        with patch.object(util, "edit_message", failing), self.assertLogs(
            "test.stream_driver", level="WARNING"
        ):
            self.run_follow(producer, retry_after=0.1)

        #: About 0.2 s of changes every 0.02 s, but retries 0.1 s apart.
        self.assertGreaterEqual(len(attempts), 1)
        self.assertLessEqual(len(attempts), 3)

    def test_it_returns_when_done_without_showing_the_rest(self):
        async def producer(changed):
            self.text = "late"
            changed.set()
            await asyncio.sleep(0)

        self.run_follow(producer)

        self.assertEqual(self.edits, [])

    def test_it_returns_at_once_when_already_done(self):
        async def main():
            done = asyncio.get_running_loop().create_future()
            done.set_result(None)
            await asyncio.wait_for(
                stream_driver.follow(
                    PacedEditor("message", edit_interval=_INTERVAL),
                    render=lambda: "x",
                    changed=asyncio.Event(),
                    done=done,
                ),
                1,
            )

        asyncio.run(main())

        self.assertEqual(self.edits, [])


class StreamSettingTests(unittest.TestCase):
    def test_the_mode_values_are_what_saved_preferences_hold(self):
        self.assertEqual([mode.value for mode in StreamMode], ["drafts", "edits"])

    def test_the_scope_follows_the_chat(self):
        self.assertEqual(
            stream_driver.stream_scope(SimpleNamespace(is_private=True)),
            stream_driver.STREAM_SCOPE_PRIVATE,
        )
        self.assertEqual(
            stream_driver.stream_scope(SimpleNamespace(is_private=False)),
            stream_driver.STREAM_SCOPE_GROUPS,
        )

    def test_each_scope_reads_and_writes_its_own_field(self):
        prefs = SimpleNamespace(stream_private="drafts", stream_groups="edits")

        stream_driver.set_stream_mode(
            prefs, scope=stream_driver.STREAM_SCOPE_GROUPS, mode=StreamMode.DRAFTS
        )

        self.assertEqual(prefs.stream_groups, StreamMode.DRAFTS)
        for scope in stream_driver.STREAM_SCOPE_NAMES:
            self.assertEqual(
                stream_driver.stream_mode(prefs, scope=scope), StreamMode.DRAFTS
            )

    def test_an_unknown_scope_raises(self):
        prefs = SimpleNamespace(stream_private="drafts", stream_groups="edits")

        with self.assertRaises(ValueError):
            stream_driver.stream_mode(prefs, scope="channels")
        with self.assertRaises(ValueError):
            stream_driver.set_stream_mode(
                prefs, scope="channels", mode=StreamMode.EDITS
            )

    def test_the_chat_bot_uses_these_names_and_loads_saved_settings(self):
        from test_llm_chat_stream import plugin

        self.assertIs(plugin.StreamMode, StreamMode)
        self.assertIs(plugin.STREAM_SCOPE_NAMES, stream_driver.STREAM_SCOPE_NAMES)
        prefs = plugin.UserPrefs.model_validate(
            {"stream_private": "edits", "stream_groups": "drafts"}
        )
        self.assertEqual(prefs.stream_private, StreamMode.EDITS)
        self.assertEqual(prefs.stream_groups, StreamMode.DRAFTS)


if __name__ == "__main__":
    unittest.main()
