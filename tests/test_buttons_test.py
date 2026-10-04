"""`stdplugins/buttons_test.py` answers only the presses of its own buttons.

Its CallbackQuery handler once caught every press, echoed the data and
answered it, so it took presses meant for other plugins whenever it ran
first. The filter is checked through the real `events.CallbackQuery`
builder; no handler runs, so no shell does.
"""

import importlib.util
import logging
from pathlib import Path
from types import SimpleNamespace
import unittest
import tempfile
from unittest.mock import AsyncMock, patch

from telethon import events

from uniborg import tg_compat, util
from uniborg.storage import UserStorage

from test_advanced_get_guest import _FakeBorg

PLUGIN_PATH = Path(__file__).resolve().parent.parent / "stdplugins" / "buttons_test.py"


def _load_plugin(borg):
    previous = util.borg
    util.borg = borg
    try:
        spec = importlib.util.spec_from_file_location("_test_buttons", PLUGIN_PATH)
        mod = importlib.util.module_from_spec(spec)
        mod.borg = borg
        mod.logger = logging.getLogger("test.buttons_test")
        spec.loader.exec_module(mod)
        return mod
    finally:
        util.borg = previous


class OwnPressesTests(unittest.TestCase):
    def setUp(self):
        self.borg = _FakeBorg()
        self.plugin = _load_plugin(self.borg)
        (self.builder,) = [
            builder
            for builder, _fn in self.borg.handlers
            if isinstance(builder, events.CallbackQuery)
        ]

    def takes(self, data) -> bool:
        event = SimpleNamespace(query=SimpleNamespace(data=data, chat_instance=0))
        return bool(self.builder.filter(event))

    def test_its_own_buttons_and_commands_reach_it(self):
        self.assertTrue(self.takes(b"zsh_0b5e"))
        self.assertTrue(self.takes(b".z echo hi"))
        self.assertTrue(self.takes(".Z ls\n-la".encode()))
        self.assertTrue(self.takes(b"jjson_0123456789abcdef0123456789abcdef"))

    def test_every_other_press_is_left_alone(self):
        for data in (
            b"shk:3",
            b"shs:render:off",
            b"stream:private:drafts",
            b"Click me",
            b".zz",
            b"jjson_not-a-token",
            b"\xff\xfe",
            None,
        ):
            with self.subTest(data=data):
                self.assertFalse(self.takes(data))


class CustomDataTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.borg = _FakeBorg()
        self.plugin = _load_plugin(self.borg)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = UserStorage(purpose="buttons", root=self.tmp.name)

    async def test_custom_data_echoes_literally_and_survives_reload(self):
        for payload in ("custom data", "shk:3", "**literal**", "x" * 64, "💎" * 16):
            with self.subTest(payload=payload):
                button = self.plugin.inline_button(
                    "Press", payload, callback_store=self.store
                )
                data = tg_compat.button_data(button)
                self.assertLessEqual(len(data), 64)
                self.assertTrue(self.plugin.is_own_data(data))
                reloaded = UserStorage(purpose="buttons", root=self.tmp.name)
                event = SimpleNamespace(
                    data=data, reply=AsyncMock(), answer=AsyncMock()
                )
                with patch.object(self.plugin, "z") as shell:
                    await self.plugin.callback(event, callback_store=reloaded)
                    shell.assert_not_called()
                event.reply.assert_awaited_once_with(payload, parse_mode=None)
                event.answer.assert_awaited_once_with()

    async def test_json_sends_owned_echo_buttons(self):
        self.borg.send_message = AsyncMock()
        await self.plugin.send_json(
            self.borg,
            '[{"caption":"Hi", "buttons_inline":[["One"],["Two","custom"]]}]',
            chat=7,
            callback_store=self.store,
        )
        args, kwargs = self.borg.send_message.await_args
        self.assertEqual(args, (7, "Hi"))
        for button, expected in zip(kwargs["buttons"][0], ("One", "custom")):
            event = SimpleNamespace(
                data=tg_compat.button_data(button),
                reply=AsyncMock(),
                answer=AsyncMock(),
            )
            await self.plugin.callback(event, callback_store=self.store)
            event.reply.assert_awaited_once_with(expected, parse_mode=None)

    def test_inline_shell_data_keeps_its_existing_command_path(self):
        for data in (".z printf sentinel", "zsh_0123"):
            button = self.plugin.inline_button("Run", data, callback_store=self.store)
            self.assertEqual(tg_compat.button_data(button), data.encode())

    def test_payload_limit_is_preserved(self):
        with self.assertRaises(ValueError):
            self.plugin.inline_button("Press", "x" * 65, callback_store=self.store)

    async def test_missing_data_answers_without_echoing(self):
        event = SimpleNamespace(
            data=b"jjson_0123456789abcdef0123456789abcdef",
            reply=AsyncMock(),
            answer=AsyncMock(),
        )
        await self.plugin.callback(event, callback_store=self.store)
        event.reply.assert_not_awaited()
        self.assertTrue(event.answer.await_args.kwargs["alert"])


if __name__ == "__main__":
    unittest.main()
