"""Each admin's shell settings (`uniborg/shell_settings.py`), on a temp dir."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from uniborg import shell_settings, stream_driver
from uniborg.shell_settings import FinalMode, ShellPrefs, ShellSettings
from uniborg.storage import UserStorage
from uniborg.stream_driver import StreamMode

USER = 195391705


class ShellSettingsTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.storage = UserStorage(purpose=shell_settings.PURPOSE, root=tmp.name)
        self.settings = ShellSettings(storage=self.storage)

    def stored(self):
        path = self.storage.base_dir / f"{USER}.json"
        return json.loads(path.read_text())

    def test_the_defaults(self):
        prefs = self.settings.get(USER)

        self.assertEqual(
            prefs,
            ShellPrefs(
                stream_private=StreamMode.DRAFTS,
                stream_groups=StreamMode.EDITS,
                final_mode=FinalMode.EDIT_PREVIEW,
                render=True,
            ),
        )

    def test_the_scopes_use_the_chat_bots_names(self):
        prefs = self.settings.get(USER)

        self.assertEqual(
            stream_driver.stream_mode(prefs, scope=stream_driver.STREAM_SCOPE_PRIVATE),
            StreamMode.DRAFTS,
        )
        stream_driver.set_stream_mode(
            prefs, scope=stream_driver.STREAM_SCOPE_GROUPS, mode=StreamMode.DRAFTS
        )
        self.assertEqual(prefs.stream_groups, StreamMode.DRAFTS)

    def test_only_changed_values_are_stored_and_they_read_back(self):
        prefs = self.settings.get(USER)
        prefs.stream_private = StreamMode.EDITS
        prefs.final_mode = FinalMode.NEW_REPLY
        prefs.render = False

        self.assertTrue(self.settings.set(USER, prefs))

        self.assertEqual(
            self.stored(),
            {"stream_private": "edits", "final_mode": "new_reply", "render": False},
        )
        self.assertEqual(self.settings.get(USER), prefs)

    def test_back_to_the_defaults_stores_nothing(self):
        self.settings.set(USER, ShellPrefs(render=False))
        self.settings.set(USER, ShellPrefs())

        self.assertEqual(self.stored(), {})

    def test_unknown_stored_values_read_as_the_defaults(self):
        self.storage.set(
            USER,
            {
                "final_mode": "sideways",
                "render": "yes",
                "colour": 1,
                "stream_groups": "drafts",
            },
        )

        with self.assertLogs(shell_settings.__name__, level="WARNING") as logs:
            prefs = self.settings.get(USER)

        self.assertEqual(prefs, ShellPrefs(stream_groups=StreamMode.DRAFTS))
        self.assertEqual(len(logs.output), 3)

    def test_a_stored_file_that_is_not_an_object_reads_as_the_defaults(self):
        path = self.storage.base_dir / f"{USER}.json"
        for stored in ('["drafts"]', '"drafts"', "3"):
            with self.subTest(stored=stored):
                path.write_text(stored)

                with self.assertLogs(shell_settings.__name__, level="WARNING"):
                    prefs = self.settings.get(USER)

                self.assertEqual(prefs, ShellPrefs())

    def test_the_default_storage_is_made_on_first_use(self):
        settings = ShellSettings()

        self.assertIsNone(settings._storage)


class StreamingSwitchTests(unittest.TestCase):
    def test_unset_and_one_are_on_zero_is_off(self):
        self.assertTrue(shell_settings.streaming_switch(None))
        self.assertTrue(shell_settings.streaming_switch("1"))
        self.assertFalse(shell_settings.streaming_switch("0"))

    def test_anything_else_is_refused(self):
        for value in ("", "yes", "off", " 1"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                shell_settings.streaming_switch(value)

    def test_a_bad_value_stops_the_bot_at_startup(self):
        #: The plugin loader skips a plugin that fails to load, which would
        #: leave `.a` silent; so importing uniborg itself, as stdborg does
        #: first, must fail.
        env = {**os.environ, shell_settings.STREAMING_ENV: "yes"}
        result = subprocess.run(
            [sys.executable, "-c", "import uniborg"],
            cwd=Path(__file__).resolve().parent.parent,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("borg_shell_streaming must be 0 or 1", result.stderr)


if __name__ == "__main__":
    unittest.main()
