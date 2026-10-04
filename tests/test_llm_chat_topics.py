"""Reply detection and thread context inside forum and private-chat topics.

Terms used below:

- A *topic root* is the `MessageActionTopicCreate` service message that
  opens a topic.
- A *plain* topic message is one the user sent without replying to anything.
  Telegram still gives it a reply header, pointing at the topic root.
- *Thread context* is the default for a bot's message in a private topic: the
  topic's own recorded messages (`llm_chat.THREAD_CONTEXT_MODE`).
- A *topic context mode* is Topic Thread, Reply Chain or Until Separator.
  Its topic setting overrides the chat's default for topics.
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
from datetime import datetime, timedelta, timezone
import importlib
from pathlib import Path
import tempfile
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telethon.tl.types import (
    Document,
    DocumentAttributeFilename,
    Message,
    MessageActionTopicCreate,
    MessageFwdHeader,
    MessageMediaDocument,
    MessageReplyHeader,
    MessageService,
    PeerChannel,
    PeerUser,
)

from uniborg import history_util, tg_compat, topics
from uniborg.constants import TWIN_FILE_MARKER


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
    """The messages of one chat, and a log of the ones loaded by id.

    `fetched` logs single loads; `batches` logs the lists of ids loaded at
    once, as `build_conversation_history` loads the history.
    """

    def __init__(self, *messages):
        self.by_id = {m.id: m for m in messages}
        self.fetched = []
        self.batches = []
        self.client = SimpleNamespace(get_messages=self.get_messages)

    async def fetch(self, msg_id):
        self.fetched.append(msg_id)
        return self.by_id.get(msg_id)

    async def get_messages(self, chat_id, *, ids):
        if isinstance(ids, list):
            self.batches.append(list(ids))
            return [self.by_id.get(msg_id) for msg_id in ids]
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
            #: A bot uses thread context inside private topics instead
            #: (ThreadContextModeTests); a user account still switches.
            enter(patch.object(plugin, "IS_BOT", False))
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


T0 = datetime(2026, 9, 29, tzinfo=timezone.utc)


def _said(msg_id, text, *, top_id=TOPIC_ID, parent=ROOT_ID, bot=False):
    """A message saying TEXT in private topic TOP_ID, or outside topics for None.

    PARENT is what it replies to; the default makes a plain topic message.
    """
    header = None
    if top_id is not None:
        header = MessageReplyHeader(
            forum_topic=True, reply_to_msg_id=parent, reply_to_top_id=top_id
        )
    elif parent is not None:
        header = MessageReplyHeader(reply_to_msg_id=parent)
    message = Message(
        id=msg_id,
        peer_id=PeerUser(USER_ID),
        date=T0 + timedelta(seconds=msg_id),
        message=text,
        out=bot,
        reply_to=header,
    )
    #: Telethon renders `text` through its client's parse mode; without a
    #: client it is None. A client with no parse mode gives the raw text.
    message._client = SimpleNamespace(parse_mode=None)
    return message


#: A conversation in topic TOPIC_ID with no explicit replies, interleaved
#: with a message in another topic and one outside topics ("All").
CONVERSATION = (
    _said(330, "What is a monad?"),
    _said(331, "A way to chain computations.", parent=330, bot=True),
    _said(332, "unrelated", parent=OTHER_ROOT_ID, top_id=OTHER_TOPIC_ID),
    _said(333, "Send your API key.", top_id=None, parent=None, bot=True),
    _said(334, "Give an example."),
    _said(335, "Maybe is one.", parent=334, bot=True),
    _said(336, "And lists?"),
)
THREAD_IDS = [330, 331, 334, 335, 336]


class _BotChatCase(unittest.TestCase):
    """A bot's private chat with topics, and the history the bot recorded.

    The context built for a message is read off the message list
    `build_conversation_history` hands to `_process_turns_to_history`, which
    is where every mode's messages become the prompt.
    """

    def setUp(self):
        self.chat = _Chat(
            _topic_root(), _topic_root(OTHER_ROOT_ID, top_id=OTHER_TOPIC_ID)
        )
        self.prefs = plugin.UserPrefs()
        self.smart_state = {}
        self.chat_mode = None
        self.chat_topic_mode = None
        self.topics = plugin.TopicManager(storage=_MemoryStorage())
        self.info = AsyncMock()
        history_util._history_cache.clear()
        history_util._message_id_to_chat_id_map.clear()
        self.addCleanup(history_util._history_cache.clear)
        self.addCleanup(history_util._message_id_to_chat_id_map.clear)
        stack = ExitStack()
        self.addCleanup(stack.close)
        enter = stack.enter_context
        enter(patch.object(plugin, "IS_BOT", True))
        enter(patch.object(plugin, "topic_manager", self.topics))
        enter(
            patch.object(
                plugin, "chat_manager", plugin.ChatManager(storage=_MemoryStorage())
            )
        )
        enter(patch.object(plugin, "override_chat_context_mode", {}))
        enter(patch.object(plugin, "SMART_CONTEXT_STATE", self.smart_state))
        enter(patch.object(plugin, "send_info_message", new=self.info))
        enter(patch.object(plugin, "_refresh_message_reactions", new=AsyncMock()))
        enter(patch.object(topics, "TOPIC_ROOTS", topics.TopicRootCache()))
        enter(patch.object(plugin.redis_util, "is_redis_available", return_value=False))
        enter(patch.object(plugin.user_manager, "get_prefs", return_value=self.prefs))
        enter(
            patch.object(
                plugin.chat_manager,
                "get_topic_context_mode",
                side_effect=lambda chat_id: self.chat_topic_mode,
            )
        )
        enter(
            patch.object(
                plugin.chat_manager,
                "get_context_mode",
                side_effect=lambda chat_id: self.chat_mode,
            )
        )
        enter(
            patch.object(
                plugin.chat_manager, "get_last_n_messages_limit", return_value=None
            )
        )
        enter(
            patch.object(
                plugin.chat_manager, "get_include_reply_chain", return_value=None
            )
        )
        self.temp_dir = Path(enter(tempfile.TemporaryDirectory()))

    def topic_mode(self, mode, *, topic_id=TOPIC_ID):
        self.topics.set_context_mode(plugin.TopicManager.key(USER_ID, topic_id), mode)

    def see(self, *messages):
        """The bot receives or sends MESSAGES, and records them."""
        for message in messages:
            self.chat.by_id[message.id] = message
            asyncio.run(history_util.record_message(message))

    def event(self, message):
        return _Event(
            message,
            client=self.chat.client,
            chat_id=USER_ID,
            sender_id=USER_ID,
            is_private=True,
        )

    def mode_for(self, message):
        return asyncio.run(
            plugin._determine_context_mode_and_handle_transitions(
                self.event(message),
                prefs=self.prefs,
                user_id=USER_ID,
                is_private=True,
                group_id=None,
            )
        )

    def context_of(self, message, *, mode=None):
        """The mode used for MESSAGE, and the ids of its context, in order."""
        mode = mode or self.mode_for(message)
        processed = []

        async def capture(event, messages, *args, **kwargs):
            processed.extend(messages)
            return [], []

        with patch.object(plugin, "_process_turns_to_history", new=capture):
            asyncio.run(
                plugin.build_conversation_history(
                    self.event(message),
                    mode,
                    self.temp_dir,
                    {},
                    "api-key",
                    "model",
                    is_private=True,
                    include_system_prompt_p=False,
                )
            )
        return mode, [m.id for m in processed]


class ThreadContextTests(_BotChatCase):
    def setUp(self):
        super().setUp()
        self.see(*CONVERSATION)

    def test_a_conversation_without_replies_is_the_whole_thread(self):
        self.assertEqual(
            self.context_of(CONVERSATION[-1]),
            (plugin.THREAD_CONTEXT_MODE, THREAD_IDS),
        )

    def test_the_thread_is_loaded_in_one_batch(self):
        self.context_of(CONVERSATION[-1])
        self.assertEqual(self.chat.batches, [THREAD_IDS])
        #: The root, once, to tell the plain message from a reply.
        self.assertEqual(self.chat.fetched, [ROOT_ID])

    def test_the_other_topic_has_its_own_thread(self):
        later = _said(
            337, "still unrelated", parent=OTHER_ROOT_ID, top_id=OTHER_TOPIC_ID
        )
        self.see(later)
        self.assertEqual(self.context_of(later)[1], [332, 337])

    def test_the_topic_root_is_not_content(self):
        #: As if something had recorded the root under its topic.
        asyncio.run(history_util.add_message(USER_ID, ROOT_ID, T0, topic_id=TOPIC_ID))
        self.assertEqual(self.context_of(CONVERSATION[-1])[1], THREAD_IDS)

    def test_a_loaded_messages_own_header_has_the_last_word(self):
        stray = _said(337, "filed wrongly", parent=OTHER_ROOT_ID, top_id=OTHER_TOPIC_ID)
        self.chat.by_id[337] = stray
        asyncio.run(
            history_util.add_message(USER_ID, 337, stray.date, topic_id=TOPIC_ID)
        )
        self.assertEqual(self.context_of(CONVERSATION[-1])[1], THREAD_IDS)

    def test_the_topic_limit_caps_the_thread(self):
        self.prefs.thread_last_n_messages_limit = 3
        self.assertEqual(self.context_of(CONVERSATION[-1])[1], [334, 335, 336])

    def test_the_last_n_limit_does_not_cap_the_thread(self):
        self.prefs.last_n_messages_limit = 2
        self.assertEqual(self.context_of(CONVERSATION[-1])[1], THREAD_IDS)

    def test_the_topic_limit_defaults_to_its_own_default(self):
        self.assertEqual(
            plugin._get_effective_thread_last_n_limit(USER_ID),
            plugin.THREAD_LAST_N_MESSAGES_LIMIT,
        )
        self.assertEqual(plugin.THREAD_LAST_N_MESSAGES_LIMIT, 200)
        self.prefs.last_n_messages_limit = 50
        self.assertEqual(plugin._get_effective_thread_last_n_limit(USER_ID), 200)

    def test_an_explicit_reply_in_the_topic_still_gets_the_whole_thread(self):
        reply = _said(337, "Back to chaining?", parent=331)
        self.see(reply)
        self.assertEqual(self.context_of(reply)[1], THREAD_IDS + [337])

    def test_an_explicit_reply_beyond_the_cap_brings_its_chain(self):
        self.prefs.thread_last_n_messages_limit = 2
        reply = _said(337, "Back to chaining?", parent=331)
        self.see(reply)
        self.assertEqual(self.context_of(reply)[1], [330, 331, 336, 337])

    def test_a_separator_does_not_cut_the_thread(self):
        self.see(_said(337, "---"), _said(338, "New question."))
        self.assertEqual(self.context_of(self.chat.by_id[338])[1], THREAD_IDS + [338])

    def test_messages_recorded_before_topics_were_are_left_out(self):
        #: A legacy item: in the topic by its header, recorded without a topic.
        early = _said(329, "Hello")
        self.chat.by_id[329] = early
        asyncio.run(history_util.add_message(USER_ID, 329, early.date))
        self.assertEqual(self.context_of(CONVERSATION[-1])[1], THREAD_IDS)


class ThreadContextModeTests(_BotChatCase):
    def test_topic_modes_override_personal_and_chat_modes_without_switching_smart(self):
        self.prefs.context_mode = "smart"
        self.chat_mode = "smart"
        self.smart_state[USER_ID] = "until_separator"
        self.see(_said(330, "hi"))
        for mode, build_mode in (
            ("thread", plugin.THREAD_CONTEXT_MODE),
            ("reply_chain", "reply_chain"),
            ("until_separator", plugin.TOPIC_UNTIL_SEPARATOR_MODE),
        ):
            with self.subTest(mode=mode):
                self.topic_mode(mode)
                self.assertEqual(
                    self.mode_for(_said(331, "reply", parent=330)), build_mode
                )
                self.assertEqual(self.smart_state, {USER_ID: "until_separator"})
                self.info.assert_not_awaited()

    def test_the_chat_topic_default_applies_and_the_topic_wins(self):
        self.chat_topic_mode = "reply_chain"
        self.assertEqual(self.mode_for(_said(330, "hi")), "reply_chain")
        self.topic_mode("thread")
        self.assertEqual(self.mode_for(_said(330, "hi")), plugin.THREAD_CONTEXT_MODE)
        self.assertEqual(
            self.mode_for(
                _said(332, "other", top_id=OTHER_TOPIC_ID, parent=OTHER_ROOT_ID)
            ),
            "reply_chain",
        )

    def test_outside_topics_and_user_accounts_ignore_topic_modes(self):
        self.chat_topic_mode = "reply_chain"
        self.topic_mode("until_separator")
        self.prefs.context_mode = "last_N"
        self.assertEqual(
            self.mode_for(_said(330, "hi", top_id=None, parent=None)), "last_N"
        )
        with patch.object(plugin, "IS_BOT", False):
            self.assertEqual(self.mode_for(_said(330, "hi")), "last_N")

    def test_each_topic_mode_answers_a_separator_without_calling_the_model(self):
        for mode, reply in (
            ("thread", plugin.THREAD_SEPARATOR_REPLY),
            ("reply_chain", plugin.TOPIC_REPLY_CHAIN_SEPARATOR_REPLY),
            ("until_separator", plugin.TOPIC_CONTEXT_CLEARED_REPLY),
        ):
            with self.subTest(mode=mode):
                self.topic_mode(mode)
                self.info.reset_mock()
                self.assertIsNone(self.mode_for(_said(330, "  ---\n")))
                self.assertEqual(self.info.await_args.args[1], reply)

    def test_the_recent_override_still_wins_in_every_topic_mode(self):
        for mode in plugin.TOPIC_CONTEXT_MODES:
            with self.subTest(mode=mode):
                self.topic_mode(mode)
                with patch.dict(plugin.override_chat_context_mode, {USER_ID: "recent"}):
                    self.assertIsNone(self.mode_for(_said(330, "hi")))

    def test_every_context_mode_gives_way_in_a_topic(self):
        for mode in plugin.CONTEXT_MODES:
            with self.subTest(mode=mode):
                self.prefs.context_mode = mode
                self.assertEqual(
                    self.mode_for(_said(330, "hi")), plugin.THREAD_CONTEXT_MODE
                )

    def test_a_chat_setting_gives_way_too(self):
        self.chat_mode = "last_N"
        self.assertEqual(self.mode_for(_said(330, "hi")), plugin.THREAD_CONTEXT_MODE)

    def test_smart_mode_neither_switches_nor_applies(self):
        self.prefs.context_mode = "smart"
        self.smart_state[USER_ID] = "until_separator"
        self.see(_said(330, "hi"))
        reply = _said(331, "a real reply", parent=330)
        self.assertEqual(self.mode_for(reply), plugin.THREAD_CONTEXT_MODE)
        self.assertEqual(self.smart_state, {USER_ID: "until_separator"})
        self.info.assert_not_awaited()

    def test_a_separator_in_a_topic_is_explained_and_not_answered(self):
        self.prefs.context_mode = "smart"
        self.smart_state[USER_ID] = "reply_chain"
        self.assertIsNone(self.mode_for(_said(330, "---")))
        self.assertEqual(self.info.await_args.args[1], plugin.THREAD_SEPARATOR_REPLY)
        self.assertEqual(self.smart_state, {USER_ID: "reply_chain"})

    def test_outside_topics_the_context_mode_applies(self):
        for mode in ("reply_chain", "last_N", "until_separator"):
            with self.subTest(mode=mode):
                self.prefs.context_mode = mode
                outside = _said(330, "hi", top_id=None, parent=None)
                self.assertEqual(self.mode_for(outside), mode)

    def test_a_user_account_keeps_its_context_mode_in_a_topic(self):
        self.prefs.context_mode = "last_N"
        with patch.object(plugin, "IS_BOT", False):
            self.assertEqual(self.mode_for(_said(330, "hi")), "last_N")


class TopicReplyChainTests(_BotChatCase):
    def setUp(self):
        super().setUp()
        self.see(*CONVERSATION)
        self.topic_mode("reply_chain")

    def test_a_plain_message_gets_only_itself(self):
        self.assertEqual(self.context_of(CONVERSATION[-1]), ("reply_chain", [336]))
        self.assertEqual(self.chat.batches, [])
        self.assertEqual(self.chat.fetched, [ROOT_ID])

    def test_an_explicit_reply_gets_its_chain_not_the_thread(self):
        reply = _said(337, "Back?", parent=331)
        self.see(reply)
        self.assertEqual(self.context_of(reply), ("reply_chain", [330, 331, 337]))

    def test_the_topic_limit_does_not_cap_the_reply_chain(self):
        self.prefs.thread_last_n_messages_limit = 1
        self.test_an_explicit_reply_gets_its_chain_not_the_thread()

    def test_an_explicit_chain_leaving_the_topic_is_followed(self):
        reply = _said(337, "Other topic?", parent=332)
        self.see(reply)
        self.assertEqual(self.context_of(reply)[1], [332, 337])


class TopicUntilSeparatorTests(_BotChatCase):
    def setUp(self):
        super().setUp()
        self.topic_mode("until_separator")

    def test_without_a_separator_it_is_the_whole_thread(self):
        self.see(*CONVERSATION)
        self.assertEqual(
            self.context_of(CONVERSATION[-1]),
            (plugin.TOPIC_UNTIL_SEPARATOR_MODE, THREAD_IDS),
        )

    def test_it_starts_after_the_latest_separator_and_drops_service_messages(self):
        self.see(
            *CONVERSATION,
            _said(337, "---"),
            _said(338, "first"),
            _said(339, " --- "),
            _said(340, "last"),
        )
        asyncio.run(history_util.add_message(USER_ID, ROOT_ID, T0, topic_id=TOPIC_ID))
        self.assertEqual(self.context_of(self.chat.by_id[340])[1], [340])

    def test_separators_elsewhere_or_misfiled_do_not_cut_it(self):
        self.see(
            *CONVERSATION,
            _said(337, "---", top_id=OTHER_TOPIC_ID, parent=OTHER_ROOT_ID),
            _said(338, "---", top_id=None, parent=None),
            _said(339, "next"),
        )
        asyncio.run(
            history_util.add_message(
                USER_ID, 337, self.chat.by_id[337].date, topic_id=TOPIC_ID
            )
        )
        self.assertEqual(self.context_of(self.chat.by_id[339])[1], THREAD_IDS + [339])

    def test_cap_and_cut_keep_the_same_suffix(self):
        self.see(
            *CONVERSATION, _said(337, "---"), _said(338, "first"), _said(339, "second")
        )
        for limit in (2, 3):
            with self.subTest(limit=limit):
                self.prefs.thread_last_n_messages_limit = limit
                self.assertEqual(self.context_of(self.chat.by_id[339])[1], [338, 339])

    def test_a_separator_as_the_first_message_starts_fresh(self):
        separator, question = _said(330, "---"), _said(331, "hi")
        self.see(separator, question)
        self.assertIsNone(self.mode_for(separator))
        self.assertEqual(
            self.info.await_args.args[1], plugin.TOPIC_CONTEXT_CLEARED_REPLY
        )
        self.assertEqual(self.context_of(question)[1], [331])

    def test_include_reply_chain_can_restore_pre_separator_messages(self):
        reply = _said(338, "About Maybe", parent=335)
        self.see(*CONVERSATION, _said(337, "---"), reply)
        self.assertEqual(self.context_of(reply)[1], [334, 335, 338])
        with patch.object(
            plugin.chat_manager, "get_include_reply_chain", return_value=False
        ):
            self.assertEqual(self.context_of(reply)[1], [338])

    def test_earlier_separators_count_after_switching_and_deleting_reopens_context(
        self,
    ):
        self.topic_mode("thread")
        self.see(*CONVERSATION, _said(337, "---"), _said(338, "new"))
        self.assertEqual(self.context_of(self.chat.by_id[338])[1], THREAD_IDS + [338])
        self.topic_mode("until_separator")
        self.assertEqual(self.context_of(self.chat.by_id[338])[1], [338])
        asyncio.run(history_util.mark_as_deleted(USER_ID, [337]))
        self.assertEqual(self.context_of(self.chat.by_id[338])[1], THREAD_IDS + [338])


class OutsideTopicsContextTests(_BotChatCase):
    """Outside topics, each mode builds the context it built before."""

    def setUp(self):
        super().setUp()
        self.see(*CONVERSATION)
        self.chain = (
            _said(340, "one", top_id=None, parent=None),
            _said(341, "two", top_id=None, parent=340, bot=True),
            _said(342, "three", top_id=None, parent=341),
        )
        self.see(*self.chain)

    def test_the_reply_chain_is_unchanged(self):
        self.assertEqual(
            self.context_of(self.chain[-1]), ("reply_chain", [340, 341, 342])
        )
        #: Loaded one by one up the chain, and no history is read.
        self.assertEqual(self.chat.fetched, [341, 340])
        self.assertEqual(self.chat.batches, [])

    def test_last_n_still_spans_the_whole_chat(self):
        self.prefs.context_mode = "last_N"
        everything = sorted(m.id for m in CONVERSATION + self.chain)
        self.assertEqual(self.context_of(self.chain[-1]), ("last_N", everything))

    def test_thread_context_needs_a_topic(self):
        with self.assertRaises(ValueError):
            self.context_of(self.chain[-1], mode=plugin.THREAD_CONTEXT_MODE)

    def test_topic_until_separator_needs_a_topic(self):
        with self.assertRaises(ValueError):
            self.context_of(self.chain[-1], mode=plugin.TOPIC_UNTIL_SEPARATOR_MODE)


def _file(msg_id, caption, *, top_id=None, parent=None, bot=True, forwarded=False):
    """A `.md` file with CAPTION, sent by the bot unless BOT is False."""
    message = _said(msg_id, caption, top_id=top_id, parent=parent, bot=bot)
    message.media = MessageMediaDocument(
        document=Document(
            id=msg_id,
            access_hash=1,
            file_reference=b"",
            date=T0,
            mime_type="text/markdown",
            size=3,
            dc_id=2,
            attributes=[DocumentAttributeFilename("answer.md")],
        )
    )
    if forwarded:
        message.fwd_from = MessageFwdHeader(date=T0)
    return message


def _twin(msg_id, **kwargs):
    return _file(msg_id, f"{TWIN_FILE_MARKER}**Answer**", **kwargs)


class TwinFileContextTests(_BotChatCase):
    """Window modes skip the bot's twin files; Reply Chain keeps them."""

    def setUp(self):
        super().setUp()
        #: A question, the answer's text head, its twin, then a follow-up.
        self.see(
            _said(340, "Explain monads at length.", top_id=None, parent=None),
            _said(341, "Monads are ...", top_id=None, parent=340, bot=True),
            _twin(342, parent=340),
        )

    def after(self, message, *, mode):
        self.see(message)
        self.prefs.context_mode = mode
        return self.context_of(message)

    def test_last_n_and_until_separator_skip_the_twin(self):
        follow_up = _said(343, "And in Haskell?", top_id=None, parent=None)
        for mode in ("last_N", "until_separator"):
            with self.subTest(mode=mode):
                self.assertEqual(
                    self.after(follow_up, mode=mode), (mode, [340, 341, 343])
                )

    def test_the_reply_chain_keeps_the_twin(self):
        reply = _said(343, "Why?", top_id=None, parent=342)
        self.assertEqual(
            self.after(reply, mode="reply_chain"), ("reply_chain", [340, 342, 343])
        )

    def test_a_twin_the_chain_reaches_comes_back_into_the_window(self):
        reply = _said(343, "Why?", top_id=None, parent=342)
        self.assertEqual(
            self.after(reply, mode="last_N"), ("last_N", [340, 341, 342, 343])
        )

    def test_a_twin_only_the_chain_reaches_is_kept(self):
        self.see(_said(343, plugin.CONTEXT_SEPARATOR, top_id=None, parent=None))
        reply = _said(344, "Back to this:", top_id=None, parent=342)
        self.assertEqual(
            self.after(reply, mode="until_separator"),
            ("until_separator", [340, 342, 344]),
        )

    def test_forwarded_and_unmarked_files_are_kept(self):
        self.see(
            _twin(343, forwarded=True),
            _file(344, f"{TWIN_FILE_MARKER}**Answer**", bot=False, forwarded=True),
            _file(345, "**A file-only answer**"),
        )
        follow_up = _said(346, "Thanks", top_id=None, parent=None)
        self.assertEqual(
            self.after(follow_up, mode="last_N"),
            ("last_N", [340, 341, 343, 344, 345, 346]),
        )

    def test_a_topic_thread_skips_the_twin(self):
        self.see(
            _said(350, "Explain monads at length."),
            _said(351, "Monads are ...", parent=350, bot=True),
            _twin(352, top_id=TOPIC_ID, parent=350),
        )
        follow_up = _said(353, "And in Haskell?")
        self.see(follow_up)
        self.assertEqual(
            self.context_of(follow_up, mode=plugin.THREAD_CONTEXT_MODE)[1],
            [350, 351, 353],
        )

    def test_a_topic_until_separator_skips_the_twin(self):
        self.see(
            _said(350, "question"),
            _said(351, "answer", parent=350, bot=True),
            _twin(352, top_id=TOPIC_ID, parent=350),
            _said(353, "next"),
        )
        self.topic_mode("until_separator")
        self.assertEqual(self.context_of(self.chat.by_id[353])[1], [350, 351, 353])

    def test_our_id_identifies_twins_that_carry_a_sender(self):
        twin = _twin(343)
        twin.from_id = PeerUser(BOT_ID)
        foreign = _file(344, f"{TWIN_FILE_MARKER}**Answer**", bot=False)
        foreign.from_id = PeerUser(USER_ID)
        self.see(twin, foreign)
        follow_up = _said(345, "Thanks", top_id=None, parent=None)
        with patch.object(plugin, "BOT_ID", BOT_ID):
            self.assertEqual(
                self.after(follow_up, mode="last_N"),
                ("last_N", [340, 341, 344, 345]),
            )

    def test_the_export_skips_the_twin_too(self):
        follow_up = _said(343, "/asfile", top_id=None, parent=None)
        self.see(follow_up)
        self.prefs.context_mode = "last_N"
        processed = []

        async def capture(event, messages, *args, **kwargs):
            processed.extend(m.id for m in messages)
            return [], []

        with patch.object(plugin, "_process_turns_to_history", new=capture):
            asyncio.run(
                plugin.build_conversation_history_for_export(
                    self.event(follow_up),
                    "last_N",
                    is_private=True,
                    include_system_prompt_p=False,
                )
            )
        self.assertEqual(processed, [340, 341, 343])


class _NoAction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class AsFileTests(_BotChatCase):
    """/asfile exports the topic's thread, and sends it into that topic."""

    def setUp(self):
        super().setUp()
        self.see(*CONVERSATION)
        self.sent_file = SimpleNamespace(id=900)
        self.send = AsyncMock(side_effect=lambda **kwargs: self.sent_file)
        self.error = AsyncMock()
        self.warnings = []
        self.exported = []

        async def capture(event, messages, *args, **kwargs):
            self.exported.extend(m.id for m in messages)
            return [], list(self.warnings)

        stack = ExitStack()
        self.addCleanup(stack.close)
        enter = stack.enter_context
        enter(patch.object(plugin.util, "send_as_file_with_filename", self.send))
        enter(patch.object(plugin.llm_util, "handle_llm_error", self.error))
        enter(patch.object(plugin, "_process_turns_to_history", new=capture))
        enter(
            patch.object(
                builtins,
                "borg",
                SimpleNamespace(action=lambda *args, **kwargs: _NoAction()),
            )
        )

    def export(self, message):
        self.see(message)
        event = _Event(
            message,
            client=self.chat.client,
            chat_id=USER_ID,
            sender_id=USER_ID,
            is_private=True,
            chat=None,
            grouped_id=None,
            respond=AsyncMock(),
        )
        asyncio.run(plugin.as_file_handler(event))
        self.error.assert_not_awaited()
        return event

    def test_in_a_topic_the_file_replies_to_the_command(self):
        event = self.export(_said(337, "/asfile"))

        self.assertIs(self.send.await_args.kwargs["reply_to"], event.message)
        self.assertEqual(self.exported, THREAD_IDS + [337])

    def test_a_reply_chain_topic_exports_the_commands_explicit_chain(self):
        self.topic_mode("reply_chain")
        self.export(_said(337, "/asfile", parent=331))
        self.assertEqual(self.exported, [330, 331, 337])

    def test_a_plain_export_in_a_reply_chain_topic_has_only_the_command(self):
        self.topic_mode("reply_chain")
        self.export(_said(337, ".."))
        self.assertEqual(self.exported, [337])

    def test_an_until_separator_topic_exports_after_its_separator(self):
        self.topic_mode("until_separator")
        self.see(_said(337, "---"))
        self.export(_said(338, "/asfile"))
        self.assertEqual(self.exported, [338])

    def test_outside_topics_the_file_is_sent_as_before(self):
        self.prefs.context_mode = "last_N"
        self.export(_said(337, "/asfile", top_id=None, parent=None))

        self.assertIsNone(self.send.await_args.kwargs["reply_to"])
        self.assertEqual(self.exported, sorted(m.id for m in CONVERSATION) + [337])

    def test_warnings_follow_the_file_or_else_stay_in_the_topic(self):
        self.warnings = ["a warning"]
        event = self.export(_said(337, "/asfile"))
        self.assertIs(event.respond.await_args.kwargs["reply_to"], self.sent_file)

        self.sent_file = None
        event = self.export(_said(338, ".."))
        self.assertIs(event.respond.await_args.kwargs["reply_to"], event.message)


class ThreadStatusTests(_BotChatCase):
    IN_TOPIC_STATUS = (
        "∙ **Current Mode:** `Topic Thread (Limit: 200)`\n"
        "∙ **Source:** Using the default inside private topics.\n"
        "∙ **Outside Topics:** `Reply Chain`, from your **personal default** "
        "for private chats."
    )
    OUTSIDE_STATUS = (
        "∙ **Current Mode:** `Reply Chain`\n"
        "∙ **Source:** This is using your **personal default** for private chats."
    )

    def status_text(self, event):
        return asyncio.run(plugin._get_context_mode_status_text(event))

    def press(self, *, placement, message):
        """A press on a button of MESSAGE, as Telethon's CallbackQuery gives it."""
        return SimpleNamespace(
            is_private=True,
            chat_id=USER_ID,
            sender_id=USER_ID,
            message_id=message.id,
            client=SimpleNamespace(topic_placement=placement),
            get_message=AsyncMock(return_value=message),
        )

    def test_in_a_topic_the_status_names_the_thread(self):
        self.assertEqual(
            self.status_text(self.event(_said(330, "/contextModeHere"))),
            self.IN_TOPIC_STATUS,
        )

    def test_the_status_names_the_topic_mode_and_its_layer(self):
        self.topic_mode("reply_chain")
        status = self.status_text(self.event(_said(330, "/getContextModeHere")))
        self.assertIn("`Reply Chain`", status)
        self.assertIn("Using this topic's own setting.", status)
        self.topic_mode(None)
        self.chat_topic_mode = "until_separator"
        status = self.status_text(self.event(_said(330, "/getContextModeHere")))
        self.assertIn("`Until Separator (Limit: 200)`", status)
        self.assertIn("Using this chat's default for topics.", status)

    def test_outside_topics_the_status_is_unchanged(self):
        outside = _said(330, "/contextModeHere", top_id=None, parent=None)
        self.assertEqual(self.status_text(self.event(outside)), self.OUTSIDE_STATUS)

    def test_a_press_in_a_topic_is_placed_by_the_registry(self):
        placement = topics.TopicPlacement()
        menu = _said(331, "menu", parent=330, bot=True)
        placement.registry.record(USER_ID, menu.id, topic_id=TOPIC_ID)
        press = self.press(placement=placement, message=menu)
        self.assertEqual(self.status_text(press), self.IN_TOPIC_STATUS)
        press.get_message.assert_not_awaited()

    def test_a_press_the_registry_has_not_seen_loads_its_message_once(self):
        placement = topics.TopicPlacement()
        menu = _said(331, "menu", parent=330, bot=True)
        press = self.press(placement=placement, message=menu)
        self.assertEqual(self.status_text(press), self.IN_TOPIC_STATUS)
        self.assertEqual(placement.registry.get(USER_ID, menu.id), TOPIC_ID)
        self.assertEqual(self.status_text(press), self.IN_TOPIC_STATUS)
        press.get_message.assert_awaited_once()

    def test_a_press_outside_topics_is_unchanged(self):
        menu = _said(331, "menu", top_id=None, parent=330, bot=True)
        press = self.press(placement=None, message=menu)
        self.assertEqual(self.status_text(press), self.OUTSIDE_STATUS)

    def menu_text(self, message, *, kind="private"):
        menu = asyncio.run(
            plugin._personal_context_mode_menu(self.event(message), kind)
        )
        return menu.text

    def menu(self, message, *, kind="private"):
        return asyncio.run(
            plugin._personal_context_mode_menu(self.event(message), kind)
        )

    def test_the_private_menu_notes_the_thread_only_in_a_topic(self):
        in_topic = self.menu_text(_said(330, "/contextMode"))
        outside = self.menu_text(_said(330, "/contextMode", top_id=None, parent=None))
        self.assertIn(plugin.THREAD_CONTEXT_MENU_NOTE, in_topic)
        self.assertEqual(
            outside,
            f"{plugin.BOT_META_INFO_PREFIX}**Set Private Chat Context Mode**\n\n"
            f"{plugin._format_personal_last_n_menu_text(USER_ID, USER_ID)}",
        )
        self.assertEqual(
            in_topic.replace(f"{plugin.THREAD_CONTEXT_MENU_NOTE}\n\n", "").replace(
                f"\n\n{plugin._format_thread_last_n_menu_text(USER_ID)}", ""
            ),
            outside,
        )

    def test_the_private_menu_offers_the_topic_limit_only_in_a_topic(self):
        def labels(menu):
            return [button.text for row in menu.buttons for button in row]

        in_topic = labels(self.menu(_said(330, "/contextMode")))
        outside = labels(
            self.menu(_said(330, "/contextMode", top_id=None, parent=None))
        )
        topic_picks = [label for label in in_topic if "Topic N" in label]
        self.assertIn("✅ Topic N: 200", topic_picks)
        self.assertEqual(len(topic_picks), len(plugin.LAST_N_QUICK_PICK_LIMITS))
        self.assertEqual(
            [label for label in in_topic if label not in topic_picks], outside
        )

    def test_the_group_menu_has_no_note(self):
        text = self.menu_text(_said(330, "/groupContextMode"), kind="group")
        self.assertNotIn(plugin.THREAD_CONTEXT_MENU_NOTE, text)

    def status_message(self, message):
        with ExitStack() as stack:
            enter = stack.enter_context
            enter(
                patch.object(
                    plugin.chat_manager, "get_prefs", return_value=plugin.ChatPrefs()
                )
            )
            enter(patch.object(plugin.chat_manager, "get_model", return_value=None))
            enter(
                patch.object(
                    plugin.user_manager, "get_codex_quota_fallback", return_value=None
                )
            )
            enter(
                patch.object(
                    plugin, "_can_user_access_model", new=AsyncMock(return_value=True)
                )
            )
            enter(patch.object(plugin.llm_chat_config, "load_config"))
            asyncio.run(plugin.status_handler(self.event(message)))
        return self.info.await_args.args[1]

    def test_status_shows_the_thread_only_in_a_topic(self):
        line = "• **In This Topic:** `Topic Thread (Limit: 200)`"
        in_topic = self.status_message(_said(330, "/status"))
        outside = self.status_message(_said(330, "/status", top_id=None, parent=None))
        self.assertIn(line, in_topic)
        self.assertNotIn("In This Topic", outside)
        without_line = "".join(
            part for part in in_topic.splitlines(True) if "In This Topic" not in part
        )
        self.assertEqual(without_line, outside)


class ThreadLimitCommandTests(_BotChatCase):
    def setUp(self):
        super().setUp()
        save = patch.object(plugin.user_manager, "_save_prefs")
        self.save = save.start()
        self.addCleanup(save.stop)

    def command(self, handler, argument=None):
        event = SimpleNamespace(
            sender_id=USER_ID,
            pattern_match=SimpleNamespace(group=lambda i: argument),
            reply=AsyncMock(),
        )
        asyncio.run(handler(event))
        return event.reply.await_args.args[0]

    def test_set_get_and_reset(self):
        self.command(plugin.set_thread_last_n_handler, "300")
        self.assertEqual(self.prefs.thread_last_n_messages_limit, 300)
        self.assertIsNone(self.prefs.last_n_messages_limit)
        self.assertIn("300", self.command(plugin.get_thread_last_n_handler))
        self.command(plugin.set_thread_last_n_handler, "reset")
        self.assertIsNone(self.prefs.thread_last_n_messages_limit)
        self.assertIn("200", self.command(plugin.get_thread_last_n_handler))

    def test_bad_limits_are_refused(self):
        for argument in ("1", "abc", str(plugin.LAST_N_MAX + 1)):
            with self.subTest(argument=argument):
                reply = self.command(plugin.set_thread_last_n_handler, argument)
                self.assertIn("valid number", reply)
                self.assertIsNone(self.prefs.thread_last_n_messages_limit)
        self.assertIn("Usage", self.command(plugin.set_thread_last_n_handler))

    def test_the_commands_are_registered_with_telegram(self):
        commands = {c["command"] for c in plugin.BOT_COMMANDS}
        self.assertLessEqual({"setthreadlastn", "getthreadlastn"}, commands)
        descriptions = {c["command"]: c["description"] for c in plugin.BOT_COMMANDS}
        for name in ("contextmodehere", "getcontextmodehere"):
            self.assertIn("topic", descriptions[name])


class _MemoryStorage:
    """`UserStorage`'s get and set, in memory."""

    def __init__(self):
        self.data = {}

    def get(self, key):
        return dict(self.data.get(key, {}))

    def set(self, key, value):
        self.data[key] = dict(value)
        return True


class _TopicSettingsCase(unittest.TestCase):
    """A bot's private chat with topics, and fresh chat and topic stores."""

    CHAT_MODEL = "gemini/gemini-flash-latest"
    TOPIC_MODEL = plugin.OPENAI_CODEX_LUNA_RESERVE
    PERSONAL_MODEL = "gemini/gemini-flash-lite-latest"

    def setUp(self):
        self.prefs = plugin.UserPrefs(model=self.PERSONAL_MODEL)
        self.chats = plugin.ChatManager(storage=_MemoryStorage())
        self.topics = plugin.TopicManager(storage=_MemoryStorage())
        stack = ExitStack()
        self.addCleanup(stack.close)
        enter = stack.enter_context
        enter(patch.object(plugin, "IS_BOT", True))
        enter(patch.object(plugin, "chat_manager", self.chats))
        enter(patch.object(plugin, "topic_manager", self.topics))
        enter(patch.object(plugin.user_manager, "get_prefs", return_value=self.prefs))
        enter(patch.object(plugin.user_manager, "_save_prefs"))

    def key(self, topic_id=TOPIC_ID):
        return plugin.TopicManager.key(USER_ID, topic_id)

    def event(self, *, top_id=TOPIC_ID):
        parent = ROOT_ID if top_id is not None else None
        return _Event(
            _said(330, "hi", top_id=top_id, parent=parent),
            client=None,
            chat_id=USER_ID,
            sender_id=USER_ID,
            is_private=True,
        )


class TopicSettingsResolutionTests(_TopicSettingsCase):
    """prefix > topic > chat > personal > default, for each first-cut setting."""

    def model(self, topic_id=TOPIC_ID, **kwargs):
        return plugin._get_effective_model_and_service(
            USER_ID, USER_ID, topic_id=topic_id, **kwargs
        )[0]

    def test_the_topic_model_beats_the_chat_model_only_in_its_topic(self):
        self.chats.set_model(USER_ID, self.CHAT_MODEL)
        self.topics.set_model(self.key(), self.TOPIC_MODEL)

        self.assertEqual(self.model(), self.TOPIC_MODEL)
        self.assertEqual(self.model(OTHER_TOPIC_ID), self.CHAT_MODEL)
        self.assertEqual(self.model(None), self.CHAT_MODEL)
        self.assertEqual(self.model(prefix_model="x/y"), "x/y")

    def test_an_unset_topic_falls_through_to_the_personal_model(self):
        self.assertEqual(self.model(), self.PERSONAL_MODEL)

    def test_the_topic_effort_beats_the_chat_effort(self):
        model = self.TOPIC_MODEL
        self.chats.set_thinking(USER_ID, model=model, level="low")
        self.topics.set_thinking(self.key(), model=model, level="high")

        def resolve(topic_id, **kwargs):
            reasoning = plugin._get_effective_reasoning(
                USER_ID, USER_ID, model=model, topic_id=topic_id, **kwargs
            )
            return reasoning.level, reasoning.source

        self.assertEqual(resolve(TOPIC_ID), ("high", "topic"))
        self.assertEqual(resolve(OTHER_TOPIC_ID), ("low", "chat"))
        self.assertEqual(resolve(TOPIC_ID, prefix_effort="max"), ("max", "prefix"))
        self.assertEqual(plugin.REASONING_SOURCE_NAMES["topic"], "this topic")

    def test_a_topic_effort_the_model_does_not_accept_is_skipped(self):
        self.topics.set_thinking(self.key(), model=self.TOPIC_MODEL, level="disable")

        reasoning = plugin._get_effective_reasoning(
            USER_ID, USER_ID, model=self.TOPIC_MODEL, topic_id=TOPIC_ID
        )

        self.assertNotEqual(reasoning.source, "topic")

    def test_the_topic_prompt_beats_the_chat_prompt(self):
        self.chats.set_system_prompt(USER_ID, "chat prompt")
        self.topics.set_system_prompt(self.key(), "topic prompt")

        inside = plugin.get_system_prompt_info(self.event())
        outside = plugin.get_system_prompt_info(self.event(top_id=None))

        self.assertEqual(
            (inside.source, inside.topic_prompt, inside.chat_prompt),
            ("topic", "topic prompt", "chat prompt"),
        )
        self.assertTrue(inside.effective_prompt.startswith("topic prompt"))
        self.assertEqual((outside.source, outside.topic_prompt), ("chat", None))

    def test_a_topic_codex_model_is_a_saved_model_for_the_quota_fallback(self):
        self.topics.set_model(self.key(), plugin.OPENAI_CODEX_SOL)
        stand_in = plugin.CodexQuotaFallback(
            model=self.CHAT_MODEL, until=T0 + timedelta(days=1)
        )
        with patch.object(
            plugin.user_manager, "get_codex_quota_fallback", return_value=stand_in
        ):
            request = plugin._resolve_request_model(USER_ID, USER_ID, topic_id=TOPIC_ID)

        self.assertEqual(request.model, self.CHAT_MODEL)
        self.assertEqual(request.quota_fallback_from, plugin.OPENAI_CODEX_SOL)

    def test_a_topic_model_choice_ends_the_quota_fallback(self):
        with patch.object(
            plugin.user_manager, "clear_codex_quota_fallback"
        ) as clear_fallback:
            plugin._apply_topic_model_choice(
                USER_ID, TOPIC_ID, user_id=USER_ID, model=self.TOPIC_MODEL
            )

        self.assertEqual(self.topics.get_model(self.key()), self.TOPIC_MODEL)
        clear_fallback.assert_called_once_with(USER_ID)

    def test_topics_are_stored_apart_from_chats(self):
        self.topics.set_model(self.key(), self.TOPIC_MODEL)

        self.assertEqual(
            self.topics.storage.data,
            {f"{USER_ID}:{TOPIC_ID}": {"model": self.TOPIC_MODEL}},
        )
        self.assertEqual(self.chats.storage.data, {})
        self.assertIsNone(self.chats.get_model(USER_ID))


class TopicContextModeResolutionTests(_TopicSettingsCase):
    def choice(self, *, topic_id=TOPIC_ID):
        return plugin._get_effective_topic_context_mode(USER_ID, topic_id=topic_id)

    def test_topic_over_chat_topic_default_over_thread(self):
        self.assertEqual(
            self.choice(),
            plugin.TopicContextModeChoice(mode="thread", source="default"),
        )
        self.chats.set_topic_context_mode(USER_ID, "reply_chain")
        self.assertEqual(
            self.choice(),
            plugin.TopicContextModeChoice(mode="reply_chain", source="chat"),
        )
        self.topics.set_context_mode(self.key(), "thread")
        self.assertEqual(
            self.choice(), plugin.TopicContextModeChoice(mode="thread", source="topic")
        )
        self.assertEqual(self.choice(topic_id=OTHER_TOPIC_ID).mode, "reply_chain")

    def test_outside_modes_do_not_reach_topics(self):
        self.chats.set_context_mode(USER_ID, "until_separator")
        self.prefs.context_mode = "smart"
        self.assertEqual(self.choice().mode, "thread")

    def test_invalid_stored_modes_are_skipped_without_losing_other_settings(self):
        self.topics.storage.data[self.key()] = {
            "context_mode": "smart",
            "system_prompt": "keep me",
        }
        self.chats.set_topic_context_mode(USER_ID, "until_separator")
        with patch.object(plugin, "logger", create=True) as log:
            self.assertEqual(
                self.choice(),
                plugin.TopicContextModeChoice(mode="until_separator", source="chat"),
            )
            log.warning.assert_called_once()
        self.chats.storage.data[USER_ID] = {"topic_context_mode": "last_N"}
        with patch.object(plugin, "logger", create=True) as log:
            self.assertEqual(self.choice().mode, "thread")
            self.assertEqual(log.warning.call_count, 2)
        self.assertEqual(self.topics.get_system_prompt(self.key()), "keep me")

    def test_setters_validate_and_clear_both_stores(self):
        for mode in ("last_N", "smart", "topic_until_separator", "nope"):
            with self.subTest(mode=mode):
                with self.assertRaises(ValueError):
                    self.topics.set_context_mode(self.key(), mode)
                with self.assertRaises(ValueError):
                    self.chats.set_topic_context_mode(USER_ID, mode)
        self.assertEqual(self.topics.storage.data, {})
        self.assertEqual(self.chats.storage.data, {})
        for mode in plugin.TOPIC_CONTEXT_MODES:
            self.topics.set_context_mode(self.key(), mode)
            self.chats.set_topic_context_mode(USER_ID, mode)
            self.assertEqual(self.topics.get_context_mode(self.key()), mode)
            self.assertEqual(self.chats.get_topic_context_mode(USER_ID), mode)
        self.topics.set_context_mode(self.key(), None)
        self.chats.set_topic_context_mode(USER_ID, None)
        self.assertEqual(self.topics.storage.data[self.key()], {})
        self.assertEqual(self.chats.storage.data[USER_ID], {})

    def test_storage_shape_and_reload_keep_outside_mode_separate(self):
        self.topics.set_context_mode(self.key(), "reply_chain")
        self.chats.set_topic_context_mode(USER_ID, "until_separator")
        self.assertEqual(
            self.topics.storage.data, {self.key(): {"context_mode": "reply_chain"}}
        )
        self.assertEqual(
            self.chats.storage.data,
            {USER_ID: {"topic_context_mode": "until_separator"}},
        )
        reloaded = plugin.TopicManager(storage=self.topics.storage)
        self.assertEqual(reloaded.get_context_mode(self.key()), "reply_chain")
        self.chats.set_context_mode(USER_ID, "last_N")
        reloaded_chat = plugin.ChatManager(storage=self.chats.storage)
        self.assertEqual(
            reloaded_chat.get_topic_context_mode(USER_ID), "until_separator"
        )
        self.assertEqual(reloaded_chat.get_context_mode(USER_ID), "last_N")


MENU_ID = 700


def _button_rows(rows):
    """Each row of a keyboard as (label, data) pairs."""
    return [
        [
            (tg_compat.button_text(button), tg_compat.button_data_text(button))
            for button in row
        ]
        for row in rows
    ]


class TopicSettingsMenuTests(_TopicSettingsCase):
    """The "Here" commands and menus write the topic they are sent in, and
    their Apply-to row switches them to the whole chat."""

    LEVEL_MODEL = "gemini/gemini-flash-latest"

    def setUp(self):
        super().setUp()
        self.pending = {}
        stack = ExitStack()
        self.addCleanup(stack.close)
        enter = stack.enter_context
        enter(patch.object(plugin, "AWAITING_INPUT_FROM_USERS", self.pending))
        enter(patch.object(plugin.util, "isAdmin", new=AsyncMock(return_value=False)))
        enter(
            patch.object(
                plugin.util, "is_group_admin", new=AsyncMock(return_value=False)
            )
        )
        enter(patch.object(plugin.llm_chat_config, "load_config", return_value=None))
        enter(
            patch.object(
                plugin.llm_chat_config,
                "can_use_codex",
                new=AsyncMock(return_value=False),
            )
        )
        enter(
            patch.object(
                plugin, "_can_user_access_model", new=AsyncMock(return_value=True)
            )
        )
        enter(
            patch.object(
                plugin, "_guard_model_access", new=AsyncMock(return_value=True)
            )
        )
        enter(patch.object(plugin.user_manager, "clear_codex_quota_fallback"))
        self.info = enter(patch.object(plugin, "send_info_message", new=AsyncMock()))
        self.edits = AsyncMock()

    def command(self, handler, text, *, argument=None, top_id=TOPIC_ID, msg_id=330):
        parent = ROOT_ID if top_id is not None else None
        event = _Event(
            _said(msg_id, text, top_id=top_id, parent=parent),
            client=SimpleNamespace(edit_message=self.edits),
            chat_id=USER_ID,
            sender_id=USER_ID,
            is_private=True,
            pattern_match=SimpleNamespace(group=lambda _index: argument),
            reply=AsyncMock(return_value=SimpleNamespace(id=MENU_ID)),
        )
        asyncio.run(handler(event))
        return event

    def press(self, data, *, top_id=TOPIC_ID):
        """A press on menu MENU_ID, which sits in topic TOP_ID (None: outside
        topics, as if the topic could not be told)."""
        parent = ROOT_ID if top_id is not None else None
        menu = _said(MENU_ID, "menu", top_id=top_id, parent=parent, bot=True)
        press = SimpleNamespace(
            data=data.encode(),
            is_private=True,
            chat_id=USER_ID,
            sender_id=USER_ID,
            message_id=MENU_ID,
            client=SimpleNamespace(topic_placement=None),
            get_message=AsyncMock(return_value=menu),
            answer=AsyncMock(),
            edit=AsyncMock(),
        )
        asyncio.run(plugin.callback_handler(press))
        return press

    def typed(self, text):
        event = _Event(
            _said(340, text),
            client=SimpleNamespace(edit_message=self.edits),
            chat_id=USER_ID,
            sender_id=USER_ID,
            is_private=True,
            text=text,
            reply=AsyncMock(),
        )
        asyncio.run(plugin.generic_input_handler(event))
        return event

    def assert_apply_to_row(self, rows, kind, *, scope):
        topic_label = "📍 This Topic"
        chat_label = "💬 Whole Chat"
        if scope == plugin.REASONING_SCOPE_TOPIC:
            topic_label = f"✅ {topic_label}"
        else:
            chat_label = f"✅ {chat_label}"
        self.assertIn(
            [
                (topic_label, f"applyto:{kind}:topic"),
                (chat_label, f"applyto:{kind}:chat"),
            ],
            _button_rows(rows),
        )

    # Model

    def test_in_a_topic_the_model_menu_writes_the_topic(self):
        event = self.command(plugin.set_model_here_handler, "/setModelHere")

        ((text,), kwargs) = event.reply.await_args
        self.assertIn("Set Model for This Topic", text)
        rows = kwargs["buttons"]
        self.assert_apply_to_row(rows, "model", scope=plugin.REASONING_SCOPE_TOPIC)
        self.assertEqual(_button_rows(rows)[-1], [("❌ Cancel", "mm:cancel:topic")])
        flow = self.pending[USER_ID]
        self.assertEqual((flow["type"], flow["chat_id"]), ("topicmodel", USER_ID))

        self.press(
            f"topicmodel_{plugin.bot_util.sanitize_callback_data(self.LEVEL_MODEL)}"
        )

        self.assertEqual(self.topics.get_model(self.key()), self.LEVEL_MODEL)
        self.assertIsNone(self.chats.get_model(USER_ID))

    def test_outside_topics_the_model_menu_is_unchanged(self):
        event = self.command(
            plugin.set_model_here_handler, "/setModelHere", top_id=None
        )

        ((text,), kwargs) = event.reply.await_args
        self.assertIn("Set Model for the Whole Chat", text)
        self.assertNotIn("applyto:", str(_button_rows(kwargs["buttons"])))
        self.assertEqual(
            self.pending[USER_ID],
            {
                "type": "chatmodel",
                "chat_id": USER_ID,
                plugin.INPUT_MENU_KEY: plugin.InputMenu(USER_ID, MENU_ID),
            },
        )

    def test_apply_to_moves_the_menu_and_its_custom_id_to_the_chat(self):
        self.command(plugin.set_model_here_handler, "/setModelHere")

        press = self.press("applyto:model:chat")

        ((text,), kwargs) = press.edit.await_args
        self.assertIn("Set Model for the Whole Chat", text)
        self.assert_apply_to_row(
            kwargs["buttons"], "model", scope=plugin.REASONING_SCOPE_CHAT
        )
        self.assertEqual(self.pending[USER_ID]["type"], "chatmodel")
        self.assertEqual(self.topics.storage.data, {})

        self.typed("custom/model")

        self.assertEqual(self.chats.get_model(USER_ID), "custom/model")
        self.assertIsNone(self.topics.get_model(self.key()))

    def test_a_chat_press_in_a_topic_keeps_the_apply_to_row(self):
        press = self.press(
            f"chatmodel_{plugin.bot_util.sanitize_callback_data(self.CHAT_MODEL)}"
        )

        self.assertEqual(self.chats.get_model(USER_ID), self.CHAT_MODEL)
        self.assert_apply_to_row(
            press.edit.await_args.kwargs["buttons"],
            "model",
            scope=plugin.REASONING_SCOPE_CHAT,
        )

    def test_a_topic_press_whose_topic_is_unknown_writes_nothing(self):
        for data in (
            f"topicmodel_{plugin.bot_util.sanitize_callback_data(self.TOPIC_MODEL)}",
            "thinktopic_high",
            "applyto:model:chat",
            "prompthere:clear:chat",
            "topicctx:topic:reply_chain",
            "topicctx:chat:reply_chain",
            "topicctx:topic:rc",
            "applyto:context:chat",
        ):
            with self.subTest(data=data):
                press = self.press(data, top_id=None)

                press.answer.assert_awaited_once_with(
                    plugin.MENU_TOPIC_UNKNOWN, alert=True
                )
                press.edit.assert_not_awaited()
        self.assertEqual(self.topics.storage.data, {})
        self.assertEqual(self.chats.storage.data, {})

    def test_typed_model_ids_and_resets_go_to_the_topic(self):
        self.command(plugin.set_model_here_handler, "/setModelHere")
        self.typed("custom/model")
        self.assertEqual(self.topics.get_model(self.key()), "custom/model")

        self.command(plugin.set_model_here_handler, "/setModelHere")
        self.typed("not set")
        self.assertIsNone(self.topics.get_model(self.key()))
        self.assertEqual(self.chats.storage.data, {})

    def test_the_argument_form_sets_the_topic_and_points_to_the_chat(self):
        event = self.command(
            plugin.set_model_here_handler, "/setModelHere x/y", argument="x/y"
        )

        self.assertEqual(self.topics.get_model(self.key()), "x/y")
        self.assertIsNone(self.chats.get_model(USER_ID))
        self.assertIn("💬 Whole Chat", event.reply.await_args.args[0])

    def test_get_model_here_names_the_layer(self):
        def said():
            event = self.command(plugin.get_model_here_handler, "/getModelHere")
            return event.reply.await_args.args[0]

        self.chats.set_model(USER_ID, self.CHAT_MODEL)
        self.assertIn(f"Effective model in this topic:** `{self.CHAT_MODEL}`", said())
        self.assertIn("Source: the whole-chat default.", said())
        self.assertIn("Topic override: Not set (inherit).", said())
        self.topics.set_model(self.key(), self.TOPIC_MODEL)
        self.assertIn(f"Effective model in this topic:** `{self.TOPIC_MODEL}`", said())
        self.assertIn("Source: this topic's saved override.", said())
        self.assertIn(f"Whole-chat default: `{self.CHAT_MODEL}`", said())

    def test_model_and_effort_checkmarks_are_distinct_settings(self):
        model = plugin.OPENAI_CODEX_SOL
        self.chats.set_model(USER_ID, model)
        menu = plugin._build_model_menu(
            USER_ID,
            USER_ID,
            scope=plugin.REASONING_SCOPE_CHAT,
            admin_p=False,
            codex_p=True,
        )
        rows = plugin._model_menu_rows(menu, scope=plugin.REASONING_SCOPE_CHAT)
        selected = [
            (
                label,
                "chatmodel_"
                + plugin.bot_util.unsanitize_callback_data(data[len("chatmodel_") :]),
            )
            for row in _button_rows(rows)
            for label, data in row
            if label.startswith("✅ ")
        ]
        self.assertEqual(len(selected), 2)
        self.assertIn(
            (f"✅ {plugin._model_display_name(model)}", f"chatmodel_{model}"), selected
        )
        self.assertIn(
            ("✅ 🧠 Effort: Use Personal Default", "chatmodel_think:clear"), selected
        )
        effort_labels = [
            label
            for row in _button_rows(rows)
            for label, data in row
            if data.startswith("chatmodel_")
            and plugin.bot_util.unsanitize_callback_data(
                data[len("chatmodel_") :]
            ).startswith("think:")
        ]
        self.assertGreater(len(effort_labels), 1)
        self.assertTrue(all("Effort:" in label for label in effort_labels))
        text = plugin._model_menu_text(scope=plugin.REASONING_SCOPE_CHAT)
        self.assertIn("each has its own checkmark", text)
        self.assertIn("whole-chat default", text)

    def test_unset_model_is_distinct_from_unset_effort(self):
        self.prefs.model = plugin.OPENAI_CODEX_SOL
        for scope, topic_id, inherited in (
            (plugin.REASONING_SCOPE_CHAT, None, "Personal Default"),
            (plugin.REASONING_SCOPE_TOPIC, TOPIC_ID, "Chat/Personal Default"),
        ):
            with self.subTest(scope=scope):
                menu = plugin._build_model_menu(
                    USER_ID,
                    USER_ID,
                    scope=scope,
                    topic_id=topic_id,
                    admin_p=False,
                    codex_p=True,
                )
                selected = [
                    (label, data)
                    for row in _button_rows(plugin._model_menu_rows(menu, scope=scope))
                    for label, data in row
                    if label.startswith("✅ ")
                ]
                self.assertEqual(len(selected), 2)
                self.assertEqual(
                    {label for label, _ in selected},
                    {f"✅ Model: Use {inherited}", f"✅ 🧠 Effort: Use {inherited}"},
                )

    def test_get_model_here_reports_personal_inheritance_and_whole_chat_scope(self):
        inside = self.command(plugin.get_model_here_handler, "/getModelHere")
        self.assertIn("Source: your personal default.", inside.reply.await_args.args[0])
        self.chats.set_model(USER_ID, self.CHAT_MODEL)
        self.topics.set_model(self.key(), self.TOPIC_MODEL)
        outside = self.command(
            plugin.get_model_here_handler, "/getModelHere", top_id=None
        )
        text = outside.reply.await_args.args[0]
        self.assertIn(
            f"Effective model for the whole chat:** `{self.CHAT_MODEL}`", text
        )
        self.assertIn("Source: the whole-chat default.", text)
        self.assertNotIn("Topic override:", text)

    def test_get_model_here_keeps_saved_layers_visible_when_access_is_unavailable(self):
        self.topics.set_model(self.key(), self.TOPIC_MODEL)
        with patch.object(
            plugin, "_can_user_access_model", new=AsyncMock(return_value=False)
        ):
            event = self.command(plugin.get_model_here_handler, "/getModelHere")
        text = event.reply.await_args.args[0]
        self.assertIn(
            f"Effective model in this topic:** `{plugin.DEFAULT_MODEL}`", text
        )
        self.assertIn("access to the saved model is unavailable", text)
        self.assertIn(f"Topic override: `{self.TOPIC_MODEL}`", text)
        self.assertEqual(self.topics.get_model(self.key()), self.TOPIC_MODEL)

    def test_last_n_here_in_a_topic_explains_the_outside_topic_scope(self):
        for argument in (None, "20", "reset"):
            with self.subTest(argument=argument):
                event = self.command(
                    plugin.set_last_n_here_handler, "/setLastNHere", argument=argument
                )
                self.assertIn(
                    "Scope: outside-topic context for the whole chat",
                    event.reply.await_args.args[0],
                )
        for value in (None, 20):
            self.chats.set_last_n_messages_limit(USER_ID, value)
            inside = self.command(plugin.get_last_n_here_handler, "/getLastNHere")
            self.assertIn("/getThreadLastN", inside.reply.await_args.args[0])
            outside = self.command(
                plugin.get_last_n_here_handler, "/getLastNHere", top_id=None
            )
            self.assertNotIn("/getThreadLastN", outside.reply.await_args.args[0])

    # Reasoning effort

    def test_in_a_topic_the_effort_menu_writes_the_topic(self):
        self.topics.set_model(self.key(), self.LEVEL_MODEL)

        event = self.command(plugin.set_think_here_handler, "/setThinkHere")

        ((text,), kwargs) = event.reply.await_args
        self.assertIn("(This Topic)", text)
        self.assert_apply_to_row(
            kwargs["buttons"], "think", scope=plugin.REASONING_SCOPE_TOPIC
        )

        press = self.press("thinktopic_high")

        self.assertEqual(
            self.topics.get_thinking(self.key(), model=self.LEVEL_MODEL), "high"
        )
        self.assertIsNone(self.chats.get_thinking(USER_ID, model=self.LEVEL_MODEL))
        self.assertIn(
            ("✅ High", "thinktopic_high"),
            sum(_button_rows(press.edit.await_args.kwargs["buttons"]), []),
        )

    def test_the_effort_menu_moved_to_the_chat_sets_the_chats_model(self):
        self.chats.set_model(USER_ID, self.LEVEL_MODEL)
        self.topics.set_model(self.key(), plugin.OPENAI_CODEX_SOL)

        press = self.press("applyto:think:chat")

        ((text,), kwargs) = press.edit.await_args
        self.assertIn("(Whole Chat)", text)
        self.assert_apply_to_row(
            kwargs["buttons"], "think", scope=plugin.REASONING_SCOPE_CHAT
        )
        self.press("thinkhere_low")
        self.assertEqual(
            self.chats.get_thinking(USER_ID, model=self.LEVEL_MODEL), "low"
        )
        self.assertEqual(
            self.topics.storage.data[self.key()].get("thinking_by_model"), None
        )

    def test_the_effort_argument_form_sets_the_topic(self):
        self.topics.set_model(self.key(), self.LEVEL_MODEL)

        event = self.command(
            plugin.set_think_here_handler, "/setThinkHere low", argument="low"
        )

        self.assertEqual(
            self.topics.get_thinking(self.key(), model=self.LEVEL_MODEL), "low"
        )
        self.assertIn("in this topic", event.reply.await_args.args[0])

    def test_outside_topics_the_effort_menu_is_unchanged(self):
        with patch.object(plugin.bot_util, "present_options", new=AsyncMock()) as menu:
            self.command(plugin.set_think_here_handler, "/setThinkHere", top_id=None)

        self.assertEqual(menu.await_args.kwargs["callback_prefix"], "thinkhere_")

    # System prompt

    def test_the_prompt_menu_takes_the_topic_prompt_as_the_next_message(self):
        event = self.command(
            plugin.set_system_prompt_here_handler, "/setSystemPromptHere"
        )

        ((text,), kwargs) = event.reply.await_args
        self.assertIn("System Prompt for This Topic", text)
        self.assertIn("Not set", text)
        self.assertEqual(
            _button_rows(kwargs["buttons"]),
            [
                [
                    ("✅ 📍 This Topic", "applyto:prompt:topic"),
                    ("💬 Whole Chat", "applyto:prompt:chat"),
                ],
                [
                    ("♻️ Clear", "prompthere:clear:topic"),
                    ("❌ Cancel", "prompthere:cancel:topic"),
                ],
            ],
        )

        self.typed("Answer in French.")

        self.assertEqual(self.topics.get_system_prompt(self.key()), "Answer in French.")
        self.assertIsNone(self.chats.get_system_prompt(USER_ID))
        self.assertNotIn(USER_ID, self.pending)
        args, kwargs = self.edits.await_args
        self.assertEqual(args[:2], (USER_ID, MENU_ID))
        self.assertIn("Updated.", args[2])
        self.assertIn("Answer in French.", args[2])
        self.assertIsNone(kwargs["buttons"])

    def test_the_prompt_menu_moved_to_the_chat_writes_the_chat(self):
        self.command(plugin.set_system_prompt_here_handler, "/setSystemPromptHere")

        press = self.press("applyto:prompt:chat")

        self.assertIn("System Prompt for This Chat", press.edit.await_args.args[0])
        self.assertEqual(self.pending[USER_ID]["scope"], plugin.REASONING_SCOPE_CHAT)
        self.typed("Be brief.")
        self.assertEqual(self.chats.get_system_prompt(USER_ID), "Be brief.")
        self.assertIsNone(self.topics.get_system_prompt(self.key()))

    def test_clear_and_cancel_close_the_prompt_menu(self):
        self.topics.set_system_prompt(self.key(), "old")
        self.chats.set_system_prompt(USER_ID, "chat")
        self.command(plugin.set_system_prompt_here_handler, "/setSystemPromptHere")

        press = self.press("prompthere:clear:topic")

        self.assertIsNone(self.topics.get_system_prompt(self.key()))
        self.assertEqual(self.chats.get_system_prompt(USER_ID), "chat")
        self.assertNotIn(USER_ID, self.pending)
        self.assertIn("Cleared.", press.edit.await_args.args[0])
        self.assertIsNone(press.edit.await_args.kwargs["buttons"])

        self.command(plugin.set_system_prompt_here_handler, "/setSystemPromptHere")
        press = self.press("prompthere:cancel:topic")
        self.assertNotIn(USER_ID, self.pending)
        self.assertIn("Cancelled.", press.edit.await_args.args[0])

    def test_prompt_commands_with_text_set_and_reset_the_topic(self):
        self.chats.set_system_prompt(USER_ID, "chat")
        self.command(
            plugin.set_system_prompt_here_handler,
            "/setSystemPromptHere Be terse.",
            argument="Be terse.",
        )
        self.assertEqual(self.topics.get_system_prompt(self.key()), "Be terse.")

        get = self.command(
            plugin.get_system_prompt_here_handler, "/getSystemPromptHere"
        )
        self.assertIn("System prompt in this topic", get.reply.await_args.args[0])

        self.command(plugin.reset_system_prompt_here_handler, "/resetSystemPromptHere")
        self.assertIsNone(self.topics.get_system_prompt(self.key()))
        self.assertEqual(self.chats.get_system_prompt(USER_ID), "chat")

        get = self.command(
            plugin.get_system_prompt_here_handler, "/getSystemPromptHere"
        )
        self.assertIn(
            "This topic has no custom system prompt set. Using the whole-chat default prompt",
            get.reply.await_args.args[0],
        )

    def test_outside_topics_the_prompt_commands_are_unchanged(self):
        self.command(
            plugin.set_system_prompt_here_handler, "/setSystemPromptHere", top_id=None
        )
        self.assertIn("Usage", self.info.await_args.args[1])
        self.assertEqual(self.pending, {})

        self.command(
            plugin.set_system_prompt_here_handler,
            "/setSystemPromptHere Be terse.",
            argument="Be terse.",
            top_id=None,
        )
        self.assertEqual(self.chats.get_system_prompt(USER_ID), "Be terse.")
        self.assertEqual(self.topics.storage.data, {})

    # Context mode

    def test_context_mode_here_offers_topic_modes_and_apply_to(self):
        event = self.command(plugin.context_mode_here_handler, "/contextModeHere")
        self.assertIn(
            "**Set Context Mode for This Topic**", event.reply.await_args.args[0]
        )
        rows = event.reply.await_args.kwargs["buttons"]
        self.assertEqual(
            _button_rows(rows)[:-1],
            [
                [("Topic Thread (Limit: 200)", "topicctx:topic:thread")],
                [("Reply Chain", "topicctx:topic:reply_chain")],
                [("Until Separator (Limit: 200)", "topicctx:topic:until_separator")],
                [("✅ Not Set for This Topic", "topicctx:topic:not_set")],
                [("✅ Include Reply Chain: ON", "topicctx:topic:rc")],
            ],
        )
        self.assert_apply_to_row(rows, "context", scope="topic")

    def test_a_context_press_writes_only_the_topic_and_clear_inherits(self):
        press = self.press("topicctx:topic:reply_chain")
        self.assertEqual(self.topics.get_context_mode(self.key()), "reply_chain")
        self.assertEqual(self.chats.storage.data, {})
        press.answer.assert_awaited_once_with(plugin.TOPIC_CONTEXT_FEEDBACK["topic"])
        self.assertIn(
            [("✅ Reply Chain", "topicctx:topic:reply_chain")],
            _button_rows(press.edit.await_args.kwargs["buttons"]),
        )
        self.press("topicctx:topic:not_set")
        self.assertIsNone(self.topics.get_context_mode(self.key()))

    def test_apply_to_context_redraws_without_writing(self):
        press = self.press("applyto:context:chat")
        self.assertIn("All Topics of This Chat", press.edit.await_args.args[0])
        self.assert_apply_to_row(
            press.edit.await_args.kwargs["buttons"], "context", scope="chat"
        )
        self.assertEqual(self.chats.storage.data, {})
        self.assertEqual(self.topics.storage.data, {})
        press.answer.assert_awaited_once_with(plugin.APPLY_TO_CONTEXT_FEEDBACK["chat"])

    def test_chat_scope_sets_only_the_topic_default_and_thread_resets_it(self):
        self.chats.set_context_mode(USER_ID, "last_N")
        self.topics.set_context_mode(self.key(), "reply_chain")
        press = self.press("topicctx:chat:until_separator")
        self.assertEqual(self.chats.get_topic_context_mode(USER_ID), "until_separator")
        self.assertEqual(self.chats.get_context_mode(USER_ID), "last_N")
        self.assertEqual(self.topics.get_context_mode(self.key()), "reply_chain")
        self.assertIn("Current Mode:** `Reply Chain`", press.edit.await_args.args[0])
        self.assertIn(
            [("✅ Until Separator (Limit: 200)", "topicctx:chat:until_separator")],
            _button_rows(press.edit.await_args.kwargs["buttons"]),
        )
        self.press("topicctx:chat:thread")
        self.assertIsNone(self.chats.get_topic_context_mode(USER_ID))
        self.assertEqual(self.chats.get_context_mode(USER_ID), "last_N")

    def test_context_reply_chain_toggle_is_chat_wide_and_keeps_the_menu(self):
        press = self.press("topicctx:topic:rc")
        self.assertFalse(self.chats.get_include_reply_chain(USER_ID))
        self.assertIn("Set Context Mode for This Topic", press.edit.await_args.args[0])
        self.assertEqual(self.topics.storage.data, {})

    def test_invalid_context_presses_write_nothing(self):
        for data in (
            "topicctx:topic:last_N",
            "topicctx:topic:smart",
            "topicctx:nowhere:thread",
            "topicctx:chat:reply_chain:extra",
        ):
            with self.subTest(data=data):
                press = self.press(data)
                press.answer.assert_awaited_once_with(
                    "This menu is invalid.", alert=True
                )
                press.edit.assert_not_awaited()
        self.assertEqual(self.chats.storage.data, {})
        self.assertEqual(self.topics.storage.data, {})

    def test_stale_chat_context_menus_inside_topics_are_refused(self):
        for data in ("contexthere_last_N", "replychainhere_toggle", "lastnhere_50"):
            with self.subTest(data=data):
                press = self.press(data)
                press.answer.assert_awaited_once_with(
                    "This menu is outdated. Send /contextModeHere again.", alert=True
                )
                press.edit.assert_not_awaited()
        self.assertEqual(self.chats.storage.data, {})

    def test_outside_topics_context_menu_and_presses_stay_chat_wide(self):
        event = self.command(
            plugin.context_mode_here_handler, "/contextModeHere", top_id=None
        )
        self.assertTrue(
            event.reply.await_args.args[0].endswith(
                "**Set Context Mode for This Chat**"
            )
        )
        rows = _button_rows(event.reply.await_args.kwargs["buttons"])
        data = [value for row in rows for _, value in row]
        self.assertIn("contexthere_last_N", data)
        self.assertIn("replychainhere_toggle", data)
        self.assertIn("lastnhere_50", data)
        self.assertFalse(
            any(value.startswith(("applyto:", "topicctx:")) for value in data)
        )
        self.press("contexthere_last_N", top_id=None)
        self.press("lastnhere_50", top_id=None)
        self.press("replychainhere_toggle", top_id=None)
        self.assertEqual(self.chats.get_context_mode(USER_ID), "last_N")
        self.assertEqual(self.chats.get_last_n_messages_limit(USER_ID), 50)
        self.assertFalse(self.chats.get_include_reply_chain(USER_ID))
        self.assertIsNone(self.chats.get_topic_context_mode(USER_ID))
        self.assertEqual(self.topics.storage.data, {})

    def test_get_context_mode_here_heading_depends_on_topic(self):
        for top_id, title in ((TOPIC_ID, "Topic"), (None, "Chat")):
            with self.subTest(top_id=top_id):
                event = self.command(
                    plugin.get_context_mode_here_handler,
                    "/getContextModeHere",
                    top_id=top_id,
                )
                self.assertIn(
                    f"**{title} Context Mode Status**", event.reply.await_args.args[0]
                )

    def test_status_shows_the_topics_context_mode_and_source(self):
        self.topics.set_context_mode(self.key(), "reply_chain")
        with patch.object(
            plugin.user_manager, "get_codex_quota_fallback", return_value=None
        ):
            self.command(plugin.status_handler, "/status")
        self.assertIn(
            "• **In This Topic:** `Reply Chain`, from this topic's own setting",
            self.info.await_args.args[1],
        )

    # Status

    def test_status_shows_the_topics_own_settings(self):
        self.topics.set_model(self.key(), self.TOPIC_MODEL)
        self.topics.set_system_prompt(self.key(), "topic prompt")
        with patch.object(
            plugin.user_manager, "get_codex_quota_fallback", return_value=None
        ):
            self.command(plugin.status_handler, "/status")

        status = self.info.await_args.args[1]
        self.assertIn(f"• **Model In This Topic:** `{self.TOPIC_MODEL}`", status)
        self.assertIn("(overridden in this topic)", status)
        self.assertIn("• **System Prompt In This Topic:** `Custom", status)


class TopicTitleHookTests(_BotChatCase):
    """`_schedule_topic_title`: which answers hand their topic a title."""

    PEER = object()
    QUESTION = "What is a monad?"
    ANSWER = "A way to chain computations."

    #: What the prefix step handed over, passed through to the title step.
    PREFIXED = object()

    def setUp(self):
        super().setUp()
        schedule = patch.object(plugin.topic_titles, "schedule_title_new_topic")
        self.schedule = schedule.start()
        self.addCleanup(schedule.stop)

    def event(self, message, *, get_input_chat=None):
        return _Event(
            message,
            client=self.chat.client,
            chat_id=USER_ID,
            sender_id=USER_ID,
            is_private=True,
            get_input_chat=get_input_chat or AsyncMock(return_value=self.PEER),
        )

    def hand_over(self, message, *, answer=ANSWER, get_input_chat=None):
        event = self.event(message, get_input_chat=get_input_chat)
        asyncio.run(
            plugin._schedule_topic_title(
                event,
                question=self.QUESTION,
                answer=answer,
                model=plugin.OPENAI_CODEX_LUNA_RESERVE,
                reasoning_level="low",
                codex_p=True,
                prefixed=self.PREFIXED,
            )
        )
        return event

    def topic(self, message):
        return plugin.topic_titles.TopicRef(
            peer=self.PEER,
            chat_id=USER_ID,
            topic_id=TOPIC_ID,
            message_date=message.date,
        )

    def test_an_answer_in_a_topic_schedules_its_title(self):
        message = _said(330, self.QUESTION)

        event = self.hand_over(message)

        self.schedule.assert_called_once()
        client, request = self.schedule.call_args.args
        self.assertIs(client, event.client)
        self.assertEqual(
            request,
            plugin.topic_titles.TopicTitleRequest(
                topic=self.topic(message),
                question=self.QUESTION,
                answer=self.ANSWER,
                badge=plugin.topic_titles.TopicBadge(
                    model_emoji="🌙", effort_symbol="◔", icon_emoji="🔮"
                ),
            ),
        )
        self.assertTrue(callable(self.schedule.call_args.kwargs["generate"]))
        self.assertIs(self.schedule.call_args.kwargs["prefixed"], self.PREFIXED)
        self.assertTrue(self.schedule.call_args.kwargs["choose_icon"])

    def start(self, message, *, prefix_effort=None, get_input_chat=None):
        event = self.event(message, get_input_chat=get_input_chat)
        with patch.object(
            plugin.topic_titles, "schedule_prefix_new_topic"
        ) as schedule_prefix:
            started = asyncio.run(
                plugin._start_topic_title(
                    event,
                    model=plugin.OPENAI_CODEX_LUNA_RESERVE,
                    prefix_effort=prefix_effort,
                )
            )
        return started, schedule_prefix

    def test_a_message_in_a_topic_badges_it_at_once(self):
        message = _said(330, self.QUESTION)

        started, schedule_prefix = self.start(message, prefix_effort="xhigh")

        schedule_prefix.assert_called_once()
        self.assertIs(started, schedule_prefix.return_value)
        _client, topic = schedule_prefix.call_args.args
        badge = schedule_prefix.call_args.kwargs["badge"]
        self.assertEqual(topic, self.topic(message))
        self.assertEqual(schedule_prefix.call_args.kwargs["initial_title"], "New Chat")
        self.assertEqual(
            badge,
            plugin.topic_titles.TopicBadge(
                model_emoji="🌙", effort_symbol="●", icon_emoji="🔮"
            ),
        )

    def test_question_text_can_be_the_initial_title(self):
        self.prefs.topic_initial_name = "question"

        _, schedule_prefix = self.start(_said(330, self.QUESTION))

        self.assertEqual(
            schedule_prefix.call_args.kwargs["initial_title"], self.QUESTION
        )

    def test_no_badge_outside_topics(self):
        started, schedule_prefix = self.start(
            _said(330, "hi", top_id=None, parent=None)
        )

        self.assertIsNone(started)
        schedule_prefix.assert_not_called()

    def test_a_failure_to_badge_does_not_reach_the_answer(self):
        with patch.object(plugin, "logger", create=True) as logger:
            started, schedule_prefix = self.start(
                _said(330, "hi"),
                get_input_chat=AsyncMock(side_effect=ConnectionError("gone")),
            )

        self.assertIsNone(started)
        logger.exception.assert_called_once()
        schedule_prefix.assert_not_called()

    def test_no_title_outside_topics_for_meta_answers_or_on_user_accounts(self):
        cases = {
            "outside topics": dict(message=_said(330, "hi", top_id=None, parent=None)),
            "a meta answer": dict(
                message=_said(330, "hi"),
                answer=f"{plugin.BOT_META_INFO_PREFIX}__[No response]__",
            ),
        }
        for name, kwargs in cases.items():
            with self.subTest(name):
                self.hand_over(**kwargs)
        with patch.object(plugin, "IS_BOT", False):
            self.hand_over(_said(330, "hi"))

        self.schedule.assert_not_called()

    def test_a_failure_to_schedule_does_not_reach_the_answer(self):
        with patch.object(plugin, "logger", create=True) as logger:
            self.hand_over(
                _said(330, "hi"),
                get_input_chat=AsyncMock(side_effect=ConnectionError("gone")),
            )

        logger.exception.assert_called_once()
        self.schedule.assert_not_called()

    def test_the_title_model_writes_the_title(self):
        generate_title = AsyncMock(
            return_value=plugin.topic_titles.TopicTitle(title="Monads")
        )
        with patch.object(
            plugin.title_util, "generate_title", generate_title
        ), patch.object(
            plugin.user_manager, "get_title_model", return_value="auto"
        ), patch.object(
            plugin, "get_effective_gemini_api_key", return_value="gemini-key"
        ):
            generate = plugin._topic_title_generator(USER_ID, codex_p=True)
            title = asyncio.run(generate("prompt"))

        self.assertEqual(title.title, "Monads")
        self.assertEqual(
            generate_title.await_args.args, ("prompt", plugin.topic_titles.TopicTitle)
        )
        self.assertEqual(
            generate_title.await_args.kwargs,
            dict(
                choice="auto",
                codex_p=True,
                api_keys={"gemini": "gemini-key"},
                api_user_id=USER_ID,
            ),
        )


if __name__ == "__main__":
    unittest.main()
