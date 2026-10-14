"""Flood recovery and fairness without sleeping or contacting Telegram."""

import asyncio
import unittest
from unittest.mock import AsyncMock

from telethon import errors
from telethon.tl import functions, types

from uniborg import telegram_queue as tq


class Clock:
    def __init__(self):
        self.now = 1000.0
        self.alarms = []

    async def wait(self, event, seconds):
        wake = asyncio.create_task(event.wait())
        alarm = asyncio.get_running_loop().create_future()
        if seconds is not None:
            self.alarms.append((self.now + seconds, alarm))
        try:
            await asyncio.wait((wake, alarm), return_when=asyncio.FIRST_COMPLETED)
        finally:
            wake.cancel()
            alarm.cancel()

    def advance(self, seconds):
        self.now += seconds
        for due, alarm in self.alarms:
            if due <= self.now and not alarm.done():
                alarm.set_result(None)
        self.alarms = [(due, alarm) for due, alarm in self.alarms if not alarm.done()]


async def settle():
    for _ in range(12):
        await asyncio.sleep(0)


def send(chat=1, text="hello"):
    return functions.messages.SendMessageRequest(
        peer=types.InputPeerUser(chat, 7), message=text
    )


class QueueTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.clock = Clock()
        self.queue = tq.TelegramQueue(
            clock=lambda: self.clock.now, wait=self.clock.wait
        )

    async def advance(self, seconds):
        self.clock.advance(seconds)
        await settle()

    async def test_many_topics_share_cooldown_then_deliver_once(self):
        attempted, delivered = [], []

        async def call(request):
            attempted.append((request, self.clock.now))
            if len(attempted) == 1:
                raise errors.FloodWaitError(request=request, capture=186)
            delivered.append(request)
            return request.random_id

        requests = [send(text=f"topic {i}") for i in range(12)]
        tasks = [asyncio.create_task(self.queue.run(req, call)) for req in requests]
        await settle()
        self.assertEqual(len(attempted), 1)
        await self.advance(185)
        self.assertEqual(len(attempted), 1)
        await self.advance(1)
        for _ in range(11):
            await self.advance(1)
        await asyncio.gather(*tasks)
        self.assertCountEqual(delivered, requests)
        self.assertEqual(len(attempted), 13)
        self.assertIs(attempted[0][0], attempted[1][0])
        self.assertEqual(self.queue.pending, [])

    async def test_slow_mode_is_local_to_one_chat(self):
        call = AsyncMock(
            side_effect=[errors.SlowModeWaitError(None, 100), "other", "first"]
        )
        first = asyncio.create_task(self.queue.run(send(1), call))
        await settle()
        self.assertEqual(await self.queue.run(send(2), call), "other")
        await self.advance(100)
        self.assertEqual(await first, "first")

    async def test_send_and_edit_share_private_chat_pacing(self):
        call = AsyncMock(return_value="ok")
        await self.queue.run(send(), call)
        edit = functions.messages.EditMessageRequest(
            peer=types.InputPeerUser(1, 7), id=4, message="final"
        )
        task = asyncio.create_task(self.queue.run(edit, call))
        await settle()
        self.assertEqual(call.await_count, 1)
        await self.advance(1)
        await task
        self.assertEqual(call.await_count, 2)

    async def test_final_answer_overtakes_queued_metadata(self):
        call = AsyncMock(return_value="ok")
        req = send()
        await self.queue.run(req, call)
        with tq.operation(tq.Priority.METADATA):
            metadata = asyncio.create_task(self.queue.run(send(text="metadata"), call))
        with tq.operation(tq.Priority.FINAL):
            final = asyncio.create_task(self.queue.run(send(text="final"), call))
        await settle()
        await self.advance(1)
        self.assertTrue(final.done())
        self.assertFalse(metadata.done())
        self.assertEqual(call.await_args.args[0].message, "final")
        await self.advance(1)
        await metadata

    async def test_optional_stream_is_skipped_without_waiting(self):
        call = AsyncMock(return_value="ok")
        await self.queue.run(send(), call)
        with tq.operation(tq.Priority.STREAM):
            with self.assertRaises(tq.StreamingDeferred):
                await self.queue.run(send(text="partial"), call)
        self.assertEqual(call.await_count, 1)
        self.assertEqual(self.queue.pending, [])

    async def test_stream_flood_sets_cooldown_and_returns_to_model(self):
        call = AsyncMock(side_effect=errors.FloodWaitError(None, 150))
        with tq.operation(tq.Priority.STREAM):
            with self.assertRaises(tq.StreamingDeferred):
                await self.queue.run(send(), call)
        call.side_effect = None
        task = asyncio.create_task(self.queue.run(send(2), call))
        await settle()
        self.assertFalse(task.done())
        await self.advance(150)
        await task
        self.assertEqual(call.await_count, 2)

    async def test_cancellation_removes_waiter_and_does_not_block_other_work(self):
        call = AsyncMock(side_effect=errors.FloodWaitError(None, 163))
        task = asyncio.create_task(self.queue.run(send(), call))
        await settle()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.queue.pending, [])
        self.assertEqual(self.queue.busy, set())
        other = AsyncMock(return_value="ok")
        self.assertEqual(
            await self.queue.run(functions.help.GetConfigRequest(), other), "ok"
        )

    async def test_ambiguous_network_failure_is_not_retried(self):
        call = AsyncMock(side_effect=ConnectionError("lost"))
        with self.assertRaises(ConnectionError):
            await self.queue.run(send(), call)
        self.assertEqual(call.await_count, 1)

    async def test_guard_is_rechecked_after_a_flood_wait(self):
        valid = True
        guard = AsyncMock(side_effect=lambda: valid)
        call = AsyncMock(side_effect=errors.FloodWaitError(None, 112))
        with tq.operation(tq.Priority.METADATA, before_send=guard):
            task = asyncio.create_task(self.queue.run(send(), call))
        await settle()
        valid = False
        await self.advance(112)
        with self.assertRaises(tq.OperationSuperseded):
            await task
        self.assertEqual(call.await_count, 1)
        self.assertEqual(guard.await_count, 2)

    async def test_client_mixin_resolves_and_uses_the_same_request_for_retries(self):
        class Transport:
            def __init__(self):
                self.calls = []

            async def get_input_entity(self, peer):
                return peer

            async def __call__(self, request, **kwargs):
                self.calls.append((request, kwargs))
                if len(self.calls) == 1:
                    raise errors.FloodWaitError(request, 100)
                return "accepted"

        class Client(tq.TelegramQueueMixin, Transport):
            pass

        client = Client()
        client.telegram_queue = self.queue
        req = send()
        task = asyncio.create_task(client(req))
        await settle()
        await self.advance(100)
        self.assertEqual(await task, "accepted")
        self.assertIs(client.calls[0][0], client.calls[1][0])
        self.assertEqual(client.calls[0][1]["flood_sleep_threshold"], 0)

    async def test_topic_edits_are_paced_across_chats(self):
        call = AsyncMock(return_value="ok")

        def rename(chat):
            return functions.messages.EditForumTopicRequest(
                peer=types.InputPeerUser(chat, 7), topic_id=42, title="Title"
            )

        await self.queue.run(rename(1), call)
        task = asyncio.create_task(self.queue.run(rename(2), call))
        await settle()
        self.assertEqual(call.await_count, 1)
        await self.advance(1)
        await task

    def test_bounded_and_at_most_once_requests_are_excluded(self):
        self.assertFalse(
            self.queue.handles(
                functions.messages.SetTypingRequest(
                    peer=types.InputPeerUser(1, 7),
                    action=types.SendMessageTypingAction(),
                )
            )
        )
        self.assertFalse(
            self.queue.handles(
                functions.messages.SetBotCallbackAnswerRequest(query_id=1, cache_time=0)
            )
        )
