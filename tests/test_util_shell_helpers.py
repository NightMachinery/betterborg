import asyncio
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from brish import Brish

from uniborg import util


class _DownloadingBorg:
    def __init__(self):
        self.downloaded = []

    async def download_media(self, *, message, file):
        self.downloaded.append((message.id, file))
        Path(file).write_text(f"media of {message.id}")
        return file

    async def get_messages(self, *args, **kwargs):
        raise AssertionError("run_and_get(messages=...) must not fetch messages")


class _BorgTestCase(unittest.TestCase):
    def setUp(self):
        self._borg = util.borg
        self.borg = util.borg = self.make_borg()
        self.addCleanup(setattr, util, "borg", self._borg)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cwd = self.tmp.name + "/"


class RunAndGetMessagesTests(_BorgTestCase):
    make_borg = _DownloadingBorg

    def test_downloads_exactly_the_given_messages_without_an_event(self):
        messages = [
            SimpleNamespace(id=7, file=SimpleNamespace(name="../../escape.txt")),
            SimpleNamespace(id=5, file=SimpleNamespace(name="voice.ogg")),
            SimpleNamespace(id=6, file=None),
        ]
        seen = {}

        async def to_await(*, cwd, event):
            seen["files"] = sorted(os.listdir(cwd))
            seen["event"] = event
            Path(cwd, "5_voice.ogg").write_text("changed")

        cwd = asyncio.run(util.run_and_get(None, to_await, self.cwd, messages=messages))

        self.assertEqual(cwd, self.cwd)
        self.assertEqual([m for m, _ in self.borg.downloaded], [5, 7])
        self.assertEqual(
            seen, {"files": ["5_voice.ogg", "7_escape.txt"], "event": None}
        )
        #: Untouched downloads are removed; the one the command changed stays.
        self.assertEqual(os.listdir(self.cwd), ["5_voice.ogg"])


class _Action:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _UploadingBorg:
    def __init__(self):
        self.sends = []
        self.fail_names = set()
        #: Set when the next send has started; that one send hangs until
        #: cancelled.
        self.hang = None

    def action(self, chat, kind):
        return _Action()

    async def send_file(self, chat, file, **kwargs):
        names = (
            [Path(f).name for f in file] if isinstance(file, list) else Path(file).name
        )
        self.sends.append((chat, names, kwargs))
        if self.hang is not None:
            hang, self.hang = self.hang, None
            hang.set()
            await asyncio.Event().wait()
        if isinstance(names, str) and names in self.fail_names:
            raise RuntimeError("upload failed")
        if isinstance(file, list):
            return [SimpleNamespace(name=n) for n in names]
        return SimpleNamespace(name=names)


class UploadOutputFilesTests(_BorgTestCase):
    make_borg = _UploadingBorg

    def _files(self, *names):
        for name in names:
            Path(self.cwd, name).write_text(name)
        Path(self.cwd, "subdir").mkdir()
        return sorted(Path(self.cwd).glob("*"))

    def test_album_mode_groups_by_extension_and_returns_the_messages(self):
        files = self._files("a.txt", "b.txt", "c.png")

        sent = asyncio.run(
            util.upload_output_files(42, files, album_mode=True, on_error=None)
        )

        self.assertEqual(
            [(chat, names) for chat, names, _ in self.borg.sends],
            [(42, ["c.png"]), (42, ["a.txt", "b.txt"])],
        )
        self.assertEqual([m.name for m in sent], ["c.png", "a.txt", "b.txt"])

    def test_one_by_one_uses_name_prefixes_and_reports_failures(self):
        files = self._files("voicenote-x.ogg", "fdoc-y.pdf")
        self.borg.fail_names = {"fdoc-y.pdf"}
        errors = []

        async def on_error():
            errors.append("failed")

        sent = asyncio.run(
            util.upload_output_files(
                42, files, album_mode=False, reply_to=9, on_error=on_error
            )
        )

        kwargs = {names: kw for _chat, names, kw in self.borg.sends}
        self.assertTrue(kwargs["voicenote-x.ogg"]["voice_note"])
        self.assertTrue(kwargs["fdoc-y.pdf"]["force_document"])
        self.assertEqual(kwargs["voicenote-x.ogg"]["reply_to"], 9)
        self.assertEqual([m.name for m in sent], ["voicenote-x.ogg"])
        self.assertEqual(errors, ["failed"])

    def test_a_cancel_mid_upload_propagates_and_is_not_reported(self):
        #: The client disconnecting cancels the handler during a send.
        reports = []

        async def on_error():
            reports.append("on_error")

        async def handle_exc_chat(chat, reply_exc=True):
            reports.append("handle_exc_chat")

        async def main(album_mode):
            self.borg.hang = asyncio.Event()
            task = asyncio.create_task(
                util.upload_output_files(
                    42, files, album_mode=album_mode, on_error=on_error
                )
            )
            await self.borg.hang.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 5)

        files = self._files("a.txt", "b.png")
        for album_mode in (False, True):
            with self.subTest(album_mode=album_mode), patch.object(
                util, "handle_exc_chat", handle_exc_chat
            ):
                asyncio.run(main(album_mode))
                self.assertEqual(reports, [])


class CaptureTests(unittest.TestCase):
    def test_simple_run_capture_merges_stderr_and_keeps_the_exit_code(self):
        with tempfile.TemporaryDirectory() as cwd:
            result = asyncio.run(
                util.simple_run_capture(
                    cwd=cwd, command="printf out; printf err >&2; exit 3"
                )
            )

        self.assertEqual(result, util.CommandResult(output="outerr", retcode=3))

    def test_simple_run_capture_gives_the_command_no_input(self):
        read_end, write_end = os.pipe()
        os.write(write_end, b"leaked")
        os.close(write_end)
        saved_stdin = os.dup(0)
        os.dup2(read_end, 0)
        os.close(read_end)
        try:
            with tempfile.TemporaryDirectory() as cwd:
                result = asyncio.run(util.simple_run_capture(cwd=cwd, command="cat"))
        finally:
            os.dup2(saved_stdin, 0)
            os.close(saved_stdin)

        self.assertEqual(result, util.CommandResult(output="", retcode=0))

    def test_brishz_capture_reads_the_brish_result(self):
        calls = []

        async def fake_helper(cwd, cmd, *, brish=None, fork=True, server_index=None):
            calls.append((cwd, cmd, brish, fork, server_index))
            return SimpleNamespace(outerr="done", retcode=0)

        original = util.brishz_helper
        util.brishz_helper = fake_helper
        self.addCleanup(setattr, util, "brishz_helper", original)

        result = asyncio.run(util.brishz_capture(cwd="/w/", cmd="ls", fork=False))

        self.assertEqual(result, util.CommandResult(output="done", retcode=0))
        self.assertEqual(calls, [("/w/", "ls", None, False, 0)])


if __name__ == "__main__":
    unittest.main()


class BrishzHelperTests(unittest.TestCase):
    """Non-fork commands on a real one-worker Brish, as `.af` runs them."""

    def setUp(self):
        self.brish = Brish(server_count=1)
        self.addCleanup(self.brish.cleanup)

    def run_helper(self, cmd):
        async def run(cwd):
            return await util.brishz_helper(
                cwd, cmd, brish=self.brish, fork=False, server_index=0
            )

        with tempfile.TemporaryDirectory() as cwd:
            return asyncio.run(run(cwd + "/"))

    def test_a_worker_left_under_emulate_sh_still_runs_commands(self):
        self.run_helper("emulate sh")
        res = self.run_helper("echo hello")

        self.assertEqual((res.retcode, res.outerr), (0, "hello\n"))

    @unittest.skipUnless(hasattr(Brish, "popen"), "brish before popen")
    def test_exit_reports_its_status_and_the_pool_recovers(self):
        res = self.run_helper("exit 3")
        self.assertEqual(res.retcode, 3)

        res = self.run_helper("echo again")
        self.assertEqual((res.retcode, res.outerr), (0, "again\n"))


class _AckBorg:
    async def send_read_acknowledge(self, chat, message):
        pass


class RunAndUploadTests(_BorgTestCase):
    make_borg = _AckBorg

    def setUp(self):
        super().setUp()
        self.reports = []

        async def handle_exc(event, reply_exc=True):
            self.reports.append(event)

        for name, value in (("handle_exc", handle_exc), ("dl_base", self.cwd)):
            patcher = patch.object(util, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _event(self):
        async def get_chat():
            return "chat"

        message = SimpleNamespace(id=1, reply_to_msg_id=None, grouped_id=None)
        return SimpleNamespace(get_chat=get_chat, message=message)

    def test_a_cancel_propagates_and_is_not_reported(self):
        event = self._event()

        async def main():
            started = asyncio.Event()

            async def to_await(*, cwd, event):
                started.set()
                await asyncio.sleep(3600)

            task = asyncio.create_task(
                util.run_and_upload(event=event, to_await=to_await)
            )
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        asyncio.run(main())

        self.assertEqual(self.reports, [])

    def test_a_cancel_during_the_read_receipt_propagates_and_runs_nothing(self):
        event = self._event()
        ran = []

        async def main():
            acking = asyncio.Event()

            async def send_read_acknowledge(chat, message):
                acking.set()
                await asyncio.sleep(3600)

            self.borg.send_read_acknowledge = send_read_acknowledge

            async def to_await(*, cwd, event):
                ran.append(cwd)

            task = asyncio.create_task(
                util.run_and_upload(event=event, to_await=to_await)
            )
            await acking.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        asyncio.run(main())

        self.assertEqual(ran, [])
        self.assertEqual(self.reports, [])

    def test_a_failed_read_receipt_is_ignored(self):
        event = self._event()
        ran = []

        async def send_read_acknowledge(chat, message):
            raise RuntimeError("no receipt")

        self.borg.send_read_acknowledge = send_read_acknowledge

        async def to_await(*, cwd, event):
            ran.append(cwd)

        asyncio.run(util.run_and_upload(event=event, to_await=to_await))

        self.assertEqual(len(ran), 1)
        self.assertEqual(self.reports, [])

    def test_a_failure_is_still_reported(self):
        event = self._event()

        async def to_await(*, cwd, event):
            raise RuntimeError("boom")

        asyncio.run(util.run_and_upload(event=event, to_await=to_await))

        self.assertEqual(self.reports, [event])


class _ScriptedWorkerBrish:
    """A one-worker pool whose `z` calls fail as told, recording the lock."""

    def __init__(self, *, die_on=()):
        self.die_on = list(die_on)
        self.calls = []
        self.locks = 0

    def acquire_lock(self, server_index=None, lock_sleep=1):
        self.locks += 1
        brish = self

        class Lock:
            def release(self):
                brish.locks -= 1

        return Lock(), 0

    def z(self, template, **kwargs):
        self.calls.append(template)
        if self.die_on and self.die_on[0] == len(self.calls):
            self.die_on.pop(0)
            raise util.BrishWorkerDiedException("gone")

    def send_cmd(self, cmd, **kwargs):
        self.calls.append("cd")


class _CancellableWorkerBrish(_ScriptedWorkerBrish):
    """A `_ScriptedWorkerBrish` whose wait for the worker can be called off,
    as with brish 0.4.1: it frees the lock and raises."""

    def acquire_lock(self, server_index=None, lock_sleep=1, cancelled=None):
        lock, index = super().acquire_lock(server_index, lock_sleep)
        if cancelled is not None and cancelled():
            lock.release()
            raise util.BrishCancelledException("cancelled")
        return lock, index


@unittest.skipUnless(
    isinstance(util.BrishWorkerDiedException, type), "brish before 0.4"
)
class OnBrishWorkerTests(unittest.TestCase):
    def run_on(self, brish, **kwargs):
        runs = []

        def run(index):
            runs.append(index)
            return "result"

        res = util._on_brish_worker(
            brish, cwd="/w/", server_index=None, run=run, **kwargs
        )
        return res, runs

    def test_runs_in_the_directory_and_returns_to_tmp(self):
        brish = _ScriptedWorkerBrish()

        res, runs = self.run_on(brish)

        self.assertEqual((res, runs), ("result", [0]))
        self.assertEqual(brish.calls, ["typeset -g jd={cwd}", "cd", "cd /tmp"])
        self.assertEqual(brish.locks, 0)

    def test_a_refused_start_runs_nothing_and_frees_the_worker(self):
        brish = _ScriptedWorkerBrish()

        res, runs = self.run_on(brish, may_start=lambda: False)

        self.assertEqual((res, runs, brish.calls), (None, [], []))
        self.assertEqual(brish.locks, 0)

    def test_a_death_after_the_command_keeps_its_result(self):
        brish = _ScriptedWorkerBrish(die_on=[3])

        res, runs = self.run_on(brish)

        self.assertEqual((res, runs), ("result", [0]))
        self.assertEqual(brish.locks, 0)

    def test_a_death_before_the_command_retries_once(self):
        brish = _ScriptedWorkerBrish(die_on=[1])

        res, runs = self.run_on(brish)

        self.assertEqual((res, runs), ("result", [0]))

        brish = _ScriptedWorkerBrish(die_on=[1, 2])
        with self.assertRaises(util.BrishWorkerDiedException):
            self.run_on(brish)
        self.assertEqual(brish.locks, 0)

    def test_a_brish_without_cancelled_still_asks_may_start(self):
        #: Its `acquire_lock` takes no `cancelled=`, as before brish 0.4.1.
        brish = _ScriptedWorkerBrish()
        asked = []

        def may_start():
            asked.append(True)
            return False

        res, runs = self.run_on(brish, may_start=may_start, cancelled=lambda: True)

        self.assertEqual((res, runs, brish.calls, asked), (None, [], [], [True]))
        self.assertEqual(brish.locks, 0)

    @unittest.skipUnless(
        isinstance(util.BrishCancelledException, type), "brish before 0.4.1"
    )
    def test_a_cancelled_wait_runs_nothing_and_retries_nothing(self):
        brish = _CancellableWorkerBrish()
        asked = []

        res, runs = self.run_on(
            brish, may_start=lambda: asked.append(True), cancelled=lambda: True
        )

        self.assertEqual((res, runs, brish.calls, asked), (None, [], [], []))
        self.assertEqual(brish.locks, 0)

    @unittest.skipUnless(
        isinstance(util.BrishCancelledException, type), "brish before 0.4.1"
    )
    def test_a_wait_that_is_not_called_off_runs(self):
        brish = _CancellableWorkerBrish()

        res, runs = self.run_on(brish, cancelled=lambda: False)

        self.assertEqual((res, runs), ("result", [0]))
        self.assertEqual(brish.locks, 0)
