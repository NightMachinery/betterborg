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

    def action(self, chat, kind):
        return _Action()

    async def send_file(self, chat, file, **kwargs):
        names = (
            [Path(f).name for f in file] if isinstance(file, list) else Path(file).name
        )
        self.sends.append((chat, names, kwargs))
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

    def test_a_failure_is_still_reported(self):
        event = self._event()

        async def to_await(*, cwd, event):
            raise RuntimeError("boom")

        asyncio.run(util.run_and_upload(event=event, to_await=to_await))

        self.assertEqual(self.reports, [event])
