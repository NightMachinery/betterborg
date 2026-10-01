"""Answers streamed as Telegram drafts (`uniborg/draft_stream.py`).

A *draft* is what `messages.setTyping` with a text-draft action shows; the
fake client records each draft's text. The fake event records every real
message the stream sends: `send` for a reply to the event, `reply` for a
reply to an earlier part.
"""

import asyncio
import itertools
import unittest

from telethon import errors, types

from uniborg import draft_stream, util
from uniborg.draft_stream import DraftAnswerMessage

_IDS = itertools.count(100)
_CHAT_IDS = itertools.count(7000)


class _Client:
    def __init__(self, errors_=()):
        self.drafts = []
        self.requests = []
        self.errors = list(errors_)
        self.calls = 0
        #: Set to make calls hang, as Telethon does sleeping out a flood wait.
        self.hang = False

    async def _parse_message_text(self, text, parse_mode):
        return text, []

    async def __call__(self, request):
        self.calls += 1
        if self.hang:
            await asyncio.Event().wait()
        if self.errors:
            raise self.errors.pop(0)
        self.requests.append(request)
        self.drafts.append(request.action.text.text)


class _Message:
    def __init__(self, log, text, **kwargs):
        self.id = next(_IDS)
        self.text = text
        self.kwargs = kwargs
        self._log = log

    async def edit(self, text, **kwargs):
        self._log.append(("edit", self.id, text))
        self.text = text
        return self

    async def reply(self, text, **kwargs):
        message = _Message(self._log, text, **kwargs)
        self._log.append(("reply", self.id, text))
        return message

    async def delete(self):
        self._log.append(("delete", self.id))


class _Event:
    def __init__(self, log, *, chat_id=None):
        self.chat_id = chat_id or next(_CHAT_IDS)
        self.sender_id = self.chat_id
        self.log = log
        self.sent = []

    async def get_input_chat(self):
        return types.InputPeerUser(self.chat_id, 0)

    async def reply(self, text, **kwargs):
        message = _Message(self.log, text, **kwargs)
        self.log.append(("send", text))
        self.sent.append(message)
        return message

    async def get_chat(self):
        return object()


def _draft(client, event, **kwargs):
    kwargs.setdefault("min_interval", 0)
    kwargs.setdefault("heartbeat_seconds", 60)
    return DraftAnswerMessage(client, event=event, **kwargs)


async def _settle():
    """Lets the draft worker send what is pending."""
    for _ in range(5):
        await asyncio.sleep(0.01)


class DraftStreamTests(unittest.TestCase):
    def setUp(self):
        self.client = _Client()
        self.log = []
        self.event = _Event(self.log)

    def test_streaming_edits_are_drafts_and_the_answer_is_one_message(self):
        async def run():
            draft = _draft(self.client, self.event)
            self.assertTrue(await draft.start())
            await util.edit_message(draft, "Hel▌", parse_mode="md")
            await _settle()
            await util.edit_message(draft, "Hello▌", parse_mode="md")
            await _settle()
            self.assertEqual(self.log, [])
            await draft.end_stream()
            await util.edit_message(draft, "Hello world", parse_mode="md")
            await draft.flush()

        asyncio.run(run())

        #: The last draft is the sync draft, which matches the answer.
        self.assertEqual(self.client.drafts, ["", "Hel▌", "Hello▌", "Hello world"])
        self.assertEqual(self.log, [("send", "Hello world")])

    def test_a_draft_has_a_stop_button_where_telethon_can_build_one(self):
        asyncio.run(_draft(self.client, self.event, top_msg_id=9).start())

        (request,) = self.client.requests
        self.assertEqual(request.top_msg_id, 9)
        if draft_stream.STOP_SUPPORTED:
            self.assertTrue(request.action.can_stop)

    def test_a_long_answer_shows_its_tail_then_sends_every_chunk(self):
        head, tail = "a" * 4000, "b" * 200

        async def run():
            draft = _draft(self.client, self.event)
            await draft.start()
            await util.edit_message(draft, f"{head}\n\n{tail}▌", parse_mode="md")
            await _settle()
            self.assertEqual(self.log, [])
            await draft.end_stream()
            await util.edit_message(draft, f"{head}\n\n{tail}", parse_mode="md")
            await draft.flush()

        asyncio.run(run())

        self.assertEqual(self.client.drafts[1].strip(), f"{tail}▌")
        (sent_head,) = self.event.sent
        self.assertEqual(
            [(kind, *rest[:-1], rest[-1].strip()) for kind, *rest in self.log],
            [("send", head), ("reply", sent_head.id, tail)],
        )

    def test_what_an_error_left_in_the_draft_is_sent_at_the_end(self):
        async def run():
            draft = _draft(self.client, self.event)
            await draft.start()
            await util.edit_message(draft, "partial▌", parse_mode="md")
            await util.edit_message(draft, "❌ failed", parse_mode="md", append_p=True)
            await draft.flush()

        asyncio.run(run())

        ((kind, text),) = self.log
        self.assertEqual(kind, "send")
        self.assertTrue(text.startswith("partial▌"))
        self.assertTrue(text.endswith("❌ failed"))

    def test_an_answer_that_was_only_images_sends_no_text(self):
        async def run():
            draft = _draft(self.client, self.event)
            await draft.start()
            await draft.end_stream()
            await draft.delete()
            await draft.flush()

        asyncio.run(run())

        self.assertEqual(self.log, [])

    def test_buttons_end_the_stream_and_are_sent_for_real(self):
        buttons = [[object()]]

        async def run():
            draft = _draft(self.client, self.event)
            await draft.start()
            await draft.edit("Usage limit reached.", buttons=buttons)
            return draft

        draft = asyncio.run(run())

        self.assertFalse(draft.streaming)
        self.assertEqual(self.log, [("send", "Usage limit reached.")])
        self.assertIs(self.event.sent[0].kwargs["buttons"], buttons)

    def test_a_refused_chat_streams_by_edits_from_then_on(self):
        refused = errors.RPCError(request=None, message=draft_stream.PEER_REFUSED)
        client = _Client([refused])

        async def run():
            first = await _draft(client, self.event).start()
            second = await _draft(client, self.event).start()
            return first, second

        self.assertEqual(asyncio.run(run()), (False, False))
        self.assertEqual(client.calls, 1)

    def test_any_other_failure_falls_back_for_that_answer_only(self):
        client = _Client([errors.RPCError(request=None, message="INTERNAL")])

        async def run():
            first = await _draft(client, self.event).start()
            draft = _draft(client, self.event)
            second = await draft.start()
            await draft.end_stream()
            return first, second

        self.assertEqual(asyncio.run(run()), (False, True))

    def test_a_flood_wait_holds_the_drafts_instead_of_sleeping(self):
        async def run():
            draft = _draft(self.client, self.event)
            await draft.start()
            self.client.errors.append(errors.FloodWaitError(request=None, capture=30))
            await util.edit_message(draft, "Hel▌", parse_mode="md")
            await _settle()
            await util.edit_message(draft, "Hello▌", parse_mode="md")
            await _settle()
            blocked = draft._blocked_until > draft._clock()
            await draft.flush()
            return blocked

        self.assertTrue(asyncio.run(run()))
        self.assertEqual(self.client.drafts, [""])
        self.assertEqual(self.log, [("send", "Hello▌")])

    def test_a_first_draft_that_hangs_falls_back_to_edits(self):
        self.client.hang = True

        started = asyncio.run(
            _draft(self.client, self.event, call_timeout=0.02).start()
        )

        self.assertFalse(started)

    def test_a_draft_that_hangs_counts_as_a_flood_wait(self):
        async def run():
            draft = _draft(self.client, self.event, call_timeout=0.02)
            await draft.start()
            self.client.hang = True
            await util.edit_message(draft, "Hel▌", parse_mode="md")
            await asyncio.sleep(0.1)
            blocked = draft._blocked_until > draft._clock()
            self.client.hang = False
            await draft.flush()
            return blocked

        self.assertTrue(asyncio.run(run()))
        #: The hung call was cancelled, so its stale draft was never sent.
        self.assertEqual(self.client.drafts, [""])
        self.assertEqual(self.log, [("send", "Hel▌")])

    def test_a_quiet_stream_sends_heartbeats_with_the_elapsed_time(self):
        async def run():
            draft = _draft(self.client, self.event, heartbeat_seconds=0.02)
            await draft.start()
            await asyncio.sleep(0.08)
            await draft.end_stream()

        asyncio.run(run())

        self.assertGreater(len(self.client.drafts), 1)
        self.assertTrue(self.client.drafts[1].startswith("⏳ "))

    @unittest.skipUnless(draft_stream.STOP_SUPPORTED, "no Stop button here")
    def test_stop_reaches_only_the_stream_of_the_user_who_pressed_it(self):
        stops = []

        async def run():
            draft = _draft(self.client, self.event)
            draft.on_stop = lambda: stops.append(draft.random_id)
            await draft.start()
            action = types.SendMessageStopDraftAction(random_id=draft.random_id)
            await draft_stream.on_typing_update(
                types.UpdateUserTyping(user_id=self.event.chat_id + 1, action=action)
            )
            self.assertEqual(stops, [])
            await draft_stream.on_typing_update(
                types.UpdateUserTyping(user_id=self.event.chat_id, action=action)
            )
            await draft.end_stream()
            await draft.flush()
            return draft

        draft = asyncio.run(run())

        self.assertEqual(stops, [draft.random_id])
        self.assertNotIn(draft.random_id, draft_stream._ACTIVE)


class StreamingPaceTests(unittest.TestCase):
    def test_edits_slow_down_and_drafts_keep_the_pace(self):
        draft = DraftAnswerMessage(_Client(), event=_Event([]))
        cases = [
            (None, 10, draft_stream.StreamingPace(0.8, "▌")),
            (None, 31, draft_stream.StreamingPace(15, "▌💤")),
            (None, 121, draft_stream.StreamingPace(60, "▌💤💤")),
            (draft, 121, draft_stream.StreamingPace(0.8, "▌")),
            (draft, 5, draft_stream.StreamingPace(1.0, "▌")),
        ]
        for message, elapsed, pace in cases:
            interval = 2.0 if elapsed == 5 else 0.8
            with self.subTest(message=message, elapsed=elapsed):
                self.assertEqual(
                    draft_stream.streaming_pace(
                        message, elapsed=elapsed, edit_interval=interval
                    ),
                    pace,
                )


if __name__ == "__main__":
    unittest.main()
