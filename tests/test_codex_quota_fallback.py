import asyncio
import builtins
import importlib
import json
import unittest
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from uniborg.constants import (
    GEMINI_FLASH_LATEST,
    OPENAI_CODEX_ASTRA,
    OPENAI_CODEX_GPT_5_6_SOL,
    OR_OPENAI_5_6_SOL,
)


class _FakeLoop:
    def create_task(self, coro):
        coro.close()


builtins.borg = SimpleNamespace(loop=_FakeLoop())


async def _import_plugin():
    return importlib.import_module("llm_chat_plugins.llm_chat")


plugin = asyncio.run(_import_plugin())

NOW = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)
LATER = NOW + timedelta(days=2)


def _prefs(**overrides):
    return plugin.UserPrefs(**overrides)


def _armed_prefs(model=GEMINI_FLASH_LATEST, until=LATER):
    return _prefs(
        model=OPENAI_CODEX_GPT_5_6_SOL,
        codex_quota_fallback_model=model,
        codex_quota_fallback_until=int(until.timestamp()),
    )


class QuotaFallbackStorageTests(unittest.TestCase):
    def test_new_fields_survive_the_json_round_trip(self):
        #: Prefs are persisted with a plain json.dump, so a datetime field here
        #: would raise at save time. Epoch ints must stay epoch ints.
        prefs = _armed_prefs()
        dumped = prefs.model_dump(exclude_defaults=True)
        restored = plugin.UserPrefs.model_validate(json.loads(json.dumps(dumped)))
        self.assertEqual(restored.codex_quota_fallback_model, GEMINI_FLASH_LATEST)
        self.assertEqual(restored.codex_quota_fallback_until, int(LATER.timestamp()))

    def test_unset_fields_are_absent_from_the_dump(self):
        dumped = _prefs().model_dump(exclude_defaults=True)
        self.assertNotIn("codex_quota_fallback_model", dumped)
        self.assertNotIn("codex_quota_fallback_until", dumped)


class QuotaFallbackAccessorTests(unittest.TestCase):
    def read(self, prefs, *, now=NOW):
        with patch.object(
            plugin.user_manager, "get_prefs", return_value=prefs
        ), patch.object(plugin.user_manager, "_save_prefs") as save:
            record = plugin.user_manager.get_codex_quota_fallback(1, now=now)
        return record, save

    def test_active_record_is_returned(self):
        record, save = self.read(_armed_prefs())
        self.assertIsNotNone(record)
        self.assertEqual(record.model, GEMINI_FLASH_LATEST)
        self.assertEqual(record.until, LATER)
        save.assert_not_called()

    def test_expired_record_is_dropped_on_read(self):
        record, save = self.read(_armed_prefs(until=NOW - timedelta(minutes=1)))
        self.assertIsNone(record)
        save.assert_called_once()

    def test_unset_record_reads_as_none(self):
        record, save = self.read(_prefs())
        self.assertIsNone(record)
        save.assert_not_called()

    def test_bare_namespace_prefs_do_not_raise(self):
        #: test_codex_fallback_notice patches get_prefs to return SimpleNamespace();
        #: every new read must tolerate that.
        record, _ = self.read(SimpleNamespace())
        self.assertIsNone(record)

    def test_clearing_nothing_does_not_write(self):
        with patch.object(
            plugin.user_manager, "get_prefs", return_value=_prefs()
        ), patch.object(plugin.user_manager, "_save_prefs") as save:
            self.assertFalse(plugin.user_manager.clear_codex_quota_fallback(1))
        save.assert_not_called()

    def test_clearing_an_armed_record_writes_once(self):
        with patch.object(
            plugin.user_manager, "get_prefs", return_value=_armed_prefs()
        ), patch.object(plugin.user_manager, "_save_prefs") as save:
            self.assertTrue(plugin.user_manager.clear_codex_quota_fallback(1))
        save.assert_called_once()
        saved = save.call_args.args[1]
        self.assertIsNone(saved.codex_quota_fallback_model)
        self.assertIsNone(saved.codex_quota_fallback_until)


class RequestModelTests(unittest.TestCase):
    def resolve(self, prefs, *, chat_model=None, prefix_model=None):
        with patch.object(
            plugin.user_manager, "get_prefs", return_value=prefs
        ), patch.object(
            plugin.chat_manager, "get_model", return_value=chat_model
        ), patch.object(
            plugin.user_manager, "_save_prefs"
        ):
            return plugin._resolve_request_model(
                7, 1, prefix_model=prefix_model, now=NOW
            )

    def test_saved_personal_codex_default_is_redirected(self):
        resolved = self.resolve(_armed_prefs())
        self.assertEqual(resolved.model, GEMINI_FLASH_LATEST)
        self.assertEqual(resolved.service, "gemini")
        self.assertEqual(resolved.quota_fallback_from, OPENAI_CODEX_GPT_5_6_SOL)

    def test_chat_level_codex_model_is_redirected(self):
        prefs = _armed_prefs(model=OR_OPENAI_5_6_SOL)
        prefs.model = GEMINI_FLASH_LATEST
        resolved = self.resolve(prefs, chat_model=OPENAI_CODEX_ASTRA)
        self.assertEqual(resolved.model, OR_OPENAI_5_6_SOL)
        self.assertEqual(resolved.service, "openrouter")
        self.assertEqual(resolved.quota_fallback_from, OPENAI_CODEX_ASTRA)

    def test_explicit_prefix_is_never_redirected(self):
        resolved = self.resolve(_armed_prefs(), prefix_model=OPENAI_CODEX_ASTRA)
        self.assertEqual(resolved.model, OPENAI_CODEX_ASTRA)
        self.assertIsNone(resolved.quota_fallback_from)

    def test_persian_prefix_alias_is_never_redirected(self):
        detected = plugin._detect_and_process_message_prefix(".چه سلام", codex_p=True)
        resolved = self.resolve(_armed_prefs(), prefix_model=detected.model)
        self.assertEqual(resolved.model, detected.model)
        self.assertIsNone(resolved.quota_fallback_from)

    def test_non_codex_default_is_left_alone(self):
        prefs = _armed_prefs()
        prefs.model = GEMINI_FLASH_LATEST
        resolved = self.resolve(prefs)
        self.assertEqual(resolved.model, GEMINI_FLASH_LATEST)
        self.assertIsNone(resolved.quota_fallback_from)

    def test_expired_fallback_leaves_the_saved_codex_model_in_place(self):
        resolved = self.resolve(_armed_prefs(until=NOW - timedelta(seconds=1)))
        self.assertEqual(resolved.model, OPENAI_CODEX_GPT_5_6_SOL)
        self.assertIsNone(resolved.quota_fallback_from)


class ExplicitChoiceClearsFallbackTests(unittest.TestCase):
    def test_personal_choice_clears(self):
        with patch.object(plugin.user_manager, "set_model") as set_model, patch.object(
            plugin.user_manager, "clear_codex_quota_fallback"
        ) as clear:
            plugin._apply_personal_model_choice(1, GEMINI_FLASH_LATEST)
        set_model.assert_called_once_with(1, GEMINI_FLASH_LATEST)
        clear.assert_called_once_with(1)

    def test_chat_choice_clears_the_actors_fallback(self):
        with patch.object(plugin.chat_manager, "set_model") as set_model, patch.object(
            plugin.user_manager, "clear_codex_quota_fallback"
        ) as clear:
            plugin._apply_chat_model_choice(7, 1, model=None)
        set_model.assert_called_once_with(7, None)
        clear.assert_called_once_with(1)


class MissingFallbackKeyTests(unittest.TestCase):
    def run_request(self):
        event = SimpleNamespace(
            id=5,
            sender_id=123,
            chat_id=456,
            grouped_id=None,
            is_private=True,
            text="hello",
            file=None,
            message=SimpleNamespace(),
        )
        with ExitStack() as stack:
            enter = stack.enter_context
            enter(patch.object(plugin, "cleanup_completed_tasks"))
            enter(patch.object(plugin, "AWAITING_INPUT_FROM_USERS", {}))
            enter(patch.object(plugin.llm_db, "is_awaiting_key", return_value=False))
            enter(
                patch.object(
                    plugin.gemini_live_util.live_session_manager,
                    "is_live_mode_active",
                    return_value=False,
                )
            )
            enter(
                patch.object(plugin.util, "isAdmin", new=AsyncMock(return_value=True))
            )
            enter(
                patch.object(
                    plugin,
                    "_determine_context_mode_and_handle_transitions",
                    new=AsyncMock(return_value="recent"),
                )
            )
            enter(
                patch.object(
                    plugin,
                    "_resolve_request_model",
                    return_value=plugin.RequestModel(
                        model=OR_OPENAI_5_6_SOL,
                        service="openrouter",
                        quota_fallback_from=OPENAI_CODEX_GPT_5_6_SOL,
                    ),
                )
            )
            enter(patch.object(plugin, "get_effective_api_key", return_value=None))
            clear = enter(
                patch.object(plugin.user_manager, "clear_codex_quota_fallback")
            )
            prompt = enter(
                patch.object(plugin.llm_db, "request_api_key_message", new=AsyncMock())
            )
            info = enter(patch.object(plugin, "send_info_message", new=AsyncMock()))
            asyncio.run(plugin.chat_handler(event))
        return clear, prompt, info

    def test_unusable_stand_in_is_dropped_instead_of_prompting(self):
        clear, prompt, info = self.run_request()
        clear.assert_called_once_with(123)
        prompt.assert_not_awaited()
        self.assertIn("turned it off", info.await_args.args[1])
        self.assertIn(plugin.CODEX_SETTINGS_UNCHANGED, info.await_args.args[1])


if __name__ == "__main__":
    unittest.main()
