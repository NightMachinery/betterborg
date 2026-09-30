import asyncio
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

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


if __name__ == "__main__":
    unittest.main()
