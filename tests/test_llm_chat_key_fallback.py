"""Moving a request down the user's credential chain when a key fails."""

import asyncio
import builtins
import importlib
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from uniborg import codex_util, guest_util, key_sharing, llm_chat_config
from uniborg.constants import GPT_SOL
from uniborg.llm_util import RateLimitException


class _FakeLoop:
    def create_task(self, coro):
        coro.close()


builtins.borg = SimpleNamespace(loop=_FakeLoop())


async def _import_plugin():
    return importlib.import_module("llm_chat_plugins.llm_chat")


plugin = asyncio.run(_import_plugin())

from test_llm_chat_guest import BOT_USERNAME, _query  # noqa: E402

USER_ID = 999000773
OWNER_ID = 999000774


def _credential(key, kind=key_sharing.OWN, owner_id=USER_ID):
    return key_sharing.Credential(
        key, key_sharing.token_for(kind, owner_id), kind, owner_id
    )


def _error(name, status):
    exc = type(name, (Exception,), {})("boom")
    exc.status_code = status
    return exc


class GenerationSignalTests(unittest.TestCase):
    """`_generate_response` hands a key failure back only when a next key exists."""

    def request(self, **overrides):
        event = guest_util.GuestEvent(_query(f"{BOT_USERNAME} hi"))
        fields = dict(
            event=event,
            response_message=AsyncMock(),
            messages=[{"role": "user", "content": "hi"}],
            model="openai-api/gpt-sol-latest",
            model_capabilities={"vision": True},
            api_key="sk-first",
            prefs=SimpleNamespace(json_mode=False, enabled_tools=[]),
            prefix_effort=None,
            image_generation=False,
            warnings=[],
            reasoning_model=GPT_SOL,
            next_key_p=True,
        )
        fields.update(overrides)
        return plugin.GenerationRequest(**fields)

    def run_codex(self, request, error):
        with ExitStack() as stack:
            stack.enter_context(
                patch.object(
                    plugin.codex_util,
                    "stream_codex_response",
                    AsyncMock(side_effect=error),
                )
            )
            stack.enter_context(patch.object(plugin, "ACTIVE_LLM_TASKS", {}))
            stack.enter_context(patch.object(plugin.util, "edit_message"))
            return asyncio.run(plugin._generate_response(request))

    def codex_error(self, cause, text=""):
        error = codex_util.CodexStreamError(
            str(cause), codex_util.CodexResponse(text=text)
        )
        error.__cause__ = cause
        return error

    def test_a_rejected_openai_key_moves_on(self):
        with self.assertRaises(plugin._CredentialFailed) as raised:
            self.run_codex(
                self.request(), self.codex_error(_error("AuthenticationError", 401))
            )
        self.assertEqual(raised.exception.failure, key_sharing.KeyFailure.AUTH)

    def test_the_last_key_reports_the_error_instead(self):
        result = self.run_codex(
            self.request(next_key_p=False),
            self.codex_error(_error("AuthenticationError", 401)),
        )
        self.assertTrue(result.delivered)

    def test_a_partly_streamed_answer_is_not_retried(self):
        result = self.run_codex(
            self.request(),
            self.codex_error(_error("RateLimitError", 429), text="half an answer"),
        )
        self.assertTrue(result.delivered)

    def test_a_litellm_rate_limit_moves_on(self):
        call = AsyncMock(
            side_effect=RateLimitException(
                "API rate limit exceeded.",
                original_exception=_error("RateLimitError", 429),
            )
        )
        with ExitStack() as stack:
            stack.enter_context(patch.object(plugin, "_call_llm_with_retry", call))
            stack.enter_context(patch.object(plugin, "ACTIVE_LLM_TASKS", {}))
            with self.assertRaises(plugin._CredentialFailed) as raised:
                asyncio.run(
                    plugin._generate_response(
                        self.request(
                            model="openrouter/~openai/gpt-sol-latest",
                            api_key="sk-or-first",
                            surface=plugin.GenerationSurface.GUEST,
                        )
                    )
                )
        self.assertEqual(raised.exception.failure, key_sharing.KeyFailure.RATE_LIMIT)


class SpentKeysLastTests(unittest.TestCase):
    def test_openai_keys_known_to_be_spent_go_last(self):
        memory = plugin.openai_providers.QuotaMemory()
        asyncio.run(memory.mark_key_spent("sk-spent"))
        chain = [_credential("sk-spent"), _credential("sk-ok", key_sharing.SHARE, 7)]
        with patch.object(
            plugin, "_credential_chain", return_value=chain
        ), patch.object(plugin, "OPENAI_QUOTA", memory):
            ordered = asyncio.run(
                plugin._request_credentials(USER_ID, "openai", codex_access=False)
            )
        self.assertEqual([c.api_key for c in ordered], ["sk-ok", "sk-spent"])


class _CancellingTask:
    """The current task, with `cancelling()` on Pythons older than 3.11."""

    def __init__(self, task):
        self._task = task

    def __getattr__(self, name):
        return getattr(self._task, name)

    def cancelling(self):
        return 0


def _current_task_shim():
    if hasattr(asyncio.Task, "cancelling"):
        return None
    current_task = asyncio.current_task
    return lambda loop=None: _CancellingTask(current_task(loop))


class ChatHandlerFallbackTests(unittest.TestCase):
    def run_request(self, credentials, outcomes):
        event = SimpleNamespace(
            id=1,
            sender_id=USER_ID,
            chat_id=456,
            grouped_id=None,
            is_private=True,
            text="hello",
            file=None,
            message=SimpleNamespace(text="hello"),
        )
        config = llm_chat_config.LLMChatConfig(("MAGIC_ADMINS",), ())
        generate = AsyncMock(side_effect=outcomes)
        with ExitStack() as stack:
            stack.enter_context(patch.object(plugin, "cleanup_completed_tasks"))
            stack.enter_context(patch.object(plugin, "AWAITING_INPUT_FROM_USERS", {}))
            stack.enter_context(
                patch.object(plugin.llm_db, "is_awaiting_key", return_value=False)
            )
            stack.enter_context(
                patch.object(
                    plugin.gemini_live_util.live_session_manager,
                    "is_live_mode_active",
                    return_value=False,
                )
            )
            stack.enter_context(
                patch.object(plugin.util, "isAdmin", new=AsyncMock(return_value=False))
            )
            stack.enter_context(
                patch.object(plugin.llm_chat_config, "load_config", return_value=config)
            )
            stack.enter_context(
                patch.object(
                    plugin,
                    "_determine_context_mode_and_handle_transitions",
                    new=AsyncMock(return_value="recent"),
                )
            )
            stack.enter_context(
                patch.object(
                    plugin,
                    "_get_effective_model_and_service",
                    return_value=(plugin.DEFAULT_MODEL, "gemini"),
                )
            )
            stack.enter_context(
                patch.object(
                    plugin, "_request_credentials", AsyncMock(return_value=credentials)
                )
            )
            stack.enter_context(
                patch.object(plugin, "_start_topic_title", AsyncMock(return_value=None))
            )
            stack.enter_context(
                patch.object(
                    plugin, "_response_placeholder", AsyncMock(return_value=AsyncMock())
                )
            )
            stack.enter_context(
                patch.object(
                    plugin,
                    "build_conversation_history",
                    AsyncMock(
                        return_value=SimpleNamespace(
                            history=[{"role": "user", "content": "hello"}],
                            warnings=[],
                        )
                    ),
                )
            )
            stack.enter_context(
                patch.object(plugin, "is_native_gemini_files_mode", return_value=False)
            )
            stack.enter_context(
                patch.object(plugin, "_user_display_name", return_value="Owner")
            )
            stack.enter_context(patch.object(plugin, "_generate_streamed", generate))
            shim = _current_task_shim()
            if shim is not None:
                stack.enter_context(patch.object(plugin.asyncio, "current_task", shim))
            asyncio.run(plugin.chat_handler(event))
        return generate

    def test_a_failed_key_moves_to_the_next_with_a_note(self):
        delivered = plugin.GenerationResult(
            text="", finish_reason=None, has_image=False, delivered=True
        )
        generate = self.run_request(
            [_credential("AQ.mine"), _credential("AQ.theirs", key_sharing.SHARE, 7)],
            [plugin._CredentialFailed(key_sharing.KeyFailure.CREDIT), delivered],
        )
        first, second = (call.args[0] for call in generate.await_args_list)
        self.assertEqual((first.api_key, first.next_key_p), ("AQ.mine", True))
        self.assertEqual((second.api_key, second.next_key_p), ("AQ.theirs", False))
        self.assertIn(
            "Your own key was out of credit, so I used the key Owner shares with you.",
            second.warnings,
        )


class KeysPanelTests(unittest.TestCase):
    def setUp(self):
        self.user, self.owner = 999000775, 999000776
        plugin.llm_db.set_api_key(user_id=self.user, service="openai", key="sk-u")
        plugin.llm_db.set_api_key(user_id=self.owner, service="openai", key="sk-o")
        key_sharing.share_key(
            owner_id=self.owner, recipient_id=self.user, service="openai"
        )
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(
            patch.object(
                plugin, "_user_display_name", side_effect=lambda uid: f"U{uid}"
            )
        )
        stack.enter_context(
            patch.object(plugin, "_roster_codex_access", return_value=False)
        )

    def labels(self, buttons):
        return [plugin.tg_compat.button_text(b) for row in buttons for b in row]

    def test_the_recipient_sees_the_order_but_never_a_key(self):
        text, buttons = plugin._keys_panel(self.user, viewer_id=self.user)
        self.assertIn("1. Your own key", text)
        self.assertIn(f"2. Shared by U{self.owner}", text)
        self.assertNotIn("sk-", text)
        self.assertIn("Order: OpenAI", " ".join(self.labels(buttons)))

    def test_an_admin_sees_another_users_keys_in_the_third_person(self):
        text, _ = plugin._keys_panel(self.user, viewer_id=self.owner + 1)
        self.assertIn("1. Own key", text)
        text, _ = plugin._key_order_panel(self.user, "openai", viewer_id=self.owner + 1)
        self.assertIn("1. Own key", text)

    def test_the_owner_sees_the_share_with_a_revoke_button(self):
        text, buttons = plugin._keys_panel(self.owner, viewer_id=self.owner)
        self.assertIn(f"OpenAI → U{self.user} ({self.user})", text)
        self.assertIn(f"✖ OpenAI → U{self.user}", self.labels(buttons))


if __name__ == "__main__":
    unittest.main()
