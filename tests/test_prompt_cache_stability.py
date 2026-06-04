import asyncio
import builtins
import importlib
import re
import unittest

from uniborg import codex_util
from uniborg.constants import OPENAI_CODEX_GPT_5_5


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


class RuntimeContextPlacementTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.llm_chat = _load_llm_chat()

    def test_system_prompt_default_excludes_current_datetime(self):
        class Event:
            sender_id = 1
            chat_id = 2
            is_private = True

        info = self.llm_chat.get_system_prompt_info(Event())

        self.assertNotIn("Current date and time", info.effective_prompt)

    def test_runtime_context_appends_to_latest_string_user_turn(self):
        history = [
            {"role": "system", "content": "stable"},
            {"role": "user", "content": "hello"},
        ]

        self.llm_chat.append_runtime_context_to_latest_user_message(history, "now")

        self.assertEqual(history[1]["content"], "hello\n\n---\nRuntime context:\nnow")

    def test_runtime_context_appends_final_multimodal_text_part(self):
        history = [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}}
                ],
            }
        ]

        self.llm_chat.append_runtime_context_to_latest_user_message(history, "now")

        self.assertEqual(history[0]["content"][-1], {"type": "text", "text": "---\nRuntime context:\nnow"})

    def test_runtime_context_adds_user_turn_when_missing(self):
        history = [{"role": "system", "content": "stable"}]

        self.llm_chat.append_runtime_context_to_latest_user_message(history, "now")

        self.assertEqual(history[-1], {"role": "user", "content": "Runtime context:\nnow"})


class CodexPromptCacheHintTests(unittest.TestCase):
    def test_codex_prompt_cache_key_is_stable_and_non_raw_id(self):
        key1 = codex_util.codex_prompt_cache_key(
            model=OPENAI_CODEX_GPT_5_5, chat_id=123, user_id=456
        )
        key2 = codex_util.codex_prompt_cache_key(
            model=OPENAI_CODEX_GPT_5_5, chat_id=123, user_id=456
        )

        self.assertEqual(key1, key2)
        self.assertRegex(key1, r"^bb-codex-[0-9a-f]{32}$")
        self.assertNotIn("123", key1)
        self.assertNotIn("456", key1)

    def test_codex_response_kwargs_include_cache_hints(self):
        kwargs = codex_util.prepare_codex_response_kwargs(
            model=OPENAI_CODEX_GPT_5_5,
            instructions="stable",
            input_messages=[{"role": "user", "content": "hello"}],
            prompt_cache_key="bb-codex-test",
            prompt_cache_retention="24h",
        )

        self.assertIs(kwargs["store"], False)
        self.assertEqual(kwargs["prompt_cache_key"], "bb-codex-test")
        self.assertEqual(kwargs["prompt_cache_retention"], "24h")

    def test_codex_prompt_cache_key_varies_by_scope(self):
        key1 = codex_util.codex_prompt_cache_key(
            model=OPENAI_CODEX_GPT_5_5, chat_id=123, user_id=456
        )
        key2 = codex_util.codex_prompt_cache_key(
            model=OPENAI_CODEX_GPT_5_5, chat_id=123, user_id=789
        )

        self.assertNotEqual(key1, key2)


if __name__ == "__main__":
    unittest.main()
