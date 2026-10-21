"""STT cancellation through normal and guest messages, without Telegram calls."""

import asyncio
import builtins
import copy
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
import test_stt as stt_tests

from telethon import types
from uniborg import guest_util, util
from test_stt import BOT_ID, BOT_USERNAME, CALLER, OTHER, _query, _voice, stt


def stop_event(*, caller=CALLER, chat=CALLER, reply_id=None, private=True):
    return SimpleNamespace(
        sender_id=caller, chat_id=chat, is_private=private, raw_text="/stop",
        message=SimpleNamespace(reply_to_msg_id=reply_id), reply=AsyncMock(),
    )


class GuestStopTests(unittest.IsolatedAsyncioTestCase):
    api_key = "caller-key"
    transcript = "hello"

    def setUp(self):
        stt_tests.GuestSttTests.setUp(self)
        self.tasks = []
        self.started = asyncio.Queue()

        async def block(*args, **kwargs):
            await self.started.put(stt._current_stt.get())
            await asyncio.Future()

        for item in (patch.dict(stt._active_stt, clear=True), patch.object(stt, "run_stt_job", block)):
            item.start()
            self.addCleanup(item.stop)

    async def asyncTearDown(self):
        for task in self.tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.assertEqual(stt._active_stt, {})
        self.assertEqual(self.borg.forbidden, [])

    async def start_guest(self):
        query = _query(f"@{BOT_USERNAME}", reference_media=_voice(), query_id=len(self.tasks)+1)
        task = asyncio.create_task(stt.guest_stt_handler(query))
        self.tasks.append(task)
        request = await asyncio.wait_for(self.started.get(), 2)
        return task, request

    def reply_query(self, request, text=".stop"):
        query = _query(text)
        query.references[0].message = request.progress_text("🎙 Working…")
        return query

    async def test_implicit_guest_reply_stops_only_the_referenced_job(self):
        first, request = await self.start_guest()
        second, other = await self.start_guest()
        await stt.guest_stt_handler(self.reply_query(request))
        await asyncio.wait_for(first, 2)
        self.assertFalse(second.done())
        self.assertIn(other.token, stt._active_stt)
        self.assertEqual(self.edits[-1], {"text": stt.STT_STOPPED})
        self.assertIn("Stopping 1 transcription", self.answers[-1].text)
        self.assertEqual(len(self.downloaded), 2)  # The stop did not transcribe again.

    async def test_a_reply_to_the_original_guest_trigger_also_stops(self):
        task, request = await self.start_guest()
        stop = _query("/stop")
        stop.references = [copy.copy(request.query.trigger)]
        await stt.guest_stt_handler(stop)
        await asyncio.wait_for(task, 2)
        self.assertEqual(self.edits[-1]["text"], stt.STT_STOPPED)

    async def test_foreign_owner_and_other_threads_cannot_stop_a_guest_job(self):
        task, request = await self.start_guest()
        for caller, thread in ((OTHER, request.query.thread_key), (CALLER, "other-thread")):
            stop = self.reply_query(request)
            stop.caller_id = caller
            stop.thread_key = thread
            await stt.guest_stt_handler(stop)
            self.assertEqual(self.answers[-1].text, stt.STT_NO_MATCH)
            self.assertFalse(task.done())

    async def test_private_stop_cancels_own_guest_and_chat_jobs_only(self):
        guest_task, _ = await self.start_guest()

        async def tracked(caller):
            event = stop_event(caller=caller)
            request = stt.ActiveStt(event, asyncio.current_task())
            async with stt._track_stt(request):
                await self.started.put(request)
                await asyncio.Future()

        for caller in (CALLER, OTHER):
            self.tasks.append(asyncio.create_task(tracked(caller)))
            await asyncio.wait_for(self.started.get(), 2)
        event = stop_event()
        await stt.stop_stt_handler(event)
        await asyncio.wait_for(asyncio.gather(guest_task, self.tasks[-2]), 2)
        self.assertFalse(self.tasks[-1].done())
        self.assertIn("Stopping 2 transcriptions", event.reply.await_args.args[0])

    async def test_unmatched_stop_and_stop_without_a_reference_do_not_transcribe(self):
        await stt.guest_stt_handler(_query(".stop"))
        self.assertEqual(self.answers[-1].text, stt.STT_NO_MATCH)
        self.assertEqual((self.downloaded, self.jobs), ([], []))
        task, request = await self.start_guest()
        stop = self.reply_query(request, f"@{BOT_USERNAME} /stop")
        stop.references = []
        await stt.guest_stt_handler(stop)
        self.assertEqual(self.answers[-1].text, stt.STT_REPLY_TO_STOP)
        self.assertFalse(task.done())

    async def test_guest_progress_retains_the_job_marker(self):
        _, request = await self.start_guest()
        context = stt._current_stt.set(request)
        try:
            with patch.object(util, "edit_message", AsyncMock()) as edit:
                await stt._show_stt_status(request.status_message, "🎙 Transcribing… 10s")
            self.assertIn(f"STT #{request.token}", edit.await_args.args[1])
        finally:
            stt._current_stt.reset(context)

    async def test_repeated_stop_does_not_cancel_cleanup_twice(self):
        task, request = await self.start_guest()
        self.assertTrue(request.cancel())
        self.assertFalse(request.cancel())
        await asyncio.wait_for(task, 2)
        self.assertEqual(self.edits[-1]["text"], stt.STT_STOPPED)

    async def test_ambiguous_guest_reply_never_cancels_multiple_jobs(self):
        first, a = await self.start_guest()
        second, b = await self.start_guest()
        stop = self.reply_query(a)
        stop.references[0].message += f"\nSTT #{b.token}"
        await stt.guest_stt_handler(stop)
        self.assertEqual(self.answers[-1].text, stt.STT_REPLY_TO_STOP)
        self.assertFalse(first.done())
        self.assertFalse(second.done())


class ChatStopTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tasks = []
        self.started = asyncio.Event()
        self.download_blocked = False
        self.cwd = None
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.status = SimpleNamespace(id=101)
        self.event = stop_event(reply_id=None)
        self.event.raw_text = ""
        self.event.grouped_id = None
        self.event.message.id = 100
        self.event.get_chat = AsyncMock(return_value=CALLER)
        self.event.reply = AsyncMock(return_value=self.status)

        async def download(*, message, file):
            self.cwd = Path(file).parent
            Path(file).write_text("partial download")
            if self.download_blocked:
                self.started.set()
                await asyncio.Future()
            return file

        async def transcribe(*args, **kwargs):
            self.started.set()
            await asyncio.Future()

        message = SimpleNamespace(id=100, file=SimpleNamespace(name="voice.ogg"))
        borg = SimpleNamespace(me=types.User(id=BOT_ID, username=BOT_USERNAME),
            send_read_acknowledge=AsyncMock(), download_media=download)
        for item in (
            patch.dict(stt._active_stt, clear=True), patch.object(builtins, "borg", borg),
            patch.object(util, "borg", borg), patch.object(util, "dl_base", tmp.name+"/"),
            patch.object(util, "_messages_to_download", AsyncMock(return_value={100:message})),
            patch.object(util, "discreet_send", AsyncMock()),
            patch.object(util, "upload_output_files", AsyncMock()),
            patch.object(stt, "prepare_stt_job", return_value=SimpleNamespace(provider="gemini")),
            patch.object(stt, "run_stt_job", transcribe),
            patch.object(stt, "_log_transcription", Mock()),
        ):
            item.start()
            self.addCleanup(item.stop)
        self.edit = AsyncMock()
        item = patch.object(util, "edit_message", self.edit)
        item.start()
        self.addCleanup(item.stop)

    async def asyncTearDown(self):
        for task in self.tasks:
            if not task.done(): task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.assertEqual(stt._active_stt, {})

    async def start_job(self):
        task = asyncio.create_task(stt.media_handler(self.event))
        self.tasks.append(task)
        await asyncio.wait_for(self.started.wait(), 2)
        return task

    async def test_reply_to_progress_or_source_cancels_and_cleans_files(self):
        task = await self.start_job()
        command = stop_event(reply_id=101)
        await stt.stop_stt_handler(command)
        await asyncio.wait_for(task, 2)
        self.assertEqual(self.edit.await_args.args[1], stt.STT_STOPPED)
        self.assertFalse(self.cwd.exists())
        util.upload_output_files.assert_not_awaited()
        stt._log_transcription.assert_not_called()

    async def test_stop_during_download_cleans_partial_files(self):
        self.download_blocked = True
        task = await self.start_job()
        await stt.stop_stt_handler(stop_event(reply_id=100))
        await asyncio.wait_for(task, 2)
        self.assertFalse(self.cwd.exists())
        self.assertEqual(self.event.reply.await_args.args[0], stt.STT_STOPPED)
        self.edit.assert_not_awaited()

    async def test_wrong_reply_caller_or_chat_never_falls_back_to_stop_all(self):
        task = await self.start_job()
        for command in (stop_event(reply_id=999), stop_event(caller=OTHER, reply_id=101),
                        stop_event(chat=-42, reply_id=101, private=False)):
            await stt.stop_stt_handler(command)
            self.assertEqual(command.reply.await_args.args[0], stt.STT_NO_MATCH)
            self.assertFalse(task.done())

    async def test_external_cancellation_propagates_without_stopped_notice(self):
        task = await self.start_job()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError): await task
        self.edit.assert_not_awaited()
        self.assertFalse(self.cwd.exists())

    async def test_a_cancelled_model_request_never_retries_or_falls_back(self):
        started = asyncio.Event()

        async def prompt(**kwargs):
            started.set()
            await asyncio.Future()

        model = SimpleNamespace(supports_schema=True, prompt=AsyncMock(side_effect=prompt))
        with patch.object(stt, "_model_unavailable_p", AsyncMock(return_value=False)), patch.object(
            stt.stt_models, "load_media_model", return_value=model
        ) as load:
            task = asyncio.create_task(stt._transcribe_with_retry(
                models=["gemini/a", "gemini/b"], attachments=[], api_key="synthetic-key"
            ))
            await asyncio.wait_for(started.wait(), 2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        model.prompt.assert_awaited_once()
        load.assert_called_once_with("gemini/a")

    def test_stop_command_forms_and_media_filter(self):
        borg = SimpleNamespace(me=types.User(id=BOT_ID, usernames=[
            types.Username("TranscribeTestAI", active=True)]))
        with patch.object(builtins, "borg", borg):
            for text in (".stop", "/STOP", "/stop@TranscribeTestAI", "@TranscribeTestAI .stop"):
                self.assertTrue(stt._is_stt_stop_text(text))
            for text in (".stopping", "/stop@OtherAI", "@OtherAI .stop", "please .stop"):
                self.assertFalse(stt._is_stt_stop_text(text))
            event = SimpleNamespace(media=_voice(), sender=object(), raw_text=".stop", message=None)
            self.assertFalse(stt.is_transcribable_media_event(event))
