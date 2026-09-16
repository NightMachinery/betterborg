import asyncio
import builtins
import importlib
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from uniborg import codex_util, llm_chat_config

from uniborg.constants import (
    GEMINI_FLASH_LATEST,
    OPENAI_CODEX_ASTRA,
    OPENAI_CODEX_GPT_5_6_SOL,
    OPENAI_CODEX_LUNA_RESERVE,
)


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


class CodexImagePrefixTests(unittest.TestCase):
    def test_image_prefix_defaults_false(self):
        result = llm_chat._detect_and_process_message_prefix("hello", codex_p=True)
        self.assertFalse(result.image_generation)

    def test_image_prefix_is_whitespace_delimited(self):
        result = llm_chat._detect_and_process_message_prefix(
            ".illustrate this", codex_p=True
        )
        self.assertFalse(result.image_generation)
        self.assertEqual(result.processed_text, ".illustrate this")

    def test_image_model_and_effort_prefixes_combine_in_any_order(self):
        inputs = (".i .as .th draw", ".as .i .th draw", ".th .i .as draw")
        for text in inputs:
            result = llm_chat._detect_and_process_message_prefix(text, codex_p=True)
            self.assertTrue(result.image_generation)
            self.assertEqual(result.model, OPENAI_CODEX_ASTRA)
            self.assertEqual(result.reasoning_effort, "high")
            self.assertEqual(result.processed_text, "draw")

    def test_history_can_strip_image_prefix_without_propagating_state(self):
        historical = llm_chat._detect_and_process_message_prefix(
            ".i old request", codex_p=True
        )
        current = llm_chat._detect_and_process_message_prefix("continue", codex_p=True)
        self.assertEqual(historical.processed_text, "old request")
        self.assertTrue(historical.image_generation)
        self.assertFalse(current.image_generation)

    def test_model_resolution_precedence(self):
        self.assertEqual(
            llm_chat._resolve_image_generation_model(
                prefix_model=OPENAI_CODEX_GPT_5_6_SOL,
                selected_model=OPENAI_CODEX_ASTRA,
            ),
            OPENAI_CODEX_GPT_5_6_SOL,
        )
        self.assertEqual(
            llm_chat._resolve_image_generation_model(
                prefix_model=None, selected_model=OPENAI_CODEX_ASTRA
            ),
            OPENAI_CODEX_ASTRA,
        )
        self.assertEqual(
            llm_chat._resolve_image_generation_model(
                prefix_model=None, selected_model=GEMINI_FLASH_LATEST
            ),
            OPENAI_CODEX_GPT_5_6_SOL,
        )

    def test_a_codex_stand_in_takes_over_an_unprefixed_request(self):
        #: `.i` is a flag, not a model choice, so an armed stand-in applies to
        #: it -- and the Reserve generates images.
        self.assertEqual(
            llm_chat._resolve_image_generation_model(
                prefix_model=None,
                selected_model=OPENAI_CODEX_GPT_5_6_SOL,
                stand_in=OPENAI_CODEX_LUNA_RESERVE,
            ),
            OPENAI_CODEX_LUNA_RESERVE,
        )

    def test_a_non_codex_stand_in_cannot_take_an_image_request(self):
        #: Image generation runs as a Codex tool; there is nowhere else to
        #: send it, so the saved Codex model stands.
        self.assertEqual(
            llm_chat._resolve_image_generation_model(
                prefix_model=None,
                selected_model=OPENAI_CODEX_ASTRA,
                stand_in=GEMINI_FLASH_LATEST,
            ),
            OPENAI_CODEX_ASTRA,
        )

    def test_an_explicit_codex_prefix_outranks_the_stand_in(self):
        #: `.as` *is* a model choice, and those stay where they were aimed.
        self.assertEqual(
            llm_chat._resolve_image_generation_model(
                prefix_model=OPENAI_CODEX_ASTRA,
                selected_model=OPENAI_CODEX_GPT_5_6_SOL,
                stand_in=OPENAI_CODEX_LUNA_RESERVE,
            ),
            OPENAI_CODEX_ASTRA,
        )

    def test_explicit_non_codex_model_conflicts(self):
        with self.assertRaisesRegex(ValueError, "only be combined with a Codex"):
            llm_chat._resolve_image_generation_model(
                prefix_model=GEMINI_FLASH_LATEST,
                selected_model=OPENAI_CODEX_ASTRA,
            )

    def test_tool_is_only_added_for_current_image_request(self):
        self.assertEqual(
            llm_chat._codex_tools_for_request([], image_generation=False), []
        )
        self.assertEqual(
            llm_chat._codex_tools_for_request(["googleSearch"], image_generation=False),
            [{"type": "web_search"}],
        )
        self.assertEqual(
            llm_chat._codex_tools_for_request(["googleSearch"], image_generation=True),
            [
                {"type": "web_search"},
                {"type": "image_generation", "partial_images": 3},
            ],
        )


class _Action:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class CodexTelegramDeliveryTests(unittest.TestCase):
    def _run_text_dispatch(self, *, text, selected_model, history, stream):
        event = SimpleNamespace(
            sender_id=123,
            chat_id=44,
            grouped_id=None,
            is_private=True,
            text=text,
            file=None,
            id=55,
            message=SimpleNamespace(text=text),
        )
        response_message = SimpleNamespace(delete=AsyncMock(), id=999)
        prefs = SimpleNamespace(
            group_activation_mode="mention_and_reply",
            enabled_tools=["googleSearch"],
            json_mode=False,
        )
        config = llm_chat_config.LLMChatConfig((123,), (123,))
        with ExitStack() as stack:
            stack.enter_context(patch.object(llm_chat, "cleanup_completed_tasks"))
            stack.enter_context(
                patch.object(llm_chat.llm_db, "is_awaiting_key", return_value=False)
            )
            stack.enter_context(
                patch.object(
                    llm_chat.gemini_live_util.live_session_manager,
                    "is_live_mode_active",
                    return_value=False,
                )
            )
            stack.enter_context(
                patch.object(llm_chat.user_manager, "get_prefs", return_value=prefs)
            )
            stack.enter_context(
                patch.object(
                    llm_chat.util, "isAdmin", new=AsyncMock(return_value=False)
                )
            )
            stack.enter_context(
                patch.object(
                    llm_chat.llm_chat_config, "load_config", return_value=config
                )
            )
            stack.enter_context(
                patch.object(
                    llm_chat,
                    "_determine_context_mode_and_handle_transitions",
                    new=AsyncMock(return_value="recent"),
                )
            )
            stack.enter_context(
                patch.object(
                    llm_chat,
                    "_get_effective_model_and_service",
                    side_effect=lambda *args, prefix_model=None: (
                        (prefix_model, "codex")
                        if prefix_model
                        else (selected_model, "codex")
                    ),
                )
            )
            stack.enter_context(
                patch.object(llm_chat, "get_effective_api_key", return_value="key")
            )
            info = stack.enter_context(
                patch.object(
                    llm_chat,
                    "send_info_message",
                    new=AsyncMock(return_value=response_message),
                )
            )
            stack.enter_context(
                patch.object(
                    llm_chat,
                    "build_conversation_history",
                    new=AsyncMock(
                        return_value=SimpleNamespace(history=history, warnings=[])
                    ),
                )
            )
            stack.enter_context(
                patch.object(
                    llm_chat,
                    "_get_effective_reasoning",
                    return_value=SimpleNamespace(level=None),
                )
            )
            stack.enter_context(
                patch.object(llm_chat.codex_util, "stream_codex_response", new=stream)
            )
            stack.enter_context(patch.object(llm_chat, "add_active_llm_task"))
            stack.enter_context(patch.object(llm_chat, "remove_active_llm_task"))
            stack.enter_context(
                patch.object(llm_chat, "_handle_tts_response", new=AsyncMock())
            )
            stack.enter_context(
                patch.object(llm_chat, "_log_conversation", new=AsyncMock())
            )
            edit_message = stack.enter_context(
                patch.object(llm_chat.util, "edit_message", new=AsyncMock())
            )
            asyncio.run(llm_chat.chat_handler(event))
        return info, edit_message

    def test_chat_handler_denies_each_incomplete_access_matrix(self):
        for codex_policy, image_policy in (((123,), ()), ((), (123,)), ((), ())):
            event = SimpleNamespace(
                sender_id=123,
                chat_id=44,
                grouped_id=None,
                is_private=True,
                text=".i draw",
                file=None,
            )
            config = llm_chat_config.LLMChatConfig(codex_policy, image_policy)
            reply = AsyncMock()
            with (
                patch.object(llm_chat, "cleanup_completed_tasks"),
                patch.object(llm_chat.llm_db, "is_awaiting_key", return_value=False),
                patch.object(
                    llm_chat.gemini_live_util.live_session_manager,
                    "is_live_mode_active",
                    return_value=False,
                ),
                patch.object(
                    llm_chat.user_manager,
                    "get_prefs",
                    return_value=SimpleNamespace(
                        group_activation_mode="mention_and_reply"
                    ),
                ),
                patch.object(
                    llm_chat.util, "isAdmin", new=AsyncMock(return_value=False)
                ),
                patch.object(
                    llm_chat.llm_chat_config, "load_config", return_value=config
                ) as load_config,
                patch.object(
                    llm_chat,
                    "_determine_context_mode_and_handle_transitions",
                    new=AsyncMock(return_value="recent"),
                ),
                patch.object(llm_chat, "send_info_message", new=reply),
            ):
                asyncio.run(llm_chat.chat_handler(event))
            reply.assert_awaited_once_with(event, llm_chat.CODEX_IMAGEGEN_ACCESS_DENIED)
            load_config.assert_called_once_with()

    def test_image_helper_sends_caption_and_replies_to_trigger(self):
        event = type("Event", (), {})()
        event.chat = object()
        event.chat_id = 44
        event.id = 55
        event.client = type("Client", (), {})()
        event.client.send_file = AsyncMock()
        old_borg = builtins.borg
        builtins.borg = type("Borg", (), {"action": lambda *args: _Action()})()
        try:
            sent = asyncio.run(
                llm_chat._send_image_to_telegram(
                    event,
                    b"png",
                    filename_base="codex_preview",
                    file_extension=".png",
                    file_index=2,
                    caption="Codex preview 2",
                )
            )
        finally:
            builtins.borg = old_borg
        self.assertTrue(sent)
        event.client.send_file.assert_awaited_once()
        args, kwargs = event.client.send_file.await_args
        self.assertEqual(args, (44,))
        self.assertEqual(kwargs["reply_to"], 55)
        self.assertEqual(kwargs["caption"], "Codex preview 2")
        self.assertEqual(kwargs["file"].name, "codex_preview_2.png")

    def test_history_image_request_and_attachment_do_not_enable_tool(self):
        captured = {}

        async def stream(**kwargs):
            captured.update(kwargs)
            return codex_util.CodexResponse(text="ok")

        self._run_text_dispatch(
            text="continue",
            selected_model=OPENAI_CODEX_ASTRA,
            history=[
                {"role": "user", "content": ".i old request"},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "continue"},
                        {
                            "type": "image_url",
                            "image_url": {"url": "data:image/png;base64,eA=="},
                        },
                    ],
                },
            ],
            stream=stream,
        )
        self.assertEqual(captured["model"], OPENAI_CODEX_ASTRA)
        self.assertEqual(captured["tools"], [{"type": "web_search"}])
        self.assertIsNone(captured["image_callback"])

    def test_current_image_request_uses_selected_codex_model(self):
        captured = {}

        async def stream(**kwargs):
            captured.update(kwargs)
            return codex_util.CodexResponse(text="clarify")

        self._run_text_dispatch(
            text=".i draw",
            selected_model=OPENAI_CODEX_ASTRA,
            history=[{"role": "user", "content": "draw"}],
            stream=stream,
        )
        self.assertEqual(captured["model"], OPENAI_CODEX_ASTRA)
        self.assertIn(
            {"type": "image_generation", "partial_images": 3}, captured["tools"]
        )

    def test_explicit_non_codex_image_request_never_calls_backend(self):
        calls = 0

        async def stream(**kwargs):
            nonlocal calls
            calls += 1
            return codex_util.CodexResponse(text="unexpected")

        info, _ = self._run_text_dispatch(
            text=".f .i draw",
            selected_model=OPENAI_CODEX_ASTRA,
            history=[{"role": "user", "content": "draw"}],
            stream=stream,
        )
        self.assertEqual(calls, 0)
        info.assert_awaited_once()
        self.assertEqual(
            info.await_args.args[1], llm_chat.CODEX_IMAGEGEN_MODEL_CONFLICT
        )

    def test_codex_stream_error_preserves_partial_text_and_reports_error(self):
        calls = 0

        async def stream(**kwargs):
            nonlocal calls
            calls += 1
            raise codex_util.CodexStreamError(
                "backend failed", codex_util.CodexResponse(text="partial answer")
            )

        _, edit_message = self._run_text_dispatch(
            text=".i draw",
            selected_model=OPENAI_CODEX_ASTRA,
            history=[{"role": "user", "content": "draw"}],
            stream=stream,
        )
        self.assertEqual(calls, 1)
        rendered = edit_message.await_args.args[1]
        self.assertIn("partial answer", rendered)
        self.assertIn("backend failed", rendered)

    def test_chat_handler_enables_tool_delivers_image_and_removes_placeholder(self):
        event = SimpleNamespace(
            sender_id=123,
            chat_id=44,
            grouped_id=None,
            is_private=True,
            text=".i draw",
            file=None,
            id=55,
            message=SimpleNamespace(text=".i draw"),
        )
        response_message = SimpleNamespace(delete=AsyncMock())
        prefs = SimpleNamespace(
            group_activation_mode="mention_and_reply",
            enabled_tools=["googleSearch"],
            json_mode=False,
        )
        captured = {}

        async def stream(**kwargs):
            captured.update(kwargs)
            await kwargs["image_callback"](
                codex_util.CodexImage(b"png", "item-1", None, ".png")
            )
            return codex_util.CodexResponse(text="", images_delivered=1)

        config = llm_chat_config.LLMChatConfig((123,), (123,))
        with ExitStack() as stack:
            stack.enter_context(patch.object(llm_chat, "cleanup_completed_tasks"))
            stack.enter_context(
                patch.object(llm_chat.llm_db, "is_awaiting_key", return_value=False)
            )
            stack.enter_context(
                patch.object(
                    llm_chat.gemini_live_util.live_session_manager,
                    "is_live_mode_active",
                    return_value=False,
                )
            )
            stack.enter_context(
                patch.object(llm_chat.user_manager, "get_prefs", return_value=prefs)
            )
            stack.enter_context(
                patch.object(
                    llm_chat.util, "isAdmin", new=AsyncMock(return_value=False)
                )
            )
            load_config = stack.enter_context(
                patch.object(
                    llm_chat.llm_chat_config, "load_config", return_value=config
                )
            )
            stack.enter_context(
                patch.object(
                    llm_chat,
                    "_determine_context_mode_and_handle_transitions",
                    new=AsyncMock(return_value="recent"),
                )
            )
            stack.enter_context(
                patch.object(
                    llm_chat,
                    "_get_effective_model_and_service",
                    side_effect=lambda *args, prefix_model=None: (
                        (prefix_model, "codex")
                        if prefix_model
                        else (GEMINI_FLASH_LATEST, "gemini")
                    ),
                )
            )
            stack.enter_context(
                patch.object(llm_chat, "get_effective_api_key", return_value="key")
            )
            stack.enter_context(
                patch.object(
                    llm_chat,
                    "send_info_message",
                    new=AsyncMock(return_value=response_message),
                )
            )
            stack.enter_context(
                patch.object(
                    llm_chat,
                    "build_conversation_history",
                    new=AsyncMock(
                        return_value=SimpleNamespace(
                            history=[{"role": "user", "content": "draw"}], warnings=[]
                        )
                    ),
                )
            )
            stack.enter_context(
                patch.object(
                    llm_chat,
                    "_get_effective_reasoning",
                    return_value=SimpleNamespace(level=None),
                )
            )
            stack.enter_context(
                patch.object(llm_chat.codex_util, "stream_codex_response", new=stream)
            )
            send_image = stack.enter_context(
                patch.object(
                    llm_chat,
                    "_send_image_to_telegram",
                    new=AsyncMock(return_value=True),
                )
            )
            stack.enter_context(patch.object(llm_chat, "add_active_llm_task"))
            stack.enter_context(patch.object(llm_chat, "remove_active_llm_task"))
            stack.enter_context(
                patch.object(llm_chat, "_handle_tts_response", new=AsyncMock())
            )
            stack.enter_context(
                patch.object(llm_chat, "_log_conversation", new=AsyncMock())
            )
            asyncio.run(llm_chat.chat_handler(event))

        self.assertEqual(captured["model"], OPENAI_CODEX_GPT_5_6_SOL)
        self.assertEqual(
            captured["tools"],
            [
                {"type": "web_search"},
                {"type": "image_generation", "partial_images": 3},
            ],
        )
        send_image.assert_awaited_once()
        self.assertEqual(
            send_image.await_args.kwargs["caption"], "Codex generated image"
        )
        response_message.delete.assert_awaited_once()
        load_config.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
