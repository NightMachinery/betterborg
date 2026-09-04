import asyncio
import builtins
import importlib
import unittest
from unittest import mock

from uniborg import llm_util
from uniborg import pioneer_util
from uniborg.constants import (
    GEMINI_FLASH_LATEST,
    PIONEER_GPT_5_5,
    PIONEER_OPUS_4_8,
    PIONEER_SONNET_4_6,
)


#: IDs with no stored preferences, so the resolver falls through to defaults.
_UNUSED_CHAT_ID = -100999000555
_UNUSED_USER_ID = 999000555


class _FakeLoop:
    def create_task(self, coro):
        coro.close()
        return None


class _FakeBorg:
    loop = _FakeLoop()


_llm_chat = None


def _load_llm_chat():
    global _llm_chat
    if _llm_chat is not None:
        return _llm_chat

    builtins.borg = _FakeBorg()

    async def _import_module():
        return importlib.import_module("llm_chat_plugins.llm_chat")

    _llm_chat = asyncio.run(_import_module())
    return _llm_chat


class PioneerBackendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.llm_chat = _load_llm_chat()

    def test_pioneer_models_are_not_offered_in_the_menus(self):
        #: Pioneer is no longer used, so it is commented out of the registry.
        #: The backend below stays wired up so it can be re-enabled.
        for model in (PIONEER_OPUS_4_8, PIONEER_GPT_5_5, PIONEER_SONNET_4_6):
            self.assertNotIn(model, self.llm_chat.ADMIN_MODEL_CHOICES)
            self.assertNotIn(model, self.llm_chat.MODEL_CHOICES)

    def test_pioneer_models_are_admin_only(self):
        self.assertTrue(self.llm_chat._is_admin_only_model(PIONEER_GPT_5_5))
        self.assertTrue(self.llm_chat._is_admin_only_model("pioneer/custom-model"))

    def test_pioneer_model_maps_to_pioneer_service(self):
        self.assertEqual(llm_util.get_service_from_model(PIONEER_GPT_5_5), "pioneer")

    def test_prepare_pioneer_response_kwargs_uses_responses_api_shape(self):
        instructions, input_messages = pioneer_util.messages_to_pioneer_responses(
            [
                {"role": "system", "content": "be helpful"},
                {"role": "user", "content": "hi"},
            ]
        )
        prepared = pioneer_util.prepare_pioneer_response_kwargs(
            model=PIONEER_GPT_5_5,
            instructions=instructions,
            input_messages=input_messages,
            reasoning_effort="high",
            tools=[{"type": "web_search"}],
        )

        self.assertEqual(prepared["model"], "gpt-5.5")
        self.assertEqual(prepared["instructions"], "be helpful")
        self.assertEqual(prepared["input"], [{"role": "user", "content": "hi"}])
        self.assertIs(prepared["store"], False)
        self.assertIs(prepared["stream"], True)
        self.assertEqual(prepared["reasoning"], {"effort": "high"})
        self.assertEqual(prepared["tools"], [{"type": "web_search"}])

    def test_pioneer_tools_map_google_search_only(self):
        self.assertEqual(
            self.llm_chat.pioneer_tools_from_enabled(["googleSearch"]),
            [{"type": "web_search"}],
        )
        self.assertEqual(
            self.llm_chat.pioneer_tools_from_enabled(["urlContext", "codeExecution"]),
            [],
        )
        self.assertEqual(
            self.llm_chat.pioneer_tools_from_enabled(
                ["googleSearch", "urlContext", "codeExecution"]
            ),
            [{"type": "web_search"}],
        )

    def test_pioneer_prefixes_are_disabled(self):
        #: `.sn` and `.o` are commented out along with the models, so they are
        #: plain text again for admins and non-admins alike.
        for admin_p in (True, False):
            for text in (".sn hello", ".o hello"):
                result = self.llm_chat._detect_and_process_message_prefix(
                    text, admin_p=admin_p
                )
                self.assertIsNone(result.model)
                self.assertIsNone(result.reasoning_effort)
                self.assertEqual(result.processed_text, text)

    def test_recent_context_prefix_still_combines_with_model_prefix(self):
        result = self.llm_chat._detect_and_process_message_prefix(
            ".s .f hello", admin_p=True
        )

        self.assertEqual(result.model, GEMINI_FLASH_LATEST)
        self.assertEqual(result.processed_text, ".s hello")

    def _resolve(self, model, **kwargs):
        return self.llm_chat._get_effective_reasoning(
            _UNUSED_CHAT_ID, _UNUSED_USER_ID, model=model, **kwargs
        )

    def test_pioneer_reasoning_defaults_to_medium(self):
        for model in (PIONEER_OPUS_4_8, PIONEER_GPT_5_5, PIONEER_SONNET_4_6):
            resolution = self._resolve(model)
            self.assertEqual(resolution.level, "medium")
            self.assertEqual(resolution.source, "model_default")

    def test_pioneer_reasoning_respects_prefix_and_preference(self):
        resolution = self._resolve(PIONEER_GPT_5_5, prefix_effort="high")
        self.assertEqual(resolution.level, "high")
        self.assertEqual(resolution.source, "prefix")

        with mock.patch.object(
            self.llm_chat.user_manager, "get_thinking", return_value="low"
        ):
            resolution = self._resolve(PIONEER_GPT_5_5)
            self.assertEqual(resolution.level, "low")
            self.assertEqual(resolution.source, "personal")

            #: a per-message prefix still wins over the stored preference
            resolution = self._resolve(PIONEER_GPT_5_5, prefix_effort="high")
            self.assertEqual(resolution.level, "high")
            self.assertEqual(resolution.source, "prefix")

    def test_pioneer_models_remain_admin_gated_if_typed_directly(self):
        self.assertTrue(self.llm_chat._is_admin_only_model(PIONEER_GPT_5_5))

    def test_pioneer_reasoning_ignores_levels_the_model_rejects(self):
        #: `max` is not offered by Pioneer, so a preference carried over from
        #: another model must not reach the API.
        with mock.patch.object(
            self.llm_chat.user_manager, "get_thinking", return_value="max"
        ):
            resolution = self._resolve(PIONEER_OPUS_4_8)
            self.assertEqual(resolution.level, "medium")
            self.assertEqual(resolution.source, "model_default")


if __name__ == "__main__":
    unittest.main()
