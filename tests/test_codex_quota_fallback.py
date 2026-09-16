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
    OPENAI_CODEX_LUNA_RESERVE,
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


def _as_text(data) -> str:
    """Callback payloads are bytes in Telethon and str under the test stub."""
    return data.decode("utf-8") if isinstance(data, bytes) else data


def _usage(*, reserve=True, reserve_allowed=False, primary_allowed=False):
    additional = ()
    if reserve:
        additional = (
            plugin.codex_util.CodexMeter(
                name="gpt-reserve",
                allowed=reserve_allowed,
                used_percent=100,
                resets_at=NOW + timedelta(days=5),
            ),
        )
    return plugin.codex_util.CodexUsage(
        plan_type="prolite",
        primary=plugin.codex_util.CodexMeter(
            name="primary",
            allowed=primary_allowed,
            used_percent=100,
            resets_at=NOW + timedelta(days=2),
        ),
        additional=additional,
    )


class QuotaPanelTests(unittest.TestCase):
    quota = None

    def setUp(self):
        self.quota = plugin.codex_util.CodexUsageLimit(
            plan_type="prolite", resets_at=NOW + timedelta(days=2)
        )

    def panel(self, *, keys=("gemini", "openrouter"), **kwargs):
        with patch.object(
            plugin,
            "get_effective_api_key",
            side_effect=lambda user_id, service: "k" if service in keys else None,
        ):
            return plugin._codex_quota_panel(123, now=NOW, **kwargs)

    def test_both_keys_present_offers_both_stand_ins(self):
        panel = self.panel(quota=self.quota, usage=_usage())
        flat = [button for row in panel.buttons for button in row]
        self.assertEqual(len(flat), 2)
        self.assertIn("Codex Usage Limit Reached", panel.text)

    def test_one_key_offers_one_and_names_the_missing_command(self):
        panel = self.panel(quota=self.quota, usage=_usage(), keys=("gemini",))
        flat = [button for row in panel.buttons for button in row]
        self.assertEqual(len(flat), 1)
        self.assertIn("/setOpenRouterKey", panel.text)

    def test_no_keys_offers_nothing_and_explains(self):
        panel = self.panel(quota=self.quota, usage=_usage(), keys=())
        self.assertIsNone(panel.buttons)
        self.assertIn("No stand-in available", panel.text)
        self.assertIn("/setGeminiKey", panel.text)
        self.assertIn("/setOpenRouterKey", panel.text)

    def test_reserve_line_is_shown_only_when_the_account_has_one(self):
        with_reserve = self.panel(quota=self.quota, usage=_usage())
        self.assertIn("Luna Reserve", with_reserve.text)
        without = self.panel(quota=self.quota, usage=_usage(reserve=False))
        self.assertNotIn("Luna Reserve", without.text)

    def test_the_reserve_offer_joins_the_stand_in_buttons(self):
        panel = self.panel(
            quota=self.quota,
            usage=_usage(reserve_allowed=True),
            source_message_id=99,
        )
        labels = [button.text for row in panel.buttons for button in row]
        self.assertIn("🌙 Answer this from the Luna Reserve", labels)

        #: A spent Reserve has nothing to offer, so the button stays away.
        spent = self.panel(quota=self.quota, usage=_usage(), source_message_id=99)
        spent_labels = [button.text for row in spent.buttons for button in row]
        self.assertNotIn("🌙 Answer this from the Luna Reserve", spent_labels)

    def test_only_an_active_rule_wears_the_active_icon(self):
        #: `🔁` is a state, not an offer: a button wearing it while nothing had
        #: switched is what made the panel read as though it already had.
        offers = self.panel(quota=self.quota, usage=_usage())
        for row in offers.buttons:
            for button in row:
                self.assertNotIn("🔁", button.text)

        fallback = plugin.CodexQuotaFallback(model=GEMINI_FLASH_LATEST, until=LATER)
        labels = [b.text for row in self.panel(fallback=fallback).buttons for b in row]
        active = [label for label in labels if "🔁" in label]
        self.assertEqual(len(active), 1)
        self.assertIn(plugin._model_display_name(GEMINI_FLASH_LATEST), active[0])

    def test_the_active_stand_in_keeps_a_button_rather_than_vanishing(self):
        #: It used to be dropped from the offers, which left the rule in force
        #: visible nowhere among the switches.
        fallback = plugin.CodexQuotaFallback(model=GEMINI_FLASH_LATEST, until=LATER)
        buttons = [b for row in self.panel(fallback=fallback).buttons for b in row]
        self.assertIn("cq:a:123", [_as_text(b.data) for b in buttons])

    def test_the_luna_reserve_is_offered_beside_the_vendor_stand_ins(self):
        panel = self.panel(quota=self.quota, usage=_usage(reserve_allowed=True))
        labels = [b.text for row in panel.buttons for b in row]
        self.assertTrue(
            any("Luna Reserve" in label and "until reset" in label for label in labels),
            labels,
        )

    def test_a_spent_or_absent_reserve_is_not_offered_as_a_stand_in(self):
        for usage in (_usage(), _usage(reserve=False)):
            with self.subTest(usage=usage):
                panel = self.panel(quota=self.quota, usage=usage)
                labels = [b.text for row in panel.buttons for b in row]
                self.assertFalse(
                    any("Luna Reserve" in label for label in labels), labels
                )
                #: ...and not listed as something a key would fix, either.
                self.assertNotIn("Luna Reserve —", panel.text)

    def test_active_fallback_offers_an_undo(self):
        fallback = plugin.CodexQuotaFallback(model=GEMINI_FLASH_LATEST, until=LATER)
        panel = self.panel(fallback=fallback)
        flat = [button for row in panel.buttons for button in row]
        self.assertIn("Switch back", flat[0].text)
        self.assertEqual(flat[0].data, "cq:u:123")
        self.assertIn("Temporary Codex Stand-in Active", panel.text)

    def test_idle_panel_reports_meters_without_a_limit(self):
        panel = self.panel(usage=_usage(primary_allowed=True))
        self.assertIn("Codex Status", panel.text)
        self.assertIn("Regular allowance", panel.text)

    def model_line(self, model, **kwargs):
        return plugin._codex_quota_model_line(model, **kwargs)

    def test_a_codex_model_is_told_the_allowances_are_its_own(self):
        line = self.model_line(OPENAI_CODEX_GPT_5_6_SOL)
        self.assertIn("Your model", line)
        self.assertIn("the ones it uses", line)

    def test_a_non_codex_model_is_told_the_allowances_do_not_apply(self):
        line = self.model_line(GEMINI_FLASH_LATEST)
        self.assertIn("not Codex", line)
        self.assertIn("`.c`", line)

    def test_the_reserve_model_is_distinguished_from_the_plan_allowance(self):
        line = self.model_line(OPENAI_CODEX_LUNA_RESERVE)
        self.assertIn("Reserve", line)
        self.assertNotIn("not Codex", line)

    def test_a_stand_in_names_both_the_saved_model_and_the_replacement(self):
        line = self.model_line(OPENAI_CODEX_GPT_5_6_SOL, stand_in=GEMINI_FLASH_LATEST)
        self.assertIn("temporarily switched to", line)
        self.assertIn(plugin._model_display_name(GEMINI_FLASH_LATEST), line)

    def test_a_stand_in_over_a_non_codex_model_says_it_is_not_redirecting(self):
        #: Armed, then the saved model changed away from Codex: the stand-in is
        #: still armed but redirects nothing, and the panel has to say so.
        line = self.model_line(GEMINI_FLASH_LATEST, stand_in=OR_OPENAI_5_6_SOL)
        self.assertIn("not redirecting", line)

    def test_the_panel_states_the_model_in_every_branch(self):
        fallback = plugin.CodexQuotaFallback(model=GEMINI_FLASH_LATEST, until=LATER)
        for kwargs in (
            {"usage": _usage(primary_allowed=True)},
            {"quota": self.quota, "usage": _usage()},
            {"fallback": fallback},
        ):
            with self.subTest(kwargs=sorted(kwargs)):
                self.assertIn("Your model", self.panel(**kwargs).text)

    def test_buttons_are_suppressed_for_userbot_mode(self):
        panel = self.panel(quota=self.quota, usage=_usage(), buttons_p=False)
        self.assertIsNone(panel.buttons)

    def test_callback_payloads_fit_telegram_and_carry_the_owner(self):
        panel = self.panel(quota=self.quota, usage=_usage())
        for row in panel.buttons:
            for button in row:
                self.assertLessEqual(
                    len(button.data.encode("utf-8")),
                    plugin.TELEGRAM_CALLBACK_BYTES_LIMIT,
                )
                self.assertIn(":123:", button.data)

    def test_panel_stays_within_the_telegram_message_limit(self):
        panel = self.panel(quota=self.quota, usage=_usage())
        self.assertLessEqual(
            plugin._utf16_units(panel.text), plugin.TELEGRAM_TEXT_UTF16_LIMIT
        )

    def test_missing_deadline_falls_back_to_a_bounded_window(self):
        deadline, reported = plugin._codex_quota_deadline(None, None, now=NOW)
        self.assertFalse(reported)
        self.assertEqual(deadline, NOW + plugin.CODEX_QUOTA_DEFAULT_WINDOW)


class QuotaCallbackTests(unittest.TestCase):
    def press(self, data, *, sender_id=123, keys=("gemini",)):
        event = SimpleNamespace(
            data=data.encode("utf-8"),
            sender_id=sender_id,
            chat_id=456,
            is_private=True,
            answer=AsyncMock(),
            edit=AsyncMock(),
        )
        with ExitStack() as stack:
            enter = stack.enter_context
            enter(
                patch.object(
                    plugin,
                    "get_effective_api_key",
                    side_effect=lambda uid, service: "k" if service in keys else None,
                )
            )
            enter(
                patch.object(
                    plugin.codex_util,
                    "fetch_codex_usage",
                    new=AsyncMock(return_value=None),
                )
            )
            enter(
                patch.object(
                    plugin.user_manager, "get_codex_quota_fallback", return_value=None
                )
            )
            armed = enter(patch.object(plugin.user_manager, "set_codex_quota_fallback"))
            cleared = enter(
                patch.object(plugin.user_manager, "clear_codex_quota_fallback")
            )
            asyncio.run(plugin.callback_handler(event))
        return event, armed, cleared

    def _future(self):
        return int((datetime.now(timezone.utc) + timedelta(days=1)).timestamp())

    def test_owner_can_arm_a_stand_in(self):
        event, armed, _ = self.press(f"cq:s:123:g:{self._future()}")
        armed.assert_called_once()
        self.assertEqual(armed.call_args.kwargs["model"], GEMINI_FLASH_LATEST)
        event.edit.assert_awaited()

    def test_another_user_cannot_press_someone_elses_panel(self):
        event, armed, _ = self.press(f"cq:s:123:g:{self._future()}", sender_id=999)
        armed.assert_not_called()
        self.assertIn("another user", event.answer.await_args.args[0])

    def test_expired_window_is_refused(self):
        past = int((datetime.now(timezone.utc) - timedelta(days=1)).timestamp())
        event, armed, _ = self.press(f"cq:s:123:g:{past}")
        armed.assert_not_called()
        self.assertIn("already passed", event.answer.await_args_list[0].args[0])

    def test_unknown_token_is_refused(self):
        event, armed, _ = self.press(f"cq:s:123:zz:{self._future()}")
        armed.assert_not_called()
        self.assertIn("no longer offered", event.answer.await_args.args[0])

    def test_candidate_without_a_key_is_refused_with_the_setup_command(self):
        event, armed, _ = self.press(f"cq:s:123:o:{self._future()}")
        armed.assert_not_called()
        self.assertIn("/setOpenRouterKey", event.answer.await_args.args[0])

    def test_undo_clears_and_rerenders(self):
        event, _, cleared = self.press("cq:u:123")
        cleared.assert_called_once_with(123)
        event.edit.assert_awaited()

    def test_malformed_payload_is_refused(self):
        event, armed, _ = self.press("cq:s:notanid:g:1")
        armed.assert_not_called()
        self.assertIn("invalid", event.answer.await_args.args[0])


class TimeFormattingTests(unittest.TestCase):
    def test_absolute_time_is_rendered_in_the_display_timezone(self):
        rendered = plugin._format_local(
            datetime(2026, 9, 20, 16, 32, tzinfo=timezone.utc)
        )
        self.assertTrue(rendered.startswith("2026-09-20 20:02"))

    def test_relative_time_keeps_two_units(self):
        self.assertEqual(
            plugin._format_relative(
                NOW + timedelta(days=4, hours=6, minutes=30), now=NOW
            ),
            "in 4 days, 6 hours",
        )

    def test_relative_time_in_the_past_reads_as_now(self):
        self.assertEqual(
            plugin._format_relative(NOW - timedelta(hours=1), now=NOW), "now"
        )

    def test_inline_code_neutralises_backticks(self):
        self.assertNotIn("`x`", plugin._md_code("a`x`b")[1:-1])


class CodexStatusCommandTests(unittest.TestCase):
    def test_command_is_registered_for_the_telegram_menu(self):
        commands = {entry["command"] for entry in plugin.BOT_COMMANDS}
        self.assertIn("codexstatus", commands)

    def test_command_is_known_so_it_never_reaches_the_chat_handler(self):
        self.assertIn("/codexstatus", plugin.KNOWN_COMMAND_SET)

    def test_help_mentions_the_command(self):
        #: /help builds its text inline, so assert on the source of truth.
        import inspect

        source = inspect.getsource(plugin.help_handler)
        self.assertIn("/codexStatus", source)
        self.assertIn("Luna Reserve", source)

    def test_armed_stand_in_opens_even_without_codex_access(self):
        #: Otherwise a user who lost access could never cancel their stand-in.
        event = SimpleNamespace(sender_id=123, chat_id=456, is_private=True)
        fallback = plugin.CodexQuotaFallback(model=GEMINI_FLASH_LATEST, until=LATER)
        with ExitStack() as stack:
            enter = stack.enter_context
            enter(
                patch.object(
                    plugin.user_manager,
                    "get_codex_quota_fallback",
                    return_value=fallback,
                )
            )
            enter(
                patch.object(
                    plugin.codex_util,
                    "fetch_codex_usage",
                    new=AsyncMock(return_value=None),
                )
            )
            enter(patch.object(plugin, "get_effective_api_key", return_value="k"))
            can_use = enter(
                patch.object(
                    plugin.llm_chat_config,
                    "can_use_codex",
                    new=AsyncMock(return_value=False),
                )
            )
            show = enter(
                patch.object(plugin, "_show_codex_quota_panel", new=AsyncMock())
            )
            enter(patch.object(plugin, "IS_BOT", True))
            asyncio.run(plugin.codex_status_handler(event))
        can_use.assert_not_awaited()
        show.assert_awaited_once()
        self.assertIn("Stand-in Active", show.await_args.args[1].text)

    def test_without_access_and_without_a_stand_in_it_is_declined(self):
        event = SimpleNamespace(sender_id=123, chat_id=456, is_private=True)
        with ExitStack() as stack:
            enter = stack.enter_context
            enter(
                patch.object(
                    plugin.user_manager, "get_codex_quota_fallback", return_value=None
                )
            )
            enter(
                patch.object(
                    plugin.llm_chat_config,
                    "can_use_codex",
                    new=AsyncMock(return_value=False),
                )
            )
            info = enter(patch.object(plugin, "send_info_message", new=AsyncMock()))
            asyncio.run(plugin.codex_status_handler(event))
        self.assertEqual(info.await_args.args[1], plugin.CODEX_ACCESS_DENIED)


class StatusLineTests(unittest.TestCase):
    def render(self, fallback):
        event = SimpleNamespace(sender_id=123, chat_id=456, is_private=True)
        with ExitStack() as stack:
            enter = stack.enter_context
            enter(patch.object(plugin.user_manager, "get_prefs", return_value=_prefs()))
            enter(
                patch.object(
                    plugin.chat_manager, "get_prefs", return_value=plugin.ChatPrefs()
                )
            )
            enter(patch.object(plugin.chat_manager, "get_model", return_value=None))
            enter(
                patch.object(
                    plugin.user_manager,
                    "get_codex_quota_fallback",
                    return_value=fallback,
                )
            )
            enter(
                patch.object(
                    plugin, "_can_user_access_model", new=AsyncMock(return_value=True)
                )
            )
            enter(patch.object(plugin.llm_chat_config, "load_config"))
            info = enter(patch.object(plugin, "send_info_message", new=AsyncMock()))
            asyncio.run(plugin.status_handler(event))
        return info.await_args.args[1]

    def test_line_appears_only_while_a_stand_in_is_armed(self):
        armed = plugin.CodexQuotaFallback(model=GEMINI_FLASH_LATEST, until=LATER)
        self.assertIn("Codex Stand-in", self.render(armed))
        self.assertNotIn("Codex Stand-in", self.render(None))


if __name__ == "__main__":
    unittest.main()
