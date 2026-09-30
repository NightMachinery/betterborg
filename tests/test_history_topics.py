"""Topic recording in the chat history (uniborg/history_util.py).

Terms follow uniborg/topics.py:

- a *private topic* is a topic in a bot's private chat, and its *topic id*
  is the `reply_to_top_id` every message in it carries;
- a *legacy item* is a history item stored before the history recorded
  topics: its stored form has no `topic_id` key.

The header shapes are the ones a canary bot observed live (Telethon 1.45,
layer 229): see tests/test_llm_chat_topics.py.
"""

import asyncio
from datetime import datetime, timedelta, timezone
import json
import unittest
from unittest.mock import AsyncMock, patch

from telethon.tl.types import (
    InputReplyToMessage,
    Message,
    MessageReactions,
    MessageReplyHeader,
    PeerChannel,
    PeerUser,
    ReactionCount,
    ReactionEmoji,
)

from uniborg import history_util, redis_util, topics

USER_ID = 999000771
CHANNEL_ID = 999000773
ROOT_ID, TOPIC_ID = 322, 1241380
OTHER_TOPIC_ID = 1241384
FORUM_TOPIC_ID = 40
T0 = datetime(2026, 9, 29, tzinfo=timezone.utc)


def _date(msg_id):
    return T0 + timedelta(seconds=msg_id)


def _in_topic(msg_id, *, parent=ROOT_ID, top_id=TOPIC_ID):
    return Message(
        id=msg_id,
        peer_id=PeerUser(USER_ID),
        date=_date(msg_id),
        message=f"message {msg_id}",
        reply_to=MessageReplyHeader(
            forum_topic=True, reply_to_msg_id=parent, reply_to_top_id=top_id
        ),
    )


def _outside_topics(msg_id, *, reply_to=None):
    return Message(
        id=msg_id,
        peer_id=PeerUser(USER_ID),
        date=_date(msg_id),
        message=f"message {msg_id}",
        reply_to=reply_to,
    )


def _in_forum(msg_id, *, parent=45, top_id=FORUM_TOPIC_ID):
    return Message(
        id=msg_id,
        peer_id=PeerChannel(CHANNEL_ID),
        date=_date(msg_id),
        message=f"message {msg_id}",
        reply_to=MessageReplyHeader(
            forum_topic=True, reply_to_msg_id=parent, reply_to_top_id=top_id
        ),
    )


def _legacy_dict(msg_id, **fields):
    """The stored form of an item as the history wrote it before topics."""
    return {
        "message_id": msg_id,
        "timestamp": _date(msg_id).isoformat(),
        "deleted": False,
        "reactions": None,
        **fields,
    }


def _reactions(emoji="👍"):
    return MessageReactions(
        results=[ReactionCount(reaction=ReactionEmoji(emoji), count=1)]
    )


def _run(coro):
    return asyncio.run(coro)


class _FakePipeline:
    def __init__(self, redis):
        self.redis = redis
        self.ops = []

    def delete(self, key):
        self.ops.append(lambda: self.redis.sets.pop(key, None))

    def zadd(self, key, mapping):
        self.ops.append(lambda: self.redis.sets.setdefault(key, {}).update(mapping))

    def expire(self, key, seconds):
        pass

    async def execute(self):
        for op in self.ops:
            op()


class _FakeRedis:
    """The sorted-set and string commands history_util uses."""

    def __init__(self):
        self.sets = {}
        self.strings = {}

    async def zrange(self, key, start, end):
        members = self.sets.get(key, {})
        ordered = sorted(members, key=members.get)
        return ordered[start:] if end == -1 else ordered[start : end + 1]

    async def expire(self, key, seconds):
        return True

    async def set(self, key, value, ex=None):
        self.strings[key] = value

    def pipeline(self):
        return _FakePipeline(self)

    def members(self, chat_id):
        """The stored strings of CHAT_ID's history, oldest first."""
        return _run(self.zrange(redis_util.chat_history_key(chat_id), 0, -1))


class _MemoryBackendCase(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(redis_util, "is_redis_available", return_value=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        history_util._history_cache.clear()
        history_util._message_id_to_chat_id_map.clear()
        self.addCleanup(history_util._history_cache.clear)
        self.addCleanup(history_util._message_id_to_chat_id_map.clear)

    def record(self, *messages):
        for message in messages:
            _run(history_util.record_message(message))

    def items(self):
        return {
            item.message_id: item
            for item in history_util._get_history_items_memory(USER_ID)
        }


class PrivateMessageTopicIdTests(unittest.TestCase):
    def test_a_message_in_a_private_topic_gives_its_topic(self):
        self.assertEqual(topics.private_message_topic_id(_in_topic(330)), TOPIC_ID)

    def test_an_explicit_reply_in_a_topic_gives_the_same_topic(self):
        message = _in_topic(330, parent=329)
        self.assertEqual(topics.private_message_topic_id(message), TOPIC_ID)

    def test_a_message_outside_topics_gives_none(self):
        self.assertIsNone(topics.private_message_topic_id(_outside_topics(5)))
        replied = _outside_topics(6, reply_to=MessageReplyHeader(reply_to_msg_id=5))
        self.assertIsNone(topics.private_message_topic_id(replied))

    def test_a_forum_supergroup_message_gives_none(self):
        self.assertIsNone(topics.private_message_topic_id(_in_forum(50)))

    def test_a_message_built_from_a_short_sent_update_gives_its_placement(self):
        #: Telethon builds this for `UpdateShortSentMessage`, with the
        #: request's reply target in place of a header.
        placed = _outside_topics(
            7, reply_to=InputReplyToMessage(reply_to_msg_id=6, top_msg_id=TOPIC_ID)
        )
        unplaced = _outside_topics(8, reply_to=InputReplyToMessage(reply_to_msg_id=6))
        self.assertEqual(topics.private_message_topic_id(placed), TOPIC_ID)
        self.assertIsNone(topics.private_message_topic_id(unplaced))


class HistoryItemSerializationTests(unittest.TestCase):
    def test_a_legacy_item_loads_outside_every_topic(self):
        item = history_util.HistoryItem.from_dict(_legacy_dict(5))
        self.assertIsNone(item.topic_id)
        self.assertEqual(item.message_id, 5)

    def test_an_item_outside_topics_is_stored_as_before(self):
        item = history_util.HistoryItem(message_id=5, timestamp=_date(5))
        self.assertEqual(item.to_dict(), _legacy_dict(5))

    def test_an_item_in_a_topic_round_trips(self):
        item = history_util.HistoryItem(
            message_id=5, timestamp=_date(5), topic_id=TOPIC_ID
        )
        self.assertEqual(item.to_dict(), _legacy_dict(5, topic_id=TOPIC_ID))
        self.assertEqual(history_util.HistoryItem.from_dict(item.to_dict()), item)


class RecordMessageTests(_MemoryBackendCase):
    def test_each_message_is_filed_under_its_topic(self):
        self.record(
            _in_topic(330),
            _in_topic(331, parent=330),
            _in_topic(332, parent=326, top_id=OTHER_TOPIC_ID),
            _outside_topics(333),
        )
        topic_of = {msg_id: item.topic_id for msg_id, item in self.items().items()}
        self.assertEqual(
            topic_of, {330: TOPIC_ID, 331: TOPIC_ID, 332: OTHER_TOPIC_ID, 333: None}
        )

    def test_reactions_keep_the_topic_either_way_round(self):
        self.record(_in_topic(330))
        _run(history_util.record_message_reactions(USER_ID, 330, _reactions()))
        _run(history_util.record_message_reactions(USER_ID, 331, _reactions("🔥")))
        self.record(_in_topic(331))
        items = self.items()
        for msg_id in (330, 331):
            self.assertEqual(items[msg_id].topic_id, TOPIC_ID)
            self.assertIsNotNone(items[msg_id].reactions)

    def test_a_deleted_message_keeps_its_topic(self):
        self.record(_in_topic(330))
        _run(history_util.mark_as_deleted(USER_ID, [330]))
        item = self.items()[330]
        self.assertTrue(item.deleted)
        self.assertEqual(item.topic_id, TOPIC_ID)


class LastNTopicIdsTests(_MemoryBackendCase):
    def setUp(self):
        super().setUp()
        self.record(
            _outside_topics(300),
            _in_topic(330),
            _in_topic(331, parent=326, top_id=OTHER_TOPIC_ID),
            _in_topic(332),
            _outside_topics(333),
            _in_topic(334, parent=332),
            _in_topic(335),
        )

    def ids(self, **kwargs):
        kwargs.setdefault("n", 100)
        return _run(history_util.get_last_n_topic_ids(USER_ID, TOPIC_ID, **kwargs))

    def test_only_the_topics_messages_come_back_oldest_first(self):
        self.assertEqual(self.ids(), [330, 332, 334, 335])

    def test_the_cap_keeps_the_latest(self):
        self.assertEqual(self.ids(n=2), [334, 335])

    def test_deleted_messages_are_skipped_unless_asked_for(self):
        _run(history_util.mark_as_deleted(USER_ID, [332]))
        self.assertEqual(self.ids(), [330, 334, 335])
        self.assertEqual(self.ids(skip_deleted_p=False), [330, 332, 334, 335])

    def test_the_whole_chat_is_unchanged(self):
        self.assertEqual(
            _run(history_util.get_last_n_ids(USER_ID, 100)),
            [300, 330, 331, 332, 333, 334, 335],
        )


class RedisBackendTests(unittest.TestCase):
    """The same, through Redis, where history recorded before topics lives."""

    def setUp(self):
        self.redis = _FakeRedis()
        for patcher in (
            patch.object(redis_util, "is_redis_available", return_value=True),
            patch.object(
                redis_util, "get_redis", new=AsyncMock(return_value=self.redis)
            ),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        key = redis_util.chat_history_key(USER_ID)
        #: Two legacy items, one of them in what was already a topic.
        self.redis.sets[key] = {
            json.dumps(_legacy_dict(msg_id)): _date(msg_id).timestamp()
            for msg_id in (320, 321)
        }

    def test_legacy_items_still_load_and_stay_outside_topics(self):
        _run(history_util.record_message(_in_topic(330)))
        _run(history_util.record_message(_outside_topics(331)))
        self.assertEqual(
            _run(history_util.get_last_n_ids(USER_ID, 100)), [320, 321, 330, 331]
        )
        self.assertEqual(
            _run(history_util.get_last_n_topic_ids(USER_ID, TOPIC_ID, n=100)), [330]
        )

    def test_rewrites_store_legacy_and_outside_items_byte_for_byte(self):
        _run(history_util.record_message(_in_topic(330)))
        _run(history_util.record_message(_outside_topics(331)))
        _run(history_util.mark_as_deleted(USER_ID, [330]))
        expected = [
            _legacy_dict(320),
            _legacy_dict(321),
            _legacy_dict(330, deleted=True, topic_id=TOPIC_ID),
            _legacy_dict(331),
        ]
        self.assertEqual(
            self.redis.members(USER_ID), [json.dumps(item) for item in expected]
        )


if __name__ == "__main__":
    unittest.main()
