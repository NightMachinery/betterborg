import asyncio
import builtins
import importlib
import re
import unittest

from uniborg import codex_util
from uniborg.constants import (
    GEMINI_FLASH_LATEST,
    GEMINI_FLASH_LITE_LATEST,
    OPENAI_CODEX_GPT_5_5,
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


class ModelPrefixAfterMentionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.llm_chat = _load_llm_chat()

    def setUp(self):
        self.original_bot_username = self.llm_chat.BOT_USERNAME
        self.llm_chat.BOT_USERNAME = "@vlm_chat_bot"

    def tearDown(self):
        self.llm_chat.BOT_USERNAME = self.original_bot_username

    def _detect_after_leading_mention_strip(self, text: str):
        normalized = self.llm_chat.strip_leading_bot_username(text)
        return self.llm_chat._detect_and_process_message_prefix(normalized)

    def test_leading_mention_then_flash_prefix(self):
        result = self._detect_after_leading_mention_strip("@vlm_chat_bot .f hello")

        self.assertEqual(result.model, GEMINI_FLASH_LATEST)
        self.assertEqual(result.processed_text, "hello")

    def test_leading_mention_then_flash_lite_prefix(self):
        result = self._detect_after_leading_mention_strip("@vlm_chat_bot .fl hello")

        self.assertEqual(result.model, GEMINI_FLASH_LITE_LATEST)
        self.assertEqual(result.processed_text, "hello")

    def test_leading_mention_recent_mode_keeps_existing_prefix_behavior(self):
        result = self._detect_after_leading_mention_strip("@vlm_chat_bot .s .f hello")

        self.assertEqual(result.model, GEMINI_FLASH_LATEST)
        self.assertEqual(result.processed_text, ".s hello")

    def test_non_leading_mention_does_not_enable_prefix(self):
        result = self._detect_after_leading_mention_strip("hello @vlm_chat_bot .f")

        self.assertIsNone(result.model)
        self.assertEqual(result.processed_text, "hello @vlm_chat_bot .f")

    def test_direct_prefix_still_works(self):
        result = self.llm_chat._detect_and_process_message_prefix(".f hello")

        self.assertEqual(result.model, GEMINI_FLASH_LATEST)
        self.assertEqual(result.processed_text, "hello")


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

    def test_codex_response_kwargs_include_supported_cache_hint(self):
        kwargs = codex_util.prepare_codex_response_kwargs(
            model=OPENAI_CODEX_GPT_5_5,
            instructions="stable",
            input_messages=[{"role": "user", "content": "hello"}],
            prompt_cache_key="bb-codex-test",
        )

        self.assertIs(kwargs["store"], False)
        self.assertEqual(kwargs["prompt_cache_key"], "bb-codex-test")
        self.assertNotIn("prompt_cache_retention", kwargs)

    def test_codex_prompt_cache_key_is_chat_scoped_not_user_scoped(self):
        key1 = codex_util.codex_prompt_cache_key(
            model=OPENAI_CODEX_GPT_5_5, chat_id=123, user_id=456
        )
        key2 = codex_util.codex_prompt_cache_key(
            model=OPENAI_CODEX_GPT_5_5, chat_id=123, user_id=789
        )
        key3 = codex_util.codex_prompt_cache_key(
            model=OPENAI_CODEX_GPT_5_5, chat_id=999, user_id=456
        )

        self.assertEqual(key1, key2)
        self.assertNotEqual(key1, key3)


if __name__ == "__main__":
    unittest.main()
