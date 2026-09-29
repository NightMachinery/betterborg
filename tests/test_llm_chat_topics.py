"""Reply detection inside forum and private-chat topics.

Terms used below:

- A *topic root* is the `MessageActionTopicCreate` service message that
  opens a topic.
- A *plain* topic message is one the user sent without replying to anything.
  Telegram still gives it a reply header, pointing at the topic root.
- The shapes are the ones a canary bot observed live (Telethon 1.45, layer
  229; layer 224 carries the same `MessageReplyHeader` fields):
  - private chat: the root is message 322 in the bot's box, the topic id
    1241380 comes from the user's box, a plain message has
    `reply_to_msg_id=322, reply_to_top_id=1241380`, and an explicit reply has
    `reply_to_msg_id=<the parent>` with the same `reply_to_top_id`;
  - forum supergroup: a plain message has `reply_to_msg_id=<topic id>` and no
    `reply_to_top_id`, and an explicit reply has both.
  Every topic header has `forum_topic=True`.
"""

import asyncio
import builtins
import importlib
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telethon.tl.types import (
    Message,
    MessageActionTopicCreate,
    MessageReplyHeader,
    MessageService,
    PeerChannel,
    PeerUser,
)

from uniborg import topics


class _FakeLoop:
    def create_task(self, coro):
        coro.close()


class _FakeBorg:
    loop = _FakeLoop()


def _import_plugin():
    previous = getattr(builtins, "borg", None)
    builtins.borg = _FakeBorg()
    try:

        async def _import():
            return importlib.import_module("llm_chat_plugins.llm_chat")

        return asyncio.run(_import())
    finally:
        if previous is not None:
            builtins.borg = previous


plugin = _import_plugin()

USER_ID = 999000771
BOT_ID = 999000772
CHANNEL_ID = 999000773

#: Private chat: two topics, each root in the bot's box, each id from the user's.
ROOT_ID, TOPIC_ID = 322, 1241380
OTHER_ROOT_ID, OTHER_TOPIC_ID = 326, 1241384

#: Forum supergroup: the topic id is the root's own id.
FORUM_TOPIC_ID = 40


def _topic_root(msg_id=ROOT_ID, *, top_id=TOPIC_ID, peer=None):
    return MessageService(
        id=msg_id,
        peer_id=peer or PeerUser(USER_ID),
        action=MessageActionTopicCreate(title="Topic", icon_color=0),
        reply_to=MessageReplyHeader(forum_topic=True, reply_to_top_id=top_id),
    )


def _private(msg_id, *, parent=ROOT_ID, top_id=TOPIC_ID):
    """A private-chat topic message; PARENT=ROOT_ID makes it a plain one."""
    return Message(
        id=msg_id,
        peer_id=PeerUser(USER_ID),
        message="",
        reply_to=MessageReplyHeader(
            forum_topic=True, reply_to_msg_id=parent, reply_to_top_id=top_id
        ),
    )


def _forum(msg_id, *, parent=FORUM_TOPIC_ID, top_id=None):
    """A forum topic message; the default is a plain one."""
    return Message(
        id=msg_id,
        peer_id=PeerChannel(CHANNEL_ID),
        message="",
        reply_to=MessageReplyHeader(
            forum_topic=True, reply_to_msg_id=parent, reply_to_top_id=top_id
        ),
    )


def _outside_topics(msg_id, *, parent=None):
    return Message(
        id=msg_id,
        peer_id=PeerUser(USER_ID),
        message="",
        reply_to=(
            MessageReplyHeader(reply_to_msg_id=parent) if parent is not None else None
        ),
    )


class _Chat:
    """The messages of one chat, and a log of the ones loaded by id."""

    def __init__(self, *messages):
        self.by_id = {m.id: m for m in messages}
        self.fetched = []
        self.client = SimpleNamespace(get_messages=self.get_messages)

    async def fetch(self, msg_id):
        self.fetched.append(msg_id)
        return self.by_id.get(msg_id)

    async def get_messages(self, chat_id, *, ids):
        return await self.fetch(ids)


class _Event:
    """Forwards to MESSAGE the way Telethon's NewMessage event does."""

    def __init__(self, message, *, client, **overrides):
        self.message = message
        self.client = client
        self.__dict__.update(overrides)

    def __getattr__(self, name):
        return getattr(self.message, name)


def _resolve(message, chat, *, roots, context=None):
    return asyncio.run(
        plugin.resolve_reply_target(
            message,
            fetch_message=chat.fetch,
            context_messages_by_id=context,
            topic_roots=roots,
        )
    )


class ResolveReplyTargetOutsideTopicsTests(unittest.TestCase):
    def setUp(self):
        self.chat = _Chat()
        self.roots = plugin.TopicRootCache()

    def test_a_message_without_a_header_replies_to_nothing(self):
        self.assertIsNone(_resolve(_outside_topics(5), self.chat, roots=self.roots))

    def test_a_reply_resolves_to_its_parent_without_loading_it(self):
        target = _resolve(_outside_topics(5, parent=3), self.chat, roots=self.roots)
        self.assertEqual(target, plugin.ReplyTarget(msg_id=3))
        self.assertEqual(self.chat.fetched, [])

    def test_a_parent_in_the_context_comes_along(self):
        parent = _outside_topics(3)
        target = _resolve(
            _outside_topics(5, parent=3),
            self.chat,
            roots=self.roots,
            context={3: parent},
        )
        self.assertIs(target.message, parent)

    def test_a_topic_root_replies_to_nothing(self):
        self.assertIsNone(_resolve(_topic_root(), self.chat, roots=self.roots))


class ResolveReplyTargetPrivateTopicTests(unittest.TestCase):
    def setUp(self):
        self.chat = _Chat(
            _topic_root(), _topic_root(OTHER_ROOT_ID, top_id=OTHER_TOPIC_ID)
        )
        self.roots = plugin.TopicRootCache()

    def resolve(self, message, **kwargs):
        return _resolve(message, self.chat, roots=self.roots, **kwargs)

    def test_plain_messages_reply_to_nothing_and_load_the_root_once(self):
        self.assertIsNone(self.resolve(_private(330)))
        self.assertIsNone(self.resolve(_private(331)))
        self.assertEqual(self.chat.fetched, [ROOT_ID])
        self.assertEqual(self.roots.get(USER_ID, TOPIC_ID), ROOT_ID)

    def test_a_root_in_the_context_is_learned_without_loading(self):
        context = {ROOT_ID: self.chat.by_id[ROOT_ID]}
        self.assertIsNone(self.resolve(_private(330), context=context))
        self.assertEqual(self.chat.fetched, [])
        self.assertEqual(self.roots.get(USER_ID, TOPIC_ID), ROOT_ID)

    def test_an_explicit_reply_before_the_root_is_known_keeps_its_parent(self):
        parent = _private(329)
        self.chat.by_id[329] = parent
        target = self.resolve(_private(330, parent=329))
        self.assertEqual(target, plugin.ReplyTarget(msg_id=329, message=parent))
        #: The loaded parent comes along, so callers need not load it again.
        self.assertEqual(self.chat.fetched, [329])

    def test_an_explicit_reply_after_the_root_is_known_loads_nothing(self):
        self.roots.remember(USER_ID, TOPIC_ID, root_id=ROOT_ID)
        target = self.resolve(_private(330, parent=329))
        self.assertEqual(target, plugin.ReplyTarget(msg_id=329))
        self.assertEqual(self.chat.fetched, [])

    def test_each_topic_has_its_own_root(self):
        self.assertIsNone(self.resolve(_private(330)))
        self.assertIsNone(
            self.resolve(_private(331, parent=OTHER_ROOT_ID, top_id=OTHER_TOPIC_ID))
        )
        #: The first topic's root is not the second topic's.
        self.assertEqual(
            self.resolve(_private(332, parent=ROOT_ID, top_id=OTHER_TOPIC_ID)),
            plugin.ReplyTarget(msg_id=ROOT_ID),
        )
        self.assertEqual(self.chat.fetched, [ROOT_ID, OTHER_ROOT_ID])

    def test_roots_are_remembered_per_chat(self):
        self.roots.remember(USER_ID + 1, TOPIC_ID, root_id=ROOT_ID)
        self.assertIsNone(self.resolve(_private(330)))
        self.assertEqual(self.chat.fetched, [ROOT_ID])

    def test_a_parent_that_fails_to_load_counts_as_a_reply(self):
        async def fail(msg_id):
            raise ConnectionError("offline")

        target = asyncio.run(
            plugin.resolve_reply_target(
                _private(330), fetch_message=fail, topic_roots=self.roots
            )
        )
        self.assertEqual(target, plugin.ReplyTarget(msg_id=ROOT_ID))
        self.assertIsNone(self.roots.get(USER_ID, TOPIC_ID))

    def test_the_default_fetcher_is_the_message_client(self):
        message = _Event(_private(330), client=self.chat.client)
        target = asyncio.run(
            plugin.resolve_reply_target(message, topic_roots=self.roots)
        )
        self.assertIsNone(target)
        self.assertEqual(self.chat.fetched, [ROOT_ID])


class ResolveReplyTargetForumTests(unittest.TestCase):
    def setUp(self):
        self.chat = _Chat()
        self.roots = plugin.TopicRootCache()

    def test_a_plain_message_replies_to_nothing(self):
        self.assertIsNone(_resolve(_forum(50), self.chat, roots=self.roots))

    def test_an_explicit_reply_resolves_without_loading(self):
        target = _resolve(
            _forum(50, parent=45, top_id=FORUM_TOPIC_ID), self.chat, roots=self.roots
        )
        self.assertEqual(target, plugin.ReplyTarget(msg_id=45))
        self.assertEqual(self.chat.fetched, [])

    def test_a_reply_to_the_topic_itself_replies_to_nothing(self):
        message = _forum(50, parent=FORUM_TOPIC_ID, top_id=FORUM_TOPIC_ID)
        self.assertIsNone(_resolve(message, self.chat, roots=self.roots))


class TopicRootCacheTests(unittest.TestCase):
    def test_the_least_recently_stored_root_goes_first(self):
        roots = plugin.TopicRootCache(max_size=2)
        roots.remember(1, 10, root_id=100)
        roots.remember(1, 11, root_id=110)
        roots.remember(1, 10, root_id=100)
        roots.remember(1, 12, root_id=120)
        self.assertIsNone(roots.get(1, 11))
        self.assertEqual((roots.get(1, 10), roots.get(1, 12)), (100, 120))


class ReplyChainTests(unittest.TestCase):
    def chain(self, chat, message):
        event = _Event(message, client=chat.client, chat_id=USER_ID)
        return asyncio.run(
            plugin._get_initial_messages_for_reply_chain(
                event, topic_roots=plugin.TopicRootCache()
            )
        )

    def test_a_plain_topic_message_has_no_chain(self):
        chat = _Chat(_topic_root())
        self.assertEqual(self.chain(chat, _private(330)), [])

    def test_a_topic_chain_stops_below_the_root(self):
        #: 330 replies to the bot's 329, which replied to the plain 328.
        chat = _Chat(_topic_root(), _private(328), _private(329, parent=328))
        chain = self.chain(chat, _private(330, parent=329))
        self.assertEqual([m.id for m in chain], [328, 329])
        #: Each message is loaded once, the root only to be recognized.
        self.assertEqual(chat.fetched, [329, 328, ROOT_ID])

    def test_a_chain_outside_topics_loads_the_same_messages(self):
        chat = _Chat(_outside_topics(1), _outside_topics(2, parent=1))
        chain = self.chain(chat, _outside_topics(3, parent=2))
        self.assertEqual([m.id for m in chain], [1, 2])
        self.assertEqual(chat.fetched, [2, 1])


class ReplyQuoteTests(unittest.TestCase):
    def quote(self, message, *, context):
        return asyncio.run(
            plugin._build_reply_quote(
                message, context, topic_roots=plugin.TopicRootCache()
            )
        )

    def test_a_plain_topic_message_quotes_nothing(self):
        context = {ROOT_ID: _topic_root()}
        self.assertEqual(self.quote(_private(330), context=context), "")

    def test_an_explicit_topic_reply_quotes_its_parent(self):
        parent = SimpleNamespace(
            id=329,
            text="earlier answer",
            media=None,
            date=None,
            get_sender=AsyncMock(return_value=SimpleNamespace(username="bot")),
        )
        context = {ROOT_ID: _topic_root(), 329: parent}
        self.assertEqual(
            self.quote(_private(330, parent=329), context=context),
            "[Replying to @bot]:\n> earlier answer",
        )


class SmartModeTests(unittest.TestCase):
    """A reply switches smart mode to the reply chain; a plain topic message
    must not."""

    def run_smart(self, message, *, chat):
        event = _Event(
            message,
            client=chat.client,
            text="hello",
            forward=None,
            reply=AsyncMock(),
        )
        prefs = SimpleNamespace(context_mode="smart", group_context_mode="smart")
        state = {USER_ID: "until_separator"}
        with ExitStack() as stack:
            enter = stack.enter_context
            enter(patch.object(plugin, "override_chat_context_mode", {}))
            enter(patch.object(plugin, "SMART_CONTEXT_STATE", state))
            enter(patch.object(topics, "TOPIC_ROOTS", topics.TopicRootCache()))
            enter(
                patch.object(plugin.chat_manager, "get_context_mode", return_value=None)
            )
            enter(
                patch.object(
                    plugin.redis_util, "is_redis_available", return_value=False
                )
            )
            mode = asyncio.run(
                plugin._determine_context_mode_and_handle_transitions(
                    event,
                    prefs=prefs,
                    user_id=USER_ID,
                    is_private=True,
                    group_id=None,
                )
            )
        return mode, state[USER_ID]

    def test_a_plain_topic_message_keeps_until_separator(self):
        chat = _Chat(_topic_root())
        self.assertEqual(
            self.run_smart(_private(330), chat=chat),
            ("until_separator", "until_separator"),
        )

    def test_an_explicit_topic_reply_switches_to_the_reply_chain(self):
        chat = _Chat(_topic_root(), _private(329))
        self.assertEqual(
            self.run_smart(_private(330, parent=329), chat=chat),
            ("reply_chain", "reply_chain"),
        )


class GroupReplyActivationTests(unittest.TestCase):
    """`mention_and_reply` answers a reply to the bot, not a plain message in a
    topic the bot opened."""

    def is_valid(self, message):
        #: The bot opened the topic, so the topic root is the bot's message.
        get_reply_message = AsyncMock(return_value=SimpleNamespace(sender_id=BOT_ID))
        event = _Event(
            message,
            client=_Chat().client,
            text="no mention here",
            media=None,
            forward=None,
            out=False,
            is_private=False,
            sender_id=USER_ID,
            get_reply_message=get_reply_message,
        )
        prefs = SimpleNamespace(group_activation_mode="mention_and_reply")
        with ExitStack() as stack:
            enter = stack.enter_context
            enter(patch.object(plugin, "BOT_USERNAME", "@testbot"))
            enter(patch.object(plugin.user_manager, "get_prefs", return_value=prefs))
            enter(
                patch.object(
                    builtins, "borg", SimpleNamespace(me=SimpleNamespace(id=BOT_ID))
                )
            )
            valid = asyncio.run(plugin.is_valid_chat_message(event))
        return valid, get_reply_message.await_count

    def test_a_plain_message_in_the_bots_topic_is_ignored(self):
        self.assertEqual(self.is_valid(_forum(50)), (False, 0))

    def test_an_explicit_reply_to_the_bot_in_a_topic_is_answered(self):
        message = _forum(50, parent=45, top_id=FORUM_TOPIC_ID)
        self.assertEqual(self.is_valid(message), (True, 1))


if __name__ == "__main__":
    unittest.main()
