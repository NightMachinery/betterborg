"""`topic_titles`: when a new private topic is renamed, and to what."""

import asyncio
import logging
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from telethon.tl import functions, types
from telethon.tl.types import messages as messages_types

from uniborg import tg_format, topic_titles

T0 = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
CHAT_ID = 5
TOPIC_ID = 1242175
PEER = types.InputPeerUser(user_id=CHAT_ID, access_hash=7)


def _forum_topic(*, topic_id=TOPIC_ID, date=T0, title_missing=True):
    return types.ForumTopic(
        id=topic_id,
        date=date,
        peer=types.PeerUser(CHAT_ID),
        title="what is a monad",
        icon_color=0,
        top_message=0,
        read_inbox_max_id=0,
        read_outbox_max_id=0,
        unread_count=0,
        unread_mentions_count=0,
        unread_reactions_count=0,
        unread_poll_votes_count=0,
        from_id=types.PeerUser(CHAT_ID),
        notify_settings=types.PeerNotifySettings(),
        title_missing=title_missing,
    )


def _forum_topics(*topics):
    return messages_types.ForumTopics(
        count=len(topics), topics=list(topics), messages=[], chats=[], users=[], pts=0
    )


class _Client:
    """Answers `getForumTopicsByID` with TOPICS and records every request."""

    def __init__(self, *topics):
        self.topics = topics
        self.requests = []

    async def __call__(self, request):
        self.requests.append(request)
        if isinstance(request, functions.messages.GetForumTopicsByIDRequest):
            return _forum_topics(*self.topics)
        if isinstance(request, functions.messages.EditForumTopicRequest):
            return types.Updates(updates=[], users=[], chats=[], date=T0, seq=0)
        raise AssertionError(f"Unexpected request: {request!r}")


class _Redis:
    """`SET key value NX EX ttl`, the only command the marks use."""

    def __init__(self):
        self.values = {}
        self.calls = []

    async def set(self, key, value, *, nx, ex):
        self.calls.append((key, value, nx, ex))
        if nx and key in self.values:
            return None
        self.values[key] = value
        return True


def _request(**overrides):
    fields = dict(
        peer=PEER,
        chat_id=CHAT_ID,
        topic_id=TOPIC_ID,
        message_date=T0 + timedelta(seconds=1),
        question="What is a monad?",
        answer="A monad is a monoid in the category of endofunctors.",
        model_emoji="⚡",
        effort_symbol="◕",
    )
    fields.update(overrides)
    return topic_titles.TopicTitleRequest(**fields)


def _memory_marks():
    async def no_redis():
        return None

    return topic_titles.TopicTitleMarks(get_redis=no_redis)


class ComposeTopicTitleTests(unittest.TestCase):
    def compose(self, title, *, model_emoji="⚡", effort_symbol="◕"):
        return topic_titles.compose_topic_title(
            title, model_emoji=model_emoji, effort_symbol=effort_symbol
        )

    def test_emoji_and_symbol_come_first(self):
        self.assertEqual(self.compose("Monads explained"), "⚡◕ Monads explained")

    def test_a_model_without_reasoning_shows_only_its_emoji(self):
        self.assertEqual(
            self.compose("Monads explained", effort_symbol=""), "⚡ Monads explained"
        )

    def test_quotes_periods_and_extra_space_are_dropped(self):
        self.assertEqual(
            self.compose('  "Monads\n  explained."  '), "⚡◕ Monads explained"
        )

    def test_a_blank_title_is_none(self):
        for title in ("", "  ", '"."'):
            with self.subTest(title=title):
                self.assertIsNone(self.compose(title))

    def test_a_long_title_is_cut_to_telegrams_limit(self):
        title = self.compose("😀" * 200)

        self.assertEqual(tg_format.utf16_len(title), topic_titles.TOPIC_TITLE_MAX_UNITS)
        self.assertTrue(title.startswith("⚡◕ 😀"))
        self.assertTrue(title.endswith("…"))


class FirstMessageTests(unittest.TestCase):
    def first(self, topic, *, after):
        return topic_titles.is_first_message_of_untitled_topic(
            topic, message_date=T0 + after
        )

    def test_a_message_sent_with_its_untitled_topic_is_first(self):
        self.assertTrue(self.first(_forum_topic(), after=timedelta(seconds=0)))
        self.assertTrue(self.first(_forum_topic(), after=timedelta(minutes=10)))

    def test_a_later_message_is_not(self):
        self.assertFalse(self.first(_forum_topic(), after=timedelta(minutes=11)))

    def test_a_topic_the_user_named_is_never_renamed(self):
        self.assertFalse(
            self.first(_forum_topic(title_missing=None), after=timedelta(0))
        )

    def test_a_topic_without_a_date_is_not_renamed(self):
        self.assertFalse(self.first(_forum_topic(date=None), after=timedelta(0)))


class TopicTitleMarksTests(unittest.IsolatedAsyncioTestCase):
    async def test_redis_claims_once_with_a_long_expiry(self):
        redis = _Redis()

        async def get_redis():
            return redis

        marks = topic_titles.TopicTitleMarks(get_redis=get_redis, ttl_seconds=99)

        self.assertTrue(await marks.claim(CHAT_ID, TOPIC_ID))
        self.assertFalse(await marks.claim(CHAT_ID, TOPIC_ID))
        self.assertTrue(await marks.claim(CHAT_ID, TOPIC_ID + 1))
        self.assertEqual(
            redis.calls[0], (f"borg:topic_titled:{CHAT_ID}:{TOPIC_ID}", "1", True, 99)
        )

    async def test_without_redis_claims_live_in_memory(self):
        marks = _memory_marks()

        self.assertTrue(await marks.claim(CHAT_ID, TOPIC_ID))
        self.assertFalse(await marks.claim(CHAT_ID, TOPIC_ID))

    async def test_a_failing_redis_falls_back_to_memory(self):
        class _Broken:
            async def set(self, *args, **kwargs):
                raise ConnectionError("gone")

        async def get_redis():
            return _Broken()

        marks = topic_titles.TopicTitleMarks(get_redis=get_redis)
        with mock.patch.object(
            topic_titles.redis_util, "note_error"
        ) as note_error, self.assertLogs(topic_titles.logger, logging.WARNING):
            self.assertTrue(await marks.claim(CHAT_ID, TOPIC_ID))
            self.assertFalse(await marks.claim(CHAT_ID, TOPIC_ID))

        self.assertEqual(note_error.call_count, 2)

    async def test_memory_keeps_only_the_newest_claims(self):
        marks = _memory_marks()
        with mock.patch.object(topic_titles, "MEMORY_MARKS_MAX", 2):
            for topic_id in (1, 2, 3):
                await marks.claim(CHAT_ID, topic_id)

            self.assertTrue(await marks.claim(CHAT_ID, 1))
            self.assertFalse(await marks.claim(CHAT_ID, 3))


class TitleNewTopicTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.marks = _memory_marks()
        self.prompts = []
        self.reply = "Monads explained"

    async def generate(self, prompt):
        self.prompts.append(prompt)
        return topic_titles.TopicTitle(title=self.reply)

    async def title(self, client, **overrides):
        return await topic_titles.title_new_topic(
            client, _request(**overrides), generate=self.generate, marks=self.marks
        )

    async def test_the_first_answer_renames_the_topic_by_its_topic_id(self):
        client = _Client(_forum_topic())

        title = await self.title(client)

        self.assertEqual(title, "⚡◕ Monads explained")
        lookup, edit = client.requests
        self.assertEqual((lookup.peer, lookup.topics), (PEER, [TOPIC_ID]))
        self.assertEqual(
            (edit.peer, edit.topic_id, edit.title),
            (PEER, TOPIC_ID, "⚡◕ Monads explained"),
        )
        self.assertIn("What is a monad?", self.prompts[0])
        self.assertIn("monoid in the category", self.prompts[0])

    async def test_a_topic_is_renamed_once(self):
        client = _Client(_forum_topic())
        await self.title(client)
        client.requests.clear()

        self.assertIsNone(await self.title(client))
        self.assertEqual(client.requests, [])
        self.assertEqual(len(self.prompts), 1)

    async def test_untouched_topics(self):
        cases = {
            "named by the user": (_forum_topic(title_missing=None), {}),
            "an older topic": (
                _forum_topic(),
                {"message_date": T0 + timedelta(hours=1)},
            ),
            "deleted": (types.ForumTopicDeleted(id=TOPIC_ID), {}),
        }
        for name, (topic, overrides) in cases.items():
            with self.subTest(name):
                self.marks = _memory_marks()
                client = _Client(topic)

                self.assertIsNone(await self.title(client, **overrides))
                self.assertEqual(len(client.requests), 1)
        self.assertEqual(self.prompts, [])

    async def test_a_blank_title_leaves_the_topic_alone(self):
        self.reply = " . "
        client = _Client(_forum_topic())

        self.assertIsNone(await self.title(client))
        self.assertEqual(len(client.requests), 1)

    async def test_a_scheduled_failure_is_logged_not_raised(self):
        async def failing(prompt):
            raise topic_titles.asyncio.TimeoutError("slow")

        with self.assertLogs(topic_titles.logger, logging.ERROR) as logs:
            task = topic_titles.schedule_title_new_topic(
                _Client(_forum_topic()),
                _request(),
                generate=failing,
                marks=self.marks,
            )
            self.assertIsNone(await task)

        self.assertIn(str(TOPIC_ID), logs.output[0])


if __name__ == "__main__":
    unittest.main()
