"""GPT families on the user's /openaiProvider: routing, the OpenAI-key backend,
and moving a request to the other backend when the first is spent."""

import asyncio
import builtins
import importlib
from contextlib import ExitStack
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from uniborg import codex_util, guest_util, openai_providers
from uniborg.constants import GPT_SOL, OPENAI_CODEX_SOL, OR_OPENAI_SOL

USER_ID = 999000772


class _FakeLoop:
    def create_task(self, coro):
        coro.close()


class _FakeBorg:
    loop = _FakeLoop()


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

from test_llm_chat_guest import BOT_USERNAME, _query  # noqa: E402


def _insufficient_quota():
    cause = RuntimeError("Error code: 429 - {'error': {'code': 'insufficient_quota'}}")
    error = codex_util.CodexStreamError(str(cause), codex_util.CodexResponse(text=""))
    error.__cause__ = cause
    return error


class RouteTests(unittest.TestCase):
    def route(self, provider, *, keys=(), codex_access=True, spent=()):
        memory = openai_providers.QuotaMemory()
        for key in spent:
            asyncio.run(memory.mark_key_spent(key))
        with ExitStack() as stack:
            stack.enter_context(
                patch.object(
                    plugin.user_manager, "get_openai_provider", return_value=provider
                )
            )
            stack.enter_context(
                patch.object(plugin, "_openai_api_keys", return_value=list(keys))
            )
            stack.enter_context(patch.object(plugin, "OPENAI_QUOTA", memory))
            stack.enter_context(
                patch.object(
                    plugin.user_manager, "get_codex_quota_fallback", return_value=None
                )
            )
            return asyncio.run(
                plugin._route_request_model(
                    plugin.RequestModel(model=GPT_SOL, service="openai"),
                    user_id=USER_ID,
                    codex_access=codex_access,
                )
            )

    def test_openai_with_a_key(self):
        routed = self.route(openai_providers.OPENAI, keys=["sk-a"])
        self.assertEqual(routed.model, "openai-api/gpt-sol-latest")
        self.assertEqual(routed.service, "openai")
        self.assertEqual(routed.display_model, GPT_SOL)

    def test_openrouter(self):
        routed = self.route(openai_providers.OPENROUTER)
        self.assertEqual(routed.model, OR_OPENAI_SOL)
        self.assertEqual(routed.service, "openrouter")

    def test_auto_openai_first_skips_a_spent_key(self):
        routed = self.route(
            openai_providers.AUTO_OPENAI_FIRST, keys=["sk-a"], spent=["sk-a"]
        )
        self.assertEqual(routed.model, OPENAI_CODEX_SOL)
        self.assertEqual(routed.route_note, openai_providers.OPENAI_SPENT_NOTE)

    def test_auto_openai_first_keeps_codex_as_its_fallback(self):
        routed = self.route(openai_providers.AUTO_OPENAI_FIRST, keys=["sk-a"])
        self.assertEqual(routed.model, "openai-api/gpt-sol-latest")
        self.assertEqual(routed.fallback_backend, openai_providers.CODEX)

    def test_other_models_pass_through(self):
        request = plugin.RequestModel(model=OR_OPENAI_SOL, service="openrouter")
        routed = asyncio.run(
            plugin._route_request_model(request, user_id=USER_ID, codex_access=True)
        )
        self.assertIs(routed, request)


class GenerationTests(unittest.TestCase):
    def _request(self, **overrides):
        event = guest_util.GuestEvent(_query(f"{BOT_USERNAME} hi"))
        fields = dict(
            event=event,
            response_message=AsyncMock(),
            messages=[{"role": "user", "content": "hi"}],
            model="openai-api/gpt-sol-latest",
            model_capabilities={"vision": True},
            api_key="sk-user",
            prefs=SimpleNamespace(json_mode=False, enabled_tools=[]),
            prefix_effort=None,
            image_generation=False,
            warnings=[],
            reasoning_model=GPT_SOL,
        )
        fields.update(overrides)
        return plugin.GenerationRequest(**fields)

    def run_generation(self, request, stream):
        with ExitStack() as stack:
            stack.enter_context(
                patch.object(plugin.codex_util, "stream_codex_response", stream)
            )
            stack.enter_context(patch.object(plugin, "ACTIVE_LLM_TASKS", {}))
            edit = stack.enter_context(patch.object(plugin.util, "edit_message"))
            result = asyncio.run(plugin._generate_response(request))
        return result, edit

    def test_the_openai_backend_streams_with_the_users_key(self):
        stream = AsyncMock(return_value=codex_util.CodexResponse(text="hello"))
        result, _ = self.run_generation(self._request(), stream)
        self.assertEqual(result.text, "hello")
        kwargs = stream.await_args.kwargs
        self.assertEqual(kwargs["api_key"], "sk-user")
        self.assertEqual(kwargs["model"], "openai-api/gpt-sol-latest")

    def test_codex_streams_without_a_key(self):
        stream = AsyncMock(return_value=codex_util.CodexResponse(text="hello"))
        self.run_generation(
            self._request(model=OPENAI_CODEX_SOL, api_key="codex-oauth"), stream
        )
        self.assertIsNone(stream.await_args.kwargs["api_key"])

    def test_a_spent_key_reroutes_to_codex_and_is_remembered(self):
        stream = AsyncMock(side_effect=_insufficient_quota())
        with self.assertRaises(plugin._RerouteRequest) as raised:
            self.run_generation(
                self._request(fallback_backend=openai_providers.CODEX), stream
            )
        self.assertEqual(raised.exception.backend, openai_providers.CODEX)
        self.assertIsNotNone(
            asyncio.run(plugin.OPENAI_QUOTA.key_spent_until("sk-user"))
        )

    def test_a_spent_key_without_a_fallback_says_so(self):
        stream = AsyncMock(side_effect=_insufficient_quota())
        result, edit = self.run_generation(self._request(), stream)
        self.assertTrue(result.delivered)
        text = edit.await_args.args[1]
        self.assertIn("OpenAI request failed", text)
        self.assertIn("out of credit", text)

    def test_a_codex_usage_limit_reroutes_to_openai(self):
        error = codex_util.CodexStreamError(
            "Error code: 429",
            codex_util.CodexResponse(text=""),
            usage_limit=codex_util.CodexUsageLimit(plan_type="pro"),
        )
        stream = AsyncMock(side_effect=error)
        with self.assertRaises(plugin._RerouteRequest) as raised:
            self.run_generation(
                self._request(
                    model=OPENAI_CODEX_SOL,
                    api_key="codex-oauth",
                    fallback_backend=openai_providers.OPENAI,
                ),
                stream,
            )
        self.assertEqual(raised.exception.backend, openai_providers.OPENAI)
        self.assertIsNotNone(asyncio.run(plugin.OPENAI_QUOTA.codex_spent_until()))

    def test_openrouter_effort_is_clamped(self):
        call = AsyncMock(
            return_value=plugin.LLMResponse(text="ok", finish_reason="stop")
        )
        with ExitStack() as stack:
            stack.enter_context(patch.object(plugin, "_call_llm_with_retry", call))
            stack.enter_context(patch.object(plugin, "ACTIVE_LLM_TASKS", {}))
            asyncio.run(
                plugin._generate_response(
                    self._request(
                        model=OR_OPENAI_SOL,
                        api_key="sk-or",
                        prefix_effort="max",
                        surface=plugin.GenerationSurface.GUEST,
                    )
                )
            )
        self.assertEqual(call.await_args.args[2]["reasoning_effort"], "high")


class ProviderMenuTests(unittest.TestCase):
    def test_menu_lists_every_provider_and_marks_the_current_one(self):
        with patch.object(
            plugin.user_manager,
            "get_openai_provider",
            return_value=openai_providers.AUTO_CODEX_FIRST,
        ), patch.object(plugin, "IS_BOT", True):
            buttons = plugin._openai_provider_menu_buttons(
                USER_ID, spent_p=True, callback_prefix="openaiprovider:"
            )
        labels = [
            plugin.tg_compat.button_text(button) for row in buttons for button in row
        ]
        self.assertIn(
            f"✅ {openai_providers.LABELS[openai_providers.AUTO_CODEX_FIRST]}", labels
        )
        self.assertIn("🔄 Retry OpenAI now", labels)
        self.assertEqual(len(labels), len(openai_providers.LABELS) + 1)

    def test_setting_a_provider_without_its_key_asks_for_one(self):
        event = SimpleNamespace(sender_id=USER_ID)
        with patch.object(
            plugin.user_manager, "set_openai_provider"
        ) as set_provider, patch.object(
            plugin, "get_effective_api_key", return_value=None
        ), patch.object(
            plugin.llm_db, "request_api_key_message", AsyncMock()
        ) as ask:
            asyncio.run(
                plugin._apply_openai_provider(event, openai_providers.OPENROUTER)
            )
        set_provider.assert_called_once_with(USER_ID, openai_providers.OPENROUTER)
        self.assertEqual(ask.await_args.args[1], "openrouter")


if __name__ == "__main__":
    unittest.main()
