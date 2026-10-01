"""The STT bot's transcription path (`llm_stt` in stt_plugins/stt.py).

The model, Telegram and the key store are faked; each test pins what the user
sees for one outcome.
"""

import asyncio
import builtins
import importlib
import json
from contextlib import ExitStack
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch


class _FakeLoop:
    def create_task(self, coro):
        coro.close()


class _FakeBorg:
    loop = _FakeLoop()

    def on(self, *args, **kwargs):
        return lambda func: func


def _import_plugin():
    previous = getattr(builtins, "borg", None)
    builtins.borg = _FakeBorg()
    try:

        async def _import():
            return importlib.import_module("stt_plugins.stt")

        return asyncio.run(_import())
    finally:
        if previous is not None:
            builtins.borg = previous


stt = _import_plugin()


class _Model:
    supports_schema = True

    def __init__(self, outcome):
        self.outcome = outcome

    async def prompt(self, **kwargs):
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return SimpleNamespace(text=AsyncMock(return_value=self.outcome))


class LlmSttTests(unittest.TestCase):
    def run_stt(
        self,
        *,
        api_key="key",
        outcome=json.dumps({"transcription": "hello", "output_type": "transcript"}),
        attachments=("a.ogg",),
        model=None,
    ):
        status = SimpleNamespace(id=2)
        event = SimpleNamespace(
            sender_id=1,
            message=SimpleNamespace(id=1),
            reply=AsyncMock(return_value=status),
        )
        calls = SimpleNamespace(status=status, event=event)
        with ExitStack() as stack:
            enter = stack.enter_context
            enter(
                patch.object(stt, "get_effective_gemini_api_key", return_value=api_key)
            )
            calls.request_key = enter(
                patch.object(stt.llm_db, "request_api_key_message", AsyncMock())
            )
            enter(
                patch.object(
                    stt.llm,
                    "get_async_model",
                    return_value=model or _Model(outcome),
                )
            )
            enter(
                patch.object(
                    stt.llm_util,
                    "create_attachments_from_dir",
                    return_value=list(attachments),
                )
            )
            enter(
                patch.object(
                    stt.llm_util,
                    "get_proxy_config_or_error",
                    return_value=(None, None),
                )
            )
            calls.edit = enter(patch.object(stt.util, "edit_message", AsyncMock()))
            calls.error = enter(
                patch.object(stt.llm_util, "handle_llm_error", AsyncMock())
            )
            asyncio.run(stt.llm_stt(cwd="/tmp/x/", event=event, log=False))
        return calls

    def test_a_transcript_replaces_the_status_message(self):
        calls = self.run_stt()

        calls.event.reply.assert_awaited_once_with("Transcribing...")
        (edit,) = calls.edit.await_args_list
        self.assertEqual(edit.args, (calls.status, "hello"))
        self.assertEqual(edit.kwargs["api_keys"], {"gemini": "key"})
        self.assertEqual(edit.kwargs["reply_to"], calls.event.message)

    def test_a_video_gets_its_visuals_and_silence_says_so(self):
        visuals = self.run_stt(
            outcome=json.dumps({"transcription": "hi", "visual_description": "a cat"})
        )
        silent = self.run_stt(outcome=json.dumps({"transcription": ""}))

        self.assertEqual(visuals.edit.await_args.args[1], "hi\n\n\n**Visuals:**\na cat")
        self.assertEqual(
            silent.edit.await_args.args[1], "__[No speech or text detected]__"
        )

    def test_unparseable_output_is_shown_raw(self):
        calls = self.run_stt(outcome="not json")

        self.assertIn("```json\nnot json\n```", calls.edit.await_args.args[1])

    def test_a_missing_key_asks_for_one_and_transcribes_nothing(self):
        calls = self.run_stt(api_key=None)

        calls.request_key.assert_awaited_once_with(calls.event, "gemini")
        calls.event.reply.assert_not_awaited()

    def test_no_media_and_a_schema_less_model_are_told(self):
        no_media = self.run_stt(attachments=())
        schema_less = _Model("")
        schema_less.supports_schema = False
        no_schema = self.run_stt(model=schema_less)

        no_media.event.reply.assert_awaited_once_with(
            "No valid media files found to transcribe."
        )
        self.assertIn(
            "does not support structured output",
            no_schema.event.reply.await_args.args[0],
        )

    def test_an_api_failure_goes_to_the_error_handler_with_the_status(self):
        calls = self.run_stt(outcome=ValueError("bad request"))

        error = calls.error.await_args.kwargs
        self.assertEqual(error["response_message"], calls.status)
        self.assertEqual(
            error["base_error_message"], "An error occurred during the API call."
        )
        calls.edit.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
