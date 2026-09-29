"""Send placement in private-chat topics (uniborg/topics.py).

Terms follow uniborg/topics.py. The shapes are the ones a canary bot saw
live (Telethon 1.45, layer 229; layer 224 has the same fields):

- a message typed in a private topic carries
  `MessageReplyHeader(forum_topic=True, reply_to_msg_id=<parent or root>,
  reply_to_top_id=<topic id>)`, and the topic id is not in the bot's
  message-id space;
- a bot reply lands in the topic only when its `InputReplyToMessage` has
  `top_msg_id=<topic id>`; a plain reply lands in "All".

Nothing here connects: the clients run on a fake transport.
"""

import asyncio
import datetime
import importlib.util
import inspect
import logging
from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock

from telethon import TelegramClient, errors, events, functions, types
from telethon.sessions import MemorySession

_ROOT = Path(__file__).resolve().parents[1]


def _load(name):
    #: From the file, so the `uniborg` package __init__ (which pulls in
    #: uniborg.util) never runs.
    spec = importlib.util.spec_from_file_location(
        f"{name}_under_test", _ROOT / "uniborg" / f"{name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


topics = _load("topics")
telethon_safety = _load("telethon_safety")

USER_ID = 999000771
BOT_ID = 999000772
CHANNEL_ID = 999000773
CHAT_ID = 999000774

#: The topic's root in the bot's box, and the topic id from the user's box.
ROOT_ID, TOPIC_ID = 322, 1241380
OTHER_TOPIC_ID = 1241384

NOW = datetime.datetime(2026, 9, 29, tzinfo=datetime.timezone.utc)
API_HASH = "0123456789abcdef0123456789abcdef"


def _peer():
    return types.InputPeerUser(user_id=USER_ID, access_hash=1)


def _reply(msg_id, **fields):
    return types.InputReplyToMessage(reply_to_msg_id=msg_id, **fields)


def _send_message(peer=None, *, reply_to=None, **fields):
    return functions.messages.SendMessageRequest(
        peer=peer or _peer(), message="hi", reply_to=reply_to, **fields
    )


def _send_media(peer=None, *, reply_to=None):
    return functions.messages.SendMediaRequest(
        peer=peer or _peer(),
        media=types.InputMediaEmpty(),
        message="",
        reply_to=reply_to,
    )


def _send_album(peer=None, *, reply_to=None):
    return functions.messages.SendMultiMediaRequest(
        peer=peer or _peer(),
        multi_media=[
            types.InputSingleMedia(media=types.InputMediaEmpty(), message=""),
            types.InputSingleMedia(media=types.InputMediaEmpty(), message=""),
        ],
        reply_to=reply_to,
    )


SEND_BUILDERS = {
    "SendMessageRequest": _send_message,
    "SendMediaRequest": _send_media,
    "SendMultiMediaRequest": _send_album,
}


def _in_topic(msg_id, *, parent=ROOT_ID, top_id=TOPIC_ID, peer=None):
    return types.Message(
        id=msg_id,
        peer_id=peer or types.PeerUser(USER_ID),
        message="",
        date=NOW,
        reply_to=types.MessageReplyHeader(
            forum_topic=True, reply_to_msg_id=parent, reply_to_top_id=top_id
        ),
    )


def _outside_topics(msg_id, *, parent=None, peer=None):
    return types.Message(
        id=msg_id,
        peer_id=peer or types.PeerUser(USER_ID),
        message="",
        date=NOW,
        reply_to=(
            types.MessageReplyHeader(reply_to_msg_id=parent)
            if parent is not None
            else None
        ),
    )


def _topic_root(msg_id=ROOT_ID, *, top_id=TOPIC_ID):
    return types.MessageService(
        id=msg_id,
        peer_id=types.PeerUser(USER_ID),
        date=NOW,
        action=types.MessageActionTopicCreate(title="Topic", icon_color=0),
        reply_to=types.MessageReplyHeader(forum_topic=True, reply_to_top_id=top_id),
    )


def _new_message(message):
    return types.UpdateNewMessage(message=message, pts=1, pts_count=1)


def _updates(*updates):
    return types.Updates(updates=list(updates), users=[], chats=[], date=NOW, seq=0)


def _short_sent(msg_id):
    return types.UpdateShortSentMessage(
        id=msg_id, pts=1, pts_count=1, date=NOW, out=True
    )


class _Fetcher:
    """Loads messages by id from BY_ID, logging each load; ERROR makes it fail."""

    def __init__(self, *messages, error=None):
        self.by_id = {m.id: m for m in messages}
        self.error = error
        self.fetched = []

    async def __call__(self, peer, msg_id):
        self.fetched.append(msg_id)
        if self.error is not None:
            raise self.error
        return self.by_id.get(msg_id)


class _BrokenRegistry:
    def get(self, chat_id, msg_id):
        raise RuntimeError("registry is broken")

    def record(self, chat_id, msg_id, *, topic_id):
        raise RuntimeError("registry is broken")


def _engine(**kwargs):
    kwargs.setdefault("topic_roots", topics.TopicRootCache())
    return topics.TopicPlacement(**kwargs)


def _place(engine, request, *, fetcher=None):
    return asyncio.run(engine.before_send(request, fetch_message=fetcher or _Fetcher()))


class PlacementTests(unittest.TestCase):
    def setUp(self):
        self.engine = _engine()

    def test_each_send_type_gets_the_topic_of_its_parent(self):
        for name, build in SEND_BUILDERS.items():
            with self.subTest(name):
                engine = _engine()
                engine.registry.record(USER_ID, 330, topic_id=TOPIC_ID)
                original = _reply(330)
                request = build(reply_to=original)
                placement = _place(engine, request)
                self.assertEqual(request.reply_to.top_msg_id, TOPIC_ID)
                self.assertEqual(request.reply_to.reply_to_msg_id, 330)
                #: The caller's reply target is copied, not edited.
                self.assertIsNone(original.top_msg_id)
                self.assertEqual(
                    placement,
                    topics.Placement(
                        chat_id=USER_ID, topic_id=TOPIC_ID, unplaced_reply_to=original
                    ),
                )
                self.assertEqual(engine.stats.placed, 1)

    def test_a_placed_request_still_serializes(self):
        self.engine.registry.record(USER_ID, 330, topic_id=TOPIC_ID)
        request = _send_message(reply_to=_reply(330))
        _place(self.engine, request)
        self.assertTrue(bytes(request))

    def test_a_parent_outside_topics_leaves_the_send_alone(self):
        self.engine.registry.record(USER_ID, 330, topic_id=None)
        fetcher = _Fetcher()
        request = _send_message(reply_to=_reply(330))
        placement = _place(self.engine, request, fetcher=fetcher)
        self.assertIsNone(request.reply_to.top_msg_id)
        self.assertEqual(placement, topics.Placement(chat_id=USER_ID, topic_id=None))
        self.assertEqual(fetcher.fetched, [])

    def test_an_unseen_parent_is_loaded_once(self):
        fetcher = _Fetcher(_in_topic(330))
        first = _send_message(reply_to=_reply(330))
        second = _send_media(reply_to=_reply(330))
        _place(self.engine, first, fetcher=fetcher)
        _place(self.engine, second, fetcher=fetcher)
        self.assertEqual(first.reply_to.top_msg_id, TOPIC_ID)
        self.assertEqual(second.reply_to.top_msg_id, TOPIC_ID)
        self.assertEqual(fetcher.fetched, [330])
        self.assertEqual(self.engine.stats.fetched, 1)

    def test_an_unseen_parent_outside_topics_is_remembered_as_such(self):
        fetcher = _Fetcher(_outside_topics(330))
        for _ in range(2):
            request = _send_message(reply_to=_reply(330))
            _place(self.engine, request, fetcher=fetcher)
            self.assertIsNone(request.reply_to.top_msg_id)
        self.assertEqual(fetcher.fetched, [330])
        self.assertIsNone(self.engine.registry.get(USER_ID, 330))

    def test_a_parent_that_no_longer_exists_counts_as_outside_topics(self):
        fetcher = _Fetcher()
        request = _send_message(reply_to=_reply(330))
        _place(self.engine, request, fetcher=fetcher)
        self.assertIsNone(request.reply_to.top_msg_id)
        self.assertIsNone(self.engine.registry.get(USER_ID, 330))

    def test_a_loaded_root_is_remembered_for_reply_detection(self):
        roots = topics.TopicRootCache()
        engine = _engine(topic_roots=roots)
        request = _send_message(reply_to=_reply(ROOT_ID))
        _place(engine, request, fetcher=_Fetcher(_topic_root()))
        self.assertEqual(request.reply_to.top_msg_id, TOPIC_ID)
        self.assertEqual(roots.get(USER_ID, TOPIC_ID), ROOT_ID)

    def test_every_form_of_a_private_peer_is_placed(self):
        peers = {
            "InputPeerUser": _peer(),
            "PeerUser": types.PeerUser(USER_ID),
            "marked id": USER_ID,
            "User": types.User(id=USER_ID, access_hash=1),
        }
        for name, peer in peers.items():
            with self.subTest(name):
                engine = _engine()
                engine.registry.record(USER_ID, 330, topic_id=TOPIC_ID)
                request = _send_message(peer, reply_to=_reply(330))
                _place(engine, request)
                self.assertEqual(request.reply_to.top_msg_id, TOPIC_ID)

    def test_groups_and_channels_are_never_touched(self):
        peers = {
            "channel": types.InputPeerChannel(channel_id=CHANNEL_ID, access_hash=1),
            "basic group": types.InputPeerChat(chat_id=CHAT_ID),
            "marked channel id": -1000000000000 - CHANNEL_ID,
            "marked group id": -CHAT_ID,
        }
        for name, peer in peers.items():
            with self.subTest(name):
                engine = _engine()
                fetcher = _Fetcher(_in_topic(330))
                request = _send_message(peer, reply_to=_reply(330))
                self.assertIsNone(_place(engine, request, fetcher=fetcher))
                self.assertIsNone(request.reply_to.top_msg_id)
                self.assertEqual(fetcher.fetched, [])

    def test_peers_that_do_not_show_their_kind_are_left_alone(self):
        for peer in ("some_username", types.InputPeerSelf()):
            with self.subTest(peer):
                fetcher = _Fetcher(_in_topic(330))
                request = _send_message(peer, reply_to=_reply(330))
                self.assertIsNone(_place(self.engine, request, fetcher=fetcher))
                self.assertIsNone(request.reply_to.top_msg_id)
                self.assertEqual(fetcher.fetched, [])

    def test_a_send_without_a_reply_target_is_not_guessed(self):
        fetcher = _Fetcher()
        request = _send_message()
        placement = _place(self.engine, request, fetcher=fetcher)
        self.assertIsNone(request.reply_to)
        self.assertEqual(placement, topics.Placement(chat_id=USER_ID, topic_id=None))
        self.assertEqual(fetcher.fetched, [])

    def test_a_topic_the_caller_chose_is_kept(self):
        self.engine.registry.record(USER_ID, 330, topic_id=TOPIC_ID)
        request = _send_message(reply_to=_reply(330, top_msg_id=OTHER_TOPIC_ID))
        placement = _place(self.engine, request)
        self.assertEqual(request.reply_to.top_msg_id, OTHER_TOPIC_ID)
        self.assertEqual(placement.topic_id, OTHER_TOPIC_ID)
        self.assertEqual(self.engine.stats.placed, 0)

    def test_a_reply_to_another_chat_is_left_alone(self):
        self.engine.registry.record(USER_ID, 330, topic_id=TOPIC_ID)
        request = _send_message(
            reply_to=_reply(330, reply_to_peer_id=types.InputPeerUser(USER_ID + 1, 1))
        )
        self.assertIsNone(_place(self.engine, request))
        self.assertIsNone(request.reply_to.top_msg_id)

    def test_a_story_reply_is_left_alone(self):
        story = types.InputReplyToStory(peer=_peer(), story_id=5)
        request = _send_message(reply_to=story)
        self.assertIsNone(_place(self.engine, request))
        self.assertIs(request.reply_to, story)

    def test_other_requests_are_not_handled(self):
        request = functions.messages.GetMessagesRequest(id=[types.InputMessageID(330)])
        self.assertFalse(self.engine.handles(request))
        self.assertIsNone(_place(self.engine, request))

    def test_a_failed_lookup_sends_unchanged_and_logs_once(self):
        fetcher = _Fetcher(error=ConnectionError("offline"))
        with self.assertLogs(topics._log, level=logging.DEBUG) as logs:
            for _ in range(3):
                request = _send_message(reply_to=_reply(330))
                self.assertIsNone(_place(self.engine, request, fetcher=fetcher))
                self.assertIsNone(request.reply_to.top_msg_id)
        warnings = [r for r in logs.records if r.levelno >= logging.WARNING]
        self.assertEqual(len(warnings), 1)
        self.assertEqual(self.engine.stats.failures, 3)
        #: A failure is not remembered, so the next send tries again.
        self.assertEqual(fetcher.fetched, [330, 330, 330])
        self.assertIs(self.engine.registry.get(USER_ID, 330), topics.UNKNOWN)

    def test_a_broken_registry_sends_unchanged(self):
        engine = _engine(registry=_BrokenRegistry())
        request = _send_message(reply_to=_reply(330))
        with self.assertLogs(topics._log, level=logging.WARNING):
            self.assertIsNone(_place(engine, request))
        self.assertIsNone(request.reply_to.top_msg_id)


class IncomingRecordingTests(unittest.TestCase):
    def setUp(self):
        self.roots = topics.TopicRootCache()
        self.engine = _engine(topic_roots=self.roots)
        self.registry = self.engine.registry

    def test_a_topic_message_is_filed_under_its_topic(self):
        self.engine.record_update(_new_message(_in_topic(330)))
        self.assertEqual(self.registry.get(USER_ID, 330), TOPIC_ID)

    def test_a_message_outside_topics_is_filed_as_such(self):
        self.engine.record_update(_new_message(_outside_topics(330)))
        self.assertIsNone(self.registry.get(USER_ID, 330))

    def test_a_topic_root_is_filed_for_reply_detection_too(self):
        self.engine.record_update(_new_message(_topic_root()))
        self.assertEqual(self.registry.get(USER_ID, ROOT_ID), TOPIC_ID)
        self.assertEqual(self.roots.get(USER_ID, TOPIC_ID), ROOT_ID)

    def test_an_edit_is_filed(self):
        self.engine.record_update(
            types.UpdateEditMessage(message=_in_topic(330), pts=1, pts_count=1)
        )
        self.assertEqual(self.registry.get(USER_ID, 330), TOPIC_ID)

    def test_a_short_private_message_is_filed(self):
        update = types.UpdateShortMessage(
            id=330,
            user_id=USER_ID,
            message="hi",
            pts=1,
            pts_count=1,
            date=NOW,
            reply_to=types.MessageReplyHeader(
                forum_topic=True, reply_to_msg_id=ROOT_ID, reply_to_top_id=TOPIC_ID
            ),
        )
        self.engine.record_update(update)
        self.assertEqual(self.registry.get(USER_ID, 330), TOPIC_ID)

    def test_channel_messages_are_not_filed(self):
        message = _in_topic(330, peer=types.PeerChannel(CHANNEL_ID))
        self.engine.record_update(
            types.UpdateNewChannelMessage(message=message, pts=1, pts_count=1)
        )
        self.engine.record_update(_new_message(message))
        self.assertEqual(len(self.registry), 0)

    def test_other_updates_are_ignored(self):
        self.engine.record_update(types.UpdateConfig())
        self.assertEqual(len(self.registry), 0)

    def test_a_filing_failure_never_raises(self):
        engine = _engine(registry=_BrokenRegistry())
        with self.assertLogs(topics._log, level=logging.WARNING):
            engine.record_update(_new_message(_in_topic(330)))
        self.assertEqual(engine.stats.failures, 1)


class SentRecordingTests(unittest.TestCase):
    def setUp(self):
        self.engine = _engine()
        self.registry = self.engine.registry
        self.in_topic = topics.Placement(chat_id=USER_ID, topic_id=TOPIC_ID)

    def record(self, result, *, placement, request=None):
        self.engine.record_sent(request or _send_message(), result, placement=placement)

    def test_a_short_sent_message_is_filed_under_the_placement(self):
        self.record(_short_sent(500), placement=self.in_topic)
        self.assertEqual(self.registry.get(USER_ID, 500), TOPIC_ID)

    def test_bare_ids_are_filed_under_the_placement(self):
        self.record(
            _updates(
                types.UpdateMessageID(id=501, random_id=1),
                types.UpdateMessageID(id=502, random_id=2),
            ),
            placement=self.in_topic,
        )
        self.assertEqual(self.registry.get(USER_ID, 501), TOPIC_ID)
        self.assertEqual(self.registry.get(USER_ID, 502), TOPIC_ID)

    def test_an_echoed_message_is_filed_by_its_own_header(self):
        self.record(
            _updates(
                types.UpdateMessageID(id=503, random_id=1),
                _new_message(_in_topic(503, parent=330)),
            ),
            placement=topics.Placement(chat_id=USER_ID, topic_id=None),
        )
        self.assertEqual(self.registry.get(USER_ID, 503), TOPIC_ID)

    def test_an_echo_is_filed_even_when_placement_is_unknown(self):
        self.record(
            _updates(_new_message(_outside_topics(504, parent=330))),
            placement=None,
        )
        self.assertIsNone(self.registry.get(USER_ID, 504))

    def test_an_update_short_echo_is_filed(self):
        self.record(
            types.UpdateShort(update=_new_message(_in_topic(505)), date=NOW),
            placement=None,
        )
        self.assertEqual(self.registry.get(USER_ID, 505), TOPIC_ID)

    def test_bare_ids_without_a_placement_are_not_filed(self):
        self.record(_short_sent(506), placement=None)
        self.record(
            _updates(types.UpdateMessageID(id=507, random_id=1)), placement=None
        )
        self.assertEqual(len(self.registry), 0)

    def test_scheduled_sends_are_not_filed(self):
        self.record(
            _updates(
                types.UpdateMessageID(id=508, random_id=1),
                _new_message(_in_topic(508)),
            ),
            placement=self.in_topic,
            request=_send_message(schedule_date=NOW),
        )
        self.assertEqual(len(self.registry), 0)

    def test_a_filing_failure_never_raises(self):
        engine = _engine(registry=_BrokenRegistry())
        with self.assertLogs(topics._log, level=logging.WARNING):
            engine.record_sent(
                _send_message(), _short_sent(500), placement=self.in_topic
            )
        self.assertEqual(engine.stats.failures, 1)


class TopicRefusalTests(unittest.TestCase):
    def test_topic_errors_are_refusals(self):
        refusals = [
            errors.TopicDeletedError(request=None),
            errors.BadRequestError(request=None, message="TOPIC_CLOSED"),
            errors.RPCError(request=None, message="TOPIC_ID_INVALID", code=400),
        ]
        for error in refusals:
            with self.subTest(error):
                self.assertTrue(topics.is_topic_refusal(error))

    def test_other_errors_are_not(self):
        others = [
            errors.BadRequestError(request=None, message="MESSAGE_ID_INVALID"),
            errors.FloodWaitError(request=None, capture=5),
            ValueError("TOPIC_DELETED"),
        ]
        for error in others:
            with self.subTest(error):
                self.assertFalse(topics.is_topic_refusal(error))


class TopicRegistryTests(unittest.TestCase):
    def test_unseen_and_outside_topics_are_different_answers(self):
        registry = topics.TopicRegistry()
        registry.record(USER_ID, 1, topic_id=None)
        self.assertIsNone(registry.get(USER_ID, 1))
        self.assertIs(registry.get(USER_ID, 2), topics.UNKNOWN)
        self.assertIs(registry.get(USER_ID + 1, 1), topics.UNKNOWN)

    def test_the_least_recently_stored_message_goes_first(self):
        registry = topics.TopicRegistry(max_size=2)
        registry.record(USER_ID, 1, topic_id=TOPIC_ID)
        registry.record(USER_ID, 2, topic_id=TOPIC_ID)
        registry.record(USER_ID, 1, topic_id=TOPIC_ID)
        registry.record(USER_ID, 3, topic_id=None)
        self.assertIs(registry.get(USER_ID, 2), topics.UNKNOWN)
        self.assertEqual(registry.get(USER_ID, 1), TOPIC_ID)
        self.assertEqual(len(registry), 2)


class _Client(
    topics.TopicPlacementMixin,
    telethon_safety.DifferenceFallbackMixin,
    TelegramClient,
):
    """Composed the way `Uniborg` is, on Telethon's real `TelegramClient`."""


class _Transport:
    """Stands in for `TelegramClient._call`: answers by request type and logs
    every request, with the reply target it had when it went out.

    A list of responses is answered in order, one per call."""

    def __init__(self, responses):
        self.responses = responses
        self.requests = []
        self.reply_targets = []

    async def __call__(
        self, sender, request, ordered=False, flood_sleep_threshold=None
    ):
        self.requests.append(request)
        self.reply_targets.append(getattr(request, "reply_to", None))
        response = self.responses[type(request)]
        if isinstance(response, list):
            response = response.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def sent(self, request_type=functions.messages.SendMessageRequest):
        return [r for r in self.requests if isinstance(r, request_type)]


def _state():
    return types.updates.State(pts=500, qts=9, date=NOW, seq=3, unread_count=0)


class ComposedClientTests(unittest.TestCase):
    """The mixin on a real client, stacked on the difference fallback."""

    def run_with_client(self, body, *, responses=None, engine=None):
        async def main():
            client = _Client(MemorySession(), 1, API_HASH)
            client.topic_placement = engine or _engine()
            client._call = _Transport(
                responses or {functions.messages.SendMessageRequest: _short_sent(500)}
            )
            return await body(client)

        return asyncio.run(main())

    def test_the_mro_keeps_both_overrides(self):
        mro = _Client.__mro__
        self.assertLess(
            mro.index(topics.TopicPlacementMixin),
            mro.index(telethon_safety.DifferenceFallbackMixin),
        )
        self.assertLess(
            mro.index(telethon_safety.DifferenceFallbackMixin),
            mro.index(TelegramClient),
        )
        self.assertIs(_Client.__call__, topics.TopicPlacementMixin.__call__)

    def test_send_message_is_placed_and_its_result_filed(self):
        async def body(client):
            client.topic_placement.registry.record(USER_ID, 330, topic_id=TOPIC_ID)
            sent = await client.send_message(_peer(), "hi", reply_to=330)
            return client, sent

        client, sent = self.run_with_client(body)
        (request,) = client._call.sent()
        self.assertEqual(request.reply_to.top_msg_id, TOPIC_ID)
        self.assertEqual(sent.id, 500)
        #: A reply to the bot's own message (the next chunk of a split
        #: answer, say) now finds the topic without a load.
        self.assertEqual(client.topic_placement.registry.get(USER_ID, 500), TOPIC_ID)

    def test_a_reply_chain_stays_in_the_topic(self):
        responses = {functions.messages.SendMessageRequest: _short_sent(500)}

        async def body(client):
            client.topic_placement.registry.record(USER_ID, 330, topic_id=TOPIC_ID)
            first = await client.send_message(_peer(), "one", reply_to=330)
            client._call.responses[functions.messages.SendMessageRequest] = _short_sent(
                501
            )
            await client.send_message(_peer(), "two", reply_to=first.id)
            return client

        client = self.run_with_client(body, responses=responses)
        self.assertEqual(
            [r.reply_to.top_msg_id for r in client._call.sent()], [TOPIC_ID, TOPIC_ID]
        )
        self.assertEqual(client.topic_placement.stats.fetched, 0)

    def test_an_unseen_parent_is_loaded_with_get_messages(self):
        async def body(client):
            client.get_messages = AsyncMock(return_value=_in_topic(330))
            await client.send_message(_peer(), "hi", reply_to=330)
            return client

        client = self.run_with_client(body)
        client.get_messages.assert_awaited_once_with(_peer(), ids=330)
        (request,) = client._call.sent()
        self.assertEqual(request.reply_to.top_msg_id, TOPIC_ID)

    def test_a_failed_load_sends_unchanged(self):
        async def body(client):
            client.get_messages = AsyncMock(side_effect=ConnectionError("offline"))
            with self.assertLogs(topics._log, level=logging.WARNING):
                sent = await client.send_message(_peer(), "hi", reply_to=330)
            return client, sent

        client, sent = self.run_with_client(body)
        (request,) = client._call.sent()
        self.assertIsNone(request.reply_to.top_msg_id)
        self.assertEqual(sent.id, 500)

    def test_a_filing_failure_still_returns_the_sent_message(self):
        async def body(client):
            with self.assertLogs(topics._log, level=logging.WARNING):
                return await client.send_message(_peer(), "hi")

        sent = self.run_with_client(body, engine=_engine(registry=_BrokenRegistry()))
        self.assertEqual(sent.id, 500)

    def test_without_a_placement_nothing_changes(self):
        async def body(client):
            client.topic_placement = None
            client.get_messages = AsyncMock()
            await client.send_message(_peer(), "hi", reply_to=330)
            return client

        client = self.run_with_client(body)
        (request,) = client._call.sent()
        self.assertIsNone(request.reply_to.top_msg_id)
        client.get_messages.assert_not_awaited()

    def test_a_refused_placement_is_sent_again_unplaced(self):
        responses = {
            functions.messages.SendMessageRequest: [
                errors.TopicDeletedError(request=None),
                _short_sent(500),
            ]
        }

        async def body(client):
            client.topic_placement.registry.record(USER_ID, 330, topic_id=TOPIC_ID)
            with self.assertLogs(topics._log, level=logging.WARNING):
                sent = await client.send_message(_peer(), "hi", reply_to=330)
            return client, sent

        client, sent = self.run_with_client(body, responses=responses)
        self.assertEqual(sent.id, 500)
        self.assertEqual(
            [target.top_msg_id for target in client._call.reply_targets],
            [TOPIC_ID, None],
        )
        registry = client.topic_placement.registry
        #: The parent is not placed again, and the sent message is in "All".
        self.assertIsNone(registry.get(USER_ID, 330))
        self.assertIsNone(registry.get(USER_ID, 500))
        self.assertEqual(client.topic_placement.stats.refused, 1)

    def test_other_errors_are_not_retried(self):
        responses = {
            functions.messages.SendMessageRequest: errors.BadRequestError(
                request=None, message="MESSAGE_TOO_LONG"
            )
        }

        async def body(client):
            client.topic_placement.registry.record(USER_ID, 330, topic_id=TOPIC_ID)
            with self.assertRaises(errors.BadRequestError):
                await client.send_message(_peer(), "hi", reply_to=330)
            return client

        client = self.run_with_client(body, responses=responses)
        self.assertEqual(len(client._call.sent()), 1)

    def test_a_topic_error_on_an_unplaced_send_is_not_retried(self):
        responses = {
            functions.messages.SendMessageRequest: errors.TopicDeletedError(
                request=None
            )
        }

        async def body(client):
            client.topic_placement.registry.record(USER_ID, 330, topic_id=None)
            with self.assertRaises(errors.TopicDeletedError):
                await client.send_message(_peer(), "hi", reply_to=330)
            return client

        client = self.run_with_client(body, responses=responses)
        self.assertEqual(len(client._call.sent()), 1)

    def test_the_difference_fallback_still_runs(self):
        responses = {
            functions.updates.GetDifferenceRequest: errors.TypeNotFoundError(
                0xDEADBEEF, b""
            ),
            functions.updates.GetStateRequest: _state(),
        }

        async def sleep(seconds):
            pass

        async def body(client):
            monitor = telethon_safety.SafetyMonitor()
            client.difference_fallback = telethon_safety.DifferenceFallback(
                report=monitor.report, sleep=sleep
            )
            with self.assertLogs(telethon_safety._log, level=logging.WARNING):
                return await client(
                    functions.updates.GetDifferenceRequest(pts=1, date=NOW, qts=1)
                )

        difference = self.run_with_client(body, responses=responses)
        self.assertIsInstance(difference, types.updates.Difference)

    def test_a_flood_wait_on_a_placed_send_propagates_unchanged(self):
        flood = errors.FloodWaitError(request=None, capture=120)
        responses = {functions.messages.SendMessageRequest: flood}

        async def body(client):
            client.topic_placement.registry.record(USER_ID, 330, topic_id=TOPIC_ID)
            with self.assertRaises(errors.FloodWaitError) as caught:
                await client.send_message(_peer(), "hi", reply_to=330)
            self.assertIs(caught.exception, flood)
            return client

        client = self.run_with_client(body, responses=responses)
        self.assertEqual(len(client._call.sent()), 1)
        self.assertEqual(client.topic_placement.stats.refused, 0)

    def test_incoming_messages_are_filed_before_any_handler_runs(self):
        seen = []

        async def body(client):
            client._mb_entity_cache.set_self_user(BOT_ID, True, 0)

            async def handler(update):
                seen.append(client.topic_placement.registry.get(USER_ID, 330))

            client.add_event_handler(handler, events.Raw)
            await client._dispatch_update(_new_message(_in_topic(330)))

        self.run_with_client(body)
        self.assertEqual(seen, [TOPIC_ID])

    def test_telethons_update_loop_still_calls_the_hooked_method(self):
        #: An upgrade tripwire: if Telethon stops dispatching through
        #: `_dispatch_update`, incoming messages stop being filed, and every
        #: reply in a topic costs a load, with no error anywhere.
        source = inspect.getsource(TelegramClient._update_loop)
        self.assertIn("self._dispatch_update(", source)


class OutsideTopicsGoldenTests(unittest.TestCase):
    """Outside private topics a send reaches Telegram exactly as it was built.

    Each case runs every placed request type through the composed client and
    compares the serialized request Telegram receives with the one the caller
    built, so any field placement touched would show.
    """

    CASES = {
        "private reply to a message outside topics": (_peer, 330),
        "private send without a reply": (_peer, None),
        "forum supergroup reply": (
            lambda: types.InputPeerChannel(channel_id=CHANNEL_ID, access_hash=1),
            330,
        ),
        "basic group reply": (lambda: types.InputPeerChat(chat_id=CHAT_ID), 330),
    }

    def send_through_client(self, request):
        async def main():
            client = _Client(MemorySession(), 1, API_HASH)
            engine = _engine()
            engine.registry.record(USER_ID, 330, topic_id=None)
            client.topic_placement = engine
            client.get_messages = AsyncMock(return_value=_in_topic(330))
            client._call = _Transport(
                {
                    request_type: _updates()
                    for request_type in topics.PLACED_REQUEST_TYPES
                }
            )
            await client(request)
            return client

        return asyncio.run(main())

    def test_every_send_goes_out_byte_for_byte(self):
        for case, (make_peer, parent) in self.CASES.items():
            for name, build in SEND_BUILDERS.items():
                with self.subTest(case=case, request=name):
                    reply_to = _reply(parent) if parent is not None else None
                    request = build(make_peer(), reply_to=reply_to)
                    built = bytes(request)
                    client = self.send_through_client(request)
                    (sent,) = client._call.requests
                    self.assertEqual(bytes(sent), built)
                    self.assertIs(sent.reply_to, reply_to)
                    client.get_messages.assert_not_awaited()

    def test_an_unseen_parent_outside_topics_costs_one_load_and_no_change(self):
        async def main():
            client = _Client(MemorySession(), 1, API_HASH)
            client.topic_placement = _engine()
            client.get_messages = AsyncMock(return_value=_outside_topics(330))
            client._call = _Transport(
                {functions.messages.SendMessageRequest: _short_sent(500)}
            )
            requests = [_send_message(reply_to=_reply(330)) for _ in range(2)]
            built = [bytes(request) for request in requests]
            for request in requests:
                await client(request)
            return client, built

        client, built = asyncio.run(main())
        self.assertEqual([bytes(r) for r in client._call.sent()], built)
        client.get_messages.assert_awaited_once_with(_peer(), ids=330)


class UniborgWiringTests(unittest.TestCase):
    def test_uniborg_composes_both_mixins_in_order(self):
        #: Imported here: `uniborg.uniborg` pulls in uniborg.util, which the
        #: rest of this file avoids.
        from uniborg import telethon_safety as safety
        from uniborg import topics as package_topics
        from uniborg.uniborg import Uniborg

        mro = Uniborg.__mro__
        self.assertLess(
            mro.index(package_topics.TopicPlacementMixin),
            mro.index(safety.DifferenceFallbackMixin),
        )
        self.assertLess(
            mro.index(safety.DifferenceFallbackMixin), mro.index(TelegramClient)
        )


if __name__ == "__main__":
    unittest.main()
