import asyncio
import builtins
import importlib
import unittest

from uniborg import llm_util
from uniborg.constants import (
    PIONEER_BASE_URL,
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

    def test_prepare_pioneer_api_kwargs_rewrites_for_openai_compatible_litellm(self):
        prepared = self.llm_chat.prepare_pioneer_api_kwargs(
            {
                "model": PIONEER_GPT_5_5,
                "messages": [{"role": "user", "content": "hi"}],
                "api_key": "pio_test_key",
                "stream": True,
            },
            reasoning_effort="high",
        )

        self.assertEqual(prepared["model"], "openai/gpt-5.5")
        self.assertEqual(prepared["base_url"], PIONEER_BASE_URL)
        self.assertIs(prepared["store"], False)
        self.assertEqual(prepared["extra_headers"], {"X-API-Key": "pio_test_key"})
        self.assertEqual(prepared["reasoning_effort"], "high")
        self.assertIn("reasoning_effort", prepared["allowed_openai_params"])

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
