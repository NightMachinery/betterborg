"""The chat bot's guest answers (`guest_chat_handler` in llm_chat).

Terms are those of docs/guest_mode.md: a *guest query* carries the *trigger*
(the message that mentioned the bot) and at most one *reference* (the message
it replies to); the bot answers once and then edits that *guest answer*. An
*explicit* call mentions the bot; a reply to the answer without a mention is
*implicit*.
"""

import asyncio
import builtins
import datetime
import importlib
import logging
from contextlib import ExitStack
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from telethon import errors, types
from telethon._updates import EntityCache

from uniborg import codex_util, guest_util, llm_chat_config, llm_util, tg_compat

BOT_ID = 4242
BOT_USERNAME = "@vlm_test_bot"
CALLER = 777001
OTHER = 777002
NOW = datetime.datetime.now(datetime.timezone.utc)
IMAGE_MODEL = "gemini/gemini-2.5-flash-image-preview"


class _RecordingBorg:
    """Fails the test on any send or read by chat id."""

    me = types.User(id=BOT_ID, bot=True, first_name="Vlm", username="vlm_test_bot")

    class loop:
        @staticmethod
        def create_task(coro):
            coro.close()

    def __init__(self):
        self.forbidden = []

    def __getattr__(self, name):
        if name in ("send_message", "send_file", "get_messages", "iter_messages"):

            async def refuse(*args, **kwargs):
                self.forbidden.append(name)

            return refuse
        raise AttributeError(name)


def _import_plugin():
    previous = getattr(builtins, "borg", None)
    builtins.borg = _RecordingBorg()
    try:

        async def _import():
            return importlib.import_module("llm_chat_plugins.llm_chat")

        return asyncio.run(_import())
    finally:
        if previous is not None:
            builtins.borg = previous


plugin = _import_plugin()


def _query(
    text,
    *,
    caller=CALLER,
    reference_text=None,
    reference_from=OTHER,
    reference_date=None,
    reference_via_guest=False,
    query_id=1,
):
    client = SimpleNamespace(
        _self_id=BOT_ID, _mb_entity_cache=EntityCache(), parse_mode=None
    )
    trigger = types.Message(
        id=20,
        peer_id=types.PeerUser(OTHER),
        date=NOW,
        message=text,
        from_id=types.PeerUser(caller),
        out=True,
    )
    references = []
    if reference_text is not None:
        reference = types.Message(
            id=19,
            peer_id=types.PeerUser(OTHER),
            date=reference_date or NOW,
            message=reference_text,
            from_id=(
                types.PeerUser(reference_from) if reference_from is not None else None
            ),
        )
        if reference_via_guest:
            #: A guest bot's answer for CALLER (an attribute: Telethon 1.43
            #: lacks the field).
            reference.guestchat_via_from = types.PeerUser(caller)
        references.append(reference)
    update = SimpleNamespace(
        query_id=query_id, message=trigger, reference_messages=references
    )
    update._entities = {
        caller: types.User(id=caller, first_name="Caller"),
        OTHER: types.User(id=OTHER, first_name="Other"),
    }
    return guest_util.guest_query_from_update(update, client=client)


class _FakeEditor:
    def __init__(self, log, *, refuse_rich=False):
        self.log = log
        self.refuse_rich = refuse_rich

    def __call__(self, client, inline_id):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def edit(self, **kwargs):
        self.log.append(kwargs)
        if kwargs.get("markdown") is not None and self.refuse_rich:
            raise errors.RPCError(request=None, message="RICH_MESSAGE_INVALID")
        return True


class _GuestTestCase(unittest.TestCase):
    """Runs `guest_chat_handler` with Telegram, the config and the model faked."""

    policy = llm_chat_config.GuestConfig()
    api_key = "caller-key"
    admin = False
    answer_text = "**Ether** is at $1."
    refuse_rich = False

    def setUp(self):
        self.answers = []
        self.edits = []
        self.borg = _RecordingBorg()
        self.generate = AsyncMock(
            side_effect=lambda req: plugin.GenerationResult(
                text=self.answer_text, finish_reason="stop", has_image=False
            )
        )

        async def answer_guest(client, *, query_id, title, text=None, **kwargs):
            self.answers.append(SimpleNamespace(text=text, **kwargs))
            return types.InputBotInlineMessageID(dc_id=2, id=query_id, access_hash=1)

        config = llm_chat_config.LLMChatConfig((), (), guest=self.policy)
        stack = ExitStack()
        self.addCleanup(stack.close)
        #: Uniborg injects `logger` into a plugin; a plain import has none.
        stack.enter_context(
            patch.object(
                builtins, "logger", logging.getLogger("test.guest"), create=True
            )
        )
        for target, name, value in (
            (builtins, "borg", self.borg),
            (plugin, "BOT_ID", BOT_ID),
            (plugin, "BOT_USERNAME", BOT_USERNAME),
            (plugin.tg_raw, "answer_guest", answer_guest),
            (
                plugin.tg_raw,
                "InlineEditor",
                _FakeEditor(self.edits, refuse_rich=self.refuse_rich),
            ),
            (plugin.llm_chat_config, "load_config", lambda: config),
            (plugin.util, "isAdmin", AsyncMock(return_value=self.admin)),
            (plugin, "get_effective_api_key", lambda user_id, service: self.api_key),
            (plugin, "_generate_response", self.generate),
            (plugin, "_guest_claims", guest_util.QueryClaims()),
            (plugin, "_guest_limiter", guest_util.CallLimiter()),
            (plugin, "_guest_threads", guest_util.GuestThreadStore()),
        ):
            stack.enter_context(patch.object(target, name, value))

    def run_query(self, query):
        asyncio.run(plugin.guest_chat_handler(query))
        self.assertEqual(self.borg.forbidden, [])

    def request(self):
        (call,) = self.generate.await_args_list
        return call.args[0]


class AnswerTests(_GuestTestCase):
    def test_an_explicit_call_streams_then_ends_as_rich_markdown(self):
        self.run_query(_query(f"{BOT_USERNAME} hi\nWhat's eth price?"))

        self.assertEqual([a.text for a in self.answers], [plugin.GUEST_PLACEHOLDER])
        request = self.request()
        self.assertIs(request.surface, plugin.GenerationSurface.GUEST)
        self.assertFalse(request.image_generation)
        self.assertIsInstance(request.response_message, guest_util.GuestAnswerMessage)
        self.assertEqual(self.edits, [{"markdown": self.answer_text}])

    def test_the_history_is_the_query_without_the_mention_under_the_guest_prompt(
        self,
    ):
        self.run_query(
            _query(f"{BOT_USERNAME} what does this mean?", reference_text="ciao")
        )

        system, *turns = self.request().messages
        self.assertIn(plugin.GUEST_CHAT_PROMPT, system["content"])
        self.assertIn(plugin.RICH_MARKDOWN_PROMPT, system["content"])
        self.assertNotIn(plugin.TELEGRAM_MARKDOWN_PROMPT, system["content"])
        self.assertNotIn(plugin.GROUP_CHAT_ETIQUETTE_PROMPT, system["content"])
        text = str(turns)
        self.assertIn("ciao", text)
        self.assertIn("what does this mean?", text)
        self.assertNotIn(BOT_USERNAME, text)

    def test_an_image_model_is_swapped_for_the_default(self):
        with patch.object(
            plugin,
            "_resolve_request_model",
            return_value=SimpleNamespace(model=IMAGE_MODEL),
        ):
            self.run_query(_query(f"{BOT_USERNAME} draw a cat"))

        self.assertEqual(self.request().model, plugin.DEFAULT_MODEL)

    def test_an_empty_answer_says_so(self):
        self.answer_text = ""

        self.run_query(_query(f"{BOT_USERNAME} hi"))

        self.assertIn("no answer", self.edits[-1]["markdown"])


class ContinuationTests(_GuestTestCase):
    answer_text = "ETH is at $1."

    def _reply_to_answer(self, text, *, date, query_id=2):
        return _query(
            text,
            reference_text=self.answer_text,
            reference_from=BOT_ID,
            reference_date=date,
            query_id=query_id,
        )

    def test_a_reply_to_an_answer_continues_its_exchange(self):
        self.run_query(_query(f"{BOT_USERNAME} what's eth price?", query_id=1))
        self.answer_text = "BTC is at $2."
        self.run_query(
            self._reply_to_answer(
                "and btc?", date=datetime.datetime.now(datetime.timezone.utc)
            )
        )

        first, second = [call.args[0] for call in self.generate.await_args_list]
        _system, *turns = second.messages
        #: Stored turns keep their metadata prefix, as live turns have one.
        self.assertEqual([t["role"] for t in turns[:2]], ["user", "assistant"])
        self.assertIn("what's eth price?", turns[0]["content"])
        self.assertEqual(turns[1]["content"], "ETH is at $1.")
        self.assertEqual(turns[2]["role"], "user")
        self.assertIn("and btc?", str(turns[2]["content"]))
        self.assertEqual(len(turns), 3)
        records = asyncio.run(
            plugin._guest_threads.records(plugin._guest_thread_name(_query("x")))
        )
        self.assertEqual(records[0]["parent"], records[1]["id"])
        self.assertEqual(records[0]["answer"], "BTC is at $2.")

    def test_another_guest_bots_answer_is_not_taken_for_ours(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        self.run_query(_query(f"{BOT_USERNAME} what's eth price?", query_id=1))

        self.run_query(
            _query(
                "is this right?",
                reference_text="The earth is flat.",
                reference_from=BOT_ID + 1,
                reference_date=now,
                reference_via_guest=True,
                query_id=2,
            )
        )

        _system, *turns = self.generate.await_args_list[-1].args[0].messages
        self.assertEqual([t["role"] for t in turns], ["user", "user"], msg=str(turns))
        self.assertIn("The earth is flat.", str(turns[0]["content"]))

    def test_a_guest_answer_without_a_sender_counts_as_ours(self):
        self.run_query(
            _query(
                "and btc?",
                reference_text=self.answer_text,
                reference_from=None,
                reference_via_guest=True,
                query_id=2,
            )
        )

        _system, *turns = self.request().messages
        self.assertEqual(turns[0]["role"], "assistant", msg=str(turns))

    def test_an_unmatched_answer_is_still_read_as_the_assistants(self):
        long_ago = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)

        self.run_query(self._reply_to_answer("and btc?", date=long_ago))

        _system, *turns = self.request().messages
        self.assertEqual(
            [t["role"] for t in turns], ["assistant", "user"], msg=str(turns)
        )
        self.assertIn(self.answer_text, str(turns[0]["content"]))


class RichFallbackTests(_GuestTestCase):
    refuse_rich = True

    def test_a_refused_rich_answer_is_sent_as_classic_markdown(self):
        self.run_query(_query(f"{BOT_USERNAME} hi"))

        rich, classic = self.edits
        self.assertIn("markdown", rich)
        self.assertEqual(classic, {"text": self.answer_text, "parse_mode": "md"})


class LongAnswerTests(_GuestTestCase):
    answer_text = "word " * 10000

    def test_a_long_answer_is_cut_to_the_rich_limit_with_a_note(self):
        self.run_query(_query(f"{BOT_USERNAME} hi"))

        markdown = self.edits[-1]["markdown"]
        self.assertLessEqual(
            len(markdown.encode("utf-8")), plugin.GUEST_RICH_LIMIT_BYTES
        )
        self.assertTrue(markdown.endswith(plugin.GUEST_TRUNCATED_NOTE))


class InviteTests(_GuestTestCase):
    api_key = None

    def test_every_explicit_call_without_a_key_gets_the_invite(self):
        for query_id in (1, 2):
            self.run_query(_query(f"{BOT_USERNAME} hi", query_id=query_id))

        self.assertEqual([a.text for a in self.answers], [plugin.GUEST_INVITE_TEXT] * 2)
        ((button,),) = self.answers[0].buttons
        self.assertEqual(
            tg_compat.button_url(button), "https://t.me/vlm_test_bot?start=guest"
        )
        self.generate.assert_not_awaited()

    def test_implicit_calls_get_the_invite_once(self):
        for query_id in (1, 2):
            self.run_query(_query("thanks!", query_id=query_id))

        self.assertEqual([a.text for a in self.answers], [plugin.GUEST_INVITE_TEXT])


class PolicyOffTests(_GuestTestCase):
    policy = llm_chat_config.GUEST_OFF

    def test_an_explicit_call_is_told_and_an_implicit_one_is_not(self):
        self.run_query(_query(f"{BOT_USERNAME} hi", query_id=1))
        self.run_query(_query("thanks!", query_id=2))

        self.assertEqual(
            [a.text for a in self.answers],
            ["Guest answers are turned off for this bot."],
        )
        self.generate.assert_not_awaited()


class AdminsOnlyTests(_GuestTestCase):
    policy = llm_chat_config.GuestConfig(policy=llm_chat_config.GuestPolicy.ADMINS)

    def test_a_non_admin_is_refused(self):
        self.run_query(_query(f"{BOT_USERNAME} hi"))

        self.assertEqual([a.text for a in self.answers], ["Not available here."])
        self.generate.assert_not_awaited()


class RateLimitTests(_GuestTestCase):
    policy = llm_chat_config.GuestConfig(max_calls_per_hour=1)

    def test_a_caller_over_the_limit_is_told(self):
        self.run_query(_query(f"{BOT_USERNAME} hi", query_id=1))
        self.run_query(_query(f"{BOT_USERNAME} again", query_id=2))

        self.assertEqual(self.generate.await_count, 1)
        self.assertIn("1 answers this hour", self.answers[-1].text)


class AdminRateLimitTests(RateLimitTests):
    admin = True

    def test_a_caller_over_the_limit_is_told(self):
        self.run_query(_query(f"{BOT_USERNAME} hi", query_id=1))
        self.run_query(_query(f"{BOT_USERNAME} again", query_id=2))

        self.assertEqual(self.generate.await_count, 2)


class SurfaceTests(unittest.TestCase):
    def _request(self, **overrides):
        event = guest_util.GuestEvent(_query(f"{BOT_USERNAME} hi"))
        fields = dict(
            event=event,
            response_message=AsyncMock(),
            messages=[{"role": "user", "content": "hi"}],
            model=plugin.DEFAULT_MODEL,
            model_capabilities={},
            api_key="k",
            prefs=SimpleNamespace(json_mode=False, enabled_tools=[]),
            prefix_effort=None,
            image_generation=False,
            warnings=[],
            surface=plugin.GenerationSurface.GUEST,
        )
        fields.update(overrides)
        return plugin.GenerationRequest(**fields)

    def test_a_guest_request_for_an_image_model_is_refused(self):
        for overrides in (
            {"model": IMAGE_MODEL},
            {"image_generation": True},
            {"model_capabilities": {"image_generation": True}},
        ):
            with self.subTest(**overrides):
                with self.assertRaises(ValueError):
                    asyncio.run(plugin._generate_response(self._request(**overrides)))

    def test_an_unknown_surface_is_refused(self):
        with self.assertRaises(ValueError):
            asyncio.run(plugin._generate_response(self._request(surface="inline")))

    def test_a_guest_codex_usage_limit_gets_one_line_and_no_panel(self):
        error = codex_util.CodexStreamError(
            "Error code: 429",
            codex_util.CodexResponse(text="partial"),
            usage_limit=codex_util.CodexUsageLimit(plan_type="prolite"),
        )
        edit = AsyncMock()
        with ExitStack() as stack:
            stack.enter_context(
                patch.object(
                    plugin.codex_util,
                    "stream_codex_response",
                    AsyncMock(side_effect=error),
                )
            )
            stack.enter_context(patch.object(plugin.util, "edit_message", edit))
            panel = stack.enter_context(
                patch.object(plugin, "_show_codex_quota_panel", AsyncMock())
            )
            stack.enter_context(patch.object(plugin, "ACTIVE_LLM_TASKS", {}))
            result = asyncio.run(
                plugin._generate_response(self._request(model=plugin.OPENAI_CODEX_SOL))
            )

        self.assertTrue(result.delivered)
        panel.assert_not_awaited()
        text = edit.await_args.args[1]
        self.assertTrue(text.startswith("partial"))
        self.assertIn("Codex usage limit reached", text)


class SharedHelperTests(unittest.TestCase):
    def test_guest_media_cache_keys_include_the_caller(self):
        query = _query(f"{BOT_USERNAME} hi")
        #: A photo's id is on `.photo`, so the key part is "unknown": without
        #: the caller, any two messages with the same chat and message id
        #: would share a cache entry.
        query.trigger.media = types.MessageMediaPhoto(photo=types.PhotoEmpty(id=5))

        self.assertEqual(
            plugin._media_cache_key(query.trigger),
            f"guest_{CALLER}_{OTHER}_20_unknown",
        )

    def test_admin_details_never_reach_a_guest_answer(self):
        event = guest_util.GuestEvent(_query(f"{BOT_USERNAME} hi"))
        with patch.object(llm_util.util, "isAdmin", AsyncMock(return_value=True)):
            self.assertFalse(asyncio.run(llm_util.may_show_admin_details(event)))
            self.assertTrue(
                asyncio.run(llm_util.may_show_admin_details(SimpleNamespace()))
            )

    def test_an_echoed_guest_answer_is_not_a_chat_message(self):
        echo = SimpleNamespace(
            out=True, guestchat_via_from=types.PeerUser(CALLER), media=None
        )
        event = SimpleNamespace(
            message=echo,
            text=f"{BOT_USERNAME} said hi",
            media=None,
            forward=None,
            is_private=False,
        )
        with patch.object(plugin, "BOT_USERNAME", BOT_USERNAME):
            self.assertFalse(asyncio.run(plugin.is_valid_chat_message(event)))


class GuestThreadStoreTests(unittest.TestCase):
    def test_records_are_newest_first_and_expire(self):
        now = [100.0]
        store = guest_util.GuestThreadStore(
            ttl_seconds=10, max_records=2, clock=lambda: now[0]
        )
        for n in range(3):
            asyncio.run(store.add("t", {"id": str(n), "answered_at": n}))

        self.assertEqual([r["id"] for r in asyncio.run(store.records("t"))], ["2", "1"])
        self.assertEqual(asyncio.run(store.records("other")), [])
        now[0] = 111.0
        self.assertEqual(asyncio.run(store.records("t")), [])

    def test_expired_records_stay_gone_after_a_new_answer(self):
        now = [100.0]
        store = guest_util.GuestThreadStore(ttl_seconds=10, clock=lambda: now[0])
        asyncio.run(store.add("t", {"id": "old"}))
        asyncio.run(store.add("u", {"id": "other"}))
        now[0] = 120.0

        asyncio.run(store.add("t", {"id": "new"}))

        self.assertEqual([r["id"] for r in asyncio.run(store.records("t"))], ["new"])
        self.assertNotIn("u", store._memory)

    def test_the_redis_list_is_used_when_there_is_a_connection(self):
        calls = []

        class _Redis:
            async def lpush(self, name, value):
                calls.append(("lpush", name))

            async def ltrim(self, name, start, end):
                calls.append(("ltrim", name, start, end))

            async def expire(self, name, ttl):
                calls.append(("expire", name, ttl))

            async def lrange(self, name, start, end):
                return [b'{"id": "r", "answered_at": 1}']

        async def get_redis():
            return _Redis()

        store = guest_util.GuestThreadStore(get_redis=get_redis, max_records=5)
        asyncio.run(store.add("t", {"id": "x", "answered_at": 1}))

        name = "borg:guest:thread:t"
        self.assertEqual(
            calls,
            [("lpush", name), ("ltrim", name, 0, 4), ("expire", name, 7 * 86400)],
        )
        self.assertEqual(
            asyncio.run(store.records("t")), [{"id": "r", "answered_at": 1}]
        )

    def test_find_answer_takes_the_closest_within_the_tolerance(self):
        records = [{"id": "a", "answered_at": 100}, {"id": "b", "answered_at": 103}]

        self.assertEqual(guest_util.find_answer(records, answered_at=102.5)["id"], "b")
        self.assertIsNone(guest_util.find_answer(records, answered_at=120))

    def test_answer_chain_follows_parents_oldest_first_within_the_limit(self):
        records = [
            {"id": "c", "parent": "b"},
            {"id": "b", "parent": "a"},
            {"id": "a", "parent": None},
        ]

        self.assertEqual(
            [r["id"] for r in guest_util.answer_chain(records, records[0])],
            ["a", "b", "c"],
        )
        self.assertEqual(
            [r["id"] for r in guest_util.answer_chain(records, records[0], limit=2)],
            ["b", "c"],
        )


class GuestCallLimiterTests(unittest.TestCase):
    def test_counts_per_key_and_window(self):
        now = [0.0]
        limiter = guest_util.CallLimiter(window_seconds=10, clock=lambda: now[0])

        def allow(key):
            return asyncio.run(limiter.allow(key, limit=2))

        self.assertEqual(
            [allow("a"), allow("a"), allow("a"), allow("b")], [True] * 2 + [False, True]
        )
        now[0] = 10.0
        self.assertTrue(allow("a"))

    def test_the_backend_count_wins_and_a_failing_backend_falls_back(self):
        async def shared(key, ttl):
            return 5

        async def broken(key, ttl):
            raise ConnectionError("down")

        self.assertFalse(
            asyncio.run(guest_util.CallLimiter(backend=shared).allow("a", limit=2))
        )
        with self.assertLogs(guest_util._log, level="WARNING"):
            self.assertTrue(
                asyncio.run(guest_util.CallLimiter(backend=broken).allow("a", limit=2))
            )

    def test_the_redis_backend_increments_and_expires(self):
        calls = []

        class _Redis:
            async def incr(self, name):
                calls.append(("incr", name))
                return 3

            async def expire(self, name, ttl):
                calls.append(("expire", name, ttl))

        async def get_redis():
            return _Redis()

        backend = guest_util.redis_counter_backend(get_redis)

        self.assertEqual(asyncio.run(backend("k:1", 3600)), 3)
        self.assertEqual(
            calls,
            [
                ("incr", "borg:guest:count:k:1"),
                ("expire", "borg:guest:count:k:1", 3600),
            ],
        )


if __name__ == "__main__":
    unittest.main()
