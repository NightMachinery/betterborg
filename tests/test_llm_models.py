"""`llm_models`: the short names of reasoning levels and the emoji of models."""

import unittest

from uniborg import llm_models
from uniborg.constants import GEMINI_FLASH_LITE_LATEST, OPENAI_CODEX_LUNA_RESERVE


class ReasoningLevelAliasTests(unittest.TestCase):
    def test_each_level_has_its_prefix_suffix(self):
        self.assertEqual(
            {
                level: llm_models.reasoning_level_alias(level)
                for level in llm_models.OPENAI_REASONING_LEVELS
            },
            {
                "none": "n",
                "low": "l",
                "medium": "m",
                "high": "h",
                "xhigh": "x",
                "max": "xx",
            },
        )

    def test_gemini_disable_shows_as_none(self):
        self.assertEqual(llm_models.reasoning_level_alias("disable"), "n")

    def test_no_level_has_no_alias(self):
        self.assertEqual(llm_models.reasoning_level_alias(None), "")

    def test_a_level_without_an_alias_shows_in_full(self):
        self.assertEqual(llm_models.reasoning_level_alias("minimal"), "minimal")


class ModelEmojiTests(unittest.TestCase):
    def test_registered_models_have_distinct_emoji(self):
        emoji = [spec.emoji for spec in llm_models.MODEL_SPECS]

        self.assertNotIn("🤖", emoji)
        self.assertEqual(len(emoji), len(set(emoji)))

    def test_registered_models(self):
        self.assertEqual(llm_models.model_emoji(OPENAI_CODEX_LUNA_RESERVE), "🌙")
        self.assertEqual(llm_models.model_emoji(GEMINI_FLASH_LITE_LATEST), "🪶")

    def test_custom_ids_get_their_provider_emoji(self):
        for model, emoji in (
            ("openai-codex/gpt-9", "🔷"),
            ("gemini/gemini-9-ultra", "♊"),
            ("openrouter/vendor/model", "🔀"),
            ("somebody/else", "🤖"),
            (None, "🤖"),
        ):
            with self.subTest(model=model):
                self.assertEqual(llm_models.model_emoji(model), emoji)


if __name__ == "__main__":
    unittest.main()
