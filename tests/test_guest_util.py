import asyncio
import datetime
import logging
from types import SimpleNamespace
import unittest
from unittest import mock

from telethon import errors, types
from telethon._updates import EntityCache

from uniborg import guest_util, util
from uniborg.uniborg import Uniborg

GuestContextError = guest_util.GuestContextError
GUEST_TYPES = hasattr(types, "UpdateBotGuestChatQuery")
BOT_ID = 999
NOW = datetime.datetime(2026, 9, 30, 12, 0, tzinfo=datetime.timezone.utc)


def _message(text="@SugarBot hi", *, msg_id=10, peer=None, from_id=None, **kwargs):
    return types.Message(
        id=msg_id,
        peer_id=peer or types.PeerUser(200),
        date=kwargs.pop("date", NOW),
        message=text,
        from_id=from_id if from_id is not None else types.PeerUser(100),
        **kwargs,
    )


class _Client:
    def __init__(self):
        self._self_id = BOT_ID
        self._mb_entity_cache = EntityCache()
        self.me = SimpleNamespace(bot=True)
        self.handlers = []
        self.downloads = []
        self.parse_mode = None

    async def download_media(self, message, *args, **kwargs):
        self.downloads.append(message)
        return "/tmp/file"

    async def get_messages(self, *args, **kwargs):
        raise AssertionError("guest code must never fetch messages")

    async def get_entity(self, entity):
        return entity

    def add_event_handler(self, callback, builder):
        self.handlers.append((builder, callback))


def _update(message, *, references=(), query_id=5, entities=None):
    update = SimpleNamespace(
        query_id=query_id,
        message=message,
        reference_messages=list(references),
    )
    update._entities = entities or {}
    return update


def _query(message=None, *, references=(), clock=lambda: NOW.timestamp(), **kwargs):
    return guest_util.guest_query_from_update(
        _update(message or _message(), references=references, **kwargs),
        client=_Client(),
        clock=clock,
    )


class GuestQueryTests(unittest.TestCase):
    def test_reads_the_caller_from_from_id_and_marks_every_message(self):
        reference = _message("look", msg_id=9, from_id=types.PeerUser(200))
        query = _query(_message(out=True), references=[reference])

        self.assertEqual(query.caller_id, 100)
        self.assertIs(query.chat_kind, guest_util.ChatKind.PRIVATE)
        self.assertEqual([m.id for m in query.messages], [9, 10])
        self.assertTrue(all(guest_util.is_guest_message(m) for m in query.messages))
        self.assertEqual(query.received_at, NOW.timestamp())
        self.assertIsInstance(query.trigger._client, guest_util.GuestClient)

    def test_private_thread_key_is_the_unordered_pair(self):
        one = _query(_message(peer=types.PeerUser(200), from_id=types.PeerUser(100)))
        other = _query(_message(peer=types.PeerUser(100), from_id=types.PeerUser(200)))

        self.assertEqual(one.thread_key, "pair:100:200")
        self.assertEqual(other.thread_key, one.thread_key)

    def test_group_thread_key_is_the_chat(self):
        query = _query(_message(peer=types.PeerChannel(300)))

        self.assertIs(query.chat_kind, guest_util.ChatKind.GROUP)
        self.assertEqual(query.thread_key, "chat:-1000000000300")

    def test_a_channel_sender_has_no_caller(self):
        query = _query(
            _message(peer=types.PeerChannel(300), from_id=types.PeerChannel(301))
        )

        self.assertIsNone(query.caller_id)

    def test_rich_messages_read_as_flattened_markdown(self):
        message = _message("")
        message.rich_message = object()

        with mock.patch.object(
            guest_util.tg_format, "flatten_rich_message", return_value="# flat"
        ):
            query = _query(message)

        self.assertEqual(query.text, "# flat")

    def test_a_min_sender_is_never_reloaded_through_the_chat(self):
        sender = types.User(id=100, min=True, first_name="A")
        query = _query(entities={100: sender})

        got = asyncio.run(query.trigger.get_sender())

        self.assertEqual(got.id, 100)

    def test_the_guest_client_refuses_chat_access(self):
        query = _query(_message(reply_to=types.MessageReplyHeader(reply_to_msg_id=9)))

        with self.assertRaises(GuestContextError):
            asyncio.run(query.trigger.get_reply_message())
        with self.assertRaises(GuestContextError):
            query.client.send_message
        with self.assertRaises(GuestContextError):
            query.client.anything = 1
        self.assertTrue(issubclass(GuestContextError, ValueError))

        asyncio.run(query.trigger.download_media(file="/tmp/x"))
        self.assertEqual(
            query.client.wrapped.downloads, [guest_util.download_target(query.trigger)]
        )

    def test_guest_media_is_downloaded_by_media_never_by_message(self):
        media = types.MessageMediaDocument(
            document=types.Document(
                id=9,
                access_hash=1,
                file_reference=b"",
                date=NOW,
                mime_type="audio/ogg",
                size=3,
                dc_id=2,
                attributes=[],
            )
        )
        query = _query(_message("@SugarBot", media=media))
        plain = _message("not a guest message", media=media)

        asyncio.run(query.trigger.download_media(file="/tmp/x"))

        self.assertEqual(query.client.wrapped.downloads, [media])
        self.assertIs(guest_util.download_target(query.trigger), media)
        self.assertIs(guest_util.download_target(plain), plain)


class MentionTests(unittest.TestCase):
    def _strip(self, text, entities=None):
        message = _message(text, entities=entities)
        mentioned = guest_util.strip_mention(message, username="SugarBot")
        return mentioned, message.message, message.entities

    def test_leading_and_trailing_mentions_are_stripped(self):
        self.assertEqual(self._strip("@SugarBot  hi")[:2], (True, "hi"))
        self.assertEqual(self._strip("@sugarbot, hi")[:2], (True, "hi"))
        self.assertEqual(self._strip("hi there @SUGARBOT ")[:2], (True, "hi there"))

    def test_a_middle_mention_is_kept(self):
        self.assertEqual(
            self._strip("ask @SugarBot now")[:2], (True, "ask @SugarBot now")
        )

    def test_no_mention(self):
        self.assertEqual(self._strip("hi")[:2], (False, "hi"))
        self.assertEqual(self._strip("@SugarBotx hi")[:2], (False, "@SugarBotx hi"))
        self.assertEqual(self._strip("a@SugarBot")[:2], (False, "a@SugarBot"))

    def test_entities_move_in_utf16_units(self):
        text = "@SugarBot 😀 x"
        entities = [
            types.MessageEntityMention(0, 9),
            types.MessageEntityBold(13, 1),
        ]

        _mentioned, stripped, kept = self._strip(text, entities)

        self.assertEqual(stripped, "😀 x")
        self.assertEqual(
            [(type(e), e.offset, e.length) for e in kept],
            [(types.MessageEntityBold, 3, 1)],
        )

    def test_trailing_strip_drops_entities_past_the_end(self):
        _m, stripped, kept = self._strip(
            "go @SugarBot",
            [types.MessageEntityMention(3, 9), types.MessageEntityBold(0, 2)],
        )

        self.assertEqual(stripped, "go")
        self.assertEqual([(e.offset, e.length) for e in kept], [(0, 2)])

    def test_text_after_leading_mention(self):
        after = guest_util.text_after_leading_mention
        self.assertEqual(after("@SugarBot .a ls", username="sugarbot"), ".a ls")
        self.assertEqual(after(" @SugarBot\n.a ls", username="SugarBot"), ".a ls")
        self.assertIsNone(after(".a ls @SugarBot", username="SugarBot"))
        self.assertIsNone(after("x @SugarBot", username="SugarBot"))
        self.assertTrue(guest_util.mentions("x @sugarbot y", username="SugarBot"))
        self.assertFalse(guest_util.mentions(None, username="SugarBot"))


class EchoTests(unittest.TestCase):
    def test_only_our_own_outgoing_guest_answers_are_echoes(self):
        via = types.PeerUser(100)
        self.assertTrue(
            guest_util.is_guest_answer(
                SimpleNamespace(out=True, guestchat_via_from=via)
            )
        )
        self.assertFalse(
            guest_util.is_guest_answer(
                SimpleNamespace(out=False, guestchat_via_from=via)
            )
        )
        self.assertFalse(
            guest_util.is_guest_answer(
                SimpleNamespace(out=True, guestchat_via_from=None)
            )
        )
        self.assertFalse(guest_util.is_guest_answer(None))


class ClaimTests(unittest.TestCase):
    def test_memory_claims_expire(self):
        now = [0.0]
        claims = guest_util.QueryClaims(ttl_seconds=10, clock=lambda: now[0])

        async def run():
            first = await claims.claim("k")
            again = await claims.claim("k")
            now[0] = 11
            later = await claims.claim("k")
            return first, again, later

        self.assertEqual(asyncio.run(run()), (True, False, True))

    def test_the_backend_decides_across_processes(self):
        taken = set()

        class _Redis:
            async def set(self, key, value, *, nx, ex):
                self.args = (key, value, nx, ex)
                if key in taken:
                    return None
                taken.add(key)
                return True

        redis = _Redis()

        async def get_redis():
            return redis

        backend = guest_util.redis_claim_backend(get_redis)
        one = guest_util.QueryClaims(backend=backend, ttl_seconds=60)
        two = guest_util.QueryClaims(backend=backend, ttl_seconds=60)

        async def run():
            return await one.claim("q:1"), await two.claim("q:1")

        self.assertEqual(asyncio.run(run()), (True, False))
        self.assertEqual(redis.args, ("borg:guest:claim:q:1", "1", True, 60))

    def test_a_failing_or_missing_backend_falls_back_to_memory(self):
        async def broken(key, ttl):
            raise ConnectionError("down")

        async def missing(key, ttl):
            return None

        async def run():
            with self.assertLogs(guest_util.__name__, logging.WARNING):
                a = await guest_util.QueryClaims(backend=broken).claim("x")
            b = await guest_util.QueryClaims(backend=missing).claim("x")
            return a, b

        self.assertEqual(asyncio.run(run()), (True, True))


async def _plugin_handler(query):
    _plugin_handler.calls.append(query)


_plugin_handler.calls = []
_plugin_handler.__module__ = "_UniborgPlugins.test.fake_plugin"


@unittest.skipUnless(GUEST_TYPES, "needs Telethon 1.45 guest-mode types")
class RegisterTests(unittest.TestCase):
    def setUp(self):
        _plugin_handler.calls = []
        self.client = _Client()
        self.claims = guest_util.QueryClaims()
        self.callback = guest_util.register_guest_handler(
            self.client,
            _plugin_handler,
            claims=self.claims,
            max_age_seconds=60,
            clock=lambda: NOW.timestamp() + 5,
        )

    def _deliver(self, message, *, query_id=5):
        asyncio.run(self.callback(_update(message, query_id=query_id)))

    def test_a_fresh_query_reaches_the_handler_once(self):
        self._deliver(_message())
        self._deliver(_message())

        self.assertEqual(len(_plugin_handler.calls), 1)
        self.assertEqual(_plugin_handler.calls[0].query_id, 5)

    def test_bots_sharing_claims_each_get_their_query(self):
        other = _Client()
        other._self_id = BOT_ID + 1
        other_callback = guest_util.register_guest_handler(
            other,
            _plugin_handler,
            claims=self.claims,
            max_age_seconds=60,
            clock=lambda: NOW.timestamp() + 5,
        )

        self._deliver(_message())
        asyncio.run(other_callback(_update(_message(), query_id=5)))

        self.assertEqual(len(_plugin_handler.calls), 2)

    def test_stale_and_forwarded_triggers_are_dropped(self):
        self._deliver(_message(date=NOW - datetime.timedelta(minutes=5)), query_id=6)
        self._deliver(_message(fwd_from=types.MessageFwdHeader(date=NOW)), query_id=7)
        self._deliver(_message(via_bot_id=123), query_id=8)

        self.assertEqual(_plugin_handler.calls, [])

    def test_a_plugin_reload_removes_the_callback(self):
        ((builder, callback),) = self.client.handlers
        self.assertIsInstance(builder, guest_util.events.Raw)
        fake_borg = SimpleNamespace(_event_builders=list(self.client.handlers))

        Uniborg.remove_events_of_mod(fake_borg, "_UniborgPlugins.test.fake_plugin")

        self.assertEqual(fake_borg._event_builders, [])

    def test_user_accounts_register_nothing(self):
        client = _Client()
        client.me = SimpleNamespace(bot=False)

        self.assertIsNone(
            guest_util.register_guest_handler(
                client, _plugin_handler, claims=guest_util.QueryClaims()
            )
        )
        self.assertEqual(client.handlers, [])


class RegisterWithoutGuestTypesTests(unittest.TestCase):
    def test_registers_nothing_without_the_update_type(self):
        client = _Client()
        with mock.patch.object(
            guest_util.types, "UpdateBotGuestChatQuery", None, create=True
        ):
            self.assertIsNone(
                guest_util.register_guest_handler(
                    client, _plugin_handler, claims=guest_util.QueryClaims()
                )
            )


class GuestEventTests(unittest.TestCase):
    def test_has_a_synthetic_chat_and_refuses_to_reply(self):
        event = guest_util.GuestEvent(_query(), text="hi")

        self.assertEqual(event.chat_id, "guest:pair:100:200")
        self.assertEqual(
            (event.sender_id, event.text, event.is_private), (100, "hi", False)
        )
        self.assertTrue(guest_util.is_guest_event(event))
        for method in (
            event.reply,
            event.respond,
            event.get_chat,
            event.get_reply_message,
        ):
            with self.assertRaises(GuestContextError):
                asyncio.run(method("x"))
        with self.assertRaises(GuestContextError):
            event.chat
        with self.assertRaises(GuestContextError):
            event.action("typing")


class _Editor:
    def __init__(self, errors_=()):
        self.edits = []
        self.errors = list(errors_)

    async def edit(self, **kwargs):
        if self.errors:
            raise self.errors.pop(0)
        self.edits.append(kwargs)
        return True


class GuestAnswerMessageTests(unittest.TestCase):
    def _answer(self, editor, now):
        slept = []

        async def sleep(seconds):
            slept.append(seconds)
            now[0] += seconds

        answer = guest_util.GuestAnswerMessage(
            editor, min_interval=1.0, clock=lambda: now[0], sleep=sleep
        )
        return answer, slept

    def test_edits_are_spaced_and_recorded(self):
        editor, now = _Editor(), [100.0]
        answer, slept = self._answer(editor, now)

        async def run():
            await answer.edit("a", parse_mode="md")
            now[0] += 0.25
            await answer.edit("ab", parse_mode="md")

        asyncio.run(run())

        self.assertEqual([e["text"] for e in editor.edits], ["a", "ab"])
        self.assertEqual(slept, [0.75])
        self.assertEqual(answer.text, "ab")
        self.assertLess(answer.id, 0)

    def test_a_flood_wait_blocks_partials_but_not_the_final_edit(self):
        flood = errors.FloodWaitError(request=None, capture=90)
        editor, now = _Editor([flood]), [0.0]
        answer, slept = self._answer(editor, now)

        async def run():
            await answer.edit("a")
            await answer.edit("ab")
            return await answer.finalize(markdown="# done")

        self.assertTrue(asyncio.run(run()))
        self.assertEqual(editor.edits, [{"markdown": "# done"}])
        self.assertEqual(slept, [90.0])
        self.assertEqual(answer.text, "# done")

    def test_finalize_gives_up_on_a_very_long_wait(self):
        editor, now = _Editor([errors.FloodWaitError(request=None, capture=900)]), [0.0]
        answer, _slept = self._answer(editor, now)

        async def run():
            await answer.edit("a")
            await answer.finalize(text="b")

        with self.assertRaises(errors.FloodWaitError):
            asyncio.run(run())

    def test_util_edit_message_streams_into_it_without_posting_elsewhere(self):
        editor, now = _Editor(), [0.0]
        answer, _slept = self._answer(editor, now)

        asyncio.run(util.edit_message(answer, "x" * 5000, parse_mode="md"))

        self.assertEqual(len(editor.edits), 1)
        self.assertLessEqual(len(editor.edits[0]["text"]), 4096)
        with self.assertRaises(GuestContextError):
            asyncio.run(answer.reply("y"))


class TriggerGuardTests(unittest.TestCase):
    def test_a_leading_bot_mention_before_dot_a_is_defanged(self):
        mention = types.MessageEntityMention(0, 10)
        bold = types.MessageEntityBold(11, 5)

        text, entities = guest_util.defang_guest_trigger(
            "@julia_bot .a ls", [mention, bold]
        )

        self.assertEqual(text, "\uff20julia_bot .a ls")
        self.assertEqual(entities, [bold])
        self.assertEqual(
            guest_util.defang_guest_trigger("  @X_BOT\n.af rm x")[0],
            "  \uff20X_BOT\n.af rm x",
        )

    def test_every_separator_telegram_ends_a_mention_with_is_defanged(self):
        for text in (
            "@julia_bot: .a ls",
            "@julia_bot, .a ls",
            "@julia_bot:.a ls",
            "@julia_bot,.a ls",
            "@julia_bot.a ls",
            "@Julia_Bot,  .a ls",
            "@julia_bot:\n.a ls",
        ):
            with self.subTest(text=text):
                defanged, _ = guest_util.defang_guest_trigger(text)
                self.assertTrue(defanged.startswith("\uff20"))

    def test_the_guard_covers_every_text_the_shell_would_run(self):
        for text in (
            "@julia_bot .a ls",
            " @julia_bot\t.af ls",
            "@JULIA_BOT\n.ad ls",
            "@julia_bot: .a ls",
            "@julia_bot.a ls",
        ):
            with self.subTest(text=text):
                runs = (
                    guest_util.shell_command_after_mention(text, username="julia_bot")
                    is not None
                )
                defanged, _ = guest_util.defang_guest_trigger(text)
                self.assertTrue(defanged.lstrip().startswith("\uff20") or not runs)

    def test_other_text_is_untouched(self):
        for text in (
            "@julia_bot hi",
            "hi @julia_bot .a ls",
            "@julia .a ls",
            ".a ls @julia_bot",
            "@julia_botx .a ls",
            "@julia_bot hi .a ls",
            None,
        ):
            with self.subTest(text=text):
                self.assertEqual(
                    guest_util.defang_guest_trigger(text, None), (text, None)
                )

    def _client(self, *, is_bot, guard=True):
        class _Base:
            async def __call__(
                self, request, ordered=False, flood_sleep_threshold=None
            ):
                self.sent = request
                return "ok"

        class _Client(guest_util.OutgoingTriggerGuardMixin, _Base):
            pass

        client = _Client()
        client._is_bot = is_bot
        client.trigger_guard = guard
        return client

    def _send(self, client, text):
        from telethon import functions

        request = functions.messages.SendMessageRequest(
            peer=types.InputPeerSelf(), message=text
        )
        asyncio.run(client(request))
        return client.sent.message

    def test_userbots_defang_and_bots_do_not(self):
        self.assertEqual(
            self._send(self._client(is_bot=False), "@x_bot .a ls"), "\uff20x_bot .a ls"
        )
        self.assertEqual(
            self._send(self._client(is_bot=True), "@x_bot .a ls"), "@x_bot .a ls"
        )
        self.assertEqual(
            self._send(self._client(is_bot=False, guard=False), "@x_bot .a ls"),
            "@x_bot .a ls",
        )

    def test_album_captions_are_defanged_too(self):
        from telethon import functions

        media = types.InputSingleMedia(
            media=types.InputMediaEmpty(), message="@x_bot .a ls", random_id=1
        )
        request = functions.messages.SendMultiMediaRequest(
            peer=types.InputPeerSelf(), multi_media=[media]
        )

        self.assertTrue(guest_util.defang_request(request))
        self.assertEqual(media.message, "\uff20x_bot .a ls")
        self.assertFalse(
            guest_util.defang_request(
                SimpleNamespace(message="@x_bot .a ls", entities=None)
            )
        )

    def test_the_switch_parses_strictly(self):
        self.assertTrue(guest_util.trigger_guard_enabled(environ={}))
        self.assertFalse(
            guest_util.trigger_guard_enabled(
                environ={"borg_guest_trigger_guard": "off"}
            )
        )
        with self.assertRaises(ValueError):
            guest_util.trigger_guard_enabled(
                environ={"borg_guest_trigger_guard": "maybe"}
            )


if __name__ == "__main__":
    unittest.main()
