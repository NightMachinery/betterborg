"""Gemini models on Vertex AI: routing, refusals, request shape and the provider panel."""

import asyncio
import builtins
import importlib
from contextlib import ExitStack
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from uniborg import gemini_providers, guest_util, title_util

VERTEX = gemini_providers.VERTEX
AI_STUDIO = gemini_providers.AI_STUDIO
FLASH = "gemini/gemini-2.5-flash"
USER_ID = 999000771


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


def _on(provider):
    return patch.object(
        plugin.user_manager, "get_gemini_provider", return_value=provider
    )


class RoutingTests(unittest.TestCase):
    def test_native_gemini_chat_follows_the_users_provider(self):
        with _on(VERTEX):
            self.assertEqual(
                plugin._gemini_chat_service(USER_ID, FLASH, AI_STUDIO), VERTEX
            )
        with _on(AI_STUDIO):
            self.assertEqual(
                plugin._gemini_chat_service(USER_ID, FLASH, AI_STUDIO), AI_STUDIO
            )

    def test_image_generation_and_other_services_stay_where_they_are(self):
        with _on(VERTEX):
            self.assertEqual(
                plugin._gemini_chat_service(
                    USER_ID, FLASH, AI_STUDIO, image_generation=True
                ),
                AI_STUDIO,
            )
            self.assertEqual(
                plugin._gemini_chat_service(
                    USER_ID, "openrouter/google/gemini-2.5-pro", "openrouter"
                ),
                "openrouter",
            )

    def test_a_model_vertex_lacks_is_refused_with_the_way_out(self):
        refusal = plugin._vertex_refusal(plugin.GEMINI_FLASH_LATEST, VERTEX)
        self.assertIn("not available on Vertex AI", refusal)
        self.assertIn("/geminiProvider", refusal)
        self.assertIsNone(plugin._vertex_refusal(FLASH, VERTEX))
        self.assertIsNone(plugin._vertex_refusal(plugin.GEMINI_FLASH_LATEST, AI_STUDIO))

    def test_vertex_attachments_are_sent_inline(self):
        self.assertTrue(plugin.is_native_gemini_files_mode(FLASH))
        self.assertFalse(
            plugin.is_native_gemini_files_mode(FLASH, gemini_provider=VERTEX)
        )


class RequestShapeTests(unittest.TestCase):
    def _api_kwargs(self, provider):
        event = guest_util.GuestEvent(_query(f"{BOT_USERNAME} hi"))
        request = plugin.GenerationRequest(
            event=event,
            response_message=AsyncMock(),
            messages=[{"role": "user", "content": "hi"}],
            model=FLASH,
            model_capabilities={},
            api_key="k",
            prefs=SimpleNamespace(json_mode=False, enabled_tools=[]),
            prefix_effort=None,
            image_generation=False,
            warnings=[],
            surface=plugin.GenerationSurface.GUEST,
            gemini_provider=provider,
        )
        call = AsyncMock(
            return_value=plugin.LLMResponse(text="ok", finish_reason="stop")
        )
        with ExitStack() as stack:
            stack.enter_context(patch.object(plugin, "_call_llm_with_retry", call))
            stack.enter_context(patch.object(plugin, "ACTIVE_LLM_TASKS", {}))
            stack.enter_context(
                patch.object(
                    plugin.history_util,
                    "is_gemini_caching_disabled",
                    AsyncMock(return_value=False),
                )
            )
            stack.enter_context(
                patch.object(
                    plugin.llm_util, "create_litellm_proxy_client", return_value=None
                )
            )
            asyncio.run(plugin._generate_response(request))
        return call.await_args.args[2]

    def test_vertex_requests_go_to_vertex_without_explicit_caching(self):
        kwargs = self._api_kwargs(VERTEX)
        self.assertEqual(kwargs["api_base"], gemini_providers.VERTEX_API_BASE)
        self.assertNotIn("cache_control", kwargs["messages"][0])

    def test_ai_studio_requests_are_unchanged(self):
        kwargs = self._api_kwargs(AI_STUDIO)
        self.assertNotIn("api_base", kwargs)
        self.assertIn("cache_control", kwargs["messages"][0])


class ProviderPanelTests(unittest.TestCase):
    def setUp(self):
        self.provider = AI_STUDIO
        self.keys = {AI_STUDIO: "ai-studio-key"}
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(
            patch.object(
                plugin.user_manager,
                "get_gemini_provider",
                side_effect=lambda uid: self.provider,
            )
        )
        stack.enter_context(
            patch.object(
                plugin.user_manager,
                "set_gemini_provider",
                side_effect=lambda uid, provider: setattr(self, "provider", provider),
            )
        )
        stack.enter_context(
            patch.object(
                plugin,
                "get_effective_api_key",
                side_effect=lambda uid, service: self.keys.get(service),
            )
        )
        stack.enter_context(
            patch.object(
                plugin.user_manager,
                "get_prefs",
                return_value=SimpleNamespace(model=plugin.GEMINI_FLASH_LATEST),
            )
        )
        self.request_key = stack.enter_context(
            patch.object(plugin.llm_db, "request_api_key_message", AsyncMock())
        )
        self.info = stack.enter_context(
            patch.object(plugin, "send_info_message", AsyncMock())
        )
        stack.enter_context(patch.object(plugin, "IS_BOT", True))

    def press(self, provider):
        event = SimpleNamespace(
            sender_id=USER_ID,
            data=f"{plugin.GEMINI_PROVIDER_CALLBACK_PREFIX}{provider}".encode(),
            edit=AsyncMock(),
            answer=AsyncMock(),
        )
        asyncio.run(plugin._gemini_provider_press_handler(event, provider))
        return event

    def test_switching_to_vertex_without_a_key_asks_for_one(self):
        event = self.press(VERTEX)
        self.assertEqual(self.provider, VERTEX)
        self.request_key.assert_awaited_once_with(event, VERTEX)
        text = event.edit.await_args.args[0]
        self.assertIn("Current: **Vertex AI**", text)
        self.assertIn("no key yet", text)

    def test_switching_warns_when_the_current_model_is_not_on_vertex(self):
        self.keys[VERTEX] = "vertex-key"
        event = self.press(VERTEX)
        self.request_key.assert_not_awaited()
        self.assertIn("is not available on Vertex AI", event.edit.await_args.args[0])

    def test_an_argument_switches_without_the_menu(self):
        self.keys[VERTEX] = "vertex-key"
        event = SimpleNamespace(
            sender_id=USER_ID,
            pattern_match=SimpleNamespace(group=lambda index: "Vertex"),
        )
        asyncio.run(plugin.gemini_provider_handler(event))
        self.assertEqual(self.provider, VERTEX)

    def test_an_unknown_argument_shows_the_usage(self):
        event = SimpleNamespace(
            sender_id=USER_ID,
            pattern_match=SimpleNamespace(group=lambda index: "azure"),
        )
        asyncio.run(plugin.gemini_provider_handler(event))
        self.assertEqual(self.provider, AI_STUDIO)
        self.assertIn("Usage", self.info.await_args.args[1])


class TitleTests(unittest.IsolatedAsyncioTestCase):
    async def test_vertex_titles_use_a_vertex_model_with_the_vertex_key(self):
        calls = []

        async def complete(prompt, schema, *, model, api_key, api_user_id, **extra):
            calls.append((model, api_key, extra))
            return schema(title="T")

        await title_util.generate_title(
            "prompt",
            plugin.topic_titles.TopicTitle,
            choice="auto",
            codex_p=False,
            api_keys={AI_STUDIO: "ai-studio-key", VERTEX: "vertex-key"},
            api_user_id=USER_ID,
            complete=complete,
            gemini_provider=VERTEX,
        )
        self.assertEqual(
            calls,
            [
                (
                    gemini_providers.VERTEX_TITLE_MODEL,
                    "vertex-key",
                    {"api_base": gemini_providers.VERTEX_API_BASE},
                )
            ],
        )


if __name__ == "__main__":
    unittest.main()
