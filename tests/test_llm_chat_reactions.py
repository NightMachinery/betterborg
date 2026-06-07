import asyncio
import builtins
import contextlib
import importlib
import io
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from telethon.tl.types import (
    MessageReactions,
    PeerChannel,
    PeerUser,
    ReactionCount,
    ReactionEmoji,
    UpdateBotMessageReaction,
    UpdateBotMessageReactions,
    UpdateMessageReactions,
    Updates,
)

from uniborg import history_util


class _FakeLoop:
    def create_task(self, coro):
        coro.close()
        return None


class _FakeBorg:
    loop = _FakeLoop()


_llm_chat = None


def _load_llm_chat():
    global _llm_chat
    if _llm_chat is not None:
        return _llm_chat

    builtins.borg = _FakeBorg()

    async def _import_module():
        return importlib.import_module("llm_chat_plugins.llm_chat")

    _llm_chat = asyncio.run(_import_module())
    return _llm_chat


def _reaction_payload(emoji="👍", count=1):
    return MessageReactions(
        results=[ReactionCount(reaction=ReactionEmoji(emoji), count=count)]
    )


class _FakeClient:
    def __init__(self, updates=None, fail_refresh=False):
        self.updates = updates if updates is not None else []
        self.fail_refresh = fail_refresh
        self.requests = []

    async def get_input_entity(self, chat_id):
        self.chat_id = chat_id
        return f"input-{chat_id}"

    async def __call__(self, request):
        self.requests.append(request)
        if self.fail_refresh:
            raise RuntimeError("telegram unavailable")
        return self.updates


class ReactionHydrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.llm_chat = _load_llm_chat()

    def setUp(self):
        self.original_is_bot = self.llm_chat.IS_BOT
        self.original_verbosity_mode = history_util.REACTION_HISTORY_CACHE_VERBOSITY_MODE
        self.original_reaction_debug = history_util.REACTION_CACHE_DEBUG
        self.redis_available = patch(
            "uniborg.redis_util.is_redis_available", return_value=False
        )
        self.redis_available.start()
        self.llm_chat.IS_BOT = None
        history_util.REACTION_HISTORY_CACHE_VERBOSITY_MODE = "silent"
        history_util.REACTION_CACHE_DEBUG = False
        history_util._history_cache.clear()
        history_util._message_id_to_chat_id_map.clear()

    def tearDown(self):
        self.redis_available.stop()
        self.llm_chat.IS_BOT = self.original_is_bot
        history_util.REACTION_HISTORY_CACHE_VERBOSITY_MODE = self.original_verbosity_mode
        history_util.REACTION_CACHE_DEBUG = self.original_reaction_debug
        history_util._history_cache.clear()
        history_util._message_id_to_chat_id_map.clear()

    def test_history_item_accepts_old_records_without_reactions(self):
        item = history_util.HistoryItem.from_dict(
            {"message_id": 1, "timestamp": "2026-01-01T00:00:00", "deleted": False}
        )

        self.assertIsNone(item.reactions)

    def test_reaction_serialization_round_trips(self):
        reactions = MessageReactions(
            results=[ReactionCount(reaction=ReactionEmoji("❤️"), count=2)],
            can_see_list=True,
            recent_reactions=[
                history_util.MessagePeerReaction(
                    peer_id=PeerUser(456), date=None, reaction=ReactionEmoji("❤️")
                )
            ],
        )

        restored = history_util.message_reactions_from_dict(
            history_util.message_reactions_to_dict(reactions)
        )

        self.assertEqual(restored.results[0].reaction.emoticon, "❤️")
        self.assertEqual(restored.results[0].count, 2)
        self.assertEqual(restored.recent_reactions[0].peer_id.user_id, 456)

    def test_refresh_populates_matching_message_reactions_and_persists(self):
        reactions = _reaction_payload("❤️", 2)
        messages = [
            SimpleNamespace(id=1209, reactions=None),
            SimpleNamespace(id=1210, reactions=None),
        ]
        client = _FakeClient(
            Updates(
                updates=[
                    UpdateMessageReactions(
                        peer=PeerChannel(123), msg_id=1210, reactions=reactions
                    )
                ],
                users=[],
                chats=[],
                date=None,
                seq=0,
            )
        )

        asyncio.run(self.llm_chat._refresh_message_reactions(client, 123, messages))
        persisted = asyncio.run(history_util.get_message_reactions(123, 1210))

        self.assertIsNone(messages[0].reactions)
        self.assertIs(messages[1].reactions, reactions)
        self.assertEqual(client.requests[0].id, [1209, 1210])
        self.assertEqual(persisted.results[0].reaction.emoticon, "❤️")

    def test_refresh_accepts_single_update_response(self):
        reactions = _reaction_payload("🔥", 1)
        message = SimpleNamespace(id=1209, reactions=None)
        client = _FakeClient(
            UpdateMessageReactions(
                peer=PeerChannel(123), msg_id=1209, reactions=reactions
            )
        )

        asyncio.run(self.llm_chat._refresh_message_reactions(client, 123, [message]))

        self.assertIs(message.reactions, reactions)

    def test_refresh_failure_leaves_messages_unchanged(self):
        message = SimpleNamespace(id=1209, reactions=None)
        client = _FakeClient(fail_refresh=True)

        asyncio.run(self.llm_chat._refresh_message_reactions(client, 123, [message]))

        self.assertIsNone(message.reactions)
        self.assertEqual(len(client.requests), 1)

    def test_bot_refresh_uses_history_util_cached_reactions(self):
        reactions = _reaction_payload("👍", 1)
        message = SimpleNamespace(id=1209, reactions=None)
        client = _FakeClient()
        asyncio.run(history_util.record_message_reactions(123, 1209, reactions))

        self.llm_chat.IS_BOT = True
        asyncio.run(self.llm_chat._refresh_message_reactions(client, 123, [message]))

        self.assertEqual(client.requests, [])
        self.assertEqual(message.reactions.results[0].count, 1)
        self.assertEqual(message.reactions.results[0].reaction.emoticon, "👍")

    def test_record_reaction_update_stores_individual_actor_reaction(self):
        update = UpdateBotMessageReaction(
            peer=PeerChannel(123),
            msg_id=1209,
            date=None,
            actor=PeerUser(456),
            old_reactions=[],
            new_reactions=[ReactionEmoji("👍")],
            qts=0,
        )
        chat_id = -1000000000123
        message = SimpleNamespace(id=1209, reactions=None)

        asyncio.run(history_util.record_reaction_update(update))
        applied = asyncio.run(history_util.hydrate_message_reactions(chat_id, [message]))

        self.assertEqual(applied, 1)
        self.assertEqual(message.reactions.results[0].count, 1)
        self.assertEqual(message.reactions.results[0].reaction.emoticon, "👍")
        self.assertEqual(message.reactions.recent_reactions[0].peer_id.user_id, 456)

    def test_record_reaction_update_stores_aggregate_reactions(self):
        update = UpdateBotMessageReactions(
            peer=PeerChannel(123),
            msg_id=1210,
            date=None,
            reactions=[ReactionCount(reaction=ReactionEmoji("❤️"), count=3)],
            qts=0,
        )
        chat_id = -1000000000123
        message = SimpleNamespace(id=1210, reactions=None)

        asyncio.run(history_util.record_reaction_update(update))
        asyncio.run(history_util.hydrate_message_reactions(chat_id, [message]))

        self.assertEqual(message.reactions.results[0].count, 3)
        self.assertEqual(message.reactions.results[0].reaction.emoticon, "❤️")

    def test_individual_reaction_update_preserves_cached_aggregate_count(self):
        aggregate_update = UpdateBotMessageReactions(
            peer=PeerChannel(123),
            msg_id=1211,
            date=None,
            reactions=[ReactionCount(reaction=ReactionEmoji("👍"), count=4)],
            qts=0,
        )
        actor_update = UpdateBotMessageReaction(
            peer=PeerChannel(123),
            msg_id=1211,
            date=None,
            actor=PeerUser(456),
            old_reactions=[],
            new_reactions=[ReactionEmoji("❤️")],
            qts=0,
        )
        message = SimpleNamespace(id=1211, reactions=None)

        asyncio.run(history_util.record_reaction_update(aggregate_update))
        asyncio.run(history_util.record_reaction_update(actor_update))
        asyncio.run(history_util.hydrate_message_reactions(-1000000000123, [message]))

        counts = {
            result.reaction.emoticon: result.count
            for result in message.reactions.results
        }
        self.assertEqual(counts, {"👍": 4, "❤️": 1})
        self.assertEqual(message.reactions.recent_reactions[0].peer_id.user_id, 456)

    def test_print_each_update_logs_individual_reaction_update(self):
        history_util.REACTION_CACHE_DEBUG = True
        history_util.REACTION_HISTORY_CACHE_VERBOSITY_MODE = "print_each_update"
        update = UpdateBotMessageReaction(
            peer=PeerChannel(123),
            msg_id=1212,
            date=None,
            actor=PeerUser(456),
            old_reactions=[],
            new_reactions=[ReactionEmoji("🤣")],
            qts=0,
        )
        out = io.StringIO()

        with contextlib.redirect_stdout(out):
            asyncio.run(history_util.record_reaction_update(update))

        printed = out.getvalue()
        self.assertIn(
            "HistoryUtil reaction_history_cache reaction_update_cached", printed
        )
        self.assertIn('"update_type": "UpdateBotMessageReaction"', printed)
        self.assertIn('"new_reactions": ["🤣"]', printed)
        self.assertIn('"cache_key": [-1000000000123, 1212]', printed)

    def test_print_each_update_logs_aggregate_reaction_update(self):
        history_util.REACTION_CACHE_DEBUG = True
        history_util.REACTION_HISTORY_CACHE_VERBOSITY_MODE = "print_each_update"
        update = UpdateBotMessageReactions(
            peer=PeerChannel(123),
            msg_id=1213,
            date=None,
            reactions=[ReactionCount(reaction=ReactionEmoji("❤️"), count=3)],
            qts=0,
        )
        out = io.StringIO()

        with contextlib.redirect_stdout(out):
            asyncio.run(history_util.record_reaction_update(update))

        printed = out.getvalue()
        self.assertIn(
            "HistoryUtil reaction_history_cache reaction_update_cached", printed
        )
        self.assertIn('"update_type": "UpdateBotMessageReactions"', printed)
        self.assertIn(
            '"aggregate_update_reactions": '
            '[{"chosen_order": null, "count": 3, "reaction": "❤️"}]',
            printed,
        )

    def test_print_each_update_logs_cache_application_to_history(self):
        history_util.REACTION_CACHE_DEBUG = True
        history_util.REACTION_HISTORY_CACHE_VERBOSITY_MODE = "print_each_update"
        reactions = _reaction_payload("👍", 2)
        message = SimpleNamespace(id=1214, reactions=None)
        asyncio.run(history_util.record_message_reactions(123, 1214, reactions))
        out = io.StringIO()

        with contextlib.redirect_stdout(out):
            applied = asyncio.run(
                history_util.hydrate_message_reactions(123, [message])
            )

        self.assertEqual(applied, 1)
        printed = out.getvalue()
        self.assertIn(
            "HistoryUtil reaction_history_cache cached_reactions_applied_to_history",
            printed,
        )
        self.assertIn('"applied": 1', printed)
        self.assertIn('"message_ids": [1214]', printed)

    def test_silent_verbosity_suppresses_reaction_cache_prints(self):
        history_util.REACTION_CACHE_DEBUG = True
        history_util.REACTION_HISTORY_CACHE_VERBOSITY_MODE = "silent"
        update = UpdateBotMessageReaction(
            peer=PeerChannel(123),
            msg_id=1215,
            date=None,
            actor=PeerUser(456),
            old_reactions=[],
            new_reactions=[ReactionEmoji("🔥")],
            qts=0,
        )
        out = io.StringIO()

        with contextlib.redirect_stdout(out):
            asyncio.run(history_util.record_reaction_update(update))

        self.assertEqual(out.getvalue(), "")

    def test_add_message_preserves_existing_reaction_metadata(self):
        reactions = _reaction_payload("⚡", 1)

        asyncio.run(history_util.record_message_reactions(123, 1216, reactions))
        asyncio.run(
            history_util.add_message(
                123, 1216, history_util.datetime.fromisoformat("2026-01-01T00:00:00")
            )
        )
        restored = asyncio.run(history_util.get_message_reactions(123, 1216))

        self.assertEqual(restored.results[0].reaction.emoticon, "⚡")
        self.assertEqual(restored.results[0].count, 1)

    def test_empty_unexpected_refresh_response_does_not_crash(self):
        message = SimpleNamespace(id=1209, reactions=None)
        client = _FakeClient(SimpleNamespace(updates=[]))

        asyncio.run(self.llm_chat._refresh_message_reactions(client, 123, [message]))

        self.assertIsNone(message.reactions)
        self.assertEqual(len(client.requests), 1)


if __name__ == "__main__":
    unittest.main()
