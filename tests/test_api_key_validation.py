import re
import unittest

from uniborg.llm_db import API_KEY_CONFIG


class GeminiApiKeyValidationTests(unittest.TestCase):
    def setUp(self):
        self.regex = API_KEY_CONFIG["gemini"]["regex"]

    def assertGeminiKeyAccepted(self, key: str):
        self.assertIsNotNone(re.match(self.regex, key))

    def assertGeminiKeyRejected(self, key: str):
        self.assertIsNone(re.match(self.regex, key))

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


if __name__ == "__main__":
    unittest.main()
