"""Live output of `.a`, `.af` and `.aa` in chats (`stdplugins/advanced_get.py`).

A fake chat records every message the plugin sends, edits and deletes, and
the drafts it shows. Commands run through a fake producer that follows a
script (text to write, seconds to wait, `STOP` to wait for a stop), so the
timings hold however slowly zsh starts; a few tests run inert commands
(`printf`, `sleep`) in a real zsh. The timings are injected (`LIVE_TIMING`),
in tenths of a second.
"""

import asyncio
from functools import partial
import itertools
import os
from pathlib import Path
import re
import tempfile
import threading
import warnings
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from telethon import events
from telethon.tl import types

from uniborg import (
    draft_stream,
    guest_util,
    shell_settings,
    shell_stream,
    stream_driver,
    tg_compat,
    util,
)
from uniborg.shell_settings import FinalMode, ShellPrefs, ShellSettings
from uniborg.storage import UserStorage
from uniborg.stream_driver import StreamMode

from test_advanced_get_guest import (
    ADMIN,
    BOT_USERNAME,
    _FakeBorg,
    _GuestTestCase,
    _load_plugin,
    _query,
)

CHAT = -1001
DM = ADMIN
COMMAND_ID = 1


class _Message:
    def __init__(self, chat, text, *, chat_id, **kwargs):
        self._chat = chat
        self.id = next(chat.ids)
        self.chat_id = chat_id
        self.text = text
        self.kwargs = kwargs

    async def edit(self, text, **kwargs):
        self._chat.log.append(("edit", self.id, text, kwargs))
        self.text = text
        return self

    async def reply(self, text, **kwargs):
        """A chain message: `util.edit_message` splitting a long text."""
        message = _Message(self._chat, text, chat_id=self.chat_id, **kwargs)
        self._chat.log.append(("chain", self.id, text, kwargs))
        return message

    async def delete(self):
        if self._chat.fail_delete:
            raise RuntimeError("MESSAGE_DELETE_FORBIDDEN")
        self._chat.log.append(("delete", self.id))


class _Event:
    """A `.a` message; also the chat that records what the plugin does."""

    def __init__(self, plugin, text, *, private):
        self.log = []
        self.ids = itertools.count(100)
        self.fail_delete = False
        self.chat_id = DM if private else CHAT
        self.sender_id = ADMIN
        self.is_private = private
        self.message = SimpleNamespace(
            id=COMMAND_ID,
            out=False,
            forward=None,
            reply_to=None,
            reply_to_msg_id=None,
            grouped_id=None,
        )
        self.pattern_match = plugin.pattern_a.match(text)

    def _sent(self, kind, text, kwargs):
        message = _Message(self, text, chat_id=self.chat_id, **kwargs)
        self.log.append((kind, text, kwargs))
        return message

    async def respond(self, text, **kwargs):
        return self._sent("respond", text, kwargs)

    async def reply(self, text, **kwargs):
        return self._sent("reply", text, kwargs)

    async def get_chat(self):
        return "chat"

    async def get_input_chat(self):
        return types.InputPeerUser(self.chat_id, 0)

    def sent(self, kind):
        return [entry for entry in self.log if entry[0] == kind]


class _Borg(_FakeBorg):
    """A bot account; as the drafts' client, it records each draft."""

    def __init__(self, *, bot=True):
        super().__init__()
        self.me.bot = bot
        self.drafts = []
        self.parse_modes = []
        #: Telegram refuses drafts in this chat.
        self.refuse_drafts = False

    def action(self, chat, kind):
        borg = self

        class _Action:
            async def __aenter__(self):
                return borg

            async def __aexit__(self, *exc):
                return False

        return _Action()

    async def _parse_message_text(self, text, parse_mode):
        self.parse_modes.append(parse_mode)
        return text, []

    async def __call__(self, request):
        if self.refuse_drafts:
            raise RuntimeError("TEXTDRAFT_PEER_INVALID")
        self.drafts.append(request.action.text.text)


FAST = SimpleNamespace(
    preview_delay=0.3,
    private=SimpleNamespace(interval=0.1, slow_interval=0.1),
    groups=SimpleNamespace(interval=0.1, slow_interval=0.1),
    slow_after=30,
)


class _ShellTestCase(unittest.TestCase):
    bot = True

    def setUp(self):
        self.borg = _Borg(bot=self.bot)
        self.plugin = _load_plugin(self.borg)
        self.handler = self.borg.handlers[0][1]
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.settings = ShellSettings(
            storage=UserStorage(purpose=shell_settings.PURPOSE, root=tmp.name)
        )
        self.files = []

        async def upload_output_files(chat, files, *, album_mode, reply_to, on_error):
            self.files.append(sorted(Path(f).name for f in files))
            return []

        self.text_files = []

        async def send_text_as_file(*, text, chat, reply_to, **kwargs):
            self.text_files.append((text, reply_to))
            return SimpleNamespace(id=999)

        for target, name, value in (
            (util, "isAdmin", AsyncMock(return_value=True)),
            (util, "borg", self.borg),
            (util, "dl_base", tmp.name + "/dls/"),
            (util, "upload_output_files", upload_output_files),
            (util, "send_text_as_file", send_text_as_file),
            (shell_settings, "SETTINGS", self.settings),
            (shell_settings, "SHELL_STREAMING", True),
            (self.plugin, "LIVE_TIMING", FAST),
        ):
            patcher = patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.dict(shell_stream.JOBS, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def set_prefs(self, **changes):
        self.settings.set(ADMIN, ShellPrefs(**changes))

    def run_command(self, text, *, private=False, during=None):
        """Runs TEXT through the `.a` handler; DURING(event) runs alongside."""
        event = _Event(self.plugin, text, private=private)

        async def main():
            task = asyncio.ensure_future(self.handler(event))
            if during is not None:
                await during(event)
            await asyncio.wait_for(task, 30)

        asyncio.run(main())
        return event

    def run_script(self, steps, *, command=".aa x", retcode=0, files=(), **kwargs):
        """Runs COMMAND with both producers following STEPS (`_producer`)."""
        capture = _producer(steps, retcode=retcode, files=files)
        with patch.object(util, "simple_run_capture", capture), patch.object(
            util, "brishz_capture", capture
        ):
            return self.run_command(command, **kwargs)


#: A step that waits until the job is stopped; the command then ends with 130.
STOP = object()


def _producer(steps, *, retcode=0, files=()):
    """A fake `simple_run_capture` and `brishz_capture` that follows STEPS.

    A step is text the command writes, seconds it waits, or STOP. FILES are
    made in its directory at the end.
    """

    async def capture(*, cwd, job=None, **kwargs):
        if job is not None and not job.try_start():
            return None
        killed = asyncio.Event()
        if job is not None:
            job.attach(killed.set)
        written, code = [], retcode
        for step in steps:
            if isinstance(step, str):
                written.append(step)
                if job is not None:
                    job.output.write(step.encode())
            elif step is STOP:
                await killed.wait()
                code = 130
            else:
                await asyncio.sleep(step)
        for name in files:
            Path(cwd, name).touch()
        if job is None:
            return util.CommandResult(output="".join(written), retcode=code)
        job.detach()
        return util.CommandResult(
            output=job.output.final_text(render=False), retcode=code
        )

    return capture


async def _until(condition, *, timeout=10):
    deadline = asyncio.get_running_loop().time() + timeout
    while not condition():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("timed out")
        await asyncio.sleep(0.02)


def _buttons(rows):
    """ROWS of buttons as (text, data) pairs."""
    return [
        [(tg_compat.button_text(b), tg_compat.button_data(b)) for b in row]
        for row in rows
    ]


def _stop_button(preview_text):
    """The Stop button of the job whose preview shows PREVIEW_TEXT."""
    job_id = re.match(r"⏳ #(\d+)", preview_text).group(1)
    return ("⏹ Stop", f"shk:{job_id}".encode())


#: Output, a pause past the preview delay, more output.
SLOW = ["a\n", 0.8, "b"]
PLAIN_TEXT = {"parse_mode": None, "link_preview": False}
FINAL_EDIT = {"parse_mode": None, "link_preview": False, "buttons": None}


class FastCommandTests(_ShellTestCase):
    def test_a_fast_command_gives_exactly_todays_messages(self):
        live = self.run_script(["hi\n"]).log
        with patch.object(shell_settings, "SHELL_STREAMING", False):
            before = self.run_script(["hi\n"]).log

        self.assertEqual(live, before)
        ((_kind, text, kwargs),) = live
        self.assertEqual(text, "hi")
        self.assertEqual(kwargs["reply_to"].id, COMMAND_ID)

    def test_a_real_fast_command_too(self):
        with patch.object(
            self.plugin,
            "LIVE_TIMING",
            SimpleNamespace(**{**vars(FAST), "preview_delay": 60}),
        ):
            live = self.run_command(".aa printf 'hi\\r\\n'").log
            with patch.object(shell_settings, "SHELL_STREAMING", False):
                before = self.run_command(".aa printf 'hi\\r\\n'").log

        self.assertEqual(live, before)
        self.assertEqual([entry[:2] for entry in live], [("respond", "hi")])

    def test_in_both_final_modes(self):
        self.set_prefs(final_mode=FinalMode.NEW_REPLY)
        reply = self.run_script(["hi"]).log
        self.set_prefs(final_mode=FinalMode.EDIT_PREVIEW)
        edit = self.run_script(["hi"]).log

        self.assertEqual([entry[:2] for entry in reply], [("respond", "hi")])
        self.assertEqual([entry[:2] for entry in edit], [("respond", "hi")])

    def test_empty_output_says_how_it_exited_and_files_follow(self):
        event = self.run_script([], retcode=3, files=["out.txt"])

        self.assertEqual(
            [entry[:2] for entry in event.log],
            [("respond", "The process exited 3.")],
        )
        self.assertEqual(self.files, [["out.txt"]])
        self.assertEqual(shell_stream.JOBS, {})


class EditedPreviewTests(_ShellTestCase):
    def test_a_slow_command_shows_a_silent_preview_that_becomes_the_final(self):
        event = self.run_script(SLOW)

        first, *rest = event.log
        self.assertEqual(first[0], "respond")
        self.assertRegex(first[1], r"^⏳ #\d+\n\na\n▌$")
        kwargs = dict(first[2])
        self.assertEqual(_buttons(kwargs.pop("buttons")), [[_stop_button(first[1])]])
        self.assertEqual(
            kwargs, {**PLAIN_TEXT, "reply_to": event.message, "silent": True}
        )
        preview_id = 100
        *_partials, final = rest
        self.assertEqual(final, ("edit", preview_id, "a\nb", FINAL_EDIT))
        self.assertEqual(event.sent("respond"), [first])
        self.assertNotIn((event.chat_id, preview_id), util.EDIT_CHAINS)

    def test_the_preview_follows_the_output_at_its_pace(self):
        event = self.run_script(["a\n", 0.5, "b\n", 0.5, "c"])

        partials = [entry[2] for entry in event.log[1:-1]]
        self.assertIn(partials[-1].split("\n\n", 1)[1], ("a\nb\n▌",))
        for entry in event.log[1:-1]:
            self.assertEqual(entry[3], {"parse_mode": None, "link_preview": False})
        self.assertEqual(event.log[-1][2], "a\nb\nc")

    def test_new_reply_sends_the_final_then_deletes_the_preview(self):
        self.set_prefs(final_mode=FinalMode.NEW_REPLY)

        event = self.run_script(SLOW)

        self.assertEqual(
            [entry[:2] for entry in event.log[-2:]],
            [("respond", "a\nb"), ("delete", 100)],
        )

    def test_a_preview_that_cannot_be_deleted_says_it_finished(self):
        self.set_prefs(final_mode=FinalMode.NEW_REPLY)

        async def fail_deletes(event):
            event.fail_delete = True

        event = self.run_script(SLOW, during=fail_deletes)

        self.assertEqual(event.log[-2][:2], ("respond", "a\nb"))
        self.assertEqual(
            event.log[-1], ("edit", 100, self.plugin.FINISHED_TEXT, FINAL_EDIT)
        )

    def test_long_output_ends_the_preview_with_its_tail_and_sends_the_file(self):
        lines = "".join(f"{i}\n" for i in range(1, 2001))
        event = self.run_script(["a\n", 0.5, lines])

        kind, preview_id, text, kwargs = event.log[-1]
        self.assertEqual((kind, preview_id, kwargs), ("edit", 100, FINAL_EDIT))
        first_line, tail = text.split("\n", 1)
        self.assertEqual(first_line, self.plugin.LONG_OUTPUT_LINE)
        self.assertTrue(tail.endswith("1999\n2000"))
        self.assertLessEqual(len(text.encode("utf-16-le")) // 2, 4096)
        ((whole, reply_to),) = self.text_files
        self.assertTrue(whole.startswith("a\n1\n2\n"))
        self.assertEqual(reply_to.id, 100)

    def test_long_output_as_a_new_reply_is_todays_file(self):
        self.set_prefs(final_mode=FinalMode.NEW_REPLY)
        lines = "".join(f"{i}\n" for i in range(1, 2001))

        event = self.run_script(["a\n", 0.5, lines])

        ((whole, reply_to),) = self.text_files
        self.assertTrue(whole.endswith("2000"))
        self.assertIs(reply_to, event.message)
        self.assertEqual(event.log[-1], ("delete", 100))

    def test_a_long_running_preview_stays_one_message(self):
        lines = "".join(f"line {i}\n" for i in range(1, 2001))

        event = self.run_script(["a\n", 0.5, lines, 0.6, "end"])

        self.assertEqual(
            [entry[0] for entry in event.log if entry[0] != "edit"], ["respond"]
        )
        self.assertEqual({entry[1] for entry in event.sent("edit")}, {100})
        partial = event.log[-2][2]
        self.assertTrue(partial.endswith("line 2000\n▌"))

    def test_files_follow_the_final(self):
        event = self.run_script(["a", 0.5], files=["out.txt"])

        self.assertEqual(event.log[-1][:3], ("edit", 100, "a"))
        self.assertEqual(self.files, [["out.txt"]])

    def test_a_stop_ends_the_command_and_says_so(self):
        async def stop(event):
            await _until(lambda: event.log)
            (job,) = shell_stream.JOBS.values()
            job.cancel(reason=shell_stream.StopReason.USER)

        event = self.run_script(["started", STOP], during=stop)

        self.assertEqual(event.log[-1][2], "started\n\n⏹ Stopped (exit 130).")

    def test_a_shutdown_stop_says_the_bot_is_going_offline(self):
        async def stop(event):
            await _until(lambda: event.log)
            (job,) = shell_stream.JOBS.values()
            job.cancel(reason=shell_stream.StopReason.SHUTDOWN)

        event = self.run_script([STOP], during=stop)

        self.assertEqual(
            event.log[-1][2],
            "The process exited 130.\n\n⏹ Stopped: the bot is going offline (exit 130).",
        )

    def test_the_header_says_stopping_once_stopped(self):
        async def stop(event):
            await _until(lambda: event.log)
            (job,) = shell_stream.JOBS.values()
            job.cancel(reason=shell_stream.StopReason.USER)
            await _until(lambda: len(event.log) > 1)

        event = self.run_script(["x", STOP, 0.5], during=stop)

        self.assertRegex(event.log[1][2], r"^⏹ #\d+ stopping…\n\nx▌$")

    def test_a_producer_error_removes_the_preview_and_reports_it(self):
        async def capture(*, job, **kwargs):
            job.try_start()
            await asyncio.sleep(0.6)
            raise RuntimeError("boom")

        with patch.object(util, "brishz_capture", capture):
            event = self.run_command(".a true")

        self.assertEqual(event.log[1], ("delete", 100))
        self.assertIn("RuntimeError: boom", event.log[2][1])
        self.assertEqual(shell_stream.JOBS, {})

    def test_a_userbot_shows_the_stop_hint_and_edits(self):
        self.borg.me.bot = False

        event = self.run_script(SLOW, private=True)

        self.assertRegex(event.log[0][1], r"^⏳ #\d+ · \.k to stop")
        self.assertEqual(event.log[-1][:3], ("edit", 100, "a\nb"))
        self.assertEqual(self.borg.drafts, [])

    def test_a_real_slow_command(self):
        event = self.run_command(".aa printf 'a\\n'; sleep 1; printf b")

        self.assertEqual(event.log[-1][:3], ("edit", 100, "a\nb"))


class QueuedTests(_ShellTestCase):
    def test_a_job_stopped_while_queued_ends_at_once_and_never_runs(self):
        ran = []
        gate = {}

        async def capture(*, job, **kwargs):
            gate["free"] = free = asyncio.Event()
            await free.wait()
            if not job.try_start():
                return None
            ran.append(job)
            return util.CommandResult(output="ran", retcode=0)

        async def stop(event):
            await _until(lambda: event.log)
            self.assertIn("waiting for a free shell", event.log[0][1])
            (job,) = shell_stream.JOBS.values()
            job.cancel(reason=shell_stream.StopReason.USER)
            await _until(lambda: len(event.log) > 1)
            gate["free"].set()
            await asyncio.sleep(0.05)

        with patch.object(util, "brishz_capture", capture):
            event = self.run_command(".a true", during=stop)

        self.assertEqual(
            event.log[-1],
            ("edit", 100, self.plugin.STOPPED_BEFORE_IT_RAN, FINAL_EDIT),
        )
        self.assertEqual(ran, [])


class DraftPreviewTests(_ShellTestCase):
    def test_a_private_chat_streams_a_plain_draft_that_becomes_a_reply(self):
        event = self.run_script(["*a* `b`\n", 0.8, "c"], private=True)

        ((kind, text, kwargs),) = event.log
        self.assertEqual((kind, text, kwargs), ("reply", "*a* `b`\nc", FINAL_EDIT))
        hint = "" if draft_stream.STOP_SUPPORTED else r" · \.k to stop"
        self.assertRegex(self.borg.drafts[0], rf"^⏳ #\d+{hint}\n\n\*a\* `b`\n▌$")
        #: The last draft is the sync draft, which the reply adopts.
        self.assertEqual(self.borg.drafts[-1], "*a* `b`\nc")
        self.assertEqual(set(self.borg.parse_modes), {None})

    def test_a_draft_without_a_stop_button_names_dot_k(self):
        #: Telethon before 1.45 cannot give a draft its Stop button.
        with patch.object(draft_stream, "STOP_SUPPORTED", False):
            self.run_script(["a\n", 1.2, "b\n", 1.5, "c"], private=True)

        *partials, _sync = self.borg.drafts
        self.assertGreater(len(partials), 1)
        for draft in partials:
            self.assertRegex(draft, r"^⏳ #\d+ · \.k to stop\n")

    def test_new_reply_ends_the_draft_with_a_sync_draft_then_replies(self):
        self.set_prefs(final_mode=FinalMode.NEW_REPLY)

        event = self.run_script(SLOW, private=True)

        self.assertEqual([entry[:2] for entry in event.log], [("respond", "a\nb")])
        self.assertEqual(self.borg.drafts[-1], "a\nb")

    def test_long_output_ends_the_draft_with_its_tail_and_the_file(self):
        lines = "".join(f"{i}\n" for i in range(1, 2001))

        event = self.run_script(["a\n", 0.5, lines], private=True)

        ((kind, text, _kwargs),) = event.log
        self.assertEqual(kind, "reply")
        self.assertTrue(text.startswith(self.plugin.LONG_OUTPUT_LINE))
        ((_whole, reply_to),) = self.text_files
        self.assertEqual(reply_to.text, text)

    def test_a_long_running_draft_shows_the_header(self):
        lines = "".join(f"line {i}\n" for i in range(1, 2001))

        event = self.run_script(["a\n", 0.5, lines, 2.5, "end"], private=True)

        self.assertEqual([entry[0] for entry in event.log], ["reply"])
        *partials, _sync = self.borg.drafts
        self.assertTrue(partials)
        for draft in partials:
            self.assertRegex(draft, r"^⏳ #\d+")

    def test_edits_when_the_user_chose_edits(self):
        self.set_prefs(stream_private=StreamMode.EDITS)

        event = self.run_script(SLOW, private=True)

        self.assertEqual(event.log[0][0], "respond")
        self.assertEqual(self.borg.drafts, [])

    def test_a_group_set_to_drafts_falls_back_to_edits_when_refused(self):
        self.set_prefs(stream_groups=StreamMode.DRAFTS)
        self.borg.refuse_drafts = True

        event = self.run_script(SLOW)

        self.assertRegex(event.log[0][1], r"^⏳ #\d+\n")
        self.assertEqual(
            _buttons(event.log[0][2]["buttons"]), [[_stop_button(event.log[0][1])]]
        )
        self.assertEqual(event.log[-1][:3], ("edit", 100, "a\nb"))

    @unittest.skipUnless(draft_stream.STOP_SUPPORTED, "no Stop button here")
    def test_the_drafts_stop_button_stops_the_command(self):
        async def press_stop(event):
            await _until(lambda: draft_stream._ACTIVE)
            (draft,) = draft_stream._ACTIVE.values()
            draft.stop_pressed()

        event = self.run_script(["started", STOP], private=True, during=press_stop)

        ((_kind, text, _kwargs),) = event.log
        self.assertEqual(text, "started\n\n⏹ Stopped (exit 130).")
        #: After Stop, no more drafts: not even the sync draft.
        self.assertNotIn(text, self.borg.drafts)

    @unittest.skipUnless(draft_stream.STOP_SUPPORTED, "no Stop button here")
    def test_the_draft_stop_handler_belongs_to_the_plugin(self):
        later = [fn for _builder, fn in self.borg.handlers[1:]]

        self.assertIn(self.plugin.__name__, [fn.__module__ for fn in later])


class SourceTests(unittest.TestCase):
    def test_the_plugin_compiles_without_warnings(self):
        """An invalid escape such as "\\." warns now, and is an error in a
        future Python, where the loader would skip the plugin."""
        path = Path(__file__).resolve().parent.parent / "stdplugins/advanced_get.py"
        source = path.read_text()
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            compile(source, str(path), "exec")


class OldPathTests(_ShellTestCase):
    def capture(self, seen):
        async def capture(**kwargs):
            seen.append(kwargs)
            return util.CommandResult(output="hi", retcode=0)

        return capture

    def test_old_brish_runs_dot_a_as_before(self):
        seen = []
        with patch.object(util, "BRISH_POPEN", False), patch.object(
            util, "brishz_capture", self.capture(seen)
        ):
            event = self.run_command(".af printf hi")

        (call,) = seen
        self.assertNotIn("job", call)
        self.assertFalse(call["fork"])
        self.assertEqual([entry[:2] for entry in event.log], [("respond", "hi")])
        self.assertEqual(shell_stream.JOBS, {})

    def test_old_brish_still_streams_dot_aa(self):
        with patch.object(util, "BRISH_POPEN", False):
            event = self.run_script(SLOW)

        self.assertEqual(event.log[-1][:3], ("edit", 100, "a\nb"))

    def test_streaming_off_runs_as_before(self):
        seen = []
        with patch.object(shell_settings, "SHELL_STREAMING", False), patch.object(
            util, "brishz_capture", self.capture(seen)
        ), patch.object(util, "simple_run_capture", self.capture(seen)):
            self.run_command(".a printf hi")
            self.run_command(".aa printf hi")

        self.assertEqual([("job" in call) for call in seen], [False, False])
        self.assertEqual(shell_stream.JOBS, {})

    def test_a_streamed_dot_a_runs_on_the_shell_pool_with_a_job(self):
        seen = []
        with patch.object(util, "brishz_capture", self.capture(seen)):
            self.run_command(".af printf hi")

        (call,) = seen
        self.assertIs(call["brish"], util.persistent_brish)
        self.assertFalse(call["fork"])
        self.assertIsInstance(call["job"], shell_stream.ShellJob)


class RendererTests(_ShellTestCase):
    def test_the_final_shows_what_a_terminal_would(self):
        event = self.run_script(["abcdef\rXY\n\x1b[31mred\x1b[0m\n"])

        self.assertEqual(event.log[-1][1], "XYcdef\nred")

    def test_off_the_final_is_the_raw_output(self):
        self.set_prefs(render=False)

        event = self.run_script(["abcdef\rXY\n"])

        self.assertEqual(event.log[-1][1], "abcdef\rXY")

    def test_the_preview_and_the_file_are_rendered_too(self):
        bar = "".join(f"\r{i}%" for i in range(0, 101, 10))
        lines = "".join(f"{i}\n" for i in range(1, 2001))

        event = self.run_script([bar + "\n", 0.6, lines])

        preview = event.log[0][1]
        self.assertIn("\n\n100%\n▌", preview)
        self.assertNotIn("\r", preview)
        ((whole, _reply_to),) = self.text_files
        self.assertTrue(whole.startswith("100%\n1\n"))

    def test_off_the_preview_is_raw(self):
        self.set_prefs(render=False)

        event = self.run_script(["10%\r20%\n", 0.6, "done"])

        self.assertIn("10%\r20%", event.log[0][1])
        self.assertEqual(event.log[-1][2], "10%\r20%\ndone")

    def test_a_large_final_renders_off_the_event_loop(self):
        threads = []
        render = shell_stream.term_render.render

        def recording(text):
            threads.append(threading.current_thread() is threading.main_thread())
            return render(text)

        with patch.object(shell_stream.term_render, "render", recording):
            self.run_script(["a\rb"])
            on_loop, threads[:] = list(threads), []
            with patch.object(self.plugin, "RENDER_ON_LOOP_BYTES", 2):
                event = self.run_script(["a\rb"])

        #: One render per stream.
        self.assertEqual(on_loop, [True, True])
        self.assertEqual(threads, [False, False])
        self.assertEqual(event.log[-1][1], "b")

    def test_old_brish_renders_dot_a_too(self):
        async def capture(**kwargs):
            return util.CommandResult(output="abcdef\rXY", retcode=0)

        with patch.object(util, "BRISH_POPEN", False), patch.object(
            util, "brishz_capture", capture
        ):
            rendered = self.run_command(".a x").log
            self.set_prefs(render=False)
            raw = self.run_command(".a x").log

        self.assertEqual(
            [entry[1] for entry in rendered + raw], ["XYcdef", "abcdef\rXY"]
        )


class _KillEvent(_Event):
    """A `.k` message, as a reply to REPLY_TO when given."""

    def __init__(self, plugin, text, *, private=False, reply_to=None):
        super().__init__(plugin, ".a x", private=private)
        self.message.reply_to_msg_id = reply_to
        self.pattern_match = plugin.pattern_k.match(text)
        assert self.pattern_match, text


class KillTests(_ShellTestCase):
    def setUp(self):
        super().setUp()
        (self.kill_handler,) = [
            fn for _b, fn in self.borg.handlers if fn.__name__ == "kill_handler"
        ]

    def kill(self, text, **kwargs):
        """Sends `.k` TEXT; returns the replies."""
        event = _KillEvent(self.plugin, text, **kwargs)
        asyncio.run(self.kill_handler(event))
        return [entry[1] for entry in event.sent("reply")]

    def jobs(self, *commands, chat_id=CHAT, start=True):
        """Registers a running job per command, each with a recording kill hook."""
        killed = []

        async def make():
            made = []
            for command in commands:
                job = shell_stream.register(
                    shell_stream.ShellJob(
                        owner_id=ADMIN, chat_id=chat_id, command=command
                    )
                )
                if start:
                    job.try_start()
                    job.attach(partial(killed.append, job.id))
                made.append(job)
            return made

        return asyncio.run(make()), killed

    def test_dot_k_is_not_dot_a(self):
        self.assertIsNone(self.plugin.pattern_a.match(".k"))
        self.assertIsNone(self.plugin.pattern_k.match(".a ls"))
        self.assertIsNone(self.plugin.pattern_k.match(".kx"))

    def test_it_comes_right_after_dot_a(self):
        self.assertIs(self.borg.handlers[1][1], self.kill_handler)

    def test_bare_stops_the_only_running_command(self):
        replies = []

        async def kill(event):
            await _until(lambda: event.log)
            k = _KillEvent(self.plugin, ".k")
            await self.kill_handler(k)
            replies.extend(entry[1] for entry in k.sent("reply"))

        event = self.run_script(["started", STOP], during=kill)

        self.assertRegex(replies[0], r"^⏹ Stopping #\d+…$")
        self.assertEqual(event.log[-1][2], "started\n\n⏹ Stopped (exit 130).")

    def test_a_reply_to_the_command_or_its_preview_stops_that_job(self):
        replies = []

        async def kill(event):
            await _until(lambda: event.log)
            for reply_to in (COMMAND_ID, 100):
                k = _KillEvent(self.plugin, ".k", reply_to=reply_to)
                await self.kill_handler(k)
                replies.extend(entry[1] for entry in k.sent("reply"))

        self.run_script(["started", STOP], during=kill)

        self.assertRegex(replies[0], r"^⏹ Stopping #(\d+)…$")
        job_id = replies[0].split("#")[1].rstrip("…")
        self.assertEqual(replies[1], f"#{job_id} is already stopping.")

    def test_a_reply_to_another_message_names_no_job(self):
        self.jobs("sleep 100")

        self.assertEqual(self.kill(".k", reply_to=55), [self.plugin.NOT_A_JOB])

    def test_nothing_running(self):
        self.assertEqual(self.kill(".k"), [self.plugin.NO_RUNNING_JOB])
        self.assertEqual(self.kill(".k ls"), [self.plugin.NO_RUNNING_JOB])
        self.assertEqual(self.kill(".k all"), [self.plugin.NO_RUNNING_JOB])
        self.assertEqual(self.kill(".k 7"), ["No running command #7 here."])

    def test_several_are_listed_not_stopped(self):
        (first, second), killed = self.jobs("sleep 100", "tail -f x")

        (reply,) = self.kill(".k")

        self.assertEqual(killed, [])
        self.assertIn(f"#{first.id} · 0s · sleep 100", reply)
        self.assertIn(f"#{second.id} · 0s · tail -f x", reply)
        self.assertEqual(self.kill(".k ls"), [reply])

    def test_by_number_and_all(self):
        (first, second, third), killed = self.jobs("a", "b", "c")

        self.assertEqual(self.kill(f".k {second.id}"), [f"⏹ Stopping #{second.id}…"])
        self.assertEqual(
            self.kill(f".K #{second.id}"), [f"#{second.id} is already stopping."]
        )
        (reply,) = self.kill(".k all")

        self.assertEqual(killed, [second.id, first.id, third.id])
        self.assertEqual(
            reply.splitlines(),
            [
                f"⏹ Stopping #{first.id}…",
                f"#{second.id} is already stopping.",
                f"⏹ Stopping #{third.id}…",
            ],
        )

    def test_other_chats_jobs_are_not_visible(self):
        (job,), killed = self.jobs("sleep 100", chat_id=-1002)

        self.assertEqual(self.kill(".k"), [self.plugin.NO_RUNNING_JOB])
        self.assertEqual(
            self.kill(f".k {job.id}"), [f"No running command #{job.id} here."]
        )
        self.assertEqual(killed, [])

    def test_the_private_chat_also_sees_the_callers_guest_jobs(self):
        killed = []

        async def make():
            jobs = [
                shell_stream.register(
                    shell_stream.ShellJob(
                        owner_id=owner, chat_id=None, command="sleep 100", thread_key=t
                    )
                )
                for owner, t in ((ADMIN, "chat:-100"), (42, "chat:-100"))
            ]
            for job in jobs:
                job.try_start()
                job.attach(partial(killed.append, job.id))
            return jobs

        mine, _theirs = asyncio.run(make())

        self.assertEqual(self.kill(".k"), [self.plugin.NO_RUNNING_JOB])
        self.assertEqual(self.kill(".k", private=True), [f"⏹ Stopping #{mine.id}…"])
        self.assertEqual(killed, [mine.id])

    def test_a_queued_job_is_stopped_before_it_runs(self):
        (job,), _killed = self.jobs("sleep 100", start=False)

        self.assertIn(" · waiting · sleep 100", self.kill(".k ls")[0])
        self.assertEqual(self.kill(".k"), [f"⏹ Stopping #{job.id}…"])
        self.assertTrue(job.dropped)

    def test_an_ended_job_is_not_running(self):
        (job,), _killed = self.jobs("true")
        job.detach()

        self.assertEqual(self.kill(".k"), [self.plugin.NO_RUNNING_JOB])
        self.assertEqual(self.kill(".k", reply_to=COMMAND_ID), [self.plugin.NOT_A_JOB])

    def test_the_list_shows_age_and_the_commands_start(self):
        async def main():
            job = shell_stream.ShellJob(
                owner_id=ADMIN, chat_id=CHAT, command="echo " + "x " * 60, started_at=0
            )
            job.try_start()
            return job, self.plugin.job_list_text([job], now=3725)

        job, text = asyncio.run(main())

        line = text.splitlines()[1]
        self.assertEqual(line, f"#{job.id} · 1h 02m · echo {'x ' * 27}x…")

    def test_bad_arguments_get_the_usage(self):
        self.assertEqual(self.kill(".k now"), [self.plugin.KILL_USAGE])

    def test_several_arguments_get_the_usage_too(self):
        _jobs, killed = self.jobs("sleep 100")

        for text in (".k 3 5", ".k all now", ".k 3,5", ".k 3\n5"):
            with self.subTest(text=text):
                self.assertEqual(self.kill(text), [self.plugin.KILL_USAGE])
        self.assertEqual(killed, [])

    def test_old_brish_and_streaming_off_say_why_nothing_is_seen(self):
        with patch.object(util, "BRISH_POPEN", False):
            (old,) = self.kill(".k")
        with patch.object(shell_settings, "SHELL_STREAMING", False):
            (off,) = self.kill(".k")

        self.assertEqual(
            old, f"{self.plugin.NO_RUNNING_JOB}\n{self.plugin.OLD_BRISH_NOTE}"
        )
        self.assertEqual(
            off, f"{self.plugin.NO_RUNNING_JOB}\n{self.plugin.STREAMING_OFF_NOTE}"
        )

    def test_a_non_admin_gets_nothing(self):
        _jobs, killed = self.jobs("sleep 100")

        with patch.object(util, "isAdmin", AsyncMock(return_value=False)):
            self.assertEqual(self.kill(".k"), [])
        self.assertEqual(killed, [])

    def test_a_forwarded_k_does_nothing(self):
        _jobs, killed = self.jobs("sleep 100")
        event = _KillEvent(self.plugin, ".k")
        event.message.forward = object()

        asyncio.run(self.kill_handler(event))

        self.assertEqual((event.log, killed), ([], []))

    def test_help_names_it(self):
        self.assertIn("`.k`", self.plugin.HELP_TEXT)


class _Press:
    """A press of the Stop button of message MESSAGE_ID in CHAT_ID; records
    its answers. 100 is the fake chat's first message: a job's preview."""

    def __init__(self, data, *, chat_id=CHAT, message_id=100):
        self.data = data.encode()
        self.chat_id = chat_id
        self.message_id = message_id
        self.sender_id = ADMIN
        self.answers = []

    async def answer(self, *args, **kwargs):
        self.answers.append(args)


class StopButtonTests(_ShellTestCase):
    def setUp(self):
        super().setUp()
        (self.press_handler,) = [
            fn
            for _b, fn in self.borg.handlers
            if getattr(fn, "__name__", None) == "stop_press_handler"
        ]

    async def press(self, data, **kwargs):
        press = _Press(data, **kwargs)
        await self.press_handler(press)
        return [args[0] for args in press.answers]

    def test_a_press_stops_the_job_and_the_header_says_so(self):
        toasts = []

        async def press(event):
            await _until(lambda: event.log)
            (job,) = shell_stream.JOBS.values()
            toasts.extend(await self.press(f"shk:{job.id}"))
            await _until(lambda: len(event.log) > 1)
            toasts.extend(await self.press(f"shk:{job.id}"))

        event = self.run_script(["started", STOP, 0.5], during=press)

        job_id = re.match(r"⏳ #(\d+)", event.log[0][1]).group(1)
        self.assertEqual(
            toasts, [f"⏹ Stopping #{job_id}…", f"#{job_id} is already stopping."]
        )
        self.assertEqual(event.log[1][2], f"⏹ #{job_id} stopping…\n\nstarted▌")
        #: The final's edit removes the button.
        self.assertEqual(
            event.log[-1],
            ("edit", 100, "started\n\n⏹ Stopped (exit 130).", FINAL_EDIT),
        )

    def test_a_non_admins_press_gets_a_toast_and_stops_nothing(self):
        async def main():
            job = shell_stream.register(
                shell_stream.ShellJob(owner_id=ADMIN, chat_id=CHAT, command="x")
            )
            job.try_start()
            with patch.object(util, "isAdmin", AsyncMock(return_value=False)):
                toasts = await self.press(f"shk:{job.id}")
            return job, toasts

        job, toasts = asyncio.run(main())

        self.assertEqual(toasts, [self.plugin.ADMINS_ONLY])
        self.assertFalse(job.stopped)

    def test_a_press_for_a_job_that_ended_says_so(self):
        async def main():
            other = shell_stream.register(
                shell_stream.ShellJob(owner_id=ADMIN, chat_id=-1002, command="x")
            )
            other.try_start()
            return other, [
                *await self.press("shk:999999"),
                *await self.press(f"shk:{other.id}"),
                *await self.press("shk:"),
                *await self.press("shk:²"),
            ]

        other, toasts = asyncio.run(main())

        self.assertEqual(
            toasts,
            [
                "#999999 has already ended.",
                f"#{other.id} has already ended.",
                self.plugin.OUTDATED_BUTTON,
                self.plugin.OUTDATED_BUTTON,
            ],
        )
        self.assertFalse(other.stopped)

    def test_a_stale_button_does_not_stop_a_newer_job_with_its_number(self):
        """After a restart, job ids start again at 1: an old preview's
        button carries the id of a newer job in the same chat."""

        async def main():
            job = shell_stream.register(
                shell_stream.ShellJob(
                    owner_id=ADMIN, chat_id=CHAT, command="x", preview_id=500
                )
            )
            job.try_start()
            stale = await self.press(f"shk:{job.id}", message_id=7)
            own = await self.press(f"shk:{job.id}", message_id=500)
            return job, stale, own

        job, stale, own = asyncio.run(main())

        self.assertEqual(stale, [f"#{job.id} has already ended."])
        self.assertEqual(own, [f"⏹ Stopping #{job.id}…"])
        self.assertTrue(job.stopped)

    def test_only_its_own_presses_reach_it(self):
        ((builder, _fn),) = [
            (b, fn)
            for b, fn in self.borg.handlers
            if getattr(fn, "__name__", None) == "stop_press_handler"
        ]

        def takes(data):
            event = SimpleNamespace(query=SimpleNamespace(data=data, chat_instance=0))
            return bool(builder.filter(event))

        self.assertTrue(takes(b"shk:3"))
        self.assertFalse(takes(b"shs:render:off"))
        self.assertFalse(takes(b"zsh_1"))

    def test_partial_edits_keep_the_button(self):
        from telethon import TelegramClient
        from test_stream_driver import _EditClient, _message_with_a_button

        async def main():
            job = shell_stream.ShellJob(owner_id=ADMIN, chat_id=CHAT, command="x")
            client = _EditClient()
            client.parse_mode = None
            message = _message_with_a_button(client)
            message.reply_markup = TelegramClient.build_reply_markup(
                self.plugin._stop_buttons(job)
            )
            await util.edit_message(
                message, "⏳ #3\n\nmore▌", parse_mode=None, raise_on_head_failure=True
            )
            return message, client

        with patch.dict(util.EDIT_CHAINS, {}, clear=True):
            message, client = asyncio.run(main())

        ((_args, kwargs),) = client.edits
        self.assertIs(kwargs["buttons"], message.reply_markup)

    def test_a_userbots_preview_has_no_button(self):
        self.borg.me.bot = False

        event = self.run_script(SLOW)

        self.assertIsNone(event.log[0][2]["buttons"])
        self.assertRegex(event.log[0][1], r"^⏳ #\d+ · \.k to stop")


class OldPoolTests(_ShellTestCase):
    """`.x`, `.sbb` and `.xf` say how many jobs are queued or running on old pools."""

    def restart(self, text, *, pools):
        """Runs TEXT's handler; `init_brishes` swaps in the next of POOLS."""
        (handler,) = [
            fn
            for builder, fn in self.borg.handlers
            if isinstance(builder, events.NewMessage)
            and builder.pattern
            and builder.pattern(text)
            and fn.__name__.endswith("brishes_handler")
        ]
        event = SimpleNamespace(reply=AsyncMock(), chat_id=CHAT, sender_id=ADMIN)
        pools = iter(pools)

        def init_brishes():
            util.persistent_brish = next(pools)

        with patch.object(util, "persistent_brish", next(pools)), patch.object(
            util, "init_brishes", init_brishes
        ):
            asyncio.run(handler(event))
        return event.reply.await_args.args[0]

    def jobs(self, *pools, chat_id=CHAT):
        async def make():
            made = []
            for pool in pools:
                job = shell_stream.register(
                    shell_stream.ShellJob(owner_id=ADMIN, chat_id=chat_id, command="x")
                )
                job.pool = pool
                job.try_start()
                made.append(job)
            return made

        return asyncio.run(make())

    def test_no_jobs_no_note(self):
        self.assertEqual(self.restart(".x", pools=["old", "new"]), "Restarted brishes.")

    def test_jobs_on_the_old_pool_are_counted(self):
        old, new = object(), object()
        first, _second, _aa, _current = self.jobs(old, old, None, new)

        self.assertEqual(
            self.restart(".x", pools=[old, new]),
            "Restarted brishes.\n2 commands are queued or running on old pools; .k stops them.",
        )
        first.detach()
        self.assertEqual(
            self.restart(".sbb", pools=[old, new]),
            "Restarted brishes.\n1 command is queued or running on an old pool; .k stops it.",
        )
        self.assertTrue(
            self.restart(".xf", pools=[old, new]).endswith(
                "can still rejoin.\n1 command is queued or running on an old pool; .k stops it."
            )
        )

    def test_a_pool_retired_by_an_earlier_restart_still_counts(self):
        p1, p2, p3 = object(), object(), object()
        self.jobs(p1)

        self.assertEqual(
            self.restart(".x", pools=[p1, p2]),
            "Restarted brishes.\n1 command is queued or running on an old pool; .k stops it.",
        )
        self.assertEqual(
            self.restart(".x", pools=[p2, p3]),
            "Restarted brishes.\n1 command is queued or running on an old pool; .k stops it.",
        )

    def test_jobs_in_other_chats_are_counted_apart(self):
        """`.k` here cannot see them, so the note says where they run."""
        old, new = object(), object()
        self.jobs(old, chat_id=-1002)

        self.assertEqual(
            self.restart(".x", pools=[old, new]),
            "Restarted brishes.\n1 command in another chat is queued or running on an old"
            " pool; .k in that chat stops it.",
        )
        self.jobs(old, old)
        self.jobs(old, chat_id=-1003)
        self.assertEqual(
            self.restart(".x", pools=[old, new]),
            "Restarted brishes.\n2 commands are queued or running on old pools; .k stops them."
            "\n2 commands in other chats are queued or running on old pools; .k in their chats"
            " stops them.",
        )

    def test_guest_jobs_elsewhere_are_counted_apart(self):
        """They are stopped from their guest chat, or the caller's private chat."""
        old, new = object(), object()

        async def make():
            job = shell_stream.register(
                shell_stream.ShellJob(
                    owner_id=ADMIN, chat_id=None, command="x", thread_key="chat:-5"
                )
            )
            job.pool = old
            job.try_start()

        asyncio.run(make())

        self.assertEqual(
            self.restart(".x", pools=[old, new]),
            "Restarted brishes.\n1 guest command is queued or running on an old pool;"
            f" @{BOT_USERNAME} .k in its chat stops it.",
        )

    def test_jobs_only_another_admin_can_reach_are_counted_apart(self):
        """The caller cannot send `.k` in another admin's private chat with the
        bot, and `@bot .k` sees only the caller's own guest jobs."""
        other = ADMIN + 1
        old, new = object(), object()

        async def make():
            for chat_id, thread_key in (
                (None, "chat:-5"),
                (other, None),
                (-1002, None),
            ):
                job = shell_stream.register(
                    shell_stream.ShellJob(
                        owner_id=other,
                        chat_id=chat_id,
                        command="x",
                        thread_key=thread_key,
                    )
                )
                job.pool = old
                job.try_start()

        asyncio.run(make())

        self.assertEqual(
            self.restart(".x", pools=[old, new]),
            "Restarted brishes.\n1 command in another chat is queued or running on an old"
            " pool; .k in that chat stops it.\n2 commands of other admins are queued or running"
            " on old pools; they can stop them.",
        )

    def test_a_streamed_dot_a_records_its_pool(self):
        pools = []

        async def capture(*, job, brish, **kwargs):
            pools.append((job.pool, brish))
            return util.CommandResult(output="hi", retcode=0)

        with patch.object(util, "brishz_capture", capture):
            self.run_command(".a printf hi")
            self.run_script(["hi"], command=".aa printf hi")

        ((pool, brish),) = pools
        self.assertIs(pool, util.persistent_brish)
        self.assertIs(brish, pool)

    def retired_while_queued(self, *, ran):
        """`.af` whose pool `.x` retires while it waits for a worker; RAN
        says whether brish let the command start before refusing."""
        old, new = object(), object()
        calls = []

        async def capture(*, job, brish, **kwargs):
            calls.append(brish)
            if brish is old:
                util.persistent_brish = new
                if ran:
                    job.try_start()
                raise util.UninitializedBrishException("retired")
            job.try_start()
            job.output.write(b"hi")
            job.detach()
            return util.CommandResult(output="hi", retcode=0)

        with patch.object(util, "persistent_brish", old), patch.object(
            util, "brishz_capture", capture
        ):
            event = self.run_command(".af printf hi")
        return event, calls, old, new

    def test_a_job_queued_on_a_retired_pool_runs_on_the_new_one(self):
        event, calls, old, new = self.retired_while_queued(ran=False)

        self.assertEqual(calls, [old, new])
        self.assertEqual([entry[:2] for entry in event.log], [("respond", "hi")])

    def test_a_job_that_ran_is_not_run_again(self):
        event, calls, old, _new = self.retired_while_queued(ran=True)

        self.assertEqual(calls, [old])
        ((_kind, text, _kwargs),) = event.log
        self.assertIn("UninitializedBrishException", text)


class ShutdownTests(_ShellTestCase):
    """A shutdown stops the running commands, with an inert `.aa sleep` in a
    real zsh."""

    def test_stopping_all_before_the_disconnect_delivers_the_restart_note(self):
        took = {}

        async def shut_down(event):
            await _until(lambda: event.log)
            client = SimpleNamespace(disconnect=AsyncMock())
            started = asyncio.get_running_loop().time()
            await shell_stream.stop_all_and_disconnect(client, timeout=15)
            took["seconds"] = asyncio.get_running_loop().time() - started
            took["disconnected"] = client.disconnect.await_count

        event = self.run_command(".aa sleep 100", during=shut_down)

        self.assertLess(took["seconds"], 2)
        self.assertEqual(took["disconnected"], 1)
        self.assertRegex(
            event.log[-1][2],
            r"^The process exited -?\d+\.\n\n⏹ Stopped: the bot is going offline"
            r" \(exit -?\d+\)\.$",
        )
        self.assertEqual(shell_stream.JOBS, {})

    def test_a_command_sent_during_the_shutdown_never_runs(self):
        late = _Event(self.plugin, ".aa printf ran", private=False)
        order = []

        async def shut_down(event):
            await _until(lambda: event.log)
            client = SimpleNamespace(
                disconnect=AsyncMock(side_effect=lambda: order.append("disconnect"))
            )
            stopping = asyncio.ensure_future(
                shell_stream.stop_all_and_disconnect(client, timeout=15)
            )
            await asyncio.sleep(0)
            await asyncio.wait_for(self.handler(late), 10)
            order.append("late final")
            await stopping

        self.run_command(".aa sleep 100", during=shut_down)

        self.assertEqual(
            late.log[-1][1], "⏹ Stopped before it ran: the bot is going offline."
        )
        self.assertEqual(order, ["late final", "disconnect"])

    def test_the_loops_teardown_kills_a_command_still_running(self):
        """What `asyncio.run` does to a standalone bot on Ctrl-C: it cancels the
        handler's task, and the command dies with it."""
        seen = {}

        async def main():
            event = _Event(
                self.plugin, ".aa printf '%s\\n' $$; sleep 100", private=False
            )
            asyncio.ensure_future(self.handler(event))
            await _until(lambda: shell_stream.JOBS)
            (job,) = shell_stream.JOBS.values()
            await _until(lambda: job.output.final_text(render=False).strip())
            seen["job"] = job
            seen["pid"] = int(job.output.final_text(render=False).split()[0])

        asyncio.run(main())

        self.assertIs(seen["job"].stop_reason, shell_stream.StopReason.SHUTDOWN)

        async def gone():
            def dead():
                try:
                    os.kill(seen["pid"], 0)
                except ProcessLookupError:
                    return True
                return False

            await _until(dead)

        asyncio.run(gone())


class _QuickAnswer(guest_util.GuestAnswerMessage):
    """A guest answer whose edits may follow each other at once."""

    def __init__(self, editor, **kwargs):
        super().__init__(editor, min_interval=0, **kwargs)


class GuestLiveTests(_GuestTestCase):
    """`@bot .a CMD`: the guest answer shows the output live."""

    def setUp(self):
        super().setUp()
        for target, name, value in (
            (shell_settings, "SHELL_STREAMING", True),
            (self.plugin, "LIVE_TIMING", FAST),
            (guest_util, "GuestAnswerMessage", _QuickAnswer),
        ):
            patcher = patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.dict(shell_stream.JOBS, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_guest(self, steps, *, command=".aa x", retcode=0, during=None, peer=None):
        """Runs COMMAND as a guest query from PEER (`_query`), the producers
        following STEPS (`_producer`); DURING() runs alongside. Returns the
        answer's texts."""
        capture = _producer(steps, retcode=retcode)

        async def main():
            task = asyncio.ensure_future(
                self.plugin.guest_shell(_query(f"@{BOT_USERNAME} {command}", peer=peer))
            )
            if during is not None:
                await during()
            await asyncio.wait_for(task, 30)

        with patch.object(util, "simple_run_capture", capture), patch.object(
            util, "brishz_capture", capture
        ):
            asyncio.run(main())
        return [edit["text"] for edit in self.edits]

    async def guest_kill(self, text):
        """`@bot TEXT` from the same guest chat; returns its answers."""
        before = len(self.answers)
        await self.plugin.guest_shell(_query(f"@{BOT_USERNAME} {text}"))
        return self.answers[before:]

    def test_a_slow_command_shows_its_output_live_then_the_final(self):
        texts = self.run_guest(SLOW)

        self.assertEqual(self.answers, ["⏳ Running…"])
        first, *_partials, final = texts
        self.assertRegex(first, rf"^⏳ #\d+ · @{BOT_USERNAME} \.k to stop\n\na\n▌$")
        self.assertEqual(final, "a\nb")
        self.assertTrue(all(edit.get("parse_mode") is None for edit in self.edits))
        self.assertEqual(shell_stream.JOBS, {})

    def test_a_group_answer_edits_at_the_group_pace(self):
        """And a private chat's at the private pace, as a chat preview does."""
        timing = SimpleNamespace(
            preview_delay=0.3,
            private=SimpleNamespace(interval=0.1, slow_interval=0.1),
            groups=SimpleNamespace(interval=0.2, slow_interval=0.2),
            slow_after=30,
        )
        intervals = []

        class _Editor(stream_driver.PacedEditor):
            def __init__(self, message, **kwargs):
                intervals.append(kwargs["edit_interval"])
                super().__init__(message, **kwargs)

        with patch.object(self.plugin, "LIVE_TIMING", timing), patch.object(
            stream_driver, "PacedEditor", _Editor
        ):
            self.run_guest(SLOW, peer=types.PeerChannel(1234))
            self.run_guest(SLOW, peer=types.PeerChat(1234))
            self.run_guest(SLOW)

        self.assertEqual(intervals, [0.2, 0.2, 0.1])

    def test_a_fast_command_still_makes_one_edit(self):
        texts = self.run_guest(["hi\n"])

        self.assertEqual(texts, ["hi"])

    def test_a_long_running_preview_fits_the_answer(self):
        texts = self.run_guest(["line\n" * 2000, 0.5])

        self.assertLessEqual(
            max(len(text.encode("utf-16-le")) // 2 for text in texts[:-1]),
            self.plugin.PREVIEW_UNITS + 1,
        )
        self.assertIn("Output truncated", texts[-1])

    def test_at_kill_stops_it_and_the_answer_says_so_under_its_exit(self):
        kills = []

        async def kill():
            await _until(lambda: self.edits)
            kills.extend(await self.guest_kill(".k"))

        texts = self.run_guest(["started", STOP], during=kill)

        self.assertRegex(kills[0], r"^⏹ Stopping #\d+…$")
        self.assertEqual(texts[-1], "started\n\nexit 130\n⏹ Stopped")

    def test_a_shutdown_says_the_bot_is_going_offline(self):
        async def stop():
            await _until(lambda: self.edits)
            (job,) = shell_stream.JOBS.values()
            job.cancel(reason=shell_stream.StopReason.SHUTDOWN)

        texts = self.run_guest([STOP], during=stop)

        self.assertEqual(
            texts[-1],
            "The process exited 130.\n\nexit 130\n⏹ Stopped: the bot is going offline",
        )

    def test_a_command_stopped_while_queued_never_runs(self):
        for reason, final in (
            (shell_stream.StopReason.USER, self.plugin.STOPPED_BEFORE_IT_RAN),
            (
                shell_stream.StopReason.SHUTDOWN,
                "⏹ Stopped before it ran: the bot is going offline.",
            ),
        ):
            with self.subTest(reason=reason):
                self.edits.clear()
                self.stop_while_queued(reason=reason, final=final)

    def stop_while_queued(self, *, reason, final):
        ran = []
        gate = {}

        async def capture(*, job, **kwargs):
            gate["free"] = free = asyncio.Event()
            await free.wait()
            if not job.try_start():
                return None
            ran.append(job)
            return util.CommandResult(output="ran", retcode=0)

        async def main():
            task = asyncio.ensure_future(
                self.plugin.guest_shell(_query(f"@{BOT_USERNAME} .a true"))
            )
            await _until(lambda: self.edits)
            self.assertIn("waiting for a free shell", self.edits[0]["text"])
            (job,) = shell_stream.JOBS.values()
            job.cancel(reason=reason)
            await asyncio.wait_for(task, 30)
            gate["free"].set()
            await asyncio.sleep(0.05)

        with patch.object(util, "brishz_capture", capture):
            asyncio.run(main())

        self.assertEqual(self.edits[-1]["text"], final)
        self.assertEqual(ran, [])

    def test_a_producer_error_with_a_preview_becomes_the_traceback(self):
        async def capture(*, job, **kwargs):
            job.try_start()
            await asyncio.sleep(0.6)
            raise RuntimeError("boom")

        with patch.object(util, "simple_run_capture", capture):
            asyncio.run(self.plugin.guest_shell(_query(f"@{BOT_USERNAME} .aa x")))

        first, final = [edit["text"] for edit in self.edits]
        self.assertTrue(first.startswith("⏳ #"))
        self.assertIn("RuntimeError: boom", final)
        self.assertEqual(shell_stream.JOBS, {})

    def test_the_renderer_follows_the_callers_setting(self):
        self.assertEqual(self.run_guest(["50%\r100%\n"])[-1], "100%")

        self.edits.clear()
        self.settings.set(ADMIN, ShellPrefs(render=False))
        self.assertEqual(self.run_guest(["50%\r100%\n"])[-1], "50%\r100%")

    def test_streaming_off_runs_as_before(self):
        calls = []

        async def capture(**kwargs):
            calls.append(kwargs)
            return util.CommandResult(output="50%\r100%", retcode=0)

        with patch.object(shell_settings, "SHELL_STREAMING", False), patch.object(
            util, "simple_run_capture", capture
        ):
            asyncio.run(self.plugin.guest_shell(_query(f"@{BOT_USERNAME} .aa x")))

        ((call,),) = [calls]
        self.assertNotIn("job", call)
        self.assertEqual([edit["text"] for edit in self.edits], ["50%\r100%"])

    def test_old_brish_runs_dot_a_as_before_rendered(self):
        calls = []

        async def capture(**kwargs):
            calls.append(kwargs)
            return util.CommandResult(output="50%\r100%", retcode=0)

        with patch.object(util, "BRISH_POPEN", False), patch.object(
            util, "brishz_capture", capture
        ):
            asyncio.run(self.plugin.guest_shell(_query(f"@{BOT_USERNAME} .a x")))

        ((call,),) = [calls]
        self.assertNotIn("job", call)
        self.assertEqual([edit["text"] for edit in self.edits], ["100%"])
        self.assertEqual(shell_stream.JOBS, {})

    def test_a_brish_job_runs_on_the_shell_pool_and_records_it(self):
        seen = []

        async def capture(*, job, brish, **kwargs):
            seen.append((job.pool, brish, job.thread_key, job.chat_id))
            return util.CommandResult(output="hi", retcode=0)

        with patch.object(util, "brishz_capture", capture):
            asyncio.run(self.plugin.guest_shell(_query(f"@{BOT_USERNAME} .a x")))

        ((pool, brish, thread_key, chat_id),) = seen
        self.assertIs(pool, util.persistent_brish)
        self.assertIs(brish, pool)
        self.assertEqual((thread_key, chat_id), (_query("").thread_key, None))


class PreviewHeaderTests(_ShellTestCase):
    def test_the_header_follows_the_job(self):
        async def main():
            job = shell_stream.ShellJob(owner_id=ADMIN, chat_id=CHAT, command="x")
            header = self.plugin._preview_header
            seen = [header(job, stop_hint=True)]
            job.try_start()
            seen.append(header(job, stop_hint=True))
            seen.append(header(job, stop_hint=False))
            job.cancel(reason=shell_stream.StopReason.USER)
            seen.append(header(job, stop_hint=True))
            return job.id, seen

        job_id, seen = asyncio.run(main())

        self.assertEqual(
            seen,
            [
                f"⏳ #{job_id} waiting for a free shell · .k to stop",
                f"⏳ #{job_id} · .k to stop",
                f"⏳ #{job_id}",
                f"⏹ #{job_id} stopping…",
            ],
        )

    def test_the_preview_fits_one_message(self):
        async def main():
            job = shell_stream.ShellJob(owner_id=ADMIN, chat_id=CHAT, command="x")
            job.output.write(("😀" * 20000).encode())
            return self.plugin._preview_text(job, render=False, stop_hint=True)

        text = asyncio.run(main())

        units = len((text + "▌").encode("utf-16-le")) // 2
        self.assertLessEqual(units, self.plugin.PREVIEW_UNITS)
        self.assertGreater(units, self.plugin.PREVIEW_UNITS - 4)

    def test_edit_message_keeps_a_full_preview_of_lines_in_one_message(self):
        async def main():
            job = shell_stream.ShellJob(owner_id=ADMIN, chat_id=CHAT, command="x")
            job.output.write(("x" * 79 + "\n").encode() * 200)
            return self.plugin._preview_text(job, render=False, stop_hint=True)

        text = asyncio.run(main()) + "▌"

        self.assertGreater(len(text), self.plugin.PREVIEW_UNITS - 80)
        chunks = util._split_message_smart(
            text, max_chunk_size=self.plugin.MESSAGE_UNITS, search_direction=0
        )
        self.assertEqual(len(chunks), 1)


if __name__ == "__main__":
    unittest.main()
