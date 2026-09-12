import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from uniborg import llm_chat_config
from uniborg import llm_models
from uniborg.constants import OPENAI_CODEX_GPT_5_6_SOL


class Event:
    def __init__(self, sender_id):
        self.sender_id = sender_id


class LLMChatConfigTests(unittest.TestCase):
    def test_missing_config_is_created_with_documented_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".borg" / "llm_chat_config.json5"
            config = llm_chat_config.LLMChatConfigLoader(path).load()
            self.assertEqual(config.codex_allowed_users, ("MAGIC_ADMINS",))
            self.assertEqual(config.codex_imagegen_allowed_users, ("MAGIC_ADMINS",))
            self.assertIn("// Betterborg", path.read_text())

    def test_json5_comments_unquoted_keys_and_trailing_commas(self):
        config = llm_chat_config.parse_config(
            """{
              // regular Codex access
              codex_allowed_users: [123, "MAGIC_ADMINS",],
              /* image policy */
              codex_imagegen_allowed_users: [456,],
            }"""
        )
        self.assertEqual(config.codex_allowed_users, (123, "MAGIC_ADMINS"))
        self.assertEqual(config.codex_imagegen_allowed_users, (456,))

    def test_empty_lists_grant_nobody(self):
        config = llm_chat_config.parse_config(
            '{codex_allowed_users: [], codex_imagegen_allowed_users: []}'
        )
        with mock.patch("uniborg.util.isAdmin", new=mock.AsyncMock(return_value=True)):
            self.assertFalse(asyncio.run(llm_chat_config.can_use_codex(Event(1), config)))
            self.assertFalse(
                asyncio.run(llm_chat_config.can_use_codex_imagegen(Event(1), config))
            )

    def test_explicit_id_does_not_consult_admin_status(self):
        config = llm_chat_config.LLMChatConfig((123,), (123,))
        with mock.patch("uniborg.util.isAdmin", new=mock.AsyncMock()) as is_admin:
            self.assertTrue(asyncio.run(llm_chat_config.can_use_codex(Event(123), config)))
            self.assertTrue(
                asyncio.run(llm_chat_config.can_use_codex_imagegen(Event(123), config))
            )
            is_admin.assert_not_awaited()

    def test_sentinel_includes_is_admin_trusted_chat_behavior(self):
        config = llm_chat_config.LLMChatConfig(
            ("MAGIC_ADMINS",), ("MAGIC_ADMINS",)
        )
        with mock.patch("uniborg.util.isAdmin", new=mock.AsyncMock(return_value=True)):
            self.assertTrue(asyncio.run(llm_chat_config.can_use_codex(Event(999), config)))

    def test_image_generation_requires_both_policies(self):
        event = Event(123)
        cases = (
            ((123,), (123,), True),
            ((123,), (), False),
            ((), (123,), False),
            ((), (), False),
        )
        for codex, imagegen, expected in cases:
            config = llm_chat_config.LLMChatConfig(codex, imagegen)
            self.assertEqual(
                asyncio.run(llm_chat_config.can_use_codex_imagegen(event, config)),
                expected,
            )

    def test_invalid_config_disables_access_and_is_not_rewritten(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json5"
            original = "{codex_allowed_users: [false]}"
            path.write_text(original)
            with self.assertLogs(llm_chat_config.logger, level="ERROR"):
                config = llm_chat_config.LLMChatConfigLoader(path).load()
            self.assertFalse(config.valid)
            self.assertEqual(path.read_text(), original)

    def test_loader_reloads_after_file_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json5"
            path.write_text(
                '{codex_allowed_users: [1], codex_imagegen_allowed_users: []}'
            )
            loader = llm_chat_config.LLMChatConfigLoader(path)
            self.assertEqual(loader.load().codex_allowed_users, (1,))
            path.write_text(
                '{codex_allowed_users: [22], codex_imagegen_allowed_users: []}'
            )
            self.assertEqual(loader.load().codex_allowed_users, (22,))

    def test_previously_valid_config_becomes_deny_all_when_unreadable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json5"
            path.write_text(
                '{codex_allowed_users: [1], codex_imagegen_allowed_users: [1]}'
            )
            loader = llm_chat_config.LLMChatConfigLoader(path)
            self.assertTrue(loader.load().valid)
            with mock.patch.object(Path, "read_text", side_effect=PermissionError("denied")):
                with self.assertLogs(llm_chat_config.logger, level="ERROR"):
                    self.assertFalse(loader.load().valid)

    def test_missing_keys_and_invalid_entries_are_rejected(self):
        invalid = (
            "{}",
            '{codex_allowed_users: [], codex_imagegen_allowed_users: ["admin"]}',
            '{codex_allowed_users: true, codex_imagegen_allowed_users: []}',
        )
        for text in invalid:
            with self.assertRaises(ValueError):
                llm_chat_config.parse_config(text)

    def test_codex_models_are_separate_from_admin_models(self):
        self.assertNotIn(OPENAI_CODEX_GPT_5_6_SOL, llm_models.admin_model_choices())
        self.assertIn(OPENAI_CODEX_GPT_5_6_SOL, llm_models.codex_model_choices())
        self.assertNotIn("Admin", llm_models.codex_model_choices()[OPENAI_CODEX_GPT_5_6_SOL])



if __name__ == "__main__":
    unittest.main()
