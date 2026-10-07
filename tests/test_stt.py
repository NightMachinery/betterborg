"""The STT bot's transcription path (`llm_stt` in stt_plugins/stt.py).

The model, Telegram and the key store are faked; each test pins what the user
sees for one outcome.
"""

import asyncio
import builtins
import datetime
import importlib
import json
import logging
from contextlib import ExitStack
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from telethon import errors, types
from telethon._updates import EntityCache

from uniborg import guest_util, stt_models, tg_compat


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
        choice=stt_models.AUTO,
        speech=None,
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
            enter(patch.object(stt, "get_model_choice", return_value=choice))
            for name in ("get_and_renew", "set_with_expiry"):
                enter(patch.object(stt.redis_util, name, AsyncMock(return_value=None)))
            if speech is not None:
                calls.speech = enter(
                    patch.object(
                        stt.stt_models, "transcribe", AsyncMock(side_effect=[speech])
                    )
                )
            calls.request_key = enter(
                patch.object(stt, "_start_key_setup", AsyncMock())
            )
            calls.get_model = enter(
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

        calls.event.reply.assert_awaited_once_with(
            "Transcribing with Google AI Studio..."
        )
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

        calls.request_key.assert_awaited_once_with(calls.event, "gemini", switch=False)
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
        self.assertIn("Google AI Studio API call", error["base_error_message"])
        calls.edit.assert_not_awaited()


def _attachment(data, mime_type):
    return stt.llm.Attachment(content=data, type=mime_type)


class ModelChoiceTests(unittest.TestCase):
    """/model: the saved choice, a chosen model alone, and the speech model."""

    run_stt = LlmSttTests.run_stt

    def test_a_chosen_model_alone_answers(self):
        calls = self.run_stt(choice="gemini/gemini-3.8-flash")

        self.assertEqual(calls.edit.await_args.args[1], "hello")
        self.assertEqual(
            {c.args[0] for c in calls.get_model.call_args_list},
            {"gemini/gemini-3.8-flash"},
        )

    def test_a_chosen_model_that_refuses_the_key_says_so_in_the_status(self):
        calls = self.run_stt(choice="gemini/gemini-2.5-flash", outcome=REFUSED)

        text = calls.edit.await_args.args[1]
        self.assertIn("2.5 Flash is not available for this API key", text)
        calls.error.assert_not_awaited()

    def test_the_speech_model_sends_its_plain_text_and_counts_what_it_skipped(self):
        attachments = (
            _attachment(b"OggS", "audio/ogg"),
            _attachment(b"\x89PNG", "image/png"),
        )
        calls = self.run_stt(
            choice="gemini/gemini-3.5-transcribe",
            attachments=attachments,
            speech="Hello there.",
        )

        self.assertEqual(
            calls.edit.await_args.args[1],
            "Hello there.\n\n__(3.5 Transcribe hears speech only, so 1 file "
            "without sound was skipped.)__",
        )
        (audio,) = calls.speech.await_args.kwargs["audio"]
        self.assertEqual(audio.data, b"OggS")
        calls.get_model.assert_not_called()

    def test_the_speech_model_on_images_alone_is_told_before_any_call(self):
        calls = self.run_stt(
            choice="gemini/gemini-3.5-transcribe",
            attachments=(_attachment(b"\x89PNG", "image/png"),),
            speech="unused",
        )

        self.assertIn("speech only", calls.event.reply.await_args.args[0])
        calls.speech.assert_not_awaited()

    def test_a_saved_choice_survives_and_one_no_longer_offered_is_auto(self):
        saved = {}
        storage = SimpleNamespace(
            get=lambda user_id: saved.get(user_id, {}),
            set=lambda user_id, data: saved.__setitem__(user_id, dict(data)) or True,
        )
        with patch.object(stt, "_prefs_storage", storage):
            self.assertEqual(stt.get_model_choice(7), stt_models.AUTO)
            stt.set_model_choice(7, "gemini/gemini-3.5-transcribe")
            self.assertEqual(stt.get_model_choice(7), "gemini/gemini-3.5-transcribe")
            saved[7]["provider_models"]["gemini"] = "gemini/gemini-1.0-pro"
            self.assertEqual(stt.get_model_choice(7), stt_models.AUTO)

    def press(self, slug):
        data = stt.MODEL_CALLBACK_PREFIX + stt.bot_util.sanitize_callback_data(slug)
        event = SimpleNamespace(
            data=data.encode(),
            sender_id=7,
            answer=AsyncMock(),
            edit=AsyncMock(),
        )
        with patch.object(stt, "set_model_choice") as saved:
            asyncio.run(stt.model_callback_handler(event))
        return event, saved

    def test_a_press_saves_the_model_for_the_presser(self):
        event, saved = self.press("3.8-flash")

        saved.assert_called_once_with(7, "gemini/gemini-3.8-flash")
        event.answer.assert_awaited_once_with("Transcription model: 3.8 Flash")
        event.edit.assert_awaited_once()

    def test_a_press_on_auto_and_on_a_retired_model(self):
        _event, saved = self.press(stt_models.AUTO)
        saved.assert_called_once_with(7, stt_models.AUTO)

        event, saved = self.press("1.0-pro")
        saved.assert_not_called()
        self.assertTrue(event.answer.await_args.kwargs["alert"])


BOT_ID = 5151
BOT_USERNAME = "llm_test_stt_bot"
CALLER = 888001
OTHER = 888002
NOW = datetime.datetime.now(datetime.timezone.utc)


def _voice():
    return types.MessageMediaDocument(
        document=types.Document(
            id=9,
            access_hash=1,
            file_reference=b"",
            date=NOW,
            mime_type="audio/ogg",
            size=10,
            dc_id=2,
            attributes=[types.DocumentAttributeAudio(duration=3, voice=True)],
        )
    )


def _query(text, *, reference_media=None, reference_grouped_id=None, query_id=1):
    client = SimpleNamespace(
        _self_id=BOT_ID, _mb_entity_cache=EntityCache(), parse_mode=None
    )
    trigger = types.Message(
        id=20,
        peer_id=types.PeerUser(OTHER),
        date=NOW,
        message=text,
        from_id=types.PeerUser(CALLER),
        out=True,
    )
    reference = types.Message(
        id=19,
        peer_id=types.PeerUser(OTHER),
        date=NOW,
        message="",
        from_id=types.PeerUser(OTHER),
        media=reference_media,
        grouped_id=reference_grouped_id,
    )
    update = SimpleNamespace(
        query_id=query_id, message=trigger, reference_messages=[reference]
    )
    update._entities = {CALLER: types.User(id=CALLER, first_name="C")}
    return guest_util.guest_query_from_update(update, client=client)


class _Editor:
    def __init__(self, log):
        self.log = log

    def __call__(self, client, inline_id):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def edit(self, **kwargs):
        self.log.append(kwargs)
        return True


class _GuestBorg:
    me = types.User(id=BOT_ID, bot=True, first_name="STT", username=BOT_USERNAME)

    def __init__(self):
        self.forbidden = []

    def __getattr__(self, name):
        if name in ("send_message", "send_file", "get_messages"):

            async def refuse(*args, **kwargs):
                self.forbidden.append(name)

            return refuse
        raise AttributeError(name)


class _ScriptedModel:
    """Answers each prompt with the next outcome: text, or an exception."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    async def prompt(self, **kwargs):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return SimpleNamespace(text=AsyncMock(return_value=outcome))


REFUSED = Exception(
    "This model models/gemini-2.5-flash is no longer available to new users."
)
BUSY = Exception("503 UNAVAILABLE: high demand")


class ModelFallbackTests(unittest.TestCase):
    """`_transcribe_with_retry`: transient errors and models that refuse a key."""

    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.models = {}
        self.redis = {}

        async def get(key, **kwargs):
            return self.redis.get(key)

        async def set_(key, value, **kwargs):
            self.redis[key] = value
            return True

        for target, name, value in (
            (stt, "STT_MODELS", ["gemini/a", "gemini/b"]),
            (stt, "STT_RETRY_SLEEP", 0),
            (stt.llm, "get_async_model", lambda name: self.models[name]),
            (stt.redis_util, "get_and_renew", get),
            (stt.redis_util, "set_with_expiry", set_),
            (stt.util, "edit_message", AsyncMock()),
        ):
            stack.enter_context(patch.object(target, name, value))

    def transcribe(self, *, api_key="key-1", models=("gemini/a", "gemini/b")):
        answer = asyncio.run(
            stt._transcribe_with_retry(
                models=list(models),
                attachments=[],
                api_key=api_key,
                status_message=object(),
                italics_marker="_",
            )
        )
        self.answered_by = answer.model_name
        return answer.raw

    def test_a_refusal_falls_through_at_once_and_is_remembered_for_the_key(self):
        self.models = {
            "gemini/a": _ScriptedModel(REFUSED),
            "gemini/b": _ScriptedModel("first", "second"),
        }

        self.assertEqual(self.transcribe(), "first")
        self.assertEqual(self.answered_by, "gemini/b")
        self.assertEqual(self.transcribe(), "second")

        self.assertEqual(self.models["gemini/a"].calls, 1)
        (key,) = self.redis
        self.assertNotIn("key-1", key)

    def test_another_key_still_tries_the_model(self):
        self.models = {
            "gemini/a": _ScriptedModel(REFUSED, "for key 2"),
            "gemini/b": _ScriptedModel("for key 1"),
        }

        self.assertEqual(self.transcribe(api_key="key-1"), "for key 1")
        self.assertEqual(self.transcribe(api_key="key-2"), "for key 2")

    def test_when_every_model_refused_the_key_all_are_tried_again(self):
        self.models = {
            "gemini/a": _ScriptedModel(REFUSED, "back"),
            "gemini/b": _ScriptedModel(REFUSED),
        }

        with self.assertRaises(stt.SttModelRefusedError) as caught:
            self.transcribe()
        self.assertIs(caught.exception.__cause__, REFUSED)
        self.assertIn("/model", str(caught.exception))
        self.assertEqual(self.transcribe(), "back")

    def test_transient_errors_retry_each_model_then_switch(self):
        retries = stt.STT_RETRIES_PER_MODEL
        self.models = {
            "gemini/a": _ScriptedModel(*[BUSY] * retries),
            "gemini/b": _ScriptedModel("ok"),
        }

        self.assertEqual(self.transcribe(), "ok")
        self.assertEqual(self.models["gemini/a"].calls, retries)
        self.assertEqual(self.redis, {})

    def test_a_refusal_then_transient_errors_ends_in_the_error(self):
        retries = stt.STT_RETRIES_PER_MODEL
        self.models = {
            "gemini/a": _ScriptedModel(REFUSED),
            "gemini/b": _ScriptedModel(*[BUSY] * retries),
        }

        with self.assertRaises(Exception) as caught:
            self.transcribe()
        self.assertIs(caught.exception, BUSY)
        self.assertEqual(self.models["gemini/b"].calls, retries)

    def test_a_chosen_model_alone_is_retried_and_its_error_shown(self):
        retries = stt.STT_RETRIES_PER_MODEL
        self.models = {
            "gemini/a": _ScriptedModel(),
            "gemini/b": _ScriptedModel(*[BUSY] * retries),
        }

        with self.assertRaises(Exception) as caught:
            self.transcribe(models=["gemini/b"])
        self.assertIs(caught.exception, BUSY)
        self.assertEqual(self.models["gemini/a"].calls, 0)

    def test_a_chosen_model_that_refuses_the_key_is_not_replaced(self):
        self.models = {
            "gemini/a": _ScriptedModel(),
            "gemini/b": _ScriptedModel(REFUSED),
        }

        with self.assertRaises(stt.SttModelRefusedError):
            self.transcribe(models=["gemini/b"])
        self.assertEqual(self.models["gemini/a"].calls, 0)


class GuestSttTests(unittest.TestCase):
    api_key = "caller-key"
    transcript = "hello"

    def setUp(self):
        self.answers = []
        self.edits = []
        self.downloaded = []
        self.borg = _GuestBorg()

        async def answer_guest(client, *, query_id, title, text=None, **kwargs):
            self.answers.append(SimpleNamespace(text=text, **kwargs))
            return types.InputBotInlineMessageID(dc_id=2, id=1, access_hash=1)

        async def run_and_get(event, to_await, cwd=None, *, messages=None):
            self.downloaded.append([m.id for m in messages])
            await to_await(cwd=cwd, event=event)
            return cwd

        async def run_stt_job(job, *, user_id, status_message, italics_marker="__"):
            self.jobs.append((job, user_id))
            return stt.Transcription(text=self.transcript, raw="{}")

        self.jobs = []
        stack = ExitStack()
        self.addCleanup(stack.close)
        for target, name, value in (
            (builtins, "borg", self.borg),
            (stt.tg_raw, "answer_guest", answer_guest),
            (stt.tg_raw, "InlineEditor", _Editor(self.edits)),
            (stt.util, "isAdmin", AsyncMock(return_value=False)),
            (stt.util, "run_and_get", run_and_get),
            (stt, "get_effective_gemini_api_key", lambda user_id: self.api_key),
            (stt, "get_model_choice", lambda user_id: stt_models.AUTO),
            (stt.llm, "get_async_model", lambda name: _Model("")),
            (
                stt.llm_util,
                "create_attachments_from_dir",
                lambda cwd: ["voice.ogg"],
            ),
            (stt, "run_stt_job", run_stt_job),
            (stt, "_log_transcription", AssertionError),
            (stt.llm_db, "request_api_key_message", AssertionError),
            (stt, "_guest_limiter", guest_util.CallLimiter()),
        ):
            stack.enter_context(patch.object(target, name, value))
        stack.enter_context(
            patch.object(builtins, "logger", logging.getLogger("test.stt"), create=True)
        )

    def run_query(self, query):
        asyncio.run(stt.guest_stt_handler(query))
        self.assertEqual(self.borg.forbidden, [])

    def test_a_mention_on_a_voice_note_is_transcribed_with_the_callers_key(self):
        self.run_query(_query(f"@{BOT_USERNAME}", reference_media=_voice()))

        self.assertEqual([a.text for a in self.answers], [stt.GUEST_PLACEHOLDER])
        self.assertEqual(self.downloaded, [[19]])
        ((job, user_id),) = self.jobs
        self.assertEqual((job.api_key, user_id), ("caller-key", CALLER))
        self.assertEqual(self.edits, [{"text": "hello", "parse_mode": "md"}])

    def test_a_voice_note_of_an_album_says_only_it_was_transcribed(self):
        self.run_query(
            _query(f"@{BOT_USERNAME}", reference_media=_voice(), reference_grouped_id=7)
        )

        self.assertEqual(
            self.edits,
            [
                {
                    "text": f"hello\n\n{guest_util.ALBUM_REFERENCE_NOTE}",
                    "parse_mode": "md",
                }
            ],
        )

    def test_vertex_guest_uses_the_callers_vertex_key_and_models(self):
        with patch.object(
            stt, "get_provider_choice", return_value="vertex"
        ), patch.object(
            stt.llm_db, "get_api_key", return_value="synthetic-vertex-caller-key"
        ), patch.object(
            stt_models, "load_media_model"
        ) as load:
            self.run_query(_query(f"@{BOT_USERNAME}", reference_media=_voice()))
        ((job, user_id),) = self.jobs
        self.assertEqual(job.provider, "vertex")
        self.assertEqual(job.api_key, "synthetic-vertex-caller-key")
        self.assertEqual(
            tuple(job.models), stt.stt_providers.PROVIDERS["vertex"].auto_models
        )
        load.assert_not_called()
        self.assertEqual(self.edits, [{"text": "hello", "parse_mode": "md"}])

    def test_vertex_guest_api_errors_show_only_the_safe_status_message(self):
        error = stt.stt_providers.ProviderError("vertex", 403)
        with patch.object(
            stt, "run_stt_job", AsyncMock(side_effect=error)
        ), patch.object(stt.llm_util, "handle_llm_error", AsyncMock()) as generic:
            self.run_query(_query(f"@{BOT_USERNAME}", reference_media=_voice()))
        self.assertEqual(self.edits[-1]["text"], str(error))
        generic.assert_not_awaited()

    def test_a_long_transcript_ends_as_rich_markdown(self):
        self.transcript = "word " * 2000

        self.run_query(_query(f"@{BOT_USERNAME}", reference_media=_voice()))

        self.assertEqual(self.edits[-1], {"markdown": self.transcript})

    def test_a_reply_without_a_mention_is_ignored(self):
        self.run_query(_query("thanks", reference_media=_voice()))

        self.assertEqual(self.answers, [])

    def test_a_mention_without_media_says_so(self):
        self.run_query(_query(f"@{BOT_USERNAME} hi"))

        self.assertEqual([a.text for a in self.answers], [stt.GUEST_NO_MEDIA_TEXT])
        self.assertEqual(self.jobs, [])

    def test_a_caller_without_a_key_is_invited_and_nothing_is_downloaded(self):
        self.api_key = None

        self.run_query(_query(f"@{BOT_USERNAME}", reference_media=_voice()))

        (invite,) = self.answers
        self.assertEqual(invite.text, stt.GUEST_INVITE_TEXT)
        ((button,),) = invite.buttons
        self.assertEqual(
            tg_compat.button_url(button), f"https://t.me/{BOT_USERNAME}?start=guest"
        )
        self.assertEqual(self.downloaded, [])

    def test_unusable_files_are_told_in_the_answer(self):
        with patch.object(stt.llm_util, "create_attachments_from_dir", lambda cwd: []):
            self.run_query(_query(f"@{BOT_USERNAME}", reference_media=_voice()))

        self.assertEqual(
            self.edits, [{"text": "No valid media files found to transcribe."}]
        )

    def test_past_the_hourly_limit_the_caller_is_told_and_nothing_runs(self):
        spent = SimpleNamespace(allow=AsyncMock(return_value=False))
        with patch.object(stt, "_guest_limiter", spent):
            self.run_query(_query(f"@{BOT_USERNAME}", reference_media=_voice()))

        (note,) = self.answers
        self.assertTrue(note.text.startswith("You have had"))
        self.assertEqual((self.downloaded, self.jobs), ([], []))

    def test_admins_are_not_rate_limited(self):
        def refuse(*args, **kwargs):
            raise AssertionError("an admin must not be counted")

        with patch.object(
            stt.util, "isAdmin", AsyncMock(return_value=True)
        ), patch.object(stt, "_guest_limiter", SimpleNamespace(allow=refuse)):
            self.run_query(_query(f"@{BOT_USERNAME}", reference_media=_voice()))

        self.assertEqual(len(self.jobs), 1)

    def test_no_answer_means_nothing_is_downloaded_or_transcribed(self):
        async def fail(*args, **kwargs):
            raise RuntimeError("delivery unknown")

        with patch.object(stt.tg_raw, "answer_guest", fail):
            self.run_query(_query(f"@{BOT_USERNAME}", reference_media=_voice()))

        self.assertEqual((self.downloaded, self.jobs, self.edits), ([], [], []))

    def test_a_refused_rich_transcript_is_cut_to_one_classic_message(self):
        self.transcript = "word " * 2000

        class _RefusingEditor(_Editor):
            async def edit(self, **kwargs):
                if "markdown" in kwargs:
                    raise errors.RPCError(request=None, message="REFUSED", code=400)
                return await super().edit(**kwargs)

        with patch.object(stt.tg_raw, "InlineEditor", _RefusingEditor(self.edits)):
            self.run_query(_query(f"@{BOT_USERNAME}", reference_media=_voice()))

        final = self.edits[-1]
        self.assertEqual(final["parse_mode"], "md")
        self.assertTrue(final["text"].endswith(stt.GUEST_TRUNCATED_NOTE))
        self.assertLessEqual(
            len(final["text"].encode("utf-16-le")) // 2, stt.GUEST_CLASSIC_LIMIT_UNITS
        )

    def test_an_echoed_answer_is_not_media_to_transcribe(self):
        echo = SimpleNamespace(
            media=_voice(), out=True, guestchat_via_from=types.PeerUser(CALLER)
        )
        event = SimpleNamespace(media=echo.media, sender=object(), message=echo)

        self.assertFalse(stt.is_transcribable_media_event(event))


if __name__ == "__main__":
    unittest.main()
