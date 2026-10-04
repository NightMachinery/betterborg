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

from telethon import Button, TelegramClient, errors, events
from telethon.tl import types

from uniborg import draft_stream, stream_driver, tg_compat, util
from uniborg.uniborg import Uniborg
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

    def test_not_modified_is_unchanged_and_silent(self):
        editor = self.editor(logger=logging.getLogger("test.stream_driver"))
        self.edit.side_effect = errors.MessageNotModifiedError(request=None)

        with self.assertNoLogs("test.stream_driver"):
            self.assertEqual(self.show(editor, "a", at=2), ShowResult.UNCHANGED)
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

    def test_tiered_pace_slows_after_its_threshold_and_keeps_a_drafts_pace(self):
        pace = stream_driver.tiered_pace(slow_after=30, slow_interval=5)

        self.assertEqual(
            pace("message", elapsed=30, edit_interval=2),
            draft_stream.StreamingPace(interval=2, cursor="▌"),
        )
        self.assertEqual(
            pace("message", elapsed=30.5, edit_interval=2),
            draft_stream.StreamingPace(interval=5, cursor="▌"),
        )
        draft = draft_stream.DraftAnswerMessage(
            object(), event=SimpleNamespace(chat_id=1, sender_id=1)
        )
        self.assertEqual(
            pace(draft, elapsed=300, edit_interval=2).interval,
            draft_stream.DRAFT_MIN_INTERVAL,
        )

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


class _GoneMessage:
    """A message the user deleted: every edit fails, as Telegram's would."""

    def __init__(self):
        self.chat_id = -1001
        self.id = 77
        self.text = "..."
        self.edits = []

    async def edit(self, text, **kwargs):
        self.edits.append(text)
        raise errors.MessageIdInvalidError(request=None)


class PacedEditorFailureTests(unittest.TestCase):
    """`PacedEditor` through the real `util.edit_message`, which prints a
    failed head edit and returns unless asked to raise it."""

    def setUp(self):
        chains = patch.dict(util.EDIT_CHAINS, {}, clear=True)
        chains.start()
        self.addCleanup(chains.stop)
        printed = patch("builtins.print")
        self.printed = printed.start()
        self.addCleanup(printed.stop)
        self.clock = _Clock()
        self.message = _GoneMessage()

    def show(self, editor, text, *, at):
        self.clock.now = at
        return asyncio.run(editor.show(text))

    def test_a_reported_failure_keeps_the_last_edit_time(self):
        editor = PacedEditor(
            self.message,
            edit_interval=1.0,
            clock=self.clock,
            report_failures=True,
            logger=logging.getLogger("test.stream_driver"),
        )

        with self.assertLogs("test.stream_driver", level="WARNING"):
            self.assertEqual(self.show(editor, "a", at=2), ShowResult.FAILED)
        self.assertEqual(editor.last_edit_at, 0)
        self.assertEqual(editor.due_in(), 0)
        self.printed.assert_not_called()

    def test_by_default_a_failure_counts_as_an_edit(self):
        #: The chat bot's loops: a deleted placeholder costs one failed edit
        #: per interval, not one per delta.
        editor = PacedEditor(self.message, edit_interval=1.0, clock=self.clock)

        self.assertEqual(self.show(editor, "a", at=2), ShowResult.SHOWN)
        self.assertEqual(editor.last_edit_at, 2)
        self.assertEqual(self.show(editor, "ab", at=2.5), ShowResult.NOT_DUE)
        self.assertEqual(self.message.edits, ["a▌"])

    def test_only_a_reporting_editor_passes_raise_on_head_failure(self):
        edit = AsyncMock()
        with patch.object(util, "edit_message", edit):
            for report_failures in (False, True):
                editor = PacedEditor(
                    "message",
                    edit_interval=1.0,
                    clock=self.clock,
                    report_failures=report_failures,
                )
                self.show(editor, "a", at=2)
                self.clock.now = 0

        self.assertEqual(
            [call.kwargs for call in edit.await_args_list],
            [{"parse_mode": "md"}, {"parse_mode": "md", "raise_on_head_failure": True}],
        )


#: The edit interval of `follow`'s tests, in seconds.
_INTERVAL = 0.05


class FollowTests(unittest.TestCase):
    """`follow` with a producer that sets `text` and `changed` by hand."""

    def setUp(self):
        self.edits = []
        self.real_edit_message = util.edit_message

        async def edit_message(message, text, **kwargs):
            self.edits.append(text)

        patcher = patch.object(util, "edit_message", edit_message)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.text = ""

    def run_follow(
        self,
        producer,
        *,
        retry_after=1.0,
        message="message",
        edit_interval=_INTERVAL,
        **kwargs,
    ):
        async def main():
            editor = PacedEditor(
                message,
                edit_interval=edit_interval,
                report_failures=True,
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
                    **kwargs,
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

    def attempt_times(self, *, fail, quiet, **kwargs):
        """When each edit was tried: one change, then QUIET seconds of
        silence; FAIL(n) says whether the n-th attempt fails."""
        times = []

        async def edit_message(message, text, **_kwargs):
            times.append(asyncio.get_running_loop().time())
            if fail(len(times)):
                raise errors.MessageIdInvalidError(request=None)

        async def producer(changed):
            self.text = "a"
            changed.set()
            await asyncio.sleep(quiet)

        with patch.object(util, "edit_message", edit_message), self.assertLogs(
            "test.stream_driver", level="WARNING"
        ):
            self.run_follow(producer, **kwargs)
        return [later - earlier for earlier, later in zip(times, times[1:])]

    def test_failures_in_a_row_wait_longer_each_time(self):
        gaps = self.attempt_times(
            fail=lambda n: True, quiet=1.0, retry_after=0.05, max_retry_after=0.4
        )

        #: 0.05, 0.1, 0.2, then 0.4 s apart, not 0.05 s for the whole second.
        self.assertLessEqual(len(gaps), 5)
        self.assertGreater(gaps[2], 0.15)

    def test_the_wait_doubles_up_to_the_longest(self):
        self.assertEqual(
            [
                stream_driver.retry_wait(failures, first=1, longest=60)
                for failures in (0, 1, 2, 5, 6, 10**6)
            ],
            [1, 2, 4, 32, 60, 60],
        )
        self.assertEqual(stream_driver.retry_wait(3, first=90, longest=60), 90)

    def test_the_retry_waits_at_least_the_pace_interval(self):
        gaps = self.attempt_times(
            fail=lambda n: True, quiet=0.7, retry_after=0.01, edit_interval=0.2
        )

        self.assertGreater(min(gaps), 0.18)
        self.assertLessEqual(len(gaps), 2)

    def test_a_shown_edit_starts_the_count_again(self):
        async def producer(changed):
            for text in ("a", "b"):
                self.text = text
                changed.set()
                await asyncio.sleep(0.8)

        times = []

        async def edit_message(message, text, **_kwargs):
            times.append(asyncio.get_running_loop().time())
            if len(times) in (1, 2, 4):
                raise errors.MessageIdInvalidError(request=None)

        with patch.object(util, "edit_message", edit_message), self.assertLogs(
            "test.stream_driver", level="WARNING"
        ):
            self.run_follow(producer, retry_after=0.1)

        #: Fails, waits 0.1, fails, waits 0.2, shows; then "b" fails and
        #: waits 0.1 again, not 0.4.
        self.assertEqual(len(times), 5)
        self.assertLess(times[4] - times[3], 0.3)

    def test_an_unchanged_message_is_not_edited_again(self):
        async def producer(changed):
            self.text = "same"
            changed.set()
            await asyncio.sleep(0.4)

        attempts = []

        async def not_modified(message, text, **kwargs):
            attempts.append(text)
            raise errors.MessageNotModifiedError(request=None)

        with patch.object(util, "edit_message", not_modified):
            self.run_follow(producer, retry_after=0.05)

        #: It already shows the text, so nothing is left to show.
        self.assertEqual(attempts, ["same▌"])

    def test_a_deleted_message_backs_off_through_the_real_edit_message(self):
        async def producer(changed):
            for index in range(10):
                self.text = str(index)
                changed.set()
                await asyncio.sleep(0.02)

        message = _GoneMessage()
        with patch.object(util, "edit_message", self.real_edit_message), patch.dict(
            util.EDIT_CHAINS, {}, clear=True
        ), patch("builtins.print"), self.assertLogs(
            "test.stream_driver", level="WARNING"
        ):
            self.run_follow(producer, retry_after=0.1, message=message)

        #: About 0.2 s of changes every 0.02 s, but retries 0.1 s apart.
        self.assertGreaterEqual(len(message.edits), 1)
        self.assertLessEqual(len(message.edits), 3)

    def test_an_editor_that_hides_failures_is_refused(self):
        async def main():
            await asyncio.wait_for(
                stream_driver.follow(
                    PacedEditor("message", edit_interval=_INTERVAL),
                    render=lambda: "x",
                    changed=asyncio.Event(),
                    done=asyncio.get_running_loop().create_future(),
                ),
                1,
            )

        with self.assertRaises(ValueError):
            asyncio.run(main())

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
                    PacedEditor(
                        "message", edit_interval=_INTERVAL, report_failures=True
                    ),
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


class StreamMenuTests(unittest.TestCase):
    def prefs(self, private, groups):
        return SimpleNamespace(stream_private=private, stream_groups=groups)

    def test_a_line_per_scope(self):
        lines = stream_driver.stream_mode_lines(
            self.prefs(StreamMode.EDITS, StreamMode.DRAFTS)
        )

        self.assertEqual(lines, ["Private chats: **Edits**", "Groups: **Drafts**"])

    def test_a_row_per_scope_with_the_current_mode_checked(self):
        rows = stream_driver.stream_mode_rows(
            self.prefs(StreamMode.DRAFTS, StreamMode.EDITS), callback_prefix="p:"
        )

        self.assertEqual(
            [
                [(tg_compat.button_text(b), tg_compat.button_data(b)) for b in row]
                for row in rows
            ],
            [
                [
                    ("✅ Private chats: Drafts", b"p:private:drafts"),
                    ("Private chats: Edits", b"p:private:edits"),
                ],
                [
                    ("Groups: Drafts", b"p:groups:drafts"),
                    ("✅ Groups: Edits", b"p:groups:edits"),
                ],
            ],
        )

    def test_a_buttons_data_reads_back_as_its_choice(self):
        rows = stream_driver.stream_mode_rows(
            self.prefs(StreamMode.DRAFTS, StreamMode.EDITS), callback_prefix="p:"
        )

        choices = [
            stream_driver.stream_choice(
                tg_compat.button_data(b).decode().removeprefix("p:")
            )
            for row in rows
            for b in row
        ]

        self.assertEqual(
            [(c.scope, c.mode) for c in choices],
            [
                ("private", StreamMode.DRAFTS),
                ("private", StreamMode.EDITS),
                ("groups", StreamMode.DRAFTS),
                ("groups", StreamMode.EDITS),
            ],
        )

    def test_other_data_raises(self):
        for data in ("channels:drafts", "private:typing", "private"):
            with self.subTest(data=data), self.assertRaises(ValueError):
                stream_driver.stream_choice(data)

    def test_arguments_name_a_choice_in_any_case(self):
        self.assertEqual(
            stream_driver.stream_choice_of_args(["Groups", "DRAFTS"]),
            stream_driver.StreamChoice(scope="groups", mode=StreamMode.DRAFTS),
        )
        for args in ([], ["groups"], ["groups", "x"], ["x", "drafts"], ["a"] * 3):
            with self.subTest(args=args):
                self.assertIsNone(stream_driver.stream_choice_of_args(args))


def _draft(**kwargs):
    return draft_stream.DraftAnswerMessage(
        object(), event=SimpleNamespace(chat_id=1, sender_id=1), **kwargs
    )


class OpenStreamTargetTests(unittest.TestCase):
    def setUp(self):
        self.start = AsyncMock(return_value=True)
        patcher = patch.object(draft_stream.DraftAnswerMessage, "start", self.start)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.sent = AsyncMock(return_value="placeholder")
        self.event = SimpleNamespace(chat_id=1, sender_id=1, reply=self.sent)

    def open(self, **kwargs):
        kwargs.setdefault("placeholder_text", "⏳ #3")
        return asyncio.run(
            stream_driver.open_stream_target(self.event, client="client", **kwargs)
        )

    def test_drafts_give_a_started_draft(self):
        target = self.open(drafts=True, top_msg_id=9)

        self.assertIsInstance(target, draft_stream.DraftAnswerMessage)
        self.assertEqual((target.client, target.top_msg_id), ("client", 9))
        self.start.assert_awaited_once_with("⏳ #3")
        self.sent.assert_not_awaited()

    def test_the_first_draft_takes_the_parse_mode(self):
        self.open(drafts=True, parse_mode=None)

        self.start.assert_awaited_once_with("⏳ #3", parse_mode=None)

    def test_the_first_draft_can_differ_from_the_placeholder(self):
        self.open(drafts=True, draft_text="")

        self.start.assert_awaited_once_with("")

    def test_without_drafts_the_placeholder_is_sent_as_a_reply(self):
        self.assertEqual(self.open(drafts=False), "placeholder")

        self.start.assert_not_awaited()
        self.sent.assert_awaited_once_with("⏳ #3")

    def test_a_refused_draft_sends_the_placeholder_through_the_injected_sender(self):
        self.start.return_value = False
        send = AsyncMock(return_value="sent")

        self.assertEqual(self.open(drafts=True, send_placeholder=send), "sent")
        send.assert_awaited_once_with(self.event, "⏳ #3")


class _EditClient:
    """Records `edit_message`, as `Message.edit` calls it."""

    def __init__(self):
        self.edits = []

    async def edit_message(self, *args, **kwargs):
        self.edits.append((args, kwargs))
        return "edited"


def _message_with_a_button(client):
    message = types.Message(
        id=5,
        peer_id=types.PeerUser(1),
        message="⏳ #3",
        out=True,
        reply_markup=TelegramClient.build_reply_markup(Button.inline("⏹", b"x")),
    )
    message._client = client
    message._input_chat = types.InputPeerUser(1, 0)
    return message


class ShowFinalTests(unittest.TestCase):
    def test_telethon_keeps_a_messages_buttons_unless_told(self):
        #: Why `show_final` passes `buttons=None`: Message.edit fills in the
        #: old reply markup when the caller leaves `buttons` out.
        client = _EditClient()
        message = _message_with_a_button(client)

        asyncio.run(message.edit("done"))

        ((_args, kwargs),) = client.edits
        self.assertIs(kwargs["buttons"], message.reply_markup)

    def test_a_message_is_edited_in_place_as_plain_text_without_buttons(self):
        client = _EditClient()
        message = _message_with_a_button(client)

        shown = asyncio.run(stream_driver.show_final(message, "done"))

        ((args, kwargs),) = client.edits
        self.assertEqual(args[1:], (5, "done"))
        self.assertEqual(
            kwargs, {"parse_mode": None, "link_preview": False, "buttons": None}
        )
        self.assertIsNone(TelegramClient.build_reply_markup(kwargs["buttons"]))
        self.assertEqual(shown, "edited")

    def test_a_draft_ends_and_its_text_is_sent_after_a_sync_draft(self):
        from test_draft_stream import _Client, _Event, _draft as _test_draft

        client, log = _Client(), []
        event = _Event(log)

        async def run():
            draft = _test_draft(client, event)
            await draft.start("⏳ #3", parse_mode=None)
            shown = await stream_driver.show_final(draft, "done *plain*")
            return draft, shown

        draft, shown = asyncio.run(run())

        self.assertFalse(draft.streaming)
        self.assertEqual(client.drafts, ["⏳ #3", "done *plain*"])
        self.assertEqual(log, [("send", "done *plain*")])
        (sent,) = event.sent
        self.assertIs(shown, sent)
        self.assertEqual(
            sent.kwargs, {"parse_mode": None, "link_preview": False, "buttons": None}
        )


class SyncDraftTests(unittest.TestCase):
    def test_a_sync_draft_is_sent_only_while_a_draft_could_show(self):
        from test_draft_stream import _Client, _Event, _draft as _test_draft

        client, log = _Client(), []
        event = _Event(log)

        async def run():
            fresh = _test_draft(client, event)
            await fresh.sync_draft("never started")
            draft = _test_draft(client, event)
            await draft.start("⏳", parse_mode=None)
            await draft.end_stream()
            await draft.sync_draft("final")
            draft.stop_pressed()
            await draft.sync_draft("after Stop")

        asyncio.run(run())

        self.assertEqual(client.drafts, ["⏳", "final"])
        self.assertEqual(log, [])


class StopWiringTests(unittest.TestCase):
    def test_stop_calls_any_callable_and_leaving_ends_the_stream(self):
        stops = []

        async def run():
            draft = _draft()
            draft.streaming = True
            async with stream_driver.stop_wired(draft, on_stop=lambda: stops.append(1)):
                draft.stop_pressed()
                self.assertTrue(draft.streaming)
            return draft

        draft = asyncio.run(run())

        self.assertEqual(stops, [1])
        self.assertFalse(draft.streaming)

    def test_an_error_inside_still_ends_the_stream(self):
        async def run():
            draft = _draft()
            draft.streaming = True
            with self.assertRaises(RuntimeError):
                async with stream_driver.stop_wired(draft, on_stop=lambda: None):
                    raise RuntimeError("the command failed")
            return draft

        self.assertFalse(asyncio.run(run()).streaming)

    def test_a_target_that_is_not_a_draft_is_left_alone(self):
        target = SimpleNamespace()

        async def run():
            async with stream_driver.stop_wired(target, on_stop=lambda: None):
                return "ran"

        self.assertEqual(asyncio.run(run()), "ran")
        self.assertEqual(vars(target), {})

    def test_run_stoppable_returns_the_result_and_ends_the_stream(self):
        async def work():
            return "answer"

        async def run():
            draft = _draft()
            draft.streaming = True
            return await stream_driver.run_stoppable(draft, work()), draft

        result, draft = asyncio.run(run())

        self.assertEqual(result, "answer")
        self.assertFalse(draft.streaming)


class FlushDraftTests(unittest.TestCase):
    def test_a_draft_is_flushed(self):
        draft = _draft()
        draft.flush = AsyncMock()

        asyncio.run(stream_driver.flush_draft(draft))

        draft.flush.assert_awaited_once_with()

    def test_a_failed_flush_is_logged_to_the_draft_logger(self):
        draft = _draft(logger=logging.getLogger("test.stream_driver.draft"))
        draft.flush = AsyncMock(side_effect=RuntimeError("chat gone"))

        with self.assertLogs("test.stream_driver.draft", level="WARNING"):
            asyncio.run(stream_driver.flush_draft(draft))

    def test_a_target_that_is_not_a_draft_is_left_alone(self):
        target = SimpleNamespace(flush=AsyncMock())

        asyncio.run(stream_driver.flush_draft(target))

        target.flush.assert_not_awaited()


class _Client:
    """Records handlers as `Uniborg` does, so a plugin's removal can be run."""

    def __init__(self):
        self._event_builders = []

    def on(self, event):
        def decorator(callback):
            self._event_builders.append((event, callback))
            return callback

        return decorator


class RegisterDraftStopTests(unittest.TestCase):
    MODULE = "_UniborgPlugins.test.plugin"

    def register(self, *, supported=True):
        client = _Client()
        with patch.object(draft_stream, "STOP_SUPPORTED", supported):
            registered = stream_driver.register_draft_stop(client, module=self.MODULE)
        return client, registered

    def test_the_handler_belongs_to_the_plugin_and_goes_with_it(self):
        client, registered = self.register()

        self.assertTrue(registered)
        ((event, callback),) = client._event_builders
        self.assertIsInstance(event, events.Raw)
        self.assertEqual(callback.__module__, self.MODULE)
        Uniborg.remove_events_of_mod(client, "_UniborgPlugins.test.other")
        self.assertEqual(len(client._event_builders), 1)
        Uniborg.remove_events_of_mod(client, self.MODULE)
        self.assertEqual(client._event_builders, [])

    def test_a_press_goes_to_the_draft_streams(self):
        client, _ = self.register()
        ((_, callback),) = client._event_builders
        update = object()

        with patch.object(draft_stream, "on_typing_update", AsyncMock()) as typing:
            asyncio.run(callback(update))

        typing.assert_awaited_once_with(update)

    def test_nothing_is_registered_without_a_stop_button(self):
        client, registered = self.register(supported=False)

        self.assertFalse(registered)
        self.assertEqual(client._event_builders, [])


if __name__ == "__main__":
    unittest.main()
