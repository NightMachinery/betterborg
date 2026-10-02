"""The chat bot's guest answers (`guest_chat_handler` in llm_chat).

Terms are those of docs/guest_mode.md: a *guest query* carries the *trigger*
(the message that mentioned the bot) and at most one *reference* (the message
it replies to); the bot answers once and then edits that *guest answer*. An
*explicit* call mentions the bot; a reply to the answer without a mention is
*implicit*.
"""

import asyncio
import base64
import builtins
import datetime
import importlib
import logging
from contextlib import ExitStack
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from telethon import errors, types
from telethon._updates import EntityCache

from uniborg import (
    codex_util,
    guest_util,
    llm_chat_config,
    llm_util,
    media_store,
    tg_compat,
)

BOT_ID = 4242
BOT_USERNAME = "@vlm_test_bot"
CALLER = 777001
OTHER = 777002
GROUP = types.PeerChannel(990011)
NOW = datetime.datetime.now(datetime.timezone.utc)
IMAGE_MODEL = "gemini/gemini-2.5-flash-image-preview"
#: A 1x1 PNG.
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kg"
    "AAAABJRU5ErkJggg=="
)


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
    chat=None,
    reference_text=None,
    reference_from=OTHER,
    reference_date=None,
    reference_via_guest=False,
    reference_edited=False,
    reference_grouped_id=None,
    query_id=1,
    trigger_id=20,
    trigger_photo=None,
    photo_id=55,
    grouped_id=None,
):
    client = SimpleNamespace(
        _self_id=BOT_ID, _mb_entity_cache=EntityCache(), parse_mode=None
    )
    #: The chat as the caller sees it: in a private chat, the other person.
    chat = chat or types.PeerUser(OTHER)
    trigger = types.Message(
        id=trigger_id,
        peer_id=chat,
        date=NOW,
        message=text,
        from_id=types.PeerUser(caller),
        out=True,
        grouped_id=grouped_id,
        media=(
            types.MessageMediaPhoto(
                photo=types.Photo(
                    id=photo_id,
                    access_hash=1,
                    file_reference=b"",
                    date=NOW,
                    sizes=[],
                    dc_id=2,
                )
            )
            if trigger_photo is not None
            else None
        ),
    )
    references = []
    if reference_text is not None:
        reference = types.Message(
            id=19,
            peer_id=chat,
            date=reference_date or NOW,
            edit_date=NOW if reference_edited else None,
            grouped_id=reference_grouped_id,
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
    if isinstance(chat, types.PeerChannel):
        update._entities[chat.channel_id] = types.Channel(
            id=chat.channel_id,
            title="Group",
            photo=types.ChatPhotoEmpty(),
            date=NOW,
            megagroup=True,
        )
    query = guest_util.guest_query_from_update(update, client=client)
    if trigger_photo is not None:

        async def download_media(file):
            path = Path(file) / f"photo{photo_id}.png"
            path.write_bytes(trigger_photo)
            return str(path)

        query.trigger.download_media = download_media
    return query


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

    async def upload_photo(self, data, *, file_name):
        return f"photo:{file_name}"


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
        media_dir = stack.enter_context(tempfile.TemporaryDirectory())
        self.media = media_store.MediaStore(Path(media_dir) / "media.sqlite3")
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
            (plugin, "_guest_albums", guest_util.AlbumBatcher(wait_seconds=0.01)),
            (plugin, "_guest_media", self.media),
        ):
            stack.enter_context(patch.object(target, name, value))

    def run_query(self, query):
        asyncio.run(plugin.guest_chat_handler(query))
        self.assertEqual(self.borg.forbidden, [])

    def run_queries(self, *queries):
        """Runs QUERIES at once, as an album's queries arrive."""

        async def run():
            await asyncio.gather(*(plugin.guest_chat_handler(q) for q in queries))

        asyncio.run(run())
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

    def test_an_image_model_answers_as_itself(self):
        with ExitStack() as stack:
            stack.enter_context(
                patch.object(
                    plugin,
                    "_resolve_request_model",
                    return_value=SimpleNamespace(model=IMAGE_MODEL),
                )
            )
            stack.enter_context(
                patch.object(
                    plugin, "_can_user_access_model", AsyncMock(return_value=True)
                )
            )
            self.run_query(_query(f"{BOT_USERNAME} draw a cat"))

        self.assertEqual(self.request().model, IMAGE_MODEL)

    def test_an_empty_answer_says_so(self):
        self.answer_text = ""

        self.run_query(_query(f"{BOT_USERNAME} hi"))

        self.assertIn("no answer", self.edits[-1]["markdown"])


class _ImageGenerationCase(_GuestTestCase):
    imagegen = True

    def setUp(self):
        super().setUp()
        for name in ("can_use_codex", "can_use_codex_imagegen"):
            patcher = patch.object(
                plugin.llm_chat_config, name, AsyncMock(return_value=self.imagegen)
            )
            patcher.start()
            self.addCleanup(patcher.stop)

    def answer_with_image(self, text):
        async def generate(req):
            await req.response_message.show_image(b"png", file_name="cat.png")
            return plugin.GenerationResult(
                text=text, finish_reason="stop", has_image=True
            )

        self.generate.side_effect = generate
        self.run_query(_query(f"{BOT_USERNAME} .i draw a cat"))


class ImageGenerationTests(_ImageGenerationCase):
    def test_dot_i_generates_on_codex(self):
        self.run_query(_query(f"{BOT_USERNAME} .i draw a cat"))

        request = self.request()
        self.assertTrue(request.image_generation)
        self.assertEqual(request.model, plugin.OPENAI_CODEX_SOL)

    def test_the_image_stays_and_the_answer_is_its_caption(self):
        self.answer_with_image("x" * 3000)

        shown, final = self.edits
        self.assertEqual(shown["media"], "photo:cat.png")
        self.assertEqual(final["parse_mode"], "md")
        self.assertNotIn("media", final)
        self.assertLessEqual(len(final["text"]), guest_util.CAPTION_LIMIT_UNITS)
        self.assertTrue(final["text"].endswith(plugin.GUEST_TRUNCATED_NOTE))

    def test_an_image_without_text_gets_no_caption(self):
        self.answer_with_image("")

        self.assertEqual(self.edits[-1], {"text": "", "parse_mode": "md"})


class ImageGenerationRefusedTests(_ImageGenerationCase):
    imagegen = False

    def test_dot_i_without_access_is_refused_where_explicit(self):
        self.run_query(_query(f"{BOT_USERNAME} .i draw a cat", query_id=1))
        self.run_query(_query(".i draw a dog", query_id=2))

        self.assertEqual(
            [a.text for a in self.answers], [plugin.CODEX_IMAGEGEN_ACCESS_DENIED]
        )
        self.generate.assert_not_awaited()


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


class SeenMessageContinuationTests(_GuestTestCase):
    """A reply to any message an earlier answer saw, not only to the answer."""

    answer_text = "ETH is at $1."
    question = f"{BOT_USERNAME} what's eth price?"

    def turns(self, call=-1):
        _system, *turns = self.generate.await_args_list[call].args[0].messages
        return turns

    def test_a_reply_to_your_own_earlier_question_continues_its_exchange(self):
        self.run_query(_query(self.question, query_id=1))
        self.answer_text = "BTC is at $2."

        #: The question comes back with another id, as the other side sees it.
        self.run_query(
            _query(
                "and btc?",
                reference_text=self.question,
                reference_from=CALLER,
                query_id=2,
            )
        )

        turns = self.turns()
        self.assertEqual([t["role"] for t in turns], ["user", "assistant", "user"])
        self.assertIn("what's eth price?", str(turns[0]["content"]))
        self.assertEqual(turns[1]["content"], "ETH is at $1.")
        self.assertIn("and btc?", str(turns[2]["content"]))
        records = asyncio.run(
            plugin._guest_threads.records(plugin._guest_thread_name(_query("x")))
        )
        self.assertEqual(records[0]["parent"], records[1]["id"])

    def test_a_reply_to_what_an_earlier_question_replied_to_continues_it(self):
        self.run_query(_query(self.question, reference_text="Prices today", query_id=1))

        self.run_query(_query("and btc?", reference_text="Prices today", query_id=2))

        turns = self.turns()
        self.assertEqual(
            [t["role"] for t in turns], ["user", "user", "assistant", "user"]
        )
        self.assertIn("Prices today", str(turns[0]["content"]))

    def test_a_second_reply_to_a_question_leaves_out_the_first_replys_branch(self):
        self.run_query(_query(self.question, query_id=1))
        self.answer_text = "BTC is at $2."
        for query_id, text in ((2, "and btc?"), (3, "in euros?")):
            self.run_query(
                _query(
                    text,
                    reference_text=self.question,
                    reference_from=CALLER,
                    query_id=query_id,
                )
            )

        turns = self.turns()
        self.assertEqual([t["role"] for t in turns], ["user", "assistant", "user"])
        self.assertEqual(turns[1]["content"], "ETH is at $1.")
        self.assertNotIn("and btc?", str(turns))

    def assert_another_callers_reply_starts_fresh(self, *, chat, other_chat):
        """CALLER asks in CHAT about a message; OTHER replies to it from OTHER_CHAT."""
        self.run_query(
            _query(self.question, chat=chat, reference_text="Prices today", query_id=1)
        )

        self.run_query(
            _query(
                f"{BOT_USERNAME} is this cheap?",
                caller=OTHER,
                chat=other_chat,
                reference_text="Prices today",
                query_id=2,
            )
        )

        self.assertEqual([t["role"] for t in self.turns()], ["user", "user"])

    def test_another_callers_reply_to_the_same_message_starts_fresh_in_a_group(self):
        self.assert_another_callers_reply_starts_fresh(chat=GROUP, other_chat=GROUP)

    def test_the_other_persons_reply_to_the_same_message_starts_fresh(self):
        #: Each side of a private chat sees the other person as the chat.
        self.assert_another_callers_reply_starts_fresh(
            chat=types.PeerUser(OTHER), other_chat=types.PeerUser(CALLER)
        )

    def test_another_caller_can_continue_from_the_question_itself(self):
        self.run_query(_query(self.question, chat=GROUP, query_id=1))

        self.run_query(
            _query(
                f"{BOT_USERNAME} and in euros?",
                caller=OTHER,
                chat=GROUP,
                reference_text=self.question,
                reference_from=CALLER,
                query_id=2,
            )
        )

        self.assertEqual(
            [t["role"] for t in self.turns()], ["user", "assistant", "user"]
        )

    def test_an_edited_question_starts_fresh_with_its_current_text(self):
        self.run_query(_query(self.question, query_id=1))

        self.run_query(
            _query(
                "and btc?",
                reference_text=f"{BOT_USERNAME} what's sol price?",
                reference_from=CALLER,
                reference_edited=True,
                query_id=2,
            )
        )

        turns = self.turns()
        self.assertEqual([t["role"] for t in turns], ["user", "user"])
        self.assertIn("what's sol price?", str(turns[0]["content"]))

    def test_an_unedited_message_of_the_same_second_is_not_taken_for_it(self):
        self.run_query(_query(self.question, query_id=1))

        self.run_query(
            _query(
                "is this right?",
                reference_text="Something else, sent the same second",
                reference_from=CALLER,
                query_id=2,
            )
        )

        self.assertEqual([t["role"] for t in self.turns()], ["user", "user"])

    def build_chain(self, length):
        """Answers `question 0`, then replies to each answer LENGTH times."""
        self.answer_text = "answer 0"
        self.run_query(_query(f"{BOT_USERNAME} question 0", query_id=1))
        for n in range(1, length + 1):
            self.run_query(
                _query(
                    f"question {n}",
                    reference_text=f"answer {n - 1}",
                    reference_from=BOT_ID,
                    reference_date=datetime.datetime.now(datetime.timezone.utc),
                    query_id=n + 1,
                )
            )
            self.answer_text = f"answer {n}"

    def test_a_chain_is_not_cut_at_ten_exchanges(self):
        self.build_chain(12)

        turns = self.turns()
        self.assertEqual(len(turns), 2 * 12 + 1)
        self.assertIn("question 0", str(turns[0]["content"]))

    def test_a_long_chain_reaches_the_model_within_the_history_limit(self):
        #: Each record is one question and its answer: two turns.
        with patch.object(plugin, "HISTORY_MESSAGE_LIMIT", 4):
            self.build_chain(4)

        turns = self.turns()
        self.assertEqual(len(turns), 2 * 2 + 1)
        self.assertIn("question 2", str(turns[0]["content"]))

    def test_the_chain_keeps_the_newest_exchanges_within_the_history_limit(self):
        chain = [{"id": str(n), "turns": [{"role": "user"}]} for n in range(5)]

        with patch.object(plugin, "HISTORY_MESSAGE_LIMIT", 4):
            kept = plugin._within_history_limit(chain)

        self.assertEqual([r["id"] for r in kept], ["3", "4"])


class _MediaTestCase(_GuestTestCase):
    """Turns media into model parts without python-magic or the history cache."""

    answer_text = "A dot."

    def setUp(self):
        super().setUp()
        for target, name, value in (
            (plugin, "is_native_gemini_files_mode", lambda model: False),
            #: python-magic is not installed everywhere the tests run.
            (plugin, "mime_guess", lambda path: "image/png"),
            (plugin.history_util, "get_cached_file", AsyncMock(return_value=None)),
            (plugin.history_util, "cache_file", AsyncMock(return_value=True)),
        ):
            patcher = patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)


class AlbumTests(_MediaTestCase):
    """An album sends a query per item; a reply to an item brings that item alone."""

    def test_an_album_sent_as_a_reply_to_the_answer_gets_one_answer(self):
        self.run_query(_query(f"{BOT_USERNAME} hi", query_id=1))
        replied_at = datetime.datetime.now(datetime.timezone.utc)

        self.run_queries(
            *(
                _query(
                    "",
                    reference_text=self.answer_text,
                    reference_from=BOT_ID,
                    reference_date=replied_at,
                    query_id=10 + n,
                    trigger_id=30 + n,
                    trigger_photo=PNG,
                    photo_id=60 + n,
                    grouped_id=99,
                )
                for n in range(3)
            )
        )

        self.assertEqual(len(self.answers), 2)
        self.assertEqual(len(self.generate.await_args_list), 2)
        _system, *turns = self.generate.await_args_list[-1].args[0].messages
        images = [
            part
            for turn in turns[2:]
            if isinstance(turn["content"], list)
            for part in turn["content"]
            if part["type"] == "image_url"
        ]
        self.assertEqual(len(images), 3)
        self.assertEqual(turns[1]["content"], self.answer_text)
        self.assertNotIn(guest_util.ALBUM_TRIGGER_NOTE, self.edits[-1]["markdown"])

    def test_a_reply_to_an_album_item_says_only_that_item_was_seen(self):
        self.run_query(
            _query(
                f"{BOT_USERNAME} what is this?",
                reference_text="",
                reference_grouped_id=7,
            )
        )

        self.assertEqual(
            self.edits[-1]["markdown"],
            f"{self.answer_text}\n\n{guest_util.ALBUM_REFERENCE_NOTE}",
        )

    def test_a_lone_album_item_says_so(self):
        self.run_query(
            _query(f"{BOT_USERNAME} what is this?", trigger_photo=PNG, grouped_id=7)
        )

        self.assertTrue(
            self.edits[-1]["markdown"].endswith(guest_util.ALBUM_TRIGGER_NOTE)
        )


class MediaContinuationTests(_MediaTestCase):
    """A reply brings back the media of its exchange, from the store on disk."""

    def reply(self, text):
        self.run_query(
            _query(
                text,
                reference_text=self.answer_text,
                reference_from=BOT_ID,
                reference_date=datetime.datetime.now(datetime.timezone.utc),
                query_id=2,
            )
        )
        return self.generate.await_args_list[-1].args[0].messages

    def test_a_reply_brings_back_the_photo_it_asked_about(self):
        self.run_query(
            _query(f"{BOT_USERNAME} what is this?", trigger_photo=PNG, query_id=1)
        )
        _system, user, assistant, reply = self.reply("and its colour?")

        (photo,) = [p for p in user["content"] if p["type"] == "image_url"]
        self.assertEqual(
            photo["image_url"]["url"],
            f"data:image/png;base64,{base64.b64encode(PNG).decode()}",
        )
        self.assertIn("what is this?", str(user["content"]))
        self.assertEqual(assistant["content"], "A dot.")
        self.assertIn("and its colour?", str(reply["content"]))

    def test_a_reply_brings_back_the_image_the_answer_showed(self):
        async def generate(req):
            await req.response_message.show_image(PNG, file_name="dot.png")
            return plugin.GenerationResult(
                text="Here.", finish_reason="stop", has_image=True
            )

        self.generate.side_effect = generate
        self.run_query(_query(f"{BOT_USERNAME} draw a dot", query_id=1))
        self.generate.side_effect = None
        self.generate.return_value = plugin.GenerationResult(
            text="Red.", finish_reason="stop", has_image=False
        )
        _system, _user, assistant, _reply = self.reply("what colour?")

        image, text = assistant["content"]
        self.assertEqual(image["type"], "image_url")
        self.assertIn(base64.b64encode(PNG).decode(), image["image_url"]["url"])
        self.assertEqual(text, {"type": "text", "text": "Here."})

    def test_media_the_store_lost_becomes_a_marker(self):
        self.run_query(
            _query(f"{BOT_USERNAME} what is this?", trigger_photo=PNG, query_id=1)
        )
        self.media.path.unlink()
        _system, user, _assistant, _reply = self.reply("and its colour?")

        self.assertIn(plugin.GUEST_MEDIA_MARKER, user["content"])
        self.assertIn("what is this?", user["content"])


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

    def test_codex_images_become_the_guest_answers_photo(self):
        edits = []
        answer = guest_util.GuestAnswerMessage(
            _FakeEditor(edits), min_interval=0, logger=logging.getLogger("test")
        )

        async def stream(*, image_callback, **kwargs):
            await image_callback(codex_util.CodexImage(b"p", "a", preview_index=0))
            await image_callback(codex_util.CodexImage(b"f", "a"))
            return codex_util.CodexResponse(text="", images_delivered=1)

        with ExitStack() as stack:
            stack.enter_context(
                patch.object(plugin.codex_util, "stream_codex_response", stream)
            )
            stack.enter_context(patch.object(plugin, "ACTIVE_LLM_TASKS", {}))
            result = asyncio.run(
                plugin._generate_response(
                    self._request(
                        model=plugin.OPENAI_CODEX_SOL,
                        image_generation=True,
                        response_message=answer,
                    )
                )
            )

        self.assertTrue(result.has_image)
        self.assertEqual(
            [e["media"] for e in edits],
            ["photo:codex_preview_1.png", "photo:codex_generated_image_2.png"],
        )
        self.assertEqual(answer.media, "photo:codex_generated_image_2.png")

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


class GuestMediaCleanupTests(unittest.TestCase):
    def register(self, registered):
        tasks = []

        def create_task(coro):
            coro.close()
            tasks.append(Mock())
            return tasks[-1]

        fake_borg = SimpleNamespace(loop=SimpleNamespace(create_task=create_task))
        with ExitStack() as stack:
            for target, name, value in (
                (builtins, "borg", fake_borg),
                (builtins, "logger", logging.getLogger("test.guest")),
                (
                    plugin.guest_util,
                    "register_guest_handler",
                    lambda *args, **kwargs: registered,
                ),
            ):
                stack.enter_context(patch.object(target, name, value, create=True))
            plugin.register_guest_handlers()
            plugin.register_guest_handlers()
        return tasks

    def test_a_reload_replaces_the_cleanup_loop(self):
        first, second = self.register(object())

        first.cancel.assert_called_once_with()
        second.cancel.assert_not_called()

    def test_a_user_account_cleans_nothing_up(self):
        self.assertEqual(self.register(None), [])


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

    def test_a_message_has_one_fingerprint_on_both_sides_of_a_chat(self):
        def message(**kwargs):
            fields = dict(
                id=20,
                peer_id=types.PeerUser(OTHER),
                date=NOW,
                message="hi",
                from_id=types.PeerUser(CALLER),
            )
            fields.update(kwargs)
            return types.Message(**fields)

        mine = guest_util.message_fingerprint(message())
        theirs = guest_util.message_fingerprint(
            message(id=7, peer_id=types.PeerUser(CALLER))
        )
        edited = guest_util.message_fingerprint(message(message="hi!"))

        self.assertEqual(mine, theirs)
        self.assertNotEqual(mine.content, edited.content)
        self.assertEqual(mine.sender, edited.sender)
        self.assertNotIn(str(CALLER), str(mine.to_json()))

    def test_album_items_differ_by_their_photo_and_match_it_across_sides(self):
        def item(*, message_id, photo_id, peer):
            return types.Message(
                id=message_id,
                peer_id=peer,
                date=NOW,
                message="",
                from_id=types.PeerUser(OTHER),
                grouped_id=99,
                media=types.MessageMediaPhoto(
                    photo=types.Photo(
                        id=photo_id,
                        access_hash=1,
                        file_reference=b"",
                        date=NOW,
                        sizes=[],
                        dc_id=2,
                    )
                ),
            )

        first = guest_util.message_fingerprint(
            item(message_id=10, photo_id=111, peer=types.PeerUser(OTHER))
        )
        second = guest_util.message_fingerprint(
            item(message_id=11, photo_id=222, peer=types.PeerUser(OTHER))
        )
        first_elsewhere = guest_util.message_fingerprint(
            item(message_id=3, photo_id=111, peer=types.PeerUser(CALLER))
        )
        records = [
            {
                "id": "first",
                "seen": [guest_util.seen_entry(first, reference=False)],
            }
        ]

        self.assertNotEqual(first.content, second.content)
        self.assertEqual(first, first_elsewhere)
        self.assertIsNone(guest_util.find_seen(records, second, caller_id=CALLER))
        self.assertEqual(
            guest_util.find_seen(records, first_elsewhere, caller_id=CALLER)["id"],
            "first",
        )

    def test_find_seen_wants_date_content_and_any_known_sender(self):
        fp = guest_util.MessageFingerprint
        entry = lambda f: guest_util.seen_entry(f, reference=False)
        records = [
            {"id": "new", "seen": [entry(fp(date=10, content="other", sender="s1"))]},
            {"id": "old", "seen": [entry(fp(date=10, content="same", sender="s1"))]},
            {"id": "pre-seen"},
        ]

        def found(**kwargs):
            match = guest_util.find_seen(records, fp(**kwargs), caller_id=CALLER)
            return match and match["id"]

        self.assertEqual(found(date=10, content="same", sender="s1"), "old")
        self.assertEqual(found(date=10, content="same", sender=None), "old")
        self.assertIsNone(found(date=10, content="same", sender="s2"))
        self.assertIsNone(found(date=10, content="edited", sender="s1"))
        self.assertIsNone(found(date=11, content="same", sender="s1"))

    def test_find_seen_gives_a_reference_only_to_its_own_caller(self):
        fingerprint = guest_util.MessageFingerprint(date=10, content="c", sender="s")
        records = [
            {
                "id": "a",
                "caller_id": CALLER,
                "seen": [guest_util.seen_entry(fingerprint, reference=True)],
            }
        ]

        found = lambda caller_id: guest_util.find_seen(
            records, fingerprint, caller_id=caller_id
        )
        self.assertEqual(found(CALLER)["id"], "a")
        self.assertIsNone(found(OTHER))
        self.assertIsNone(found(None))

    def test_the_chat_bot_keeps_enough_records_for_a_full_chain(self):
        self.assertEqual(
            plugin._guest_threads._max_records, plugin.GUEST_THREAD_MAX_RECORDS
        )
        self.assertGreaterEqual(
            2 * plugin.GUEST_THREAD_MAX_RECORDS, plugin.HISTORY_MESSAGE_LIMIT
        )

    def test_answer_chain_follows_parents_oldest_first(self):
        records = [
            {"id": "c", "parent": "b"},
            {"id": "b", "parent": "a"},
            {"id": "a", "parent": None},
        ]

        self.assertEqual(
            [r["id"] for r in guest_util.answer_chain(records, records[0])],
            ["a", "b", "c"],
        )

    def test_answer_chain_stops_at_a_cycle(self):
        records = [{"id": "b", "parent": "a"}, {"id": "a", "parent": "b"}]

        self.assertEqual(
            [r["id"] for r in guest_util.answer_chain(records, records[0])],
            ["a", "b"],
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
