"""The shell's `/settings` panel and `/help` (`stdplugins/advanced_get.py`).

The plugin is loaded with a fake bot that records its handlers; commands and
button presses are fake events that record what the plugin answers, so
nothing reaches Telegram. Settings live in a temp dir.
"""

import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from telethon import events

from uniborg import draft_stream, shell_settings, tg_compat, util
from uniborg.shell_settings import FinalMode, ShellPrefs
from uniborg.stream_driver import StreamMode

from test_advanced_get_guest import ADMIN, BOT_USERNAME
from test_advanced_get_shell import _ShellTestCase


class _Command:
    """A `/settings` or `/help` message; records the plugin's replies."""

    def __init__(self, builder, text, *, private=True):
        self.replies = []
        self.sender_id = ADMIN
        self.chat_id = ADMIN if private else -1001
        self.is_private = private
        self.message = SimpleNamespace(id=1, out=False, forward=None)
        self.pattern_match = builder.pattern(text)
        assert self.pattern_match, text

    async def reply(self, text, **kwargs):
        self.replies.append((text, kwargs))


class _Press:
    """A press of a button; records the answers and edits, in order."""

    def __init__(self, data, *, sender_id=ADMIN):
        self.data = data if isinstance(data, bytes) else data.encode()
        self.sender_id = sender_id
        self.log = []

    async def answer(self, *args, **kwargs):
        self.log.append(("answer", *args))

    async def edit(self, text, **kwargs):
        self.log.append(("edit", text, kwargs))

    def answers(self):
        return [entry[1:] for entry in self.log if entry[0] == "answer"]


class _SettingsTestCase(_ShellTestCase):
    def find_handler(self, name):
        ((builder, fn),) = [
            (builder, fn)
            for builder, fn in self.borg.handlers
            if getattr(fn, "__name__", None) == name
        ]
        return builder, fn

    def command(self, text, *, private=True, handler="settings_handler"):
        builder, fn = self.find_handler(handler)
        event = _Command(builder, text, private=private)
        asyncio.run(fn(event))
        return event.replies

    def press(self, data, **kwargs):
        _builder, fn = self.find_handler("settings_press_handler")
        event = _Press(data, **kwargs)
        asyncio.run(fn(event))
        return event

    def prefs(self):
        return self.settings.get(ADMIN)

    def labels(self, rows):
        return [[tg_compat.button_text(b) for b in row] for row in rows]


class RegistrationTests(_SettingsTestCase):
    def test_a_bot_registers_its_commands_and_handlers(self):
        self.assertEqual(
            [c["command"] for c in self.plugin.BOT_COMMANDS], ["settings", "help"]
        )
        self.assertIn("register_bot_commands", self.borg.scheduled)
        for name in ("settings_handler", "help_handler", "settings_press_handler"):
            self.find_handler(name)

    def test_the_a_handler_stays_first(self):
        self.assertIs(self.borg.handlers[0][1], self.handler)

    def test_the_command_takes_this_bots_name_only(self):
        builder, _fn = self.find_handler("settings_handler")

        self.assertTrue(builder.pattern(f"/settings@{BOT_USERNAME} render off"))
        self.assertTrue(builder.pattern("/SETTINGS"))
        self.assertFalse(builder.pattern("/settings@another_bot"))
        self.assertFalse(builder.pattern("/settingsx"))

    def test_presses_of_other_buttons_do_not_reach_the_panel(self):
        builder, _fn = self.find_handler("settings_press_handler")

        def takes(data):
            event = SimpleNamespace(query=SimpleNamespace(data=data, chat_instance=0))
            return bool(builder.filter(event))

        self.assertTrue(takes(b"shs:render:off"))
        self.assertFalse(takes(b"shk:3"))
        self.assertFalse(takes(b"stream:private:edits"))


class UserbotTests(_SettingsTestCase):
    bot = False

    def test_a_userbot_has_no_slash_commands(self):
        names = [getattr(fn, "__name__", None) for _b, fn in self.borg.handlers]

        self.assertNotIn("settings_handler", names)
        self.assertNotIn("help_handler", names)
        self.assertEqual(self.borg.scheduled, [])


class PanelTests(_SettingsTestCase):
    def test_the_panel_shows_each_value_and_its_buttons(self):
        ((text, kwargs),) = self.command("/settings")

        self.assertIn("Private chats: **Drafts**", text)
        self.assertIn("Groups: **Edits**", text)
        self.assertIn("Now: **Edit the preview**", text)
        self.assertIn("Now: **On**", text)
        self.assertEqual(kwargs["parse_mode"], "md")
        self.assertEqual(
            self.labels(kwargs["buttons"]),
            [
                ["✅ Private chats: Drafts", "Private chats: Edits"],
                ["Groups: Drafts", "✅ Groups: Edits"],
                ["✅ When it ends: Edit the preview", "When it ends: New reply"],
                ["✅ Renderer: On", "Renderer: Off"],
            ],
        )

    def test_the_panel_names_the_costs(self):
        ((text, _kwargs),) = self.command("/settings")

        self.assertIn("Telegram for Android disables the send button", text)
        self.assertIn("Edits", text)
        self.assertIn("changes silently, with no notification", text)
        self.assertIn("new reply, which notifies", text)
        if draft_stream.STOP_SUPPORTED:
            self.assertIn("press Stop", text)

    def test_without_a_stop_button_the_panel_says_so(self):
        with patch.object(draft_stream, "STOP_SUPPORTED", False):
            ((text, _kwargs),) = self.command("/settings")

        self.assertIn("This bot's drafts have no Stop button", text)

    def test_each_text_form_sets_its_value(self):
        for args, expected in (
            ("private edits", ShellPrefs(stream_private=StreamMode.EDITS)),
            ("groups drafts", ShellPrefs(stream_groups=StreamMode.DRAFTS)),
            ("final reply", ShellPrefs(final_mode=FinalMode.NEW_REPLY)),
            ("render off", ShellPrefs(render=False)),
            ("RENDER On", ShellPrefs()),
        ):
            with self.subTest(args=args):
                self.settings.set(ADMIN, ShellPrefs())

                ((text, _kwargs),) = self.command(f"/settings {args}")

                self.assertEqual(self.prefs(), expected)
                self.assertIn("**Shell settings**", text)

    def test_the_panel_shows_a_change_at_once(self):
        ((_text, kwargs),) = self.command("/settings final reply")

        self.assertEqual(
            self.labels(kwargs["buttons"])[2],
            ["When it ends: Edit the preview", "✅ When it ends: New reply"],
        )

    def test_arguments_may_span_lines(self):
        ((_text, _kwargs),) = self.command("/settings render\noff")
        self.assertEqual(self.prefs(), ShellPrefs(render=False))

        ((text, _kwargs),) = self.command("/settings x\ny")
        self.assertEqual(text, self.plugin.SETTINGS_USAGE)

        self.assertTrue(self.command("/help\nme", handler="help_handler"))

    def test_a_bad_argument_gets_the_usage(self):
        for args in ("render", "render maybe", "final edit now", "channels drafts"):
            with self.subTest(args=args):
                ((text, _kwargs),) = self.command(f"/settings {args}")

                self.assertEqual(text, self.plugin.SETTINGS_USAGE)
                self.assertEqual(self.prefs(), ShellPrefs())

    def test_a_group_is_told_where_the_panel_is(self):
        replies = self.command("/settings render off", private=False)

        self.assertEqual(
            replies, [(self.plugin.SETTINGS_IN_PRIVATE, {"parse_mode": None})]
        )
        self.assertEqual(self.prefs(), ShellPrefs())

    def test_a_non_admin_gets_nothing(self):
        with patch.object(util, "isAdmin", AsyncMock(return_value=False)):
            replies = self.command("/settings render off")

        self.assertEqual(replies, [])
        self.assertEqual(self.prefs(), ShellPrefs())

    def test_a_setting_that_cannot_be_saved_says_so(self):
        with patch.object(self.settings, "set", lambda user_id, prefs: False):
            replies = self.command("/settings render off")

        self.assertEqual(replies[0][0], self.plugin.SETTINGS_NOT_SAVED)
        self.assertIn("Now: **On**", replies[1][0])


class PressTests(_SettingsTestCase):
    def test_each_button_sets_its_value_with_a_toast_first(self):
        ((_text, kwargs),) = self.command("/settings")
        datas = [
            tg_compat.button_data(button) for row in kwargs["buttons"] for button in row
        ]
        expected = [
            "Private chats: Drafts.",
            "Private chats: Edits.",
            "Groups: Drafts.",
            "Groups: Edits.",
            "When it ends: Edit the preview.",
            "When it ends: New reply.",
            "Renderer: on.",
            "Renderer: off.",
        ]

        for data, toast in zip(datas, expected, strict=True):
            with self.subTest(data=data):
                event = self.press(data)

                self.assertEqual(event.log[0], ("answer", toast))
                self.assertEqual(event.log[1][0], "edit")
                self.assertEqual(len(event.log), 2)

        self.assertEqual(
            self.prefs(),
            ShellPrefs(
                stream_private=StreamMode.EDITS,
                stream_groups=StreamMode.EDITS,
                final_mode=FinalMode.NEW_REPLY,
                render=False,
            ),
        )

    def test_a_press_redraws_the_panel(self):
        event = self.press("shs:groups:drafts")

        _kind, text, kwargs = event.log[1]
        self.assertIn("Groups: **Drafts**", text)
        self.assertEqual(
            self.labels(kwargs["buttons"])[1], ["✅ Groups: Drafts", "Groups: Edits"]
        )

    def test_a_non_admins_press_gets_a_toast_and_changes_nothing(self):
        with patch.object(util, "isAdmin", AsyncMock(return_value=False)):
            event = self.press("shs:render:off", sender_id=7)

        self.assertEqual(event.log, [("answer", self.plugin.ADMINS_ONLY)])
        self.assertEqual(self.prefs(), ShellPrefs())

    def test_an_outdated_button_says_so(self):
        event = self.press("shs:colour:blue")

        ((toast,),) = event.answers()
        self.assertIn("out of date", toast)

    def test_a_panel_that_shows_the_value_already_is_fine(self):
        from telethon import errors

        class _Same(_Press):
            async def edit(self, text, **kwargs):
                raise errors.MessageNotModifiedError(request=None)

        _builder, fn = self.find_handler("settings_press_handler")
        event = _Same(b"shs:render:on")
        asyncio.run(fn(event))

        self.assertEqual(event.answers(), [("Renderer: on.",)])


class HelpTests(_SettingsTestCase):
    def test_help_names_the_commands(self):
        ((text, kwargs),) = self.command("/help", handler="help_handler")

        for name in (".a CMD", ".aa", ".af", ".x", "/settings", f"@{BOT_USERNAME}"):
            self.assertIn(name, text)
        self.assertEqual(kwargs["parse_mode"], "md")

    def test_a_non_admin_gets_nothing(self):
        with patch.object(util, "isAdmin", AsyncMock(return_value=False)):
            replies = self.command("/help", handler="help_handler")

        self.assertEqual(replies, [])


if __name__ == "__main__":
    unittest.main()
