"""How the chat bot streams an answer: drafts or edits (/stream).

See docs/draft_streaming.md. The draft stand-in itself is tested in
tests/test_draft_stream.py; this covers the plugin's choice of it, the Stop
button, and the /stream settings.
"""

import asyncio
import builtins
import importlib
import logging
from contextlib import ExitStack
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from uniborg import draft_stream


class _FakeBorg:
    class loop:
        @staticmethod
        def create_task(coro):
            coro.close()


def _import_plugin():
    previous = getattr(builtins, "borg", None)
    builtins.borg = _FakeBorg()
    try:

        async def _import():
            return importlib.import_module("llm_chat_plugins.llm_chat")

        return asyncio.run(_import())
    finally:
        if previous is not None:
            builtins.borg = previous


plugin = _import_plugin()
StreamMode = plugin.StreamMode


def _event(*, is_private=True):
    return SimpleNamespace(chat_id=31, sender_id=31, is_private=is_private)


class _Case(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.sent = AsyncMock(return_value="placeholder")
        self.start = AsyncMock(return_value=True)
        for target, name, value in (
            (builtins, "borg", _FakeBorg()),
            (builtins, "logger", logging.getLogger("test.stream")),
            (plugin, "IS_BOT", True),
            (plugin, "send_info_message", self.sent),
            (plugin, "_thread_topic_id", lambda event: None),
            (draft_stream.DraftAnswerMessage, "start", self.start),
        ):
            stack.enter_context(patch.object(target, name, value, create=True))

    def placeholder(self, event, **prefs):
        return asyncio.run(
            plugin._response_placeholder(
                event, plugin.RESPONSE_PLACEHOLDER, prefs=plugin.UserPrefs(**prefs)
            )
        )


class PlaceholderTests(_Case):
    def test_private_chats_stream_drafts_by_default(self):
        message = self.placeholder(_event())

        self.assertIsInstance(message, draft_stream.DraftAnswerMessage)
        #: An empty first draft shows Telegram's own "Thinking…".
        self.start.assert_awaited_once_with("")
        self.sent.assert_not_awaited()

    def test_groups_stream_edits_by_default(self):
        self.assertEqual(self.placeholder(_event(is_private=False)), "placeholder")
        self.start.assert_not_awaited()

    def test_each_scope_follows_its_setting(self):
        cases = [
            (True, {"stream_private": StreamMode.EDITS}, False),
            (False, {"stream_groups": StreamMode.DRAFTS}, True),
        ]
        for is_private, prefs, drafted in cases:
            with self.subTest(is_private=is_private):
                self.start.reset_mock()
                message = self.placeholder(_event(is_private=is_private), **prefs)
                self.assertEqual(self.start.await_count, int(drafted))
                self.assertEqual(
                    isinstance(message, draft_stream.DraftAnswerMessage), drafted
                )

    def test_a_refused_draft_sends_the_placeholder(self):
        self.start.return_value = False

        self.assertEqual(self.placeholder(_event()), "placeholder")
        self.sent.assert_awaited_once_with(_event(), plugin.RESPONSE_PLACEHOLDER)

    def test_a_user_account_streams_edits(self):
        with patch.object(plugin, "IS_BOT", False):
            self.assertEqual(self.placeholder(_event()), "placeholder")


class StopTests(_Case):
    def test_stop_cancels_the_generation_and_ends_the_stream(self):
        async def generate(req):
            await asyncio.Event().wait()

        async def run():
            draft = draft_stream.DraftAnswerMessage(object(), event=_event())
            draft.streaming = True
            request = SimpleNamespace(response_message=draft)
            with patch.object(plugin, "_generate_response", generate):
                task = asyncio.ensure_future(plugin._generate_streamed(request))
                await asyncio.sleep(0)
                draft.stop_pressed()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            return draft

        draft = asyncio.run(run())

        self.assertTrue(draft.stopped)
        self.assertFalse(draft.streaming)

    def test_an_edit_streamed_answer_is_generated_as_before(self):
        generate = AsyncMock(return_value="result")
        request = SimpleNamespace(response_message="placeholder")

        with patch.object(plugin, "_generate_response", generate):
            result = asyncio.run(plugin._generate_streamed(request))

        self.assertEqual(result, "result")
        generate.assert_awaited_once_with(request)


class StreamCommandTests(_Case):
    def setUp(self):
        super().setUp()
        self.prefs = plugin.UserPrefs()

        def set_stream_mode(user_id, *, scope, mode):
            setattr(self.prefs, f"stream_{scope}", mode)

        for name, value in (
            ("get_prefs", lambda user_id: self.prefs),
            ("set_stream_mode", set_stream_mode),
        ):
            patcher = patch.object(plugin.user_manager, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def command(self, args):
        event = SimpleNamespace(
            sender_id=31, pattern_match=SimpleNamespace(group=lambda name: args)
        )
        asyncio.run(plugin.stream_handler(event))
        return self.sent.await_args

    def test_the_menu_shows_both_scopes(self):
        call = self.command(None)

        self.assertIn("Private chats: **Drafts**", call.args[1])
        self.assertIn("Groups: **Edits**", call.args[1])
        labels = [
            [plugin.tg_compat.button_text(b) for b in row]
            for row in call.kwargs["buttons"]
        ]
        self.assertEqual(
            labels,
            [
                ["✅ Private chats: Drafts", "Private chats: Edits"],
                ["Groups: Drafts", "✅ Groups: Edits"],
            ],
        )

    def test_arguments_set_a_scope(self):
        self.command("groups DRAFTS")

        self.assertEqual(self.prefs.stream_groups, StreamMode.DRAFTS)

    def test_bad_arguments_get_the_usage(self):
        call = self.command("everywhere drafts")

        self.assertEqual(call.args[1], plugin.STREAM_USAGE)
        self.assertEqual(self.prefs, plugin.UserPrefs())

    def test_a_press_sets_the_scope_and_redraws_the_menu(self):
        event = SimpleNamespace(sender_id=31, edit=AsyncMock(), answer=AsyncMock())

        asyncio.run(
            plugin._stream_menu_press_handler(
                event, scope=plugin.STREAM_SCOPE_PRIVATE, mode=StreamMode.EDITS
            )
        )

        self.assertEqual(self.prefs.stream_private, StreamMode.EDITS)
        self.assertIn("Private chats: **Edits**", event.edit.await_args.args[0])
        event.answer.assert_awaited_once_with("Private chats: Edits.")

    def test_settings_round_trip_through_json(self):
        prefs = plugin.UserPrefs(stream_groups=StreamMode.DRAFTS)

        restored = plugin.UserPrefs.model_validate_json(prefs.model_dump_json())

        self.assertEqual(restored.stream_groups, StreamMode.DRAFTS)


if __name__ == "__main__":
    unittest.main()
