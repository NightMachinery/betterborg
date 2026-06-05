import re
import unittest

from uniborg import llm_db


class GeminiApiKeyValidationTests(unittest.TestCase):
    def setUp(self):
        self.regex = llm_db.API_KEY_CONFIG["gemini"]["regex"]

    def assertGeminiKeyAccepted(self, key: str):
        self.assertTrue(llm_db.validate_api_key_format("gemini", key))
        self.assertIsNotNone(re.fullmatch(self.regex, key))

    def assertGeminiKeyRejected(self, key: str):
        self.assertFalse(llm_db.validate_api_key_format("gemini", key))
        self.assertIsNone(re.fullmatch(self.regex, key))

    def test_accepts_legacy_aiza_style_key(self):
        self.assertGeminiKeyAccepted("AIza" + "A" * 35)

    def test_accepts_aq_style_key(self):
        self.assertGeminiKeyAccepted("AQ." + "AbCdEf0123456789_-" * 3)

    def test_rejects_too_short_key(self):
        self.assertGeminiKeyRejected("AQ.short")

    def test_rejects_spaces_and_newlines(self):
        self.assertGeminiKeyRejected("AQ." + "A" * 25 + " with-space")
        self.assertGeminiKeyRejected("AQ." + "A" * 25 + "\n")

    def test_rejects_shell_punctuation_and_prose(self):
        self.assertGeminiKeyRejected("AQ." + "A" * 25 + "$")
        self.assertGeminiKeyRejected("this is not a gemini api key")

    def test_other_provider_prefixes_remain_invalid_for_gemini(self):
        self.assertGeminiKeyRejected("sk-or-v1-" + "A" * 40)
        self.assertGeminiKeyRejected("sk-" + "a" * 40)


class ApiKeyCommandPatternTests(unittest.TestCase):
    def setUp(self):
        self.valid_key = "AQ." + "AbCdEf0123456789_-" * 3
        self.pattern = llm_db.gemini_api_key_command_pattern(bot_username="@SomeBot")

    def assertCommandMatches(self, text: str, expected_key: str | None):
        match = re.match(self.pattern, text)
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), expected_key)

    def test_gemini_command_matches_inline_key_and_case(self):
        self.assertCommandMatches(f"/setgeminikey {self.valid_key}", self.valid_key)
        self.assertCommandMatches(f"/setGeminiKey {self.valid_key}", self.valid_key)

    def test_gemini_command_matches_optional_bot_username(self):
        self.assertCommandMatches(
            f"/setgeminikey@SomeBot {self.valid_key}", self.valid_key
        )

    def test_gemini_command_matches_without_inline_key(self):
        self.assertCommandMatches("/setgeminikey", None)
        self.assertCommandMatches("/setgeminikey@SomeBot", None)

    def test_gemini_command_rejects_embedded_text(self):
        self.assertIsNone(
            re.match(self.pattern, f"please /setgeminikey {self.valid_key}")
        )

    def test_trailing_prose_is_captured_for_central_validation(self):
        match = re.match(self.pattern, f"/setgeminikey {self.valid_key} extra prose")
        self.assertIsNotNone(match)
        self.assertFalse(llm_db.validate_api_key_format("gemini", match.group(1)))


if __name__ == "__main__":
    unittest.main()
