import asyncio
import builtins
import importlib
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from uniborg import llm_chat_config
from uniborg.constants import OPENAI_CODEX_GPT_5_6_SOL


class _FakeLoop:
    def create_task(self, coro):
        coro.close()


builtins.borg = SimpleNamespace(loop=_FakeLoop())


async def _import_plugin():
    return importlib.import_module("llm_chat_plugins.llm_chat")


plugin = asyncio.run(_import_plugin())


class CodexFallbackNoticeTests(unittest.TestCase):
    def run_request(self, *, text="hello", trusted=False, selected=None):
        selected = selected or OPENAI_CODEX_GPT_5_6_SOL
        event = SimpleNamespace(
            sender_id=123, chat_id=456, grouped_id=None,
            is_private=True, text=text, file=None,
        )
        config = llm_chat_config.LLMChatConfig(
            ("MAGIC_ADMINS",), (),
            codex_users=(llm_chat_config.CodexUser(123, "Test user", False, False),),
        )
        with ExitStack() as stack:
            for name in ("cleanup_completed_tasks",):
                stack.enter_context(patch.object(plugin, name))
            stack.enter_context(patch.object(plugin, "AWAITING_INPUT_FROM_USERS", {}))
            stack.enter_context(patch.object(plugin.llm_db, "is_awaiting_key", return_value=False))
            stack.enter_context(patch.object(
                plugin.gemini_live_util.live_session_manager,
                "is_live_mode_active", return_value=False,
            ))
            stack.enter_context(patch.object(plugin.user_manager, "get_prefs", return_value=SimpleNamespace()))
            save = stack.enter_context(patch.object(plugin.user_manager, "set_model"))
            stack.enter_context(patch.object(plugin.util, "isAdmin", new=AsyncMock(return_value=trusted)))
            stack.enter_context(patch.object(plugin.llm_chat_config, "load_config", return_value=config))
            stack.enter_context(patch.object(
                plugin, "_determine_context_mode_and_handle_transitions",
                new=AsyncMock(return_value="recent"),
            ))
            stack.enter_context(patch.object(
                plugin, "_get_effective_model_and_service",
                side_effect=lambda *args, prefix_model=None: (
                    prefix_model or selected,
                    plugin.llm_util.get_service_from_model(prefix_model or selected),
                ),
            ))
            # Stop after model selection, before any provider or Telegram request.
            api_key = stack.enter_context(patch.object(plugin, "get_effective_api_key", return_value=None))
            key_prompt = stack.enter_context(patch.object(plugin.llm_db, "request_api_key_message", new=AsyncMock()))
            info = stack.enter_context(patch.object(plugin, "send_info_message", new=AsyncMock()))
            asyncio.run(plugin.chat_handler(event))
        return info, api_key, key_prompt, save

    def test_stored_codex_fallback_warns_before_missing_key_prompt(self):
        info, api_key, key_prompt, save = self.run_request()
        self.assertEqual(info.await_count, 1)
        self.assertIn("Falling back", info.await_args.args[1])
        self.assertIn("saved model settings are unchanged", info.await_args.args[1])
        self.assertIsNone(info.await_args.kwargs["parse_mode"])
        fallback_service = plugin.llm_util.get_service_from_model(plugin.DEFAULT_MODEL)
        api_key.assert_called_once_with(123, fallback_service)
        self.assertEqual(key_prompt.await_args.args[1], fallback_service)
        save.assert_not_called()

    def test_trusted_access_does_not_warn_or_fall_back(self):
        info, api_key, _, save = self.run_request(trusted=True)
        info.assert_not_awaited()
        api_key.assert_called_once_with(123, "codex")
        save.assert_not_called()

    def test_explicit_codex_request_denies_without_fallback_notice(self):
        info, api_key, key_prompt, save = self.run_request(text=".cm hello")
        self.assertEqual(info.await_args.args[1], plugin.CODEX_ACCESS_DENIED)
        api_key.assert_not_called()
        key_prompt.assert_not_awaited()
        save.assert_not_called()

    def test_non_codex_default_does_not_warn(self):
        info, _, _, _ = self.run_request(selected=plugin.DEFAULT_MODEL)
        info.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
