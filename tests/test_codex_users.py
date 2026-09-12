import asyncio
import builtins
import importlib
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from uniborg import llm_chat_config
from uniborg.constants import OPENAI_CODEX_GPT_5_6_SOL, PIONEER_OPUS_4_8


class _FakeLoop:
    def create_task(self, coro):
        coro.close()
        return None


class _FakeBorg:
    loop = _FakeLoop()


builtins.borg = _FakeBorg()


async def _import_llm_chat():
    return importlib.import_module("llm_chat_plugins.llm_chat")


llm_chat = asyncio.run(_import_llm_chat())


class Event:
    sender_id = 900
    chat_id = 901
    is_private = True

    def __init__(self, argument=""):
        self.pattern_match = Mock()
        self.pattern_match.group.return_value = argument
        self.answer = AsyncMock()
        self.edit = AsyncMock()


def config(*ids, valid=True, users=()):
    return llm_chat_config.LLMChatConfig(tuple(ids), (), valid=valid, codex_users=tuple(users))


class CodexUsersTests(unittest.TestCase):
    def setUp(self):
        self.entity = SimpleNamespace(
            id=123, first_name="Ada", last_name="Lovelace", username="ada", is_self=False
        )

    def run_command(self, argument="", *, admin=True, cfg=None, entity=None):
        event = Event(argument)
        cfg = cfg or config(123)
        entity = self.entity if entity is None else entity
        with patch.object(llm_chat.util, "isAdmin", new=AsyncMock(return_value=admin)), patch.object(
            llm_chat.llm_chat_config, "load_config", return_value=cfg
        ), patch.object(builtins.borg, "get_entity", new=AsyncMock(return_value=entity), create=True), patch.object(
            llm_chat, "send_info_message", new=AsyncMock()
        ) as send, patch.object(llm_chat.user_manager, "set_model") as set_model:
            asyncio.run(llm_chat.codex_users_handler(event))
        return event, send, set_model

    def test_unauthorized_command_exposes_no_data(self):
        event, send, set_model = self.run_command(admin=False)
        set_model.assert_not_called()
        self.assertEqual(send.await_count, 1)
        self.assertNotIn("Ada", send.await_args.args[1])

    def test_list_deduplicates_and_excludes_sentinel_id_username_and_self_admins(self):
        entities = {
            123: self.entity,
            124: SimpleNamespace(id=124, first_name="ID admin", username=None, is_self=False),
            125: SimpleNamespace(id=125, first_name="Named admin", username="boss", is_self=False),
            126: SimpleNamespace(id=126, first_name="Self", username=None, is_self=True),
        }
        with patch.object(llm_chat.util, "admins", [124, "boss"]), patch.object(
            llm_chat.util, "is_admin_by_id", side_effect=lambda uid: uid == 124
        ), patch.object(builtins.borg, "get_entity", new=AsyncMock(side_effect=lambda uid: entities[uid]), create=True), patch.object(
            llm_chat.user_manager, "get_prefs", return_value=SimpleNamespace(model="model")
        ):
            users = asyncio.run(
                llm_chat._eligible_codex_users(
                    config(llm_chat.llm_chat_config.MAGIC_ADMINS, 123, 123, 124, 125, 126)
                )
            )
        self.assertEqual(users[0][0].id, 123)
        self.assertEqual(users[0][1:], ("Ada Lovelace", "model"))

    def test_entity_failure_falls_back_to_id(self):
        with patch.object(builtins.borg, "get_entity", new=AsyncMock(side_effect=ValueError), create=True), patch.object(
            llm_chat.util, "is_admin_by_id", return_value=False
        ), patch.object(llm_chat.user_manager, "get_prefs", return_value=SimpleNamespace(model="model")):
            users = asyncio.run(llm_chat._eligible_codex_users(config(123)))
        self.assertEqual(users[0][0].id, 123)
        self.assertEqual(users[0][1:], ("123", "model"))

    def test_disabled_roster_user_is_listed_and_configured_name_wins(self):
        roster_user = llm_chat_config.CodexUser(123, "Configured Name", False, True)
        cfg = config(users=(roster_user,))
        with patch.object(
            builtins.borg, "get_entity", new=AsyncMock(return_value=self.entity), create=True
        ), patch.object(llm_chat.user_manager, "get_prefs", return_value=SimpleNamespace(model="saved")):
            users = asyncio.run(llm_chat._eligible_codex_users(cfg))
        self.assertEqual(users, [(roster_user, "Configured Name", "saved")])

        event = Event()
        with patch.object(llm_chat.llm_chat_config, "load_config", return_value=cfg), patch.object(
            builtins.borg, "get_entity", new=AsyncMock(return_value=self.entity), create=True
        ), patch.object(llm_chat.user_manager, "get_prefs", return_value=SimpleNamespace(model="saved")), patch.object(
            llm_chat, "send_info_message", new=AsyncMock()
        ) as send:
            asyncio.run(llm_chat._show_codex_users(event))
        self.assertIn(
            "Personal grants shown; admin/trusted-chat policies may still allow access.",
            send.await_args.args[1],
        )

    def test_detail_shows_independent_grants_paused_images_and_fallback(self):
        roster_user = llm_chat_config.CodexUser(123, "Configured Name", False, True)
        cfg = config(users=(roster_user,))
        event = Event()
        prefs = SimpleNamespace(model=OPENAI_CODEX_GPT_5_6_SOL)
        with patch.object(llm_chat.llm_chat_config, "load_config", return_value=cfg), patch.object(
            builtins.borg, "get_entity", new=AsyncMock(return_value=self.entity), create=True
        ), patch.object(llm_chat.user_manager, "get_prefs", return_value=prefs), patch.object(
            llm_chat, "send_info_message", new=AsyncMock()
        ) as send:
            asyncio.run(llm_chat._show_codex_user_detail(event, 123))
        text = send.await_args.args[1]
        self.assertIn("Configured Name", text)
        self.assertIn("Personal Codex grant: off", text)
        self.assertIn("paused", text)
        self.assertIn(llm_chat.DEFAULT_MODEL, text)
        self.assertIn("trusted-chat policies", text)
        self.assertIn("Chat-specific model overrides", text)

    def test_detail_shows_exact_unknown_current_default(self):
        event = Event("123")
        prefs = SimpleNamespace(model="provider/a-very-custom-model")
        with patch.object(llm_chat.llm_chat_config, "load_config", return_value=config(123)), patch.object(
            builtins.borg, "get_entity", new=AsyncMock(return_value=self.entity), create=True
        ), patch.object(llm_chat.user_manager, "get_prefs", return_value=prefs), patch.object(
            llm_chat, "send_info_message", new=AsyncMock()
        ) as send:
            self.assertTrue(asyncio.run(llm_chat._show_codex_user_models(event, 123)))
        self.assertIn("provider/a-very-custom-model", send.await_args.args[1])

    def test_list_shows_name_id_and_saved_model_as_plain_text(self):
        with patch.object(
            llm_chat.user_manager, "get_prefs",
            return_value=SimpleNamespace(model="provider/custom_default"),
        ):
            _, send, set_model = self.run_command()
        text = send.await_args.args[1]
        self.assertIn("Ada Lovelace (123): provider/custom_default", text)
        self.assertIsNone(send.await_args.kwargs["parse_mode"])
        set_model.assert_not_called()

    def test_invalid_config_list_reports_error_without_reading_preferences(self):
        with patch.object(llm_chat.user_manager, "get_prefs") as get_prefs:
            _, send, set_model = self.run_command(cfg=config(valid=False))
        self.assertIn("configuration is invalid", send.await_args.args[1])
        get_prefs.assert_not_called()
        set_model.assert_not_called()

    def test_direct_custom_override(self):
        _, _, set_model = self.run_command("123 provider/custom")
        set_model.assert_called_once_with(123, "provider/custom")

    def test_direct_override_persists_target_and_preserves_other_preferences(self):
        records = {
            123: {
                "model": "old-model",
                "thinking_by_model": {"old-model": "high"},
                "enabled_tools": ["googleSearch"],
            },
            900: {"model": "caller-model", "json_mode": True},
        }
        manager = llm_chat.UserManager()
        manager.storage = Mock()
        manager.storage.get.side_effect = lambda uid: records.get(uid)
        manager.storage.set.side_effect = lambda uid, value: records.__setitem__(uid, value)
        event = Event("123 provider/custom")
        with patch.object(llm_chat, "user_manager", manager), patch.object(
            llm_chat.util, "isAdmin", new=AsyncMock(return_value=True)
        ), patch.object(llm_chat.llm_chat_config, "load_config", return_value=config(123)), patch.object(
            builtins.borg, "get_entity", new=AsyncMock(return_value=self.entity), create=True
        ), patch.object(llm_chat, "send_info_message", new=AsyncMock()):
            asyncio.run(llm_chat.codex_users_handler(event))
        self.assertEqual(records[123]["model"], "provider/custom")
        self.assertEqual(records[123]["thinking_by_model"], {"old-model": "high"})
        self.assertEqual(records[123]["enabled_tools"], ["googleSearch"])
        self.assertEqual(records[900], {"model": "caller-model", "json_mode": True})

    def test_direct_assignment_blocks_admin_only_model(self):
        _, _, set_model = self.run_command(f"123 {PIONEER_OPUS_4_8}")
        set_model.assert_not_called()

    def test_invalid_revoked_nonallowlisted_and_admin_targets_do_not_write(self):
        for cfg, entity in (
            (config(123, valid=False), self.entity),
            (config(999), self.entity),
            (config(123), SimpleNamespace(id=123, username=None, is_self=True)),
        ):
            with self.subTest(cfg=cfg, entity=entity):
                _, _, set_model = self.run_command("123 provider/custom", cfg=cfg, entity=entity)
                set_model.assert_not_called()

    def test_picker_callback_changes_target_only(self):
        token = llm_chat._codex_users_model_token(OPENAI_CODEX_GPT_5_6_SOL)
        event = Event()
        event.data = f"cu:m:123:{token}".encode()
        with patch.object(llm_chat.util, "isAdmin", new=AsyncMock(return_value=True)), patch.object(
            llm_chat.llm_chat_config, "load_config", return_value=config(123)
        ), patch.object(builtins.borg, "get_entity", new=AsyncMock(return_value=self.entity), create=True), patch.object(
            llm_chat.user_manager, "set_model"
        ) as set_model, patch.object(llm_chat, "_show_codex_user_models", new=AsyncMock(return_value=True)):
            asyncio.run(llm_chat.callback_handler(event))
        set_model.assert_called_once_with(123, OPENAI_CODEX_GPT_5_6_SOL)
        self.assertNotEqual(set_model.call_args.args[0], event.sender_id)

    def test_unauthorized_and_forged_callbacks_do_not_read_or_write(self):
        for admin, data in ((False, b"cu:u:123"), (True, b"cu:m:123:forged")):
            event = Event()
            event.data = data
            with patch.object(llm_chat.util, "isAdmin", new=AsyncMock(return_value=admin)), patch.object(
                llm_chat.llm_chat_config, "load_config", return_value=config(123)
            ) as load, patch.object(builtins.borg, "get_entity", new=AsyncMock(return_value=self.entity), create=True), patch.object(
                llm_chat.user_manager, "set_model"
            ) as set_model:
                asyncio.run(llm_chat.callback_handler(event))
            set_model.assert_not_called()
            if not admin:
                load.assert_not_called()

    def test_revoked_callback_does_not_write(self):
        token = llm_chat._codex_users_model_token(OPENAI_CODEX_GPT_5_6_SOL)
        event = Event()
        event.data = f"cu:m:123:{token}".encode()
        with patch.object(llm_chat.util, "isAdmin", new=AsyncMock(return_value=True)), patch.object(
            llm_chat.llm_chat_config, "load_config", return_value=config()
        ), patch.object(llm_chat.user_manager, "set_model") as set_model:
            asyncio.run(llm_chat.callback_handler(event))
        set_model.assert_not_called()

    def test_access_callbacks_write_explicit_desired_state_without_touching_prefs(self):
        for capability_token, capability in (("c", "codex_enabled"), ("i", "imagegen_enabled")):
            with self.subTest(capability=capability):
                roster_user = llm_chat_config.CodexUser(123, None, False, False)
                cfg = config(users=(roster_user,))
                event = Event()
                event.data = f"cu:a:123:{capability_token}:1".encode()
                with patch.object(llm_chat.util, "isAdmin", new=AsyncMock(return_value=True)), patch.object(
                    llm_chat.llm_chat_config, "load_config", return_value=cfg
                ), patch.object(builtins.borg, "get_entity", new=AsyncMock(return_value=self.entity), create=True), patch.object(
                    llm_chat.llm_chat_config, "update_user_access", return_value=cfg
                ) as update, patch.object(llm_chat, "_show_codex_user_detail", new=AsyncMock(return_value=True)), patch.object(
                    llm_chat.user_manager, "set_model"
                ) as set_model:
                    asyncio.run(llm_chat.callback_handler(event))
                update.assert_called_once_with(123, capability=capability, enabled=True)
                set_model.assert_not_called()

    def test_repeated_desired_state_click_answers_success_when_detail_is_unchanged(self):
        roster_user = llm_chat_config.CodexUser(123, None, True, False)
        cfg = config(users=(roster_user,))
        event = Event()
        event.data = b"cu:a:123:c:1"
        event.edit.side_effect = llm_chat.errors.rpcerrorlist.MessageNotModifiedError(
            request=None
        )
        with patch.object(llm_chat.util, "isAdmin", new=AsyncMock(return_value=True)), patch.object(
            llm_chat.llm_chat_config, "load_config", return_value=cfg
        ), patch.object(builtins.borg, "get_entity", new=AsyncMock(return_value=self.entity), create=True), patch.object(
            llm_chat.llm_chat_config, "update_user_access", return_value=cfg
        ) as update, patch.object(
            llm_chat.user_manager, "get_prefs", return_value=SimpleNamespace(model="saved")
        ):
            asyncio.run(llm_chat.callback_handler(event))
        update.assert_called_once_with(123, capability="codex_enabled", enabled=True)
        event.answer.assert_awaited_once_with("Codex personal grant enabled")

    def test_nonadmin_access_toggle_does_not_update(self):
        event = Event()
        event.data = b"cu:a:123:c:1"
        with patch.object(llm_chat.util, "isAdmin", new=AsyncMock(return_value=False)), patch.object(
            llm_chat.llm_chat_config, "update_user_access"
        ) as update:
            asyncio.run(llm_chat.callback_handler(event))
        update.assert_not_called()
        event.answer.assert_awaited_with(llm_chat.ADMIN_ONLY_COMMAND_IGNORED, show_alert=True)

    def test_enabling_images_does_not_enable_codex(self):
        roster_user = llm_chat_config.CodexUser(123, None, False, False)
        cfg = config(users=(roster_user,))
        event = Event()
        event.data = b"cu:a:123:i:1"
        with patch.object(llm_chat.util, "isAdmin", new=AsyncMock(return_value=True)), patch.object(
            llm_chat.llm_chat_config, "load_config", return_value=cfg
        ), patch.object(builtins.borg, "get_entity", new=AsyncMock(return_value=self.entity), create=True), patch.object(
            llm_chat.llm_chat_config, "update_user_access", return_value=cfg
        ) as update, patch.object(llm_chat, "_show_codex_user_detail", new=AsyncMock(return_value=True)):
            asyncio.run(llm_chat.callback_handler(event))
        update.assert_called_once_with(123, capability="imagegen_enabled", enabled=True)

    def test_access_callback_rechecks_target_and_handles_update_error(self):
        cases = (
            (config(), self.entity, None),
            (config(123, valid=False), self.entity, None),
            (config(users=(llm_chat_config.CodexUser(123, None, True, False),)), SimpleNamespace(id=123, username=None, is_self=True), None),
            (config(users=(llm_chat_config.CodexUser(123, None, True, False),)), self.entity, llm_chat_config.ConfigUpdateError("write failed")),
        )
        for cfg, entity, error in cases:
            with self.subTest(cfg=cfg, error=error):
                event = Event()
                event.data = b"cu:a:123:c:0"
                update = Mock(side_effect=error)
                with patch.object(llm_chat.util, "isAdmin", new=AsyncMock(return_value=True)), patch.object(
                    llm_chat.llm_chat_config, "load_config", return_value=cfg
                ), patch.object(builtins.borg, "get_entity", new=AsyncMock(return_value=entity), create=True), patch.object(
                    llm_chat.llm_chat_config, "update_user_access", update
                ):
                    asyncio.run(llm_chat.callback_handler(event))
                if error is None and cfg.codex_users and not entity.is_self:
                    self.fail("case should be rejected or raise an update error")
                if error is not None:
                    event.answer.assert_awaited_with("write failed", show_alert=True)
                else:
                    update.assert_not_called()

    def test_update_error_alert_is_bounded(self):
        roster_user = llm_chat_config.CodexUser(123, None, True, False)
        cfg = config(users=(roster_user,))
        event = Event()
        event.data = b"cu:a:123:c:0"
        with patch.object(llm_chat.util, "isAdmin", new=AsyncMock(return_value=True)), patch.object(
            llm_chat.llm_chat_config, "load_config", return_value=cfg
        ), patch.object(builtins.borg, "get_entity", new=AsyncMock(return_value=self.entity), create=True), patch.object(
            llm_chat.llm_chat_config,
            "update_user_access",
            side_effect=llm_chat_config.ConfigUpdateError("x" * 500),
        ):
            asyncio.run(llm_chat.callback_handler(event))
        self.assertEqual(len(event.answer.await_args.args[0]), 200)
        self.assertTrue(event.answer.await_args.kwargs["show_alert"])

    def test_pagination_and_callback_lengths(self):
        users = [
            (llm_chat_config.CodexUser(uid, None, True, False), f"User {uid}", "model")
            for uid in range(100, 119)
        ]
        buttons, page, page_count = llm_chat._codex_users_user_buttons(users, 1)
        flat = [button for row in buttons for button in row]
        self.assertEqual((page, page_count), (1, 3))
        self.assertTrue(all(len(button.data) <= 64 for button in flat))
        self.assertTrue(any(button.data in (b"cu:p:0", "cu:p:0") for button in flat))
        self.assertTrue(any(button.data in (b"cu:p:2", "cu:p:2") for button in flat))

    def test_picker_model_callback_payloads_fit_telegram_limit(self):
        prefs = SimpleNamespace(model="unknown/current")
        event = Event()
        with patch.object(llm_chat.llm_chat_config, "load_config", return_value=config(123)), patch.object(
            builtins.borg, "get_entity", new=AsyncMock(return_value=self.entity), create=True
        ), patch.object(llm_chat.user_manager, "get_prefs", return_value=prefs), patch.object(
            llm_chat, "send_info_message", new=AsyncMock()
        ) as send:
            asyncio.run(llm_chat._show_codex_user_models(event, 123))
        rows = send.await_args.kwargs["buttons"]
        self.assertTrue(all(len(button.data) <= 64 for row in rows for button in row))

    def test_command_routing(self):
        self.assertTrue(llm_chat._is_known_command(".codex-users"))
        self.assertTrue(llm_chat._is_known_command(".codex-users 123 model"))
        with patch.object(llm_chat, "BOT_USERNAME", "@BetterBot"):
            self.assertTrue(llm_chat._is_known_command(".codex-users@BetterBot 123"))
        for text in (".codex-users", ".codex-users 123", "/unknown-command"):
            event = SimpleNamespace(is_private=True, text=text)
            self.assertFalse(llm_chat._is_pending_input_message(event, awaiting=True))
        event = SimpleNamespace(is_private=True, text="provider/custom")
        self.assertTrue(llm_chat._is_pending_input_message(event, awaiting=True))


if __name__ == "__main__":
    unittest.main()
