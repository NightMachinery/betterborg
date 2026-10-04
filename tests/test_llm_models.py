"""`llm_models`: the symbols of reasoning levels and the emoji of models."""

import unittest

from uniborg import llm_models
from uniborg.constants import (
    GEMINI_FLASH_LITE_LATEST,
    OPENAI_CODEX_LUNA,
    OPENAI_CODEX_LUNA_RESERVE,
    OPENAI_CODEX_SOL,
)


class ReasoningLevelSymbolTests(unittest.TestCase):
    def test_each_level_has_a_circle_that_fills_with_it(self):
        symbols = [
            llm_models.reasoning_level_symbol(level)
            for level in ("none", "low", "medium", "high", "xhigh", "max")
        ]

        self.assertEqual(symbols, ["○", "◔", "◑", "◕", "●", "◉"])

    def test_every_codex_level_has_a_symbol(self):
        for level in llm_models.OPENAI_REASONING_LEVELS:
            with self.subTest(level=level):
                self.assertTrue(llm_models.reasoning_level_symbol(level))

    def test_gemini_disable_shows_as_none(self):
        self.assertEqual(llm_models.reasoning_level_symbol("disable"), "○")

    def test_no_level_has_no_symbol(self):
        self.assertEqual(llm_models.reasoning_level_symbol(None), "")

    def test_an_unknown_level_has_no_symbol(self):
        self.assertEqual(llm_models.reasoning_level_symbol("minimal"), "")

    def test_no_symbol_is_a_model_emoji(self):
        emojis = {spec.emoji for spec in llm_models.MODEL_SPECS_BY_ID.values()}

        self.assertFalse(emojis & set(llm_models.REASONING_LEVEL_SYMBOLS.values()))


class CodexModelTests(unittest.TestCase):
    def test_each_retired_model_maps_to_a_registered_one(self):
        for old, new in llm_models.RETIRED_MODELS.items():
            with self.subTest(old=old):
                self.assertNotIn(old, llm_models.MODEL_SPECS_BY_ID)
                self.assertIn(new, llm_models.MODEL_SPECS_BY_ID)
                self.assertEqual(llm_models.current_model_id(old), new)

    def test_other_ids_are_their_own_current_id(self):
        for model in (OPENAI_CODEX_SOL, "x/y", "", None):
            with self.subTest(model=model):
                self.assertEqual(llm_models.current_model_id(model), model)

    def test_sol_rejects_none_and_luna_takes_it(self):
        self.assertFalse(
            llm_models.spec_for_model(OPENAI_CODEX_SOL).supports_level_p("none")
        )
        self.assertTrue(
            llm_models.spec_for_model(OPENAI_CODEX_SOL).supports_level_p("max")
        )
        self.assertTrue(
            llm_models.spec_for_model(OPENAI_CODEX_LUNA).supports_level_p("none")
        )


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


#: Telegram's default topic icons (`inputStickerSetEmojiDefaultTopicIcons`),
#: the only icons a bot may set, as a canary bot read them on 2026-10-04,
#: without U+FE0F.
DEFAULT_TOPIC_ICONS = set(
    "📰 💡 ⚡ 🎙 🔝 🗣 🆒 ❗ 📝 📆 📁 🔎 📣 🔥 ❤ ❓ 📈 📉 💎 💰 💸 🪙 💱 ⁉ 🎮 💻 📱"
    " 🚗 🏠 💘 🎉 ‼ 🏆 🏁 🎬 🎵 🔞 📚 👑 ⚽ 🏀 📺 👀 🫦 🍓 💄 👠 ✈ 🧳 🏖 ⛅ 🦄 🛍 👜"
    " 🛒 🚂 🛥 🏔 🏕 🤖 🪩 🎟 🏴\u200d☠ 🗳 🎓 🔭 🔬 🎶 🎤 🕺 💃 🪖 💼 🧪"
    " 👨\u200d👩\u200d👧\u200d👦 👶 🤰 💅 🏛 🧮 🖨 👮\u200d♂ 🩺 💊 💉 🧼 🪪 🛃 🍽 🐟 🎨"
    " 🎭 🎩 🔮 🍹 🎂 ☕ 🍣 🍔 🍕 🦠 💬 🎄 🎃 ✍ ⭐ ✅ 🎖 🤡 🧠 🦮 🐈".split()
)


class TopicIconTests(unittest.TestCase):
    def test_every_model_has_an_icon_a_bot_may_set(self):
        models = [spec.id for spec in llm_models.MODEL_SPECS] + [
            "openai-codex/gpt-9",
            "pioneer/model",
            "gemini/gemini-9-ultra",
            "openrouter/vendor/model",
            "somebody/else",
            None,
        ]
        for model in models:
            with self.subTest(model=model):
                self.assertIn(llm_models.topic_icon_emoji(model), DEFAULT_TOPIC_ICONS)

    def test_an_emoji_in_the_set_is_its_own_icon(self):
        self.assertEqual(llm_models.topic_icon_emoji(GEMINI_FLASH_LITE_LATEST), "💡")
        self.assertEqual(llm_models.topic_icon_emoji(None), "🤖")
        for spec in llm_models.MODEL_SPECS:
            plain = spec.emoji.replace("\ufe0f", "")
            if plain in DEFAULT_TOPIC_ICONS:
                with self.subTest(model=spec.id):
                    self.assertEqual(llm_models.topic_icon_emoji(spec.id), plain)

    def test_luna_gets_a_stand_in(self):
        self.assertEqual(llm_models.topic_icon_emoji(OPENAI_CODEX_LUNA_RESERVE), "🔮")


if __name__ == "__main__":
    unittest.main()
