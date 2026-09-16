import asyncio
import builtins
import importlib
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from uniborg import codex_util, llm_models
from uniborg.constants import (
    OPENAI_CODEX_ASTRA,
    OPENAI_CODEX_GPT_5_6_LUNA,
    OPENAI_CODEX_GPT_5_6_SOL,
    OPENAI_CODEX_LUNA_RESERVE,
)


class _FakeLoop:
    def create_task(self, coro):
        coro.close()


builtins.borg = SimpleNamespace(loop=_FakeLoop())


async def _import_plugin():
    return importlib.import_module("llm_chat_plugins.llm_chat")


plugin = asyncio.run(_import_plugin())


class LunaReserveModelTests(unittest.TestCase):
    def test_reserve_slug_is_gpt_reserve_not_the_display_model(self):
        #: The whole point: `gpt-5.6-luna` bills to the regular allowance, only
        #: `gpt-reserve` reaches the separate reserve meter.
        self.assertEqual(OPENAI_CODEX_LUNA_RESERVE, "openai-codex/gpt-reserve")
        self.assertNotEqual(OPENAI_CODEX_LUNA_RESERVE, OPENAI_CODEX_GPT_5_6_LUNA)

    def test_is_luna_reserve_model(self):
        self.assertTrue(codex_util.is_luna_reserve_model(OPENAI_CODEX_LUNA_RESERVE))
        self.assertTrue(codex_util.is_luna_reserve_model("gpt-reserve"))
        for other in (
            OPENAI_CODEX_GPT_5_6_LUNA,
            OPENAI_CODEX_GPT_5_6_SOL,
            OPENAI_CODEX_ASTRA,
            "",
            None,
        ):
            with self.subTest(model=other):
                self.assertFalse(codex_util.is_luna_reserve_model(other))

    def test_reserve_is_a_codex_model(self):
        self.assertTrue(codex_util.is_codex_model(OPENAI_CODEX_LUNA_RESERVE))
        self.assertEqual(
            codex_util.codex_model_name(OPENAI_CODEX_LUNA_RESERVE), "gpt-reserve"
        )

    def test_registry_offers_the_reserve_to_codex_users(self):
        spec = llm_models.spec_for_model(OPENAI_CODEX_LUNA_RESERVE)
        self.assertEqual(spec.display_name, "Luna Reserve (Codex)")
        self.assertTrue(spec.codex_access)
        self.assertFalse(spec.hidden)
        self.assertFalse(spec.admin_only)
        self.assertIn(OPENAI_CODEX_LUNA_RESERVE, llm_models.codex_model_choices())

    def test_reserve_supports_every_openai_reasoning_level(self):
        #: Verified live against the backend: none/low/medium/high/xhigh/max.
        spec = llm_models.spec_for_model(OPENAI_CODEX_LUNA_RESERVE)
        self.assertEqual(spec.reasoning_levels, llm_models.OPENAI_REASONING_LEVELS)


class LunaReservePrefixTests(unittest.TestCase):
    def detect(self, text, *, codex_p=True):
        return plugin._detect_and_process_message_prefix(text, codex_p=codex_p)

    def test_cr_selects_the_reserve(self):
        result = self.detect(".cr hello")
        self.assertEqual(result.model, OPENAI_CODEX_LUNA_RESERVE)
        self.assertEqual(result.reasoning_effort, "medium")
        self.assertEqual(result.processed_text, "hello")

    def test_persian_alias_selects_the_reserve(self):
        result = self.detect(".چر سلام")
        self.assertEqual(result.model, OPENAI_CODEX_LUNA_RESERVE)
        self.assertEqual(result.processed_text, "سلام")

    def test_cr_does_not_shadow_the_shorter_c_prefix(self):
        #: Longest-match wins, so `.c` must still mean Sol.
        self.assertEqual(self.detect(".c hello").model, OPENAI_CODEX_GPT_5_6_SOL)

    def test_cr_combines_with_an_effort_prefix(self):
        result = self.detect(".cr .th hello")
        self.assertEqual(result.model, OPENAI_CODEX_LUNA_RESERVE)
        self.assertEqual(result.reasoning_effort, "high")

    def test_cr_is_recognized_but_restricted_without_codex_access(self):
        #: Restricted prefixes must not leak into the prompt text.
        result = self.detect(".cr hello", codex_p=False)
        self.assertEqual(result.model, OPENAI_CODEX_LUNA_RESERVE)
        self.assertIn((".cr", ".چر"), plugin.DENIED_CODEX_PREFIX_MODEL_MAPPING)


class LunaReserveRetryTests(unittest.TestCase):
    """A spent plan allowance must retry once on the separately metered Reserve."""

    def run_request(self, *, stream_results, text="hello", selected=None):
        selected = selected or OPENAI_CODEX_GPT_5_6_SOL
        event = SimpleNamespace(
            id=99,
            sender_id=123,
            chat_id=456,
            grouped_id=None,
            is_private=True,
            text=text,
            file=None,
            message=SimpleNamespace(),
        )
        with ExitStack() as stack:
            enter = stack.enter_context
            enter(patch.object(plugin, "cleanup_completed_tasks"))
            enter(patch.object(plugin, "AWAITING_INPUT_FROM_USERS", {}))
            enter(patch.object(plugin.llm_db, "is_awaiting_key", return_value=False))
            enter(
                patch.object(
                    plugin.gemini_live_util.live_session_manager,
                    "is_live_mode_active",
                    return_value=False,
                )
            )
            enter(
                patch.object(plugin.util, "isAdmin", new=AsyncMock(return_value=True))
            )
            enter(
                patch.object(
                    plugin.llm_chat_config,
                    "can_use_codex",
                    new=AsyncMock(return_value=True),
                )
            )
            enter(
                patch.object(
                    plugin,
                    "_determine_context_mode_and_handle_transitions",
                    new=AsyncMock(return_value="recent"),
                )
            )
            enter(
                patch.object(
                    plugin,
                    "_get_effective_model_and_service",
                    side_effect=lambda *a, prefix_model=None: (
                        prefix_model or selected,
                        "codex",
                    ),
                )
            )
            enter(
                patch.object(
                    plugin, "get_effective_api_key", return_value="codex-oauth"
                )
            )
            enter(
                patch.object(
                    plugin,
                    "build_conversation_history",
                    new=AsyncMock(
                        return_value=plugin.ConversationHistoryResult(
                            history=[{"role": "user", "content": "hello"}], warnings=[]
                        )
                    ),
                )
            )
            enter(
                patch.object(
                    plugin,
                    "send_info_message",
                    new=AsyncMock(
                        return_value=SimpleNamespace(id=1, delete=AsyncMock())
                    ),
                )
            )
            edit = enter(patch.object(plugin.util, "edit_message", new=AsyncMock()))
            enter(patch.object(plugin, "_log_conversation", new=AsyncMock()))
            enter(patch.object(plugin, "_handle_tts_response", new=AsyncMock()))
            stream = enter(
                patch.object(
                    plugin.codex_util,
                    "stream_codex_response",
                    new=AsyncMock(side_effect=stream_results),
                )
            )
            asyncio.run(plugin.chat_handler(event))
        return stream, edit

    @staticmethod
    def _usage_limit_error():
        return codex_util.CodexStreamError(
            "Error code: 429",
            codex_util.CodexResponse(text=""),
            usage_limit=codex_util.CodexUsageLimit(plan_type="prolite"),
        )

    @staticmethod
    def _ok(text="answered"):
        return codex_util.CodexResponse(text=text, finish_reason="completed")

    def test_usage_limit_retries_once_on_the_reserve(self):
        stream, edit = self.run_request(
            stream_results=[self._usage_limit_error(), self._ok()]
        )
        self.assertEqual(stream.await_count, 2)
        self.assertEqual(
            stream.await_args_list[0].kwargs["model"], OPENAI_CODEX_GPT_5_6_SOL
        )
        self.assertEqual(
            stream.await_args_list[1].kwargs["model"], OPENAI_CODEX_LUNA_RESERVE
        )
        final = edit.await_args_list[-1].args[1]
        self.assertIn("answered", final)
        self.assertIn("Luna Reserve", final)

    def test_reserve_attempt_uses_its_own_prompt_cache_key(self):
        stream, _ = self.run_request(
            stream_results=[self._usage_limit_error(), self._ok()]
        )
        keys = [call.kwargs["prompt_cache_key"] for call in stream.await_args_list]
        self.assertNotEqual(keys[0], keys[1])
        self.assertEqual(
            keys[1],
            codex_util.codex_prompt_cache_key(
                model=OPENAI_CODEX_LUNA_RESERVE, chat_id=456, user_id=123
            ),
        )

    def test_non_quota_failure_is_not_retried(self):
        error = codex_util.CodexStreamError(
            "connection lost", codex_util.CodexResponse(text="")
        )
        stream, edit = self.run_request(stream_results=[error])
        self.assertEqual(stream.await_count, 1)
        self.assertIn("connection lost", edit.await_args_list[-1].args[1])

    def test_reserve_is_not_retried_against_itself(self):
        stream, _ = self.run_request(
            stream_results=[self._usage_limit_error()],
            selected=OPENAI_CODEX_LUNA_RESERVE,
        )
        self.assertEqual(stream.await_count, 1)

    def test_explicit_codex_prefix_still_reaches_the_reserve(self):
        #: Asking for Codex explicitly still gets Codex, just the other meter.
        stream, _ = self.run_request(
            stream_results=[self._usage_limit_error(), self._ok()], text=".ch hello"
        )
        self.assertEqual(stream.await_count, 2)
        self.assertEqual(
            stream.await_args_list[1].kwargs["model"], OPENAI_CODEX_LUNA_RESERVE
        )

    def test_reserve_effort_is_resolved_for_the_reserve_model(self):
        with patch.object(
            plugin, "_get_effective_reasoning", wraps=plugin._get_effective_reasoning
        ) as reasoning:
            self.run_request(stream_results=[self._usage_limit_error(), self._ok()])
        models = [call.kwargs.get("model") for call in reasoning.call_args_list]
        self.assertIn(OPENAI_CODEX_LUNA_RESERVE, models)


if __name__ == "__main__":
    unittest.main()
