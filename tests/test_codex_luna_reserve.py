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


class LunaReserveOfferTests(unittest.TestCase):
    """A spent plan allowance is reported and offered, never silently rerouted."""

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
            enter(
                patch.object(
                    plugin.codex_util,
                    "fetch_codex_usage",
                    new=AsyncMock(return_value=_usage_with_reserve()),
                )
            )
            panel = enter(
                patch.object(
                    plugin, "_codex_quota_panel", wraps=plugin._codex_quota_panel
                )
            )
            enter(patch.object(plugin, "_show_codex_quota_panel", new=AsyncMock()))
            asyncio.run(plugin.chat_handler(event))
        return SimpleNamespace(stream=stream, edit=edit, panel=panel)

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

    def test_a_usage_limit_is_never_rerouted_on_its_own(self):
        #: The old behaviour spent a doomed request and a message edit on every
        #: message while the allowance was spent, and moved the account to
        #: another meter without being asked.
        run = self.run_request(stream_results=[self._usage_limit_error(), self._ok()])
        self.assertEqual(run.stream.await_count, 1)
        self.assertEqual(
            run.stream.await_args_list[0].kwargs["model"], OPENAI_CODEX_GPT_5_6_SOL
        )

    def test_the_panel_is_offered_the_message_it_could_answer(self):
        run = self.run_request(stream_results=[self._usage_limit_error()])
        self.assertEqual(run.panel.call_args.kwargs["source_message_id"], 99)

    def test_a_request_already_on_the_reserve_is_offered_nothing(self):
        run = self.run_request(
            stream_results=[self._usage_limit_error()],
            selected=OPENAI_CODEX_LUNA_RESERVE,
        )
        self.assertEqual(run.stream.await_count, 1)
        self.assertIsNone(run.panel.call_args.kwargs["source_message_id"])

    def test_non_quota_failure_is_not_retried(self):
        error = codex_util.CodexStreamError(
            "connection lost", codex_util.CodexResponse(text="")
        )
        run = self.run_request(stream_results=[error])
        self.assertEqual(run.stream.await_count, 1)
        self.assertIn("connection lost", run.edit.await_args_list[-1].args[1])


class LunaReserveButtonTests(unittest.TestCase):
    """The offer itself: when it appears, and what tapping it does."""

    def buttons(self, *, usage, source_message_id=99):
        return plugin._codex_quota_reserve_buttons(
            usage, owner_id=123, source_message_id=source_message_id
        )

    def test_offered_when_a_message_and_an_available_reserve_both_exist(self):
        buttons = self.buttons(usage=_usage_with_reserve())
        self.assertEqual(len(buttons), 1)
        self.assertIn("Luna Reserve", buttons[0].text)
        self.assertEqual(_as_text(buttons[0].data), "cq:r:123:99")

    def test_not_offered_without_a_message_to_answer(self):
        #: `/codexStatus` has no failed request behind it.
        self.assertEqual(
            self.buttons(usage=_usage_with_reserve(), source_message_id=None), []
        )

    def test_not_offered_when_the_reserve_is_spent(self):
        self.assertEqual(self.buttons(usage=_usage_with_reserve(allowed=False)), [])

    def test_not_offered_on_an_account_without_a_reserve(self):
        self.assertEqual(self.buttons(usage=plugin.codex_util.CodexUsage()), [])

    def test_the_tap_reruns_that_message_on_the_reserve(self):
        source = SimpleNamespace(id=99, text="hello")
        event = SimpleNamespace(chat_id=456, answer=AsyncMock())
        with ExitStack() as stack:
            stack.enter_context(
                patch.object(
                    builtins,
                    "borg",
                    SimpleNamespace(get_messages=AsyncMock(return_value=source)),
                )
            )
            handler = stack.enter_context(
                patch.object(plugin, "chat_handler", new=AsyncMock())
            )
            asyncio.run(plugin._answer_from_luna_reserve(event, message_id=99))

        self.assertEqual(
            handler.await_args.kwargs["forced_model"], OPENAI_CODEX_LUNA_RESERVE
        )
        self.assertEqual(handler.await_args.args[0].text, "hello")

    def test_a_vanished_message_is_reported_rather_than_rerun(self):
        event = SimpleNamespace(chat_id=456, answer=AsyncMock())
        with ExitStack() as stack:
            stack.enter_context(
                patch.object(
                    builtins,
                    "borg",
                    SimpleNamespace(get_messages=AsyncMock(return_value=None)),
                )
            )
            handler = stack.enter_context(
                patch.object(plugin, "chat_handler", new=AsyncMock())
            )
            asyncio.run(plugin._answer_from_luna_reserve(event, message_id=99))

        handler.assert_not_awaited()
        self.assertTrue(event.answer.await_args.kwargs["show_alert"])


def _as_text(data) -> str:
    """Callback payloads are bytes in Telethon and str under the test stub."""
    return data.decode("utf-8") if isinstance(data, bytes) else data


def _usage_with_reserve(*, allowed=True):
    return plugin.codex_util.CodexUsage(
        plan_type="prolite",
        primary=plugin.codex_util.CodexMeter(name="primary", allowed=False),
        additional=(plugin.codex_util.CodexMeter(name="gpt-reserve", allowed=allowed),),
    )


#: Captured from the live endpoint, trimmed to the fields that are read.
USAGE_PAYLOAD = {
    "plan_type": "prolite",
    "rate_limit": {
        "allowed": False,
        "limit_reached": True,
        "primary_window": {
            "used_percent": 100,
            "limit_window_seconds": 604800,
            "reset_after_seconds": 351009,
            "reset_at": 1789921958,
        },
        "secondary_window": None,
    },
    "additional_rate_limits": [
        {
            "limit_name": "GPT-5.3-Codex-Spark",
            "metered_feature": "codex_bengalfox",
            "rate_limit": {
                "allowed": True,
                "limit_reached": False,
                "primary_window": {
                    "used_percent": 0,
                    "limit_window_seconds": 18000,
                    "reset_at": 1789588949,
                },
            },
            "normal_model_slug": None,
        },
        {
            "limit_name": "gpt-reserve",
            "metered_feature": "base_model_inference",
            "rate_limit": {
                "allowed": True,
                "limit_reached": False,
                "primary_window": {
                    "used_percent": 1,
                    "limit_window_seconds": 604800,
                    "reset_at": 1790174851,
                },
            },
            "normal_model_slug": "gpt-5.6-luna",
        },
    ],
}


class CodexUsageParsingTests(unittest.TestCase):
    def test_primary_window_is_read(self):
        usage = codex_util._usage_from_payload(USAGE_PAYLOAD)
        self.assertEqual(usage.plan_type, "prolite")
        self.assertFalse(usage.primary.allowed)
        self.assertEqual(usage.primary.used_percent, 100)
        self.assertEqual(usage.primary.window_seconds, 604800)
        self.assertIsNotNone(usage.primary.resets_at)

    def test_reserve_meter_is_found_among_additional_limits(self):
        reserve = codex_util._usage_from_payload(USAGE_PAYLOAD).reserve()
        self.assertIsNotNone(reserve)
        self.assertTrue(reserve.allowed)
        self.assertEqual(reserve.used_percent, 1)

    def test_reserve_is_none_for_an_account_without_one(self):
        #: The Reserve is limited to selected accounts; elsewhere the entry is
        #: simply absent and nothing about it should be shown.
        payload = dict(USAGE_PAYLOAD)
        payload["additional_rate_limits"] = [
            entry
            for entry in USAGE_PAYLOAD["additional_rate_limits"]
            if entry["limit_name"] != "gpt-reserve"
        ]
        usage = codex_util._usage_from_payload(payload)
        self.assertIsNone(usage.reserve())
        self.assertEqual(len(usage.additional), 1)

    def test_malformed_payloads_do_not_raise(self):
        for payload in (None, [], "nope", {}, {"additional_rate_limits": [1, None]}):
            with self.subTest(payload=repr(payload)[:30]):
                usage = codex_util._usage_from_payload(payload)
                if payload in (None, [], "nope"):
                    self.assertIsNone(usage)
                else:
                    self.assertIsNone(usage.reserve())


if __name__ == "__main__":
    unittest.main()
