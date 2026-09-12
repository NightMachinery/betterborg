import asyncio
import builtins
import importlib
import os
import re
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from telethon.tl.types import (
    Channel, KeyboardButtonRequestPeer, MessageActionRequestedPeerSentMe, MessageReplyHeader,
    MessageService, PeerUser, ReplyKeyboardHide, ReplyKeyboardMarkup,
    RequestPeerTypeUser, RequestedPeerUser, UpdateNewMessage,
)

_TEST_HOME = tempfile.TemporaryDirectory()
_REAL_EXPANDUSER = os.path.expanduser
_REAL_PATH_HOME = Path.__dict__["home"]
os.path.expanduser = lambda path: path.replace("~", _TEST_HOME.name, 1) if path.startswith("~") else _REAL_EXPANDUSER(path)
Path.home = classmethod(lambda cls: Path(_TEST_HOME.name))

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

os.path.expanduser = _REAL_EXPANDUSER
Path.home = _REAL_PATH_HOME


class Event:
    sender_id = 900
    chat_id = 901
    is_private = True

    def __init__(self, argument=""):
        self.pattern_match = Mock()
        self.pattern_match.group.return_value = argument
        self.answer = AsyncMock()
        self.edit = AsyncMock()


class AddEvent(Event):
    def __init__(self, text="", *, sender_id=900, chat_id=901, private=True, reply_to=None):
        super().__init__()
        self.sender_id = sender_id
        self.chat_id = chat_id
        self.is_private = private
        self.out = False
        self.raw_text = text
        self.text = text
        self.message = SimpleNamespace(reply_to_msg_id=reply_to)
        self.get_sender = AsyncMock(
            return_value=llm_chat.User(id=sender_id, first_name="Admin", username="admin")
        )
        self.respond = AsyncMock(return_value=SimpleNamespace(id=777))
        self.reply = AsyncMock(return_value=SimpleNamespace(id=778))


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
        with patch.object(llm_chat, "_codex_users_admin", new=AsyncMock(return_value=admin)), patch.object(
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
        self.assertEqual(users[0][1:3], ("Ada Lovelace", "model"))

    def test_entity_failure_falls_back_to_id(self):
        with patch.object(builtins.borg, "get_entity", new=AsyncMock(side_effect=ValueError), create=True), patch.object(
            llm_chat.util, "is_admin_by_id", return_value=False
        ), patch.object(llm_chat.user_manager, "get_prefs", return_value=SimpleNamespace(model="model")):
            users = asyncio.run(llm_chat._eligible_codex_users(config(123)))
        self.assertEqual(users[0][0].id, 123)
        self.assertEqual(users[0][1:3], ("123", "model"))

    def test_disabled_roster_user_is_listed_and_configured_name_wins(self):
        roster_user = llm_chat_config.CodexUser(123, "Configured Name", False, True)
        cfg = config(users=(roster_user,))
        with patch.object(
            builtins.borg, "get_entity", new=AsyncMock(return_value=self.entity), create=True
        ), patch.object(llm_chat.user_manager, "get_prefs", return_value=SimpleNamespace(model="saved")):
            users = asyncio.run(llm_chat._eligible_codex_users(cfg))
        self.assertEqual(users[0][:3], (roster_user, "Configured Name", "saved"))

        event = Event()
        with patch.object(llm_chat.llm_chat_config, "load_config", return_value=cfg), patch.object(
            builtins.borg, "get_entity", new=AsyncMock(return_value=self.entity), create=True
        ), patch.object(llm_chat.user_manager, "get_prefs", return_value=SimpleNamespace(model="saved")), patch.object(
            llm_chat, "send_info_message", new=AsyncMock()
        ) as send:
            asyncio.run(llm_chat._show_codex_users(event))
        self.assertIn("Trusted chats may grant additional access.", send.await_args.args[1])

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
        self.assertIn(llm_chat._model_display_name(llm_chat.DEFAULT_MODEL), text)
        self.assertIn("Trusted chats", text)
        self.assertIn("Chat model overrides", text)

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

    def test_list_shows_bold_name_id_and_friendly_model(self):
        with patch.object(
            llm_chat.user_manager, "get_prefs",
            return_value=SimpleNamespace(model="provider/custom_default"),
        ):
            _, send, set_model = self.run_command()
        text = send.await_args.args[1]
        self.assertIn("<b>Ada Lovelace</b>", text)
        self.assertIn("· <code>123</code>", text)
        self.assertIn("provider/custom_default · Codex on · Images off", text)
        self.assertEqual(send.await_args.kwargs["parse_mode"], "html")
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
            llm_chat, "_codex_users_admin", new=AsyncMock(return_value=True)
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
        with patch.object(llm_chat, "_codex_users_admin", new=AsyncMock(return_value=True)), patch.object(
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
            with patch.object(llm_chat, "_codex_users_admin", new=AsyncMock(return_value=admin)), patch.object(
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
        with patch.object(llm_chat, "_codex_users_admin", new=AsyncMock(return_value=True)), patch.object(
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
                with patch.object(llm_chat, "_codex_users_admin", new=AsyncMock(return_value=True)), patch.object(
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
        with patch.object(llm_chat, "_codex_users_admin", new=AsyncMock(return_value=True)), patch.object(
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
        with patch.object(llm_chat, "_codex_users_admin", new=AsyncMock(return_value=False)), patch.object(
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
        with patch.object(llm_chat, "_codex_users_admin", new=AsyncMock(return_value=True)), patch.object(
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
                with patch.object(llm_chat, "_codex_users_admin", new=AsyncMock(return_value=True)), patch.object(
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
        with patch.object(llm_chat, "_codex_users_admin", new=AsyncMock(return_value=True)), patch.object(
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
        labels = [button.text for row in rows for button in row]
        self.assertTrue(any(OPENAI_CODEX_GPT_5_6_SOL in label for label in labels))

    def test_panel_escapes_hostile_names_and_bounds_delivery(self):
        hostile = "<&" + "😀" * 3000
        entity = SimpleNamespace(
            id=123, first_name=hostile, last_name=None, username="bad<name", is_self=False
        )
        event = Event()
        with patch.object(llm_chat.llm_chat_config, "load_config", return_value=config(123)), patch.object(
            builtins.borg, "get_entity", new=AsyncMock(return_value=entity), create=True
        ), patch.object(
            llm_chat.user_manager, "get_prefs", return_value=SimpleNamespace(model="m" * 5000)
        ), patch.object(llm_chat, "send_info_message", new=AsyncMock()) as send:
            asyncio.run(llm_chat._show_codex_users(event))
        text = send.await_args.args[1]
        self.assertIn("&lt;&amp;", text)
        self.assertNotIn("<&#", text)
        self.assertLessEqual(
            len((llm_chat.BOT_META_INFO_PREFIX + text).encode("utf-16-le")) // 2,
            llm_chat.TELEGRAM_TEXT_UTF16_LIMIT,
        )
        for row in send.await_args.kwargs["buttons"]:
            for button in row:
                self.assertLessEqual(len(button.data), 64)

    def test_overview_omits_unknown_contact_and_empty_key_metadata(self):
        roster = llm_chat_config.CodexUser(123, None, False, False)
        users = [(roster, "Ada Lovelace", llm_chat.GEMINI_FLASH_LATEST,
                  "Ada Lovelace", "ada", None)]
        event = Event()
        with patch.object(llm_chat.llm_chat_config, "load_config", return_value=config(123)), patch.object(
            llm_chat, "_eligible_codex_users", new=AsyncMock(return_value=users)
        ), patch.object(
            llm_chat.llm_db, "get_api_key_metadata", return_value=[]
        ) as metadata, patch.object(llm_chat, "send_info_message", new=AsyncMock()) as send:
            asyncio.run(llm_chat._show_codex_users(event))
        text = send.await_args.args[1]
        self.assertIn("• <b>Ada Lovelace</b> · <code>123</code>", text)
        self.assertIn("Gemini Flash (Latest) · Codex off · Images off", text)
        self.assertNotIn("Unknown", text)
        self.assertNotIn("Contact:", text)
        self.assertNotIn("Keys:", text)
        metadata.assert_called_once_with(123)

    def test_overview_omits_keys_when_metadata_is_unavailable(self):
        roster = llm_chat_config.CodexUser(123, None, True, False)
        users = [(roster, "Ada", "model", "Ada", None, None)]
        event = Event()
        with patch.object(llm_chat.llm_chat_config, "load_config", return_value=config(123)), patch.object(
            llm_chat, "_eligible_codex_users", new=AsyncMock(return_value=users)
        ), patch.object(
            llm_chat.llm_db, "get_api_key_metadata", side_effect=RuntimeError("unavailable")
        ), patch.object(llm_chat, "send_info_message", new=AsyncMock()) as send:
            asyncio.run(llm_chat._show_codex_users(event))
        self.assertNotIn("Keys:", send.await_args.args[1])

    def test_overview_does_not_repeat_unknown_telegram_identity(self):
        roster = llm_chat_config.CodexUser(123, "Configured name", False, False)
        configured = [(roster, "Configured name", "model", "123", None, None)]
        with patch.object(llm_chat.llm_db, "get_api_key_metadata", return_value=[]):
            configured_text = llm_chat._codex_user_overview_entry(configured[0])
        self.assertIn("<b>Configured name</b> · <code>123</code>", configured_text)
        self.assertNotIn("TG:", configured_text)

        unknown = [(llm_chat_config.CodexUser(456, None, False, False),
                    "456", "model", "456", None, None)]
        with patch.object(llm_chat.llm_db, "get_api_key_metadata", return_value=[]):
            unknown_text = llm_chat._codex_user_overview_entry(unknown[0])
        self.assertEqual(unknown_text.count("456"), 1)
        self.assertTrue(unknown_text.startswith("• <code>456</code>"))

    def test_overview_shows_known_contact_identity_and_key_provider_names_only(self):
        roster = llm_chat_config.CodexUser(123, "Configured <label>", True, False)
        users = [(roster, "Configured <label>", "provider/custom",
                  "Telegram & Name", "ada<admin", datetime(2026, 1, 2, tzinfo=timezone.utc))]
        keys = [
            SimpleNamespace(service="gemini", last_set_at=datetime(2026, 2, 3, tzinfo=timezone.utc), api_key="SECRET-GEMINI"),
            SimpleNamespace(service="openrouter", last_set_at=None, api_key="SECRET-OPENROUTER"),
        ]
        event = Event()
        with patch.object(llm_chat.llm_chat_config, "load_config", return_value=config(123)), patch.object(
            llm_chat, "_eligible_codex_users", new=AsyncMock(return_value=users)
        ), patch.object(
            llm_chat.llm_db, "get_api_key_metadata", return_value=keys
        ), patch.object(llm_chat, "send_info_message", new=AsyncMock()) as send:
            asyncio.run(llm_chat._show_codex_users(event))
        text = send.await_args.args[1]
        self.assertIn("<b>Configured &lt;label&gt;</b> · <code>123</code>", text)
        self.assertIn("TG: <b>Telegram &amp; Name</b> · <b>@ada&lt;admin</b>", text)
        self.assertIn("Contact: Started", text)
        self.assertIn("Keys: Gemini, OpenRouter", text)
        self.assertNotIn("2026", text)
        self.assertNotIn("SECRET", text)

    def test_payload_pages_keep_every_user_once_with_matching_buttons(self):
        from telethon.extensions import html as telegram_html
        users = [
            (llm_chat_config.CodexUser(uid, "<&😀" * 100, True, False),
             "<&😀" * 100, "model" * 100, "Name<&" * 100, "user<&" * 100, None)
            for uid in range(100, 117)
        ]
        with patch.object(llm_chat.llm_db, "get_api_key_metadata", return_value=[]):
            pages = llm_chat._codex_users_overview_pages(users)
        self.assertGreater(len(pages), 1)
        self.assertLess(len(pages[0]), llm_chat.CODEX_USERS_PAGE_SIZE)
        self.assertTrue(all(1 <= len(entries) <= llm_chat.CODEX_USERS_PAGE_SIZE for entries in pages))

        seen = []
        for page_index, expected_entries in enumerate(pages):
            event = Event()
            with patch.object(llm_chat.llm_chat_config, "load_config", return_value=config(123)), patch.object(
                llm_chat, "_eligible_codex_users", new=AsyncMock(return_value=users)
            ), patch.object(
                llm_chat.llm_db, "get_api_key_metadata", return_value=[]
            ), patch.object(llm_chat, "send_info_message", new=AsyncMock()) as send:
                asyncio.run(llm_chat._show_codex_users(event, page=page_index))
            text = send.await_args.args[1]
            parsed, entities = telegram_html.parse(llm_chat.BOT_META_INFO_PREFIX + text)
            self.assertLessEqual(len(parsed.encode("utf-16-le")) // 2, 4096)
            self.assertLessEqual(
                len((llm_chat.BOT_META_INFO_PREFIX + text).encode("utf-16-le")) // 2,
                llm_chat.TELEGRAM_TEXT_UTF16_LIMIT - llm_chat.CODEX_USERS_PAYLOAD_MARGIN,
            )
            self.assertTrue(text.endswith("Trusted chats may grant additional access."))
            self.assertTrue(entities)

            text_ids = [
                int(match)
                for match in re.findall(r"<code>(\d+)</code>", text)
            ]
            expected_ids = [entry[0][0].id for entry in expected_entries]
            button_ids = []
            for row in send.await_args.kwargs["buttons"]:
                for button in row:
                    data = button.data.decode() if isinstance(button.data, bytes) else button.data
                    if data.startswith("cu:u:"):
                        button_ids.append(int(data.removeprefix("cu:u:")))
            self.assertEqual(text_ids, expected_ids)
            self.assertEqual(button_ids, expected_ids)
            seen.extend(text_ids)

        self.assertEqual(seen, list(range(100, 117)))
        self.assertEqual(len(seen), len(set(seen)))

    def test_detail_separates_configured_and_telegram_identity_and_key_metadata(self):
        roster = llm_chat_config.CodexUser(123, "Configured <label>", True, False)
        profile = llm_chat.llm_db.UserProfile(
            42, 123, "Ada", "Lovelace", "ada", datetime(2026, 1, 2, tzinfo=timezone.utc)
        )
        keys = [
            llm_chat.llm_db.ApiKeyMetadata("gemini", datetime(2026, 2, 3, tzinfo=timezone.utc)),
            llm_chat.llm_db.ApiKeyMetadata("openrouter", None),
        ]
        event = Event()
        with patch.object(llm_chat, "BOT_ID", 42), patch.object(
            llm_chat.llm_chat_config, "load_config", return_value=config(users=(roster,))
        ), patch.object(builtins.borg, "get_entity", new=AsyncMock(return_value=self.entity), create=True), patch.object(
            llm_chat.llm_db, "record_user_profile", return_value=profile
        ), patch.object(llm_chat.llm_db, "get_user_profile", return_value=profile), patch.object(
            llm_chat.llm_db, "get_api_key_metadata", return_value=keys
        ), patch.object(llm_chat, "send_info_message", new=AsyncMock()) as send:
            asyncio.run(llm_chat._show_codex_user_detail(event, 123))
        text = send.await_args.args[1]
        self.assertIn("Configured label: <b>Configured &lt;label&gt;</b>", text)
        self.assertIn("Telegram name: <b>Ada Lovelace</b>", text)
        self.assertIn("Username: <b>@ada</b>", text)
        self.assertIn("Bot contact: Started", text)
        self.assertIn("gemini — 2026-02-03 00:00 UTC", text)
        self.assertIn("openrouter — Set date unknown", text)

    def test_identity_refresh_falls_back_to_persisted_profile(self):
        profile = llm_chat.llm_db.UserProfile(42, 123, "Stored", "Name", None, None)
        with patch.object(llm_chat, "BOT_ID", 42), patch.object(
            builtins.borg, "get_entity", new=AsyncMock(side_effect=ValueError), create=True
        ), patch.object(llm_chat.llm_db, "get_user_profile", return_value=profile):
            entity = asyncio.run(llm_chat._codex_user_entity(123))
        self.assertEqual(llm_chat._codex_user_name(123, entity), "Stored Name")

    def test_incoming_observer_records_only_user_contact_for_this_bot(self):
        sender = SimpleNamespace(first_name="Ada", last_name=None, username="ada", bot=False)
        date = datetime(2026, 4, 5, tzinfo=timezone.utc)
        record = Mock()
        with patch.object(llm_chat, "IS_BOT", True), patch.object(llm_chat, "BOT_ID", 42), patch.object(
            llm_chat.llm_db, "record_user_profile", record
        ):
            private = SimpleNamespace(
                out=False, sender_id=123, is_private=True, date=date,
                get_sender=AsyncMock(return_value=sender),
            )
            asyncio.run(llm_chat.observe_incoming_user_profile(private))
            group = SimpleNamespace(
                out=False, sender_id=123, is_private=False, date=date,
                get_sender=AsyncMock(return_value=sender),
            )
            asyncio.run(llm_chat.observe_incoming_user_profile(group))
            outgoing = SimpleNamespace(out=True, sender_id=123)
            asyncio.run(llm_chat.observe_incoming_user_profile(outgoing))
            bot_sender = SimpleNamespace(first_name="Bot", bot=True)
            from_bot = SimpleNamespace(
                out=False, sender_id=777, is_private=True, date=date,
                get_sender=AsyncMock(return_value=bot_sender),
            )
            asyncio.run(llm_chat.observe_incoming_user_profile(from_bot))
        self.assertEqual(record.call_count, 2)
        self.assertEqual(record.call_args_list[0].args[:2], (42, 123))
        self.assertEqual(record.call_args_list[0].kwargs["private_contact_at"], date)
        self.assertIsNone(record.call_args_list[1].kwargs["private_contact_at"])

    def test_private_dialog_backfills_contact_and_renders_started(self):
        entity = llm_chat.User(
            id=123, first_name="Ada", last_name="Lovelace", username="ada", bot=False
        )
        profiles = {
            123: llm_chat.llm_db.UserProfile(42, 123, "Ada", "Lovelace", "ada", None)
        }

        async def dialogs():
            yield SimpleNamespace(is_user=True, entity=entity)
            raise AssertionError("dialog scan did not stop after finding every candidate")

        def record(bot_id, user_id, first_name, last_name, username,
                   private_contact_at=None, **kwargs):
            profiles[user_id] = llm_chat.llm_db.UserProfile(
                bot_id, user_id, first_name, last_name, username, private_contact_at
            )
            return profiles[user_id]

        event = Event()
        with patch.object(llm_chat, "IS_BOT", True), patch.object(
            llm_chat, "BOT_ID", 42
        ), patch.object(
            llm_chat.llm_chat_config, "load_config", return_value=config(123)
        ), patch.object(
            llm_chat, "_codex_user_entity", new=AsyncMock(return_value=entity)
        ), patch.object(
            llm_chat.llm_db, "get_user_profile", side_effect=lambda bot_id, user_id: profiles.get(user_id)
        ), patch.object(
            llm_chat.llm_db, "record_user_profile", side_effect=record
        ) as record_profile, patch.object(
            builtins.borg, "iter_dialogs", side_effect=lambda **kwargs: dialogs(), create=True
        ) as iter_dialogs, patch.object(
            builtins.borg, "send_message", new=AsyncMock(), create=True
        ) as client_send, patch.object(
            llm_chat.llm_db, "get_api_key_metadata", return_value=[]
        ), patch.object(
            llm_chat.user_manager, "get_prefs", return_value=SimpleNamespace(model="model")
        ), patch.object(llm_chat, "send_info_message", new=AsyncMock()) as send:
            asyncio.run(llm_chat._show_codex_users(event))

        self.assertIn("Contact: Started", send.await_args.args[1])
        iter_dialogs.assert_called_once_with(
            limit=llm_chat.CODEX_USERS_DIALOG_SCAN_LIMIT, folder=None
        )
        contact_at = record_profile.call_args.kwargs["private_contact_at"]
        self.assertEqual(contact_at.tzinfo, timezone.utc)
        client_send.assert_not_awaited()

    def test_contact_backfill_ignores_group_and_unrelated_user_dialogs(self):
        group = Channel(id=123, title="Target ID group", photo=None, date=None)
        unrelated = llm_chat.User(id=999, first_name="Other", bot=False)

        async def dialogs():
            yield SimpleNamespace(is_user=False, entity=group)
            yield SimpleNamespace(is_user=True, entity=unrelated)

        with patch.object(llm_chat, "IS_BOT", True), patch.object(
            llm_chat, "BOT_ID", 42
        ), patch.object(
            builtins.borg, "iter_dialogs", side_effect=lambda **kwargs: dialogs(), create=True
        ), patch.object(llm_chat.llm_db, "record_user_profile") as record:
            profiles = asyncio.run(llm_chat._backfill_codex_user_contacts({123}))

        self.assertEqual(profiles, {})
        record.assert_not_called()

    def test_missing_or_failed_dialog_scan_keeps_contact_unknown(self):
        async def no_dialogs():
            if False:
                yield None

        async def failed_dialogs():
            raise RuntimeError("Telegram unavailable")
            yield None

        for stream in (no_dialogs, failed_dialogs):
            with self.subTest(stream=stream.__name__), patch.object(
                llm_chat, "IS_BOT", True
            ), patch.object(llm_chat, "BOT_ID", 42), patch.object(
                builtins.borg, "iter_dialogs", side_effect=lambda **kwargs: stream(), create=True
            ), patch.object(llm_chat.llm_db, "record_user_profile") as record:
                profiles = asyncio.run(llm_chat._backfill_codex_user_contacts({123}))
            self.assertEqual(profiles, {})
            record.assert_not_called()

    def test_recorded_contact_skips_dialog_api(self):
        contact_at = datetime(2026, 4, 5, tzinfo=timezone.utc)
        profile = llm_chat.llm_db.UserProfile(
            42, 123, "Ada", "Lovelace", "ada", contact_at
        )
        with patch.object(llm_chat, "IS_BOT", True), patch.object(
            llm_chat, "BOT_ID", 42
        ), patch.object(
            llm_chat, "_codex_user_entity", new=AsyncMock(return_value=self.entity)
        ), patch.object(
            llm_chat.llm_db, "get_user_profile", return_value=profile
        ), patch.object(
            builtins.borg, "iter_dialogs", create=True
        ) as iter_dialogs, patch.object(
            llm_chat.user_manager, "get_prefs", return_value=SimpleNamespace(model="model")
        ):
            users = asyncio.run(llm_chat._eligible_codex_users(config(123)))

        self.assertEqual(users[0][-1], contact_at)
        iter_dialogs.assert_not_called()

    def test_add_user_button_is_present(self):
        users = [(llm_chat_config.CodexUser(123, None, True, False), "Ada", "model")]
        rows, _, _ = llm_chat._codex_users_user_buttons(users, 0)
        self.assertTrue(any(button.data in (b"cu:add", "cu:add") for row in rows for button in row))

    def test_private_picker_markup_requests_one_nonbot_user_and_serializes(self):
        markup = llm_chat._codex_user_picker_markup(-123)
        self.assertIsInstance(markup, ReplyKeyboardMarkup)
        self.assertTrue(markup.resize)
        self.assertTrue(markup.single_use)
        choose = markup.rows[0].buttons[0]
        self.assertIsInstance(choose, KeyboardButtonRequestPeer)
        self.assertEqual(choose.button_id, -123)
        self.assertIsInstance(choose.peer_type, RequestPeerTypeUser)
        self.assertFalse(choose.peer_type.bot)
        self.assertEqual(choose.max_quantity, 1)
        self.assertGreater(len(bytes(markup)), 0)

    def test_private_prompt_has_picker_while_group_keeps_inline_cancel(self):
        for private in (True, False):
            with self.subTest(private=private):
                llm_chat.CODEX_USERS_ADD_PENDING.clear()
                event = AddEvent(private=private)
                asyncio.run(llm_chat._start_codex_user_add(event))
                buttons = event.respond.await_args.kwargs["buttons"]
                self.assertEqual(isinstance(buttons, ReplyKeyboardMarkup), private)
                pending = llm_chat.CODEX_USERS_ADD_PENDING[(900, 901)]
                self.assertEqual(pending["request_id"] is not None, private)

    def test_requested_peer_service_builder_initializes_and_previews(self):
        service = MessageService(
            id=778, peer_id=PeerUser(900), from_id=PeerUser(900), out=False,
            reply_to=MessageReplyHeader(reply_to_msg_id=777),
            action=MessageActionRequestedPeerSentMe(
                button_id=-55,
                peers=[RequestedPeerUser(123, first_name="Picker", username="picked")],
            ),
        )
        update = UpdateNewMessage(service, pts=1, pts_count=1)
        event = llm_chat.CodexUserRequestedPeer.build(update, self_id=42)
        self.assertIsNotNone(event)

        class Cache:
            def get(self, *args, **kwargs):
                return None

        event._set_client(SimpleNamespace(_self_id=42, _mb_entity_cache=Cache()))
        self.assertIs(event.message, service)
        self.assertTrue(event.is_private)
        self.assertEqual(event.sender_id, 900)
        llm_chat.CODEX_USERS_ADD_PENDING.clear()
        llm_chat.CODEX_USERS_ADD_PENDING[(900, 900)] = {
            "token": "safe", "prompt_id": 777, "phase": "input", "request_id": -55,
        }
        with patch.object(llm_chat, "_codex_users_admin", new=AsyncMock(return_value=True)), patch.object(
            builtins.borg, "get_entity", new=AsyncMock(side_effect=ValueError), create=True
        ), patch.object(llm_chat.llm_chat_config, "load_config", return_value=config()), patch.object(
            llm_chat, "send_info_message", new=AsyncMock()
        ) as send:
            with self.assertRaises(llm_chat.events.StopPropagation):
                asyncio.run(llm_chat.codex_user_requested_peer_handler(event))
        self.assertEqual(llm_chat.CODEX_USERS_ADD_PENDING[(900, 900)]["target_id"], 123)
        self.assertEqual(send.await_count, 2)
        self.assertIsInstance(send.await_args_list[0].kwargs["buttons"], ReplyKeyboardHide)
        self.assertIn("Picker", send.await_args_list[1].args[1])

    def test_requested_peer_ignores_stale_wrong_scope_and_malformed(self):
        base = {"token": "safe", "prompt_id": 777, "phase": "input", "request_id": 55}
        cases = [
            (AddEvent(sender_id=901), MessageActionRequestedPeerSentMe(55, [RequestedPeerUser(123)])),
            (AddEvent(private=False), MessageActionRequestedPeerSentMe(55, [RequestedPeerUser(123)])),
            (AddEvent(), MessageActionRequestedPeerSentMe(54, [RequestedPeerUser(123)])),
            (AddEvent(), MessageActionRequestedPeerSentMe(55, [])),
            (AddEvent(), MessageActionRequestedPeerSentMe(55, [RequestedPeerUser(123), RequestedPeerUser(124)])),
        ]
        for event, action in cases:
            with self.subTest(event=event, action=action):
                llm_chat.CODEX_USERS_ADD_PENDING.clear()
                llm_chat.CODEX_USERS_ADD_PENDING[(900, 901)] = dict(base)
                event.message.action = action
                asyncio.run(llm_chat.codex_user_requested_peer_handler(event))
                self.assertEqual(llm_chat.CODEX_USERS_ADD_PENDING[(900, 901)]["phase"], "input")

    def test_requested_peer_rejects_shared_admin_identity(self):
        llm_chat.CODEX_USERS_ADD_PENDING.clear()
        llm_chat.CODEX_USERS_ADD_PENDING[(900, 901)] = {
            "token": "safe", "prompt_id": 777, "phase": "input", "request_id": 55,
        }
        event = AddEvent()
        event.message.action = MessageActionRequestedPeerSentMe(
            55, [RequestedPeerUser(123, first_name="Admin", username="boss")]
        )
        with patch.object(llm_chat, "_codex_users_admin", new=AsyncMock(return_value=True)), patch.object(
            builtins.borg, "get_entity", new=AsyncMock(side_effect=ValueError), create=True
        ), patch.object(llm_chat.util, "admins", ["boss"]), patch.object(
            llm_chat, "send_info_message", new=AsyncMock()
        ) as send:
            with self.assertRaises(llm_chat.events.StopPropagation):
                asyncio.run(llm_chat.codex_user_requested_peer_handler(event))
        self.assertEqual(llm_chat.CODEX_USERS_ADD_PENDING[(900, 901)]["phase"], "input")
        self.assertIn("administrators", send.await_args.args[1])

    def test_preview_hide_does_not_outlive_cancelled_flow(self):
        async def scenario():
            llm_chat.CODEX_USERS_ADD_PENDING.clear()
            pending = {"token": "safe", "prompt_id": 777, "phase": "resolving", "request_id": 55}
            llm_chat.CODEX_USERS_ADD_PENDING[(900, 901)] = pending
            sent = asyncio.Event()
            release = asyncio.Event()

            async def send(*args, **kwargs):
                sent.set()
                await release.wait()

            event = AddEvent()
            with patch.object(llm_chat.llm_chat_config, "load_config", return_value=config()), patch.object(
                llm_chat, "send_info_message", side_effect=send
            ) as mocked:
                task = asyncio.create_task(llm_chat._finish_codex_add_resolution(
                    event, (900, 901), pending, 123, None, None
                ))
                await sent.wait()
                llm_chat.CODEX_USERS_ADD_PENDING.pop((900, 901))
                release.set()
                with self.assertRaises(llm_chat.events.StopPropagation):
                    await task
            self.assertEqual(mocked.await_count, 1)

        asyncio.run(scenario())

    def test_text_cancel_during_slow_picker_resolution_prevents_preview(self):
        async def scenario():
            llm_chat.CODEX_USERS_ADD_PENDING.clear()
            pending = {"token": "safe", "prompt_id": 777, "phase": "input", "request_id": 55}
            llm_chat.CODEX_USERS_ADD_PENDING[(900, 901)] = pending
            started = asyncio.Event()
            release = asyncio.Event()

            async def resolve(value):
                started.set()
                await release.wait()
                return 123, None, None

            selected = AddEvent()
            selected.message.action = MessageActionRequestedPeerSentMe(55, [RequestedPeerUser(123)])
            cancel = AddEvent("Cancel")
            with patch.object(llm_chat, "_codex_users_admin", new=AsyncMock(return_value=True)), patch.object(
                llm_chat, "_resolve_codex_add_target", side_effect=resolve
            ), patch.object(llm_chat, "send_info_message", new=AsyncMock()) as send:
                task = asyncio.create_task(llm_chat.codex_user_requested_peer_handler(selected))
                await started.wait()
                with self.assertRaises(llm_chat.events.StopPropagation):
                    await llm_chat.codex_user_add_input_handler(cancel)
                release.set()
                with self.assertRaises(llm_chat.events.StopPropagation):
                    await task
            self.assertNotIn((900, 901), llm_chat.CODEX_USERS_ADD_PENDING)
            self.assertEqual(send.await_count, 1)
            self.assertIn("cancelled", send.await_args.args[1])

        asyncio.run(scenario())

    def test_text_cancel_during_confirmation_authorization_prevents_write(self):
        async def scenario():
            llm_chat.CODEX_USERS_ADD_PENDING.clear()
            llm_chat.CODEX_USERS_ADD_PENDING[(900, 901)] = {
                "token": "right", "prompt_id": 777, "phase": "confirm",
                "target_id": 123, "request_id": 55,
            }
            started = asyncio.Event()
            release = asyncio.Event()

            async def authorize(event):
                started.set()
                await release.wait()
                return True

            confirm = AddEvent()
            confirm.data = b"cu:add:yes:right"
            cancel = AddEvent("Cancel")
            with patch.object(llm_chat, "_codex_users_admin", side_effect=authorize), patch.object(
                llm_chat.llm_chat_config, "add_user"
            ) as add, patch.object(llm_chat, "send_info_message", new=AsyncMock()):
                task = asyncio.create_task(llm_chat.callback_handler(confirm))
                await started.wait()
                with self.assertRaises(llm_chat.events.StopPropagation):
                    await llm_chat.codex_user_add_input_handler(cancel)
                release.set()
                await task
            add.assert_not_called()
            confirm.answer.assert_awaited_with("This add-user confirmation is stale.", show_alert=True)

        asyncio.run(scenario())

    def test_selection_fills_only_missing_cached_profile_fields(self):
        llm_chat.CODEX_USERS_ADD_PENDING.clear()
        llm_chat.CODEX_USERS_ADD_PENDING[(900, 901)] = {
            "token": "safe", "prompt_id": 777, "phase": "input", "request_id": 55,
        }
        event = AddEvent()
        event.message.action = MessageActionRequestedPeerSentMe(
            55, [RequestedPeerUser(123, first_name="Shared", last_name="Surname", username="shared")]
        )
        profile = llm_chat.llm_db.UserProfile(
            bot_id=42, user_id=123, first_name="Stored", last_name=None,
            username=None, private_contact_at=None,
        )
        with patch.object(llm_chat, "BOT_ID", 42), patch.object(
            llm_chat, "_codex_users_admin", new=AsyncMock(return_value=True)
        ), patch.object(builtins.borg, "get_entity", new=AsyncMock(side_effect=ValueError), create=True), patch.object(
            llm_chat.llm_db, "get_user_profile", return_value=profile
        ), patch.object(llm_chat.llm_chat_config, "load_config", return_value=config()), patch.object(
            llm_chat, "send_info_message", new=AsyncMock()
        ) as send:
            with self.assertRaises(llm_chat.events.StopPropagation):
                asyncio.run(llm_chat.codex_user_requested_peer_handler(event))
        preview = send.await_args_list[-1].args[1]
        self.assertIn("Stored Surname", preview)
        self.assertIn("@shared", preview)
        self.assertNotIn("Shared Surname", preview)

    def test_cancel_while_private_prompt_is_delivering_hides_late_keyboard(self):
        async def scenario():
            llm_chat.CODEX_USERS_ADD_PENDING.clear()
            delivering = asyncio.Event()
            release = asyncio.Event()

            async def deliver(*args, **kwargs):
                delivering.set()
                await release.wait()
                return SimpleNamespace(id=777)

            opener = AddEvent()
            opener.respond = AsyncMock(side_effect=deliver)
            cancel = AddEvent("Cancel")
            with patch.object(llm_chat, "send_info_message", new=AsyncMock()) as send:
                task = asyncio.create_task(llm_chat._start_codex_user_add(opener))
                await delivering.wait()
                with self.assertRaises(llm_chat.events.StopPropagation):
                    await llm_chat.codex_user_add_input_handler(cancel)
                release.set()
                await task
            self.assertNotIn((900, 901), llm_chat.CODEX_USERS_ADD_PENDING)
            self.assertEqual(send.await_count, 2)
            self.assertTrue(all(
                isinstance(call.kwargs["buttons"], ReplyKeyboardHide)
                for call in send.await_args_list
            ))

        asyncio.run(scenario())

    def test_sender_only_admin_does_not_trust_chat_policy(self):
        event = AddEvent(private=False)
        with patch.object(llm_chat.util, "admins", [901]), patch.object(
            llm_chat.util, "isAdmin", new=AsyncMock(return_value=True)
        ):
            self.assertFalse(asyncio.run(llm_chat._codex_users_admin(event)))

    def test_add_input_group_requires_prompt_reply_and_ignores_commands(self):
        llm_chat.CODEX_USERS_ADD_PENDING.clear()
        llm_chat.CODEX_USERS_ADD_PENDING[(900, 901)] = {
            "token": "token", "prompt_id": 777, "phase": "input"
        }
        for text, reply_to in (("123", None), ("/help", 777)):
            event = AddEvent(text, private=False, reply_to=reply_to)
            asyncio.run(llm_chat.codex_user_add_input_handler(event))
        event = AddEvent("unrelated words", private=False, reply_to=777)
        with self.assertRaises(llm_chat.events.StopPropagation):
            asyncio.run(llm_chat.codex_user_add_input_handler(event))
        self.assertEqual(llm_chat.CODEX_USERS_ADD_PENDING[(900, 901)]["phase"], "input")

    def test_add_numeric_unknown_previews_disabled_grants_without_contact(self):
        llm_chat.CODEX_USERS_ADD_PENDING.clear()
        llm_chat.CODEX_USERS_ADD_PENDING[(900, 901)] = {
            "token": "safe-token", "prompt_id": 777, "phase": "input"
        }
        event = AddEvent("123")
        with patch.object(llm_chat, "_codex_users_admin", new=AsyncMock(return_value=True)), patch.object(
            builtins.borg, "get_entity", new=AsyncMock(side_effect=ValueError), create=True
        ), patch.object(llm_chat.llm_chat_config, "load_config", return_value=config()), patch.object(
            llm_chat.llm_db, "record_user_profile"
        ) as record, patch.object(llm_chat, "send_info_message", new=AsyncMock()) as send:
            with self.assertRaises(llm_chat.events.StopPropagation):
                asyncio.run(llm_chat.codex_user_add_input_handler(event))
        self.assertIn("Profile not known yet", send.await_args.args[1])
        self.assertIn("Codex off; images off", send.await_args.args[1])
        record.assert_not_called()
        self.assertEqual(llm_chat.CODEX_USERS_ADD_PENDING[(900, 901)]["target_id"], 123)

    def test_username_add_rejects_nonusers_bots_and_admins(self):
        channel = SimpleNamespace(id=123, title="Group")
        bot = llm_chat.User(id=123, first_name="Bot", username="botname", bot=True)
        admin = llm_chat.User(id=123, first_name="Admin", username="boss")
        for entity, expected in (
            (channel, "group or channel"),
            (bot, "Bots"),
            (admin, "administrators"),
        ):
            with self.subTest(expected=expected), patch.object(
                builtins.borg, "get_entity", new=AsyncMock(return_value=entity), create=True
            ), patch.object(llm_chat.util, "admins", ["boss"] if entity is admin else []):
                _, _, error = asyncio.run(llm_chat._resolve_codex_add_target("@validname"))
            self.assertIn(expected, error)

    def test_confirmation_is_scoped_rechecks_auth_and_never_changes_model(self):
        llm_chat.CODEX_USERS_ADD_PENDING.clear()
        llm_chat.CODEX_USERS_ADD_PENDING[(900, 901)] = {
            "token": "right", "prompt_id": 777, "phase": "confirm", "target_id": 123
        }
        stale = AddEvent()
        stale.data = b"cu:add:yes:wrong"
        asyncio.run(llm_chat.callback_handler(stale))
        stale.answer.assert_awaited_with("This add-user confirmation is stale.", show_alert=True)
        self.assertIn((900, 901), llm_chat.CODEX_USERS_ADD_PENDING)

        event = AddEvent()
        event.data = b"cu:add:yes:right"
        with patch.object(llm_chat, "_codex_users_admin", new=AsyncMock(return_value=True)), patch.object(
            builtins.borg, "get_entity", new=AsyncMock(side_effect=ValueError), create=True
        ), patch.object(llm_chat.llm_chat_config, "load_config", return_value=config()), patch.object(
            llm_chat.llm_chat_config, "add_user", return_value=config(123)
        ) as add, patch.object(llm_chat, "_show_codex_user_detail", new=AsyncMock(return_value=True)), patch.object(
            llm_chat.user_manager, "set_model"
        ) as set_model:
            asyncio.run(llm_chat.callback_handler(event))
        add.assert_called_once_with(123)
        set_model.assert_not_called()
        self.assertNotIn((900, 901), llm_chat.CODEX_USERS_ADD_PENDING)

    def test_confirmation_revocation_invalid_config_and_duplicate_do_not_add(self):
        cases = ((False, config()), (True, config(valid=False)), (True, config(123)))
        for authorized, cfg in cases:
            with self.subTest(authorized=authorized, cfg=cfg):
                llm_chat.CODEX_USERS_ADD_PENDING.clear()
                llm_chat.CODEX_USERS_ADD_PENDING[(900, 901)] = {
                    "token": "right", "prompt_id": 777, "phase": "confirm", "target_id": 123
                }
                event = AddEvent()
                event.data = b"cu:add:yes:right"
                with patch.object(
                    llm_chat, "_codex_users_admin", new=AsyncMock(return_value=authorized)
                ), patch.object(llm_chat.llm_chat_config, "load_config", return_value=cfg), patch.object(
                    llm_chat.llm_chat_config, "add_user"
                ) as add, patch.object(
                    llm_chat, "_show_codex_user_detail", new=AsyncMock(return_value=True)
                ):
                    asyncio.run(llm_chat.callback_handler(event))
                add.assert_not_called()

    def test_add_callback_denies_nonadmin_even_when_chat_is_trusted(self):
        event = AddEvent(private=False)
        event.data = b"cu:add"
        with patch.object(llm_chat.util, "admins", [901]), patch.object(
            llm_chat.util, "isAdmin", new=AsyncMock(return_value=True)
        ):
            asyncio.run(llm_chat.callback_handler(event))
        event.answer.assert_awaited_with(llm_chat.ADMIN_ONLY_COMMAND_IGNORED, show_alert=True)
        event.respond.assert_not_awaited()

    def test_add_callback_rejects_invalid_config_before_prompt(self):
        event = AddEvent()
        event.data = b"cu:add"
        with patch.object(llm_chat, "_codex_users_admin", new=AsyncMock(return_value=True)), patch.object(
            llm_chat.llm_chat_config, "load_config", return_value=config(valid=False)
        ):
            asyncio.run(llm_chat.callback_handler(event))
        event.respond.assert_not_awaited()
        event.answer.assert_awaited_with(
            "The LLM chat access configuration is invalid.", show_alert=True
        )

    def test_unknown_numeric_bot_id_is_rejected(self):
        with patch.object(llm_chat, "BOT_ID", 42), patch.object(
            builtins.borg, "get_entity", new=AsyncMock(side_effect=ValueError), create=True
        ):
            _, _, error = asyncio.run(llm_chat._resolve_codex_add_target("42"))
        self.assertIn("bot itself", error)

    def test_cancel_token_is_scoped_to_admin_and_chat(self):
        llm_chat.CODEX_USERS_ADD_PENDING.clear()
        llm_chat.CODEX_USERS_ADD_PENDING[(900, 901)] = {
            "token": "right", "prompt_id": 777, "phase": "input"
        }
        wrong_chat = AddEvent(chat_id=902)
        wrong_chat.data = b"cu:add:cancel:right"
        asyncio.run(llm_chat.callback_handler(wrong_chat))
        wrong_chat.answer.assert_awaited_with("This add-user prompt is stale.", show_alert=True)
        self.assertIn((900, 901), llm_chat.CODEX_USERS_ADD_PENDING)

    def test_reverse_list_registration_orders_observer_then_add_flow(self):
        registered = []

        class RegisteringBorg:
            def on(self, builder):
                return lambda handler: registered.append(handler.__name__) or handler

        with patch.object(builtins, "borg", RegisteringBorg()):
            llm_chat.register_handlers()
        self.assertEqual(
            list(reversed(registered[-2:])),
            ["observe_incoming_user_profile", "codex_user_add_input_handler"],
        )

    def test_concurrent_add_inputs_cannot_replace_visible_preview_target(self):
        async def scenario():
            llm_chat.CODEX_USERS_ADD_PENDING.clear()
            pending = {"token": "token", "prompt_id": 777, "phase": "input"}
            llm_chat.CODEX_USERS_ADD_PENDING[(900, 901)] = pending
            started = asyncio.Event()
            release = asyncio.Event()

            async def resolve(value):
                started.set()
                await release.wait()
                return int(value), None, None

            first = AddEvent("123")
            second = AddEvent("124")
            with patch.object(llm_chat, "_codex_users_admin", new=AsyncMock(return_value=True)), patch.object(
                llm_chat, "_resolve_codex_add_target", side_effect=resolve
            ), patch.object(llm_chat.llm_chat_config, "load_config", return_value=config()), patch.object(
                llm_chat, "send_info_message", new=AsyncMock()
            ):
                first_task = asyncio.create_task(llm_chat.codex_user_add_input_handler(first))
                await started.wait()
                self.assertEqual(pending["phase"], "resolving")
                await llm_chat.codex_user_add_input_handler(second)
                release.set()
                with self.assertRaises(llm_chat.events.StopPropagation):
                    await first_task
            self.assertEqual(pending["target_id"], 123)
            self.assertEqual(pending["phase"], "confirm")

        asyncio.run(scenario())

    def test_cancel_during_final_authorization_prevents_write(self):
        async def scenario():
            llm_chat.CODEX_USERS_ADD_PENDING.clear()
            pending = {
                "token": "right", "prompt_id": 777, "phase": "confirm", "target_id": 123
            }
            llm_chat.CODEX_USERS_ADD_PENDING[(900, 901)] = pending
            final_auth_started = asyncio.Event()
            release = asyncio.Event()
            auth_calls = 0

            async def authorize(event):
                nonlocal auth_calls
                auth_calls += 1
                if auth_calls == 2:
                    final_auth_started.set()
                    await release.wait()
                return True

            confirm = AddEvent()
            confirm.data = b"cu:add:yes:right"
            cancel = AddEvent()
            cancel.data = b"cu:add:cancel:right"
            with patch.object(llm_chat, "_codex_users_admin", side_effect=authorize), patch.object(
                llm_chat, "_resolve_codex_add_target", new=AsyncMock(return_value=(123, None, None))
            ), patch.object(llm_chat.llm_chat_config, "load_config", return_value=config()), patch.object(
                llm_chat.llm_chat_config, "add_user"
            ) as add:
                task = asyncio.create_task(llm_chat.callback_handler(confirm))
                await final_auth_started.wait()
                await llm_chat.callback_handler(cancel)
                release.set()
                await task
            add.assert_not_called()
            confirm.answer.assert_awaited_with("This add-user confirmation is stale.", show_alert=True)

        asyncio.run(scenario())

    def test_double_confirmation_performs_at_most_one_write(self):
        async def scenario():
            llm_chat.CODEX_USERS_ADD_PENDING.clear()
            llm_chat.CODEX_USERS_ADD_PENDING[(900, 901)] = {
                "token": "right", "prompt_id": 777, "phase": "confirm", "target_id": 123
            }
            started = asyncio.Event()
            release = asyncio.Event()
            calls = 0

            async def authorize(event):
                nonlocal calls
                calls += 1
                if calls == 1:
                    started.set()
                    await release.wait()
                return True

            first = AddEvent()
            first.data = b"cu:add:yes:right"
            second = AddEvent()
            second.data = b"cu:add:yes:right"
            with patch.object(llm_chat, "_codex_users_admin", side_effect=authorize), patch.object(
                llm_chat, "_resolve_codex_add_target", new=AsyncMock(return_value=(123, None, None))
            ), patch.object(llm_chat.llm_chat_config, "load_config", return_value=config()), patch.object(
                llm_chat.llm_chat_config, "add_user", return_value=config(123)
            ) as add, patch.object(
                llm_chat, "_show_codex_user_detail", new=AsyncMock(return_value=True)
            ):
                task = asyncio.create_task(llm_chat.callback_handler(first))
                await started.wait()
                await llm_chat.callback_handler(second)
                release.set()
                await task
            add.assert_called_once_with(123)
            second.answer.assert_awaited_with("This add-user confirmation is stale.", show_alert=True)

        asyncio.run(scenario())

    def test_prompt_is_not_active_until_delivery_and_can_be_cancelled(self):
        async def scenario():
            llm_chat.CODEX_USERS_ADD_PENDING.clear()
            event = AddEvent()
            unrelated = AddEvent("123", private=False)
            unrelated.message.reply_to_msg_id = None
            async def deliver(*args, **kwargs):
                await llm_chat.codex_user_add_input_handler(unrelated)
                llm_chat.CODEX_USERS_ADD_PENDING.clear()
                return SimpleNamespace(id=777)
            event.respond = AsyncMock(side_effect=deliver)
            with patch.object(llm_chat, "_resolve_codex_add_target", new=AsyncMock()) as resolve:
                await llm_chat._start_codex_user_add(event)
            resolve.assert_not_awaited()
            self.assertFalse(llm_chat.CODEX_USERS_ADD_PENDING)
        asyncio.run(scenario())

    def test_new_flow_survives_previous_confirmation_write(self):
        llm_chat.CODEX_USERS_ADD_PENDING.clear()
        llm_chat.CODEX_USERS_ADD_PENDING[(900, 901)] = {
            "token": "right", "prompt_id": 777, "phase": "confirm", "target_id": 123
        }
        new_pending = {"token": "new", "prompt_id": 888, "phase": "input"}
        async def write(*args, **kwargs):
            llm_chat.CODEX_USERS_ADD_PENDING[(900, 901)] = new_pending
            return config(123)
        event = AddEvent()
        event.data = b"cu:add:yes:right"
        with patch.object(llm_chat, "_codex_users_admin", new=AsyncMock(return_value=True)), patch.object(
            llm_chat, "_resolve_codex_add_target", new=AsyncMock(return_value=(123, None, None))
        ), patch.object(llm_chat.llm_chat_config, "load_config", return_value=config()), patch.object(
            llm_chat.asyncio, "to_thread", new=AsyncMock(side_effect=write)
        ), patch.object(llm_chat, "_show_codex_user_detail", new=AsyncMock(return_value=True)):
            asyncio.run(llm_chat.callback_handler(event))
        self.assertIs(llm_chat.CODEX_USERS_ADD_PENDING[(900, 901)], new_pending)

    def test_group_cancel_without_reply_during_prompt_delivery_is_ignored(self):
        pending = {"token": "group", "prompt_id": None, "phase": "prompting"}
        event = AddEvent("Cancel", private=False)
        with patch.dict(llm_chat.CODEX_USERS_ADD_PENDING, {(900, 901): pending}, clear=True), patch.object(
            llm_chat, "send_info_message", new=AsyncMock()
        ) as send:
            asyncio.run(llm_chat.codex_user_add_input_handler(event))
            self.assertIs(llm_chat.CODEX_USERS_ADD_PENDING[(900, 901)], pending)
            send.assert_not_awaited()

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
