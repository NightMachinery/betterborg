import asyncio
import builtins
import contextlib
import importlib
import io
import unittest
from types import SimpleNamespace

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
        self.original_reaction_debug = self.llm_chat.REACTION_CACHE_DEBUG
        self.original_verbosity_mode = (
            self.llm_chat.REACTION_HISTORY_CACHE_VERBOSITY_MODE
        )
        self.llm_chat.IS_BOT = None
        self.llm_chat.REACTION_CACHE_DEBUG = False
        self.llm_chat.REACTION_HISTORY_CACHE_VERBOSITY_MODE = "silent"
        self.llm_chat.REACTION_CACHE_BY_CHAT_AND_MESSAGE.clear()
        self.llm_chat.REACTION_AGGREGATE_RESULTS_BY_CHAT_MESSAGE.clear()
        self.llm_chat.REACTION_ACTOR_STATE_BY_CHAT_MESSAGE_ACTOR.clear()

    def tearDown(self):
        self.llm_chat.IS_BOT = self.original_is_bot
        self.llm_chat.REACTION_CACHE_DEBUG = self.original_reaction_debug
        self.llm_chat.REACTION_HISTORY_CACHE_VERBOSITY_MODE = (
            self.original_verbosity_mode
        )
        self.llm_chat.REACTION_CACHE_BY_CHAT_AND_MESSAGE.clear()
        self.llm_chat.REACTION_AGGREGATE_RESULTS_BY_CHAT_MESSAGE.clear()
        self.llm_chat.REACTION_ACTOR_STATE_BY_CHAT_MESSAGE_ACTOR.clear()

    def test_refresh_populates_matching_message_reactions(self):
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

        self.assertIsNone(messages[0].reactions)
        self.assertIs(messages[1].reactions, reactions)
        self.assertEqual(client.requests[0].id, [1209, 1210])

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

    def test_bot_refresh_uses_cached_individual_reaction_update(self):
        update = UpdateBotMessageReaction(
            peer=PeerChannel(123),
            msg_id=1209,
            date=None,
            actor=PeerUser(456),
            old_reactions=[],
            new_reactions=[ReactionEmoji("👍")],
            qts=0,
        )
        chat_id = self.llm_chat._reaction_cache_key_from_peer(
            update.peer, update.msg_id
        )[0]
        message = SimpleNamespace(id=1209, reactions=None)
        client = _FakeClient()

        asyncio.run(
            self.llm_chat.reaction_update_handler(
                SimpleNamespace(original_update=update)
            )
        )
        self.llm_chat.IS_BOT = True
        asyncio.run(
            self.llm_chat._refresh_message_reactions(client, chat_id, [message])
        )

        self.assertEqual(client.requests, [])
        self.assertEqual(message.reactions.results[0].count, 1)
        self.assertEqual(message.reactions.results[0].reaction.emoticon, "👍")
        self.assertEqual(message.reactions.recent_reactions[0].peer_id.user_id, 456)

    def test_bot_refresh_uses_cached_aggregate_reactions_update(self):
        update = UpdateBotMessageReactions(
            peer=PeerChannel(123),
            msg_id=1210,
            date=None,
            reactions=[ReactionCount(reaction=ReactionEmoji("❤️"), count=3)],
            qts=0,
        )
        chat_id = self.llm_chat._reaction_cache_key_from_peer(
            update.peer, update.msg_id
        )[0]
        message = SimpleNamespace(id=1210, reactions=None)
        client = _FakeClient()

        asyncio.run(
            self.llm_chat.reaction_update_handler(
                SimpleNamespace(original_update=update)
            )
        )
        self.llm_chat.IS_BOT = True
        asyncio.run(
            self.llm_chat._refresh_message_reactions(client, chat_id, [message])
        )

        self.assertEqual(client.requests, [])
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
        chat_id = self.llm_chat._reaction_cache_key_from_peer(
            aggregate_update.peer, aggregate_update.msg_id
        )[0]
        message = SimpleNamespace(id=1211, reactions=None)
        client = _FakeClient()

        asyncio.run(
            self.llm_chat.reaction_update_handler(
                SimpleNamespace(original_update=aggregate_update)
            )
        )
        asyncio.run(
            self.llm_chat.reaction_update_handler(
                SimpleNamespace(original_update=actor_update)
            )
        )
        self.llm_chat.IS_BOT = True
        asyncio.run(
            self.llm_chat._refresh_message_reactions(client, chat_id, [message])
        )

        counts = {
            result.reaction.emoticon: result.count
            for result in message.reactions.results
        }
        self.assertEqual(counts, {"👍": 4, "❤️": 1})
        self.assertEqual(message.reactions.recent_reactions[0].peer_id.user_id, 456)

    def test_print_each_update_logs_individual_reaction_update(self):
        self.llm_chat.REACTION_CACHE_DEBUG = True
        self.llm_chat.REACTION_HISTORY_CACHE_VERBOSITY_MODE = "print_each_update"
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
            asyncio.run(
                self.llm_chat.reaction_update_handler(
                    SimpleNamespace(original_update=update)
                )
            )

        printed = out.getvalue()
        self.assertIn("LLM_Chat reaction_history_cache reaction_update_cached", printed)
        self.assertIn('"update_type": "UpdateBotMessageReaction"', printed)
        self.assertIn('"new_reactions": ["🤣"]', printed)
        self.assertIn('"cache_key": [-1000000000123, 1212]', printed)

    def test_print_each_update_logs_aggregate_reaction_update(self):
        self.llm_chat.REACTION_CACHE_DEBUG = True
        self.llm_chat.REACTION_HISTORY_CACHE_VERBOSITY_MODE = "print_each_update"
        update = UpdateBotMessageReactions(
            peer=PeerChannel(123),
            msg_id=1213,
            date=None,
            reactions=[ReactionCount(reaction=ReactionEmoji("❤️"), count=3)],
            qts=0,
        )
        out = io.StringIO()

        with contextlib.redirect_stdout(out):
            asyncio.run(
                self.llm_chat.reaction_update_handler(
                    SimpleNamespace(original_update=update)
                )
            )

        printed = out.getvalue()
        self.assertIn("LLM_Chat reaction_history_cache reaction_update_cached", printed)
        self.assertIn('"update_type": "UpdateBotMessageReactions"', printed)
        self.assertIn('"aggregate_update_reactions": [{"chosen_order": null, "count": 3, "reaction": "❤️"}]', printed)

    def test_print_each_update_logs_cache_application_to_history(self):
        self.llm_chat.REACTION_CACHE_DEBUG = True
        self.llm_chat.REACTION_HISTORY_CACHE_VERBOSITY_MODE = "print_each_update"
        reactions = _reaction_payload("👍", 2)
        message = SimpleNamespace(id=1214, reactions=None)
        self.llm_chat._cache_message_reactions(123, 1214, reactions)
        out = io.StringIO()

        with contextlib.redirect_stdout(out):
            applied = self.llm_chat._merge_reaction_cache_into_messages(
                123, [message]
            )

        self.assertEqual(applied, 1)
        self.assertIs(message.reactions, reactions)
        printed = out.getvalue()
        self.assertIn(
            "LLM_Chat reaction_history_cache cached_reactions_applied_to_history",
            printed,
        )
        self.assertIn('"applied": 1', printed)
        self.assertIn('"message_ids": [1214]', printed)

    def test_silent_verbosity_suppresses_reaction_cache_prints(self):
        self.llm_chat.REACTION_CACHE_DEBUG = True
        self.llm_chat.REACTION_HISTORY_CACHE_VERBOSITY_MODE = "silent"
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
            asyncio.run(
                self.llm_chat.reaction_update_handler(
                    SimpleNamespace(original_update=update)
                )
            )

        self.assertEqual(out.getvalue(), "")

    def test_empty_unexpected_refresh_response_does_not_crash(self):
        message = SimpleNamespace(id=1209, reactions=None)
        client = _FakeClient(SimpleNamespace(updates=[]))

        asyncio.run(self.llm_chat._refresh_message_reactions(client, 123, [message]))

        self.assertIsNone(message.reactions)
        self.assertEqual(len(client.requests), 1)


if __name__ == "__main__":
    unittest.main()
