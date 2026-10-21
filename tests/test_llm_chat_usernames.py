"""Ordinary group activation and command dispatch through every active alias."""

import asyncio
import builtins
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from telethon import events, types
from test_prompt_cache_stability import _load_llm_chat
from uniborg.constants import GEMINI_FLASH_LATEST

plugin = _load_llm_chat()
BOT_ID = 4141


class _Borg:
    def __init__(self):
        self.me = types.User(
            id=BOT_ID, bot=True, username="ChatTestBot",
            usernames=[
                types.Username("ChatTestAI", active=True),
                types.Username("ChatTestGPT", active=True),
                types.Username("RetiredAI", active=False),
            ],
        )
        self.handlers = []

    def on(self, builder):
        def register(handler):
            self.handlers.append((builder, handler))
            return handler
        return register


class GroupUsernameTests(unittest.TestCase):
    def setUp(self):
        self.borg = _Borg()
        self.prefs = SimpleNamespace(group_activation_mode="mention")
        for item in (
            patch.object(builtins, "borg", self.borg),
            patch.object(plugin, "BOT_USERNAME", "@ChatTestBot"),
            patch.object(plugin, "IS_BOT", True),
            patch.object(plugin.user_manager, "get_prefs", return_value=self.prefs),
        ):
            item.start()
            self.addCleanup(item.stop)

    def event(self, text):
        return SimpleNamespace(
            text=text, media=None, forward=None, out=False,
            is_private=False, sender_id=22, chat_id=-123,
            is_reply=False, reply_to_msg_id=None,
            message=SimpleNamespace(out=False, guestchat_via_from=None),
            get_reply_message=AsyncMock(return_value=SimpleNamespace(sender_id=BOT_ID)),
        )

    def valid(self, text):
        return asyncio.run(plugin.is_valid_chat_message(self.event(text)))

    def test_primary_and_every_active_alias_activate_anywhere_in_groups(self):
        for name in ("ChatTestBot", "chattestai", "CHATTESTGPT"):
            for text in (f"@{name} hi", f"hi @{name}", f"ask @{name}: hi"):
                with self.subTest(text=text):
                    self.assertTrue(self.valid(text))

    def test_inactive_unrelated_and_partial_mentions_do_not_activate(self):
        for text in (
            "hi @RetiredAI", "@OtherAI hi", "@ChatTestAIExtra hi",
            "email@ChatTestAI", "@@ChatTestAI hi", "just chatting",
        ):
            with self.subTest(text=text):
                self.assertFalse(self.valid(text))

    def test_alias_only_and_unnamed_bots_do_not_need_a_primary_username(self):
        self.borg.me.username = None
        with patch.object(plugin, "BOT_USERNAME", None):
            self.assertTrue(self.valid("@ChatTestAI hi"))
            self.borg.me.usernames = []
            self.assertFalse(self.valid("@ChatTestAI hi"))
            self.assertFalse(self.valid("hi"))

    def test_mentions_preserve_existing_forward_echo_and_userbot_filters(self):
        forwarded = self.event("@ChatTestAI hi")
        forwarded.forward = object()
        self.assertFalse(asyncio.run(plugin.is_valid_chat_message(forwarded)))
        echo = self.event("@ChatTestAI hi")
        echo.message.out = True
        echo.message.guestchat_via_from = types.PeerUser(22)
        self.assertFalse(asyncio.run(plugin.is_valid_chat_message(echo)))
        outgoing = self.event("@ChatTestAI hi")
        outgoing.out = True
        with patch.object(plugin, "IS_BOT", False):
            self.assertFalse(asyncio.run(plugin.is_valid_chat_message(outgoing)))

    def test_reply_activation_still_depends_on_the_users_mode(self):
        event = self.event("no mention")
        with patch.object(plugin, "is_real_reply", AsyncMock(return_value=True)):
            self.assertFalse(asyncio.run(plugin.is_valid_chat_message(event)))
            self.prefs.group_activation_mode = "mention_and_reply"
            self.assertTrue(asyncio.run(plugin.is_valid_chat_message(event)))
        event.get_reply_message.assert_awaited_once()

    def test_leading_aliases_strip_before_model_prefix_parsing(self):
        for text in ("@ChatTestAI .f hello", " @CHATTESTGPT: .f hello", "@ChatTestAI, .f hello"):
            with self.subTest(text=text):
                stripped = plugin.strip_leading_bot_username(text)
                result = plugin._detect_and_process_message_prefix(stripped)
                self.assertEqual(result.model, GEMINI_FLASH_LATEST)
                self.assertEqual(result.processed_text, "hello")
        for text in ("ask @ChatTestAI now", "@OtherAI hi", "@ChatTestAIExtra hi"):
            self.assertEqual(plugin.strip_leading_bot_username(text), text)

    def test_commands_to_aliases_do_not_enter_the_conversation_handler(self):
        for text in ("/status@ChatTestAI", "/help@ChatTestGPT", "@ChatTestAI /status", ".codex-users@ChatTestAI list"):
            with self.subTest(text=text):
                self.assertTrue(plugin._is_known_command(text))
                self.assertFalse(self.valid(text))

    def test_registered_command_patterns_accept_all_active_aliases_only(self):
        plugin.register_handlers()
        for command in ("/status", "/help", "/stop", "/setgeminikey", ".codex-users"):
            command_handlers = [
                builder for builder, handler in self.borg.handlers
                if isinstance(builder, events.NewMessage) and builder.pattern
                and builder.pattern(command)
            ]
            self.assertEqual(len(command_handlers), 1, command)
            builder = command_handlers[0]
            for name in ("ChatTestBot", "chattestai", "CHATTESTGPT"):
                self.assertTrue(builder.pattern(f"{command}@{name}"))
            for name in ("RetiredAI", "OtherAI", "ChatTestAIExtra"):
                self.assertFalse(builder.pattern(f"{command}@{name}"))

    def test_alias_only_command_registration_preserves_key_argument_capture(self):
        self.borg.me.username = None
        with patch.object(plugin, "BOT_USERNAME", None):
            plugin.register_handlers()
        builder = next(
            b for b, handler in self.borg.handlers
            if isinstance(b, events.NewMessage) and b.pattern
            and b.pattern("/setgeminikey")
        )
        match = builder.pattern("/setgeminikey@ChatTestAI synthetic-key")
        self.assertEqual(match.group(1), "synthetic-key")
