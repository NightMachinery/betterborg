import asyncio
import builtins
import importlib
import unittest

from uniborg import llm_util
from uniborg import pioneer_util
from uniborg.constants import (
    PIONEER_GPT_5_5,
    PIONEER_OPUS_4_8,
    PIONEER_SONNET_4_6,
)


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

    def test_pioneer_models_are_admin_choices_only(self):
        for model in (PIONEER_OPUS_4_8, PIONEER_GPT_5_5, PIONEER_SONNET_4_6):
            self.assertIn(model, self.llm_chat.ADMIN_MODEL_CHOICES)
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
            self.llm_chat.pioneer_tools_from_enabled(
                ["urlContext", "codeExecution"]
            ),
            [],
        )
        self.assertEqual(
            self.llm_chat.pioneer_tools_from_enabled(
                ["googleSearch", "urlContext", "codeExecution"]
            ),
            [{"type": "web_search"}],
        )

    def test_pioneer_reasoning_defaults_to_medium(self):
        self.assertEqual(self.llm_chat._pioneer_reasoning_effort(None, None), "medium")
        self.assertEqual(
            self.llm_chat._pioneer_reasoning_effort(None, "disable"), "medium"
        )

    def test_pioneer_reasoning_respects_prefix_and_preference(self):
        self.assertEqual(self.llm_chat._pioneer_reasoning_effort(None, "low"), "low")
        self.assertEqual(
            self.llm_chat._pioneer_reasoning_effort("xhigh", "low"), "xhigh"
        )


if __name__ == "__main__":
    unittest.main()
