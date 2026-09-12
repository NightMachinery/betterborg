import asyncio
import builtins
import importlib
import unittest
from unittest.mock import AsyncMock, Mock, patch

from uniborg import llm_chat_config
from uniborg.constants import OPENAI_CODEX_GPT_5_6_SOL, OR_OPENAI_LATEST


class _FakeLoop:
    def create_task(self, coro):
        coro.close()
        return None


class _FakeBorg:
    loop = _FakeLoop()


async def _import_llm_chat():
    return importlib.import_module("llm_chat_plugins.llm_chat")


builtins.borg = _FakeBorg()
llm_chat = asyncio.run(_import_llm_chat())


class Event:
    sender_id = 123
    chat_id = 456
    is_private = True


class CodexAccessIntegrationTests(unittest.TestCase):
    def test_authorized_nonadmin_gets_codex_picker_without_admin_models(self):
        choices = llm_chat._model_choices_for_access(admin_p=False, codex_p=True)
        self.assertIn(OPENAI_CODEX_GPT_5_6_SOL, choices)
        for model in llm_chat.ADMIN_MODEL_CHOICES:
            self.assertNotIn(model, choices)

    def test_admin_without_policy_does_not_get_codex_picker(self):
        choices = llm_chat._model_choices_for_access(admin_p=True, codex_p=False)
        self.assertNotIn(OPENAI_CODEX_GPT_5_6_SOL, choices)

    def test_codex_prefix_and_reasoning_work_for_authorized_nonadmin(self):
        result = llm_chat._detect_and_process_message_prefix(
            ".th .c hello", admin_p=False, codex_p=True
        )
        self.assertEqual(result.model, OPENAI_CODEX_GPT_5_6_SOL)
        self.assertEqual(result.reasoning_effort, "high")
        self.assertEqual(result.processed_text, "hello")

    def test_revoked_c_prefix_keeps_openrouter_meaning(self):
        result = llm_chat._detect_and_process_message_prefix(
            ".c hello", admin_p=True, codex_p=False
        )
        self.assertEqual(result.model, OR_OPENAI_LATEST)

    def test_restricted_prefix_is_recognized_for_explicit_denial(self):
        result = llm_chat._detect_and_process_message_prefix(
            ".cm hello", admin_p=True, codex_p=False
        )
        self.assertEqual(result.model, OPENAI_CODEX_GPT_5_6_SOL)
        config = llm_chat_config.LLMChatConfig((), ())
        self.assertFalse(
            asyncio.run(
                llm_chat._can_user_access_model(
                    Event(), result.model, config=config
                )
            )
        )
        self.assertEqual(
            llm_chat._model_access_denial(result.model),
            llm_chat.CODEX_ACCESS_DENIED,
        )

    def test_explicit_id_allows_codex_dispatch_for_nonadmin(self):
        config = llm_chat_config.LLMChatConfig((123,), ())
        with patch.object(llm_chat.util, "isAdmin", new=AsyncMock(return_value=False)):
            allowed = asyncio.run(
                llm_chat._can_user_access_model(
                    Event(), OPENAI_CODEX_GPT_5_6_SOL, config=config
                )
            )
        self.assertTrue(allowed)

    def test_authorized_nonadmin_callback_can_select_codex(self):
        event = Event()
        event.data = b"model_codex"
        event.edit = AsyncMock()
        event.answer = AsyncMock()
        config = llm_chat_config.LLMChatConfig((123,), ())
        menu = llm_chat.ModelMenu(
            options={OPENAI_CODEX_GPT_5_6_SOL: "Codex"},
            current_value=OPENAI_CODEX_GPT_5_6_SOL,
            think_state=Mock(),
        )
        with patch.object(llm_chat.llm_chat_config, "load_config", return_value=config), patch.object(
            llm_chat.util, "isAdmin", new=AsyncMock(return_value=False)
        ), patch.object(
            llm_chat.bot_util,
            "unsanitize_callback_data",
            return_value=OPENAI_CODEX_GPT_5_6_SOL,
        ), patch.object(llm_chat.user_manager, "set_model") as set_model, patch.object(
            llm_chat, "_build_model_menu", return_value=menu
        ):
            asyncio.run(llm_chat.callback_handler(event))
        set_model.assert_called_once_with(123, OPENAI_CODEX_GPT_5_6_SOL)

    def test_revoked_callback_cannot_select_codex(self):
        event = Event()
        event.data = b"model_codex"
        event.answer = AsyncMock()
        config = llm_chat_config.LLMChatConfig((), ())
        with patch.object(llm_chat.llm_chat_config, "load_config", return_value=config), patch.object(
            llm_chat.util, "isAdmin", new=AsyncMock(return_value=True)
        ), patch.object(
            llm_chat.bot_util,
            "unsanitize_callback_data",
            return_value=OPENAI_CODEX_GPT_5_6_SOL,
        ), patch.object(llm_chat.user_manager, "set_model") as set_model:
            asyncio.run(llm_chat.callback_handler(event))
        set_model.assert_not_called()
        event.answer.assert_awaited_with(llm_chat.CODEX_ACCESS_DENIED, show_alert=True)

    def test_revoked_stale_model_menu_cannot_change_codex_reasoning(self):
        event = Event()
        event.text = "think:high"
        config = llm_chat_config.LLMChatConfig((), ())
        llm_chat.AWAITING_INPUT_FROM_USERS[123] = {"type": "model"}
        with patch.object(llm_chat.llm_chat_config, "load_config", return_value=config), patch.object(
            llm_chat, "_scope_selected_model", return_value=OPENAI_CODEX_GPT_5_6_SOL
        ), patch.object(llm_chat, "send_info_message", new=AsyncMock()), patch.object(
            llm_chat, "_apply_reasoning_menu_choice", new=AsyncMock()
        ) as apply_choice:
            asyncio.run(llm_chat.generic_input_handler(event))
        apply_choice.assert_not_awaited()

    def test_stale_chat_menu_rechecks_group_permission(self):
        event = Event()
        event.is_private = False
        event.text = "think:high"
        llm_chat.AWAITING_INPUT_FROM_USERS[123] = {"type": "chatmodel"}
        with patch.object(
            llm_chat.util, "isAdmin", new=AsyncMock(return_value=False)
        ), patch.object(
            llm_chat.util, "is_group_admin", new=AsyncMock(return_value=False)
        ), patch.object(llm_chat, "send_info_message", new=AsyncMock()), patch.object(
            llm_chat, "_apply_reasoning_menu_choice", new=AsyncMock()
        ) as apply_choice:
            asyncio.run(llm_chat.generic_input_handler(event))
        apply_choice.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
