import asyncio
import builtins
import importlib
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


class _FakeLoop:
    def create_task(self, coro):
        coro.close()


builtins.borg = SimpleNamespace(loop=_FakeLoop())


async def _import_plugin():
    return importlib.import_module("llm_chat_plugins.llm_chat")


plugin = asyncio.run(_import_plugin())


def _stream(chunks):
    async def gen():
        for chunk in chunks:
            yield chunk

    return gen()


def _chunk(content=None, finish_reason=None):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(content=content), finish_reason=finish_reason
            )
        ]
    )


#: What some providers send last: token usage, with no choices at all.
_USAGE_ONLY_CHUNK = SimpleNamespace(choices=[])


class StreamingEdgeTests(unittest.TestCase):
    def _stream_result(self, chunks):
        with patch.object(
            plugin.litellm, "acompletion", new=AsyncMock(return_value=_stream(chunks))
        ):
            return asyncio.run(plugin._call_llm_with_retry(None, None, {}, 1000.0))

    def test_a_stream_with_no_chunks_returns_an_empty_answer(self):
        result = self._stream_result([])
        self.assertEqual(result.text, "")
        self.assertIsNone(result.finish_reason)

    def test_a_usage_only_tail_keeps_the_last_finish_reason(self):
        result = self._stream_result(
            [_chunk("hi"), _chunk(None, "stop"), _USAGE_ONLY_CHUNK]
        )
        self.assertEqual(result.text, "hi")
        self.assertEqual(result.finish_reason, "stop")

    def test_an_ordinary_stream_is_unchanged(self):
        result = self._stream_result([_chunk("a"), _chunk("b", "length")])
        self.assertEqual(result.text, "ab")
        self.assertEqual(result.finish_reason, "length")


class PlaceholderFailureTests(unittest.TestCase):
    def test_a_placeholder_that_cannot_be_sent_is_reported(self):
        #: E.g. a group where the bot may no longer post.
        event = SimpleNamespace(
            sender_id=1,
            chat_id=1,
            grouped_id=None,
            is_private=True,
            text="hello",
            message=SimpleNamespace(text="hello", media=None),
            id=99,
            file=None,
        )
        send_failure = RuntimeError("CHAT_WRITE_FORBIDDEN")
        handle_llm_error = AsyncMock()
        build_history = AsyncMock()
        model = SimpleNamespace(
            model="gemini/x", service="gemini", quota_fallback_from=None
        )
        with patch.object(plugin, "cleanup_completed_tasks"), patch.object(
            plugin.llm_db, "is_awaiting_key", return_value=False
        ), patch.object(
            plugin.gemini_live_util.live_session_manager,
            "is_live_mode_active",
            return_value=False,
        ), patch.object(
            plugin.user_manager, "get_prefs", return_value=SimpleNamespace()
        ), patch.object(
            plugin.util, "isAdmin", new=AsyncMock(return_value=False)
        ), patch.object(
            plugin.llm_chat_config, "load_config", return_value={}
        ), patch.object(
            plugin.llm_chat_config, "can_use_codex", new=AsyncMock(return_value=False)
        ), patch.object(
            plugin,
            "_determine_context_mode_and_handle_transitions",
            new=AsyncMock(return_value="last_n"),
        ), patch.object(
            plugin, "_resolve_request_model", return_value=model
        ), patch.object(
            plugin, "_can_user_access_model", new=AsyncMock(return_value=True)
        ), patch.object(
            plugin, "get_model_capabilities", return_value={}
        ), patch.object(
            plugin, "get_effective_api_key", return_value="k"
        ), patch.object(
            plugin, "send_info_message", new=AsyncMock(side_effect=send_failure)
        ), patch.object(
            plugin.llm_util, "handle_llm_error", new=handle_llm_error
        ), patch.object(
            plugin, "build_conversation_history", new=build_history
        ):
            asyncio.run(plugin.chat_handler(event))

        handle_llm_error.assert_awaited_once()
        kwargs = handle_llm_error.await_args.kwargs
        self.assertIsNone(kwargs["response_message"])
        self.assertIs(kwargs["exception"], send_failure)
        build_history.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
