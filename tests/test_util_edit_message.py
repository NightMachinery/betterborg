import itertools
import unittest
from unittest import mock

from telethon import errors

from uniborg import util
from uniborg.constants import BOT_META_INFO_LINE, TWIN_FILE_MARKER


class _Chat:
    """Hands out fake messages and records every Telegram call they make, in order."""

    def __init__(self, chat_id=-1001):
        self.chat_id = chat_id
        self.calls = []
        self._ids = itertools.count(100)

    def message(self, text="...", *, reply_to_msg_id=None):
        return _FakeMessage(
            self, msg_id=next(self._ids), text=text, reply_to_msg_id=reply_to_msg_id
        )

    def ops(self):
        return [call[:3] for call in self.calls]


class _FakeMessage:
    """A Telethon-like message: like the real one, `edit()` never updates `.text`."""

    def __init__(self, chat, *, msg_id, text, reply_to_msg_id):
        self.chat = chat
        self.chat_id = chat.chat_id
        self.id = msg_id
        self.text = text
        self.reply_to_msg_id = reply_to_msg_id
        #: Raised, in order, by the next calls of that kind.
        self.edit_errors = []
        self.reply_errors = []
        self.respond_errors = []
        self.delete_errors = []

    async def edit(self, text, **kwargs):
        self.chat.calls.append(("edit", self.id, text, kwargs))
        if self.edit_errors:
            raise self.edit_errors.pop(0)
        return self.chat.message(text, reply_to_msg_id=self.reply_to_msg_id)

    async def reply(self, text, **kwargs):
        self.chat.calls.append(("reply", self.id, text, kwargs))
        if self.reply_errors:
            raise self.reply_errors.pop(0)
        return self.chat.message(text, reply_to_msg_id=self.id)

    async def respond(self, text, *, reply_to=None, **kwargs):
        self.chat.calls.append(
            ("respond", self.id, text, {"reply_to": reply_to, **kwargs})
        )
        if self.respond_errors:
            raise self.respond_errors.pop(0)
        return self.chat.message(
            text, reply_to_msg_id=getattr(reply_to, "id", reply_to)
        )

    async def delete(self):
        self.chat.calls.append(("delete", self.id, None, {}))
        if self.delete_errors:
            raise self.delete_errors.pop(0)

    async def get_chat(self):
        return self.chat


def _blocks(*letters, size=10):
    """Whitespace-free text that the splitter cuts exactly every `size` characters."""
    return "".join(letter * size for letter in letters)


class SplitMessageSmartTests(unittest.TestCase):
    def test_empty_text_has_no_chunks(self):
        self.assertEqual(util._split_message_smart(""), [])

    def test_short_text_is_one_chunk(self):
        self.assertEqual(
            util._split_message_smart("hello world", search_direction=0),
            ["hello world"],
        )

    def test_text_without_boundaries_is_cut_at_the_limit(self):
        chunks = util._split_message_smart(
            "x" * 10000, max_chunk_size=4096, search_direction=0
        )
        self.assertEqual([len(c) for c in chunks], [4096, 4096, 1808])

    def test_forward_search_takes_the_first_newline_in_the_buffer(self):
        #: Lines of 100 characters: newlines at 99, 199, ..., 4999.
        text = ("a" * 99 + "\n") * 50
        forward = util._split_message_smart(
            text, max_chunk_size=4096, search_direction=0
        )
        backward = util._split_message_smart(
            text, max_chunk_size=4096, search_direction=-1
        )
        self.assertEqual(len(forward[0]), 3499)
        self.assertEqual(len(backward[0]), 3999)
        for chunks in (forward, backward):
            self.assertTrue(all(len(c) <= 4096 for c in chunks))
            self.assertEqual("\n".join(chunks), text.rstrip())

    def test_forward_search_head_chunk_is_stable_once_past_the_limit(self):
        line = "b" * 99 + "\n"
        heads = {
            util._split_message_smart(
                line * n, max_chunk_size=4096, search_direction=0
            )[0]
            for n in (41, 45, 60, 80)
        }
        self.assertEqual(len(heads), 1)

    def test_forward_search_splits_text_within_the_buffer_of_the_limit(self):
        text = ("c" * 99 + "\n") * 36
        self.assertLess(len(text), 4096)
        chunks = util._split_message_smart(
            text, max_chunk_size=4096, search_direction=0
        )
        self.assertEqual(len(chunks), 2)

    def test_code_fences_are_not_balanced_across_chunks(self):
        body = "\n".join(f"line {i:04d} " + "z" * 80 for i in range(60))
        text = f"intro\n```python\n{body}\n```\noutro"
        chunks = util._split_message_smart(
            text, max_chunk_size=4096, search_direction=0
        )
        self.assertGreater(len(chunks), 1)
        self.assertEqual(chunks[0].count("```"), 1)
        self.assertEqual(chunks[-1].count("```"), 1)
        self.assertEqual("\n".join(chunks), text)


class ShouldSendAsFileTests(unittest.TestCase):
    def decide(self, text, mode, *, threshold=10, only_threshold=20):
        return util._should_send_as_file(text, threshold, mode, only_threshold)

    def test_never_sends_text_only(self):
        decision = self.decide("x" * 50, util.SendFileMode.NEVER)
        self.assertEqual((decision.send_text, decision.send_file), (True, False))

    def test_only_sends_a_file_instead_of_text_past_the_threshold(self):
        short = self.decide("x" * 5, util.SendFileMode.ONLY)
        long = self.decide("x" * 10, util.SendFileMode.ONLY)
        self.assertEqual((short.send_text, short.send_file), (True, False))
        self.assertEqual((long.send_text, long.send_file), (False, True))

    def test_also_adds_a_file_past_the_threshold(self):
        short = self.decide("x" * 5, util.SendFileMode.ALSO)
        long = self.decide("x" * 50, util.SendFileMode.ALSO)
        self.assertEqual((short.send_text, short.send_file), (True, False))
        self.assertEqual((long.send_text, long.send_file), (True, True))

    def test_also_if_less_than_drops_the_text_past_the_file_only_threshold(self):
        mode = util.SendFileMode.ALSO_IF_LESS_THAN
        short = self.decide("x" * 5, mode)
        middle = self.decide("x" * 15, mode)
        long = self.decide("x" * 20, mode)
        self.assertEqual((short.send_text, short.send_file), (True, False))
        self.assertEqual((middle.send_text, middle.send_file), (True, True))
        self.assertEqual((long.send_text, long.send_file), (False, True))

    def test_file_threshold_never_exceeds_the_file_only_threshold(self):
        decision = self.decide(
            "x" * 25,
            util.SendFileMode.ALSO_IF_LESS_THAN,
            threshold=100,
            only_threshold=20,
        )
        self.assertEqual((decision.send_text, decision.send_file), (False, True))

    def test_non_int_threshold_is_read_as_a_flag(self):
        always = self.decide("x", util.SendFileMode.ONLY, threshold=True)
        never = self.decide("x" * 99, util.SendFileMode.ONLY, threshold=None)
        self.assertEqual((always.send_text, always.send_file), (False, True))
        self.assertEqual((never.send_text, never.send_file), (True, False))

    def test_blank_text_sends_nothing(self):
        decision = self.decide("   ", util.SendFileMode.ALSO)
        self.assertEqual((decision.send_text, decision.send_file), (False, False))


class _EditChainCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        chains = mock.patch.dict(util.EDIT_CHAINS, {}, clear=True)
        chains.start()
        self.addCleanup(chains.stop)
        self.send_file = mock.AsyncMock()
        send_file = mock.patch.object(
            util, "send_as_file_with_filename", self.send_file
        )
        send_file.start()
        self.addCleanup(send_file.stop)
        self.chat = _Chat()
        self.head = self.chat.message("...")

    async def edit(self, text, **kwargs):
        kwargs.setdefault("parse_mode", "md")
        kwargs.setdefault("max_len", 10)
        await util.edit_message(self.head, text, **kwargs)

    def state(self):
        return util.EDIT_CHAINS.get(util._edit_chain_key(self.head))


class EditMessageTests(_EditChainCase):
    async def test_short_text_edits_only_the_head(self):
        await util.edit_message(self.head, "  hello  ", parse_mode="md")

        self.assertEqual(
            self.chat.calls,
            [
                (
                    "edit",
                    self.head.id,
                    "hello",
                    {"parse_mode": "md", "link_preview": False},
                )
            ],
        )
        self.assertEqual(self.state().children, [])
        self.assertEqual(self.state().last_text, "hello")
        self.send_file.assert_not_awaited()

    async def test_long_text_grows_into_replies_to_the_previous_chunk(self):
        await self.edit(_blocks("A", "B", "C"))

        edit, reply_b, reply_c = self.chat.calls
        self.assertEqual(edit[:3], ("edit", self.head.id, "A" * 10))
        self.assertEqual(reply_b[:3], ("reply", self.head.id, "B" * 10))
        self.assertEqual(reply_b[3], {"parse_mode": "md"})
        child_b, child_c = self.state().children
        self.assertEqual(reply_c[:3], ("reply", child_b.id, "C" * 10))
        self.assertEqual(child_c.reply_to_msg_id, child_b.id)

    async def test_default_limit_splits_at_4096(self):
        text = "x" * 5000
        await util.edit_message(self.head, text, parse_mode="md")

        self.assertEqual(
            self.chat.ops(),
            [
                ("edit", self.head.id, "x" * 4096),
                ("reply", self.head.id, "x" * 904),
            ],
        )

    async def test_regrowth_edits_changed_children_and_appends_new_ones(self):
        await self.edit(_blocks("A", "B", "C"))
        child_b, child_c = self.state().children
        self.chat.calls.clear()

        await self.edit(_blocks("A", "B", "D", "E"))

        ops = self.chat.ops()
        self.assertNotIn(("edit", child_b.id, "B" * 10), ops)
        self.assertIn(("edit", child_c.id, "D" * 10), ops)
        self.assertEqual(ops[-1], ("reply", child_c.id, "E" * 10))
        self.assertEqual(len(self.state().children), 3)

    async def test_shrink_deletes_surplus_children(self):
        await self.edit(_blocks("A", "B", "C"))
        child_b, child_c = self.state().children
        self.chat.calls.clear()

        await self.edit("A" * 10)

        self.assertIn(("delete", child_b.id, None), self.chat.ops())
        self.assertIn(("delete", child_c.id, None), self.chat.ops())
        self.assertEqual(self.state().children, [])
        self.assertEqual(self.state().last_text, "A" * 10)

    async def test_append_p_appends_after_the_meta_line_using_stored_text(self):
        await util.edit_message(self.head, "answer", parse_mode="md")
        self.chat.calls.clear()

        await util.edit_message(self.head, "error", parse_mode="md", append_p=True)

        expected = f"answer\n\n{BOT_META_INFO_LINE}\nerror"
        self.assertEqual(self.chat.ops(), [("edit", self.head.id, expected)])
        self.assertEqual(self.state().last_text, expected)

    async def test_append_p_without_history_sends_the_new_text(self):
        await util.edit_message(self.head, "error", parse_mode="md", append_p=True)

        self.assertEqual(self.chat.ops(), [("edit", self.head.id, "error")])

    async def test_append_p_with_blank_text_keeps_the_existing_text(self):
        await util.edit_message(self.head, "answer", parse_mode="md")

        await util.edit_message(self.head, "  ", parse_mode="md", append_p=True)

        self.assertEqual(self.state().last_text, "answer")
        self.assertNotIn("__[empty]__", [call[2] for call in self.chat.calls])

    async def test_blank_text_empties_the_chain(self):
        await self.edit(_blocks("A", "B"))
        (child_b,) = self.state().children
        self.chat.calls.clear()

        await self.edit("   ")

        self.assertEqual(
            self.chat.calls,
            [
                ("delete", child_b.id, None, {}),
                ("edit", self.head.id, "__[empty]__", {"parse_mode": "md"}),
            ],
        )
        self.chat.calls.clear()
        await self.edit("later", append_p=True)
        self.assertEqual(self.chat.ops(), [("edit", self.head.id, "later")])

    async def test_blank_text_skips_a_head_that_already_reads_empty(self):
        self.head.text = "__[empty]__"

        await self.edit("")

        self.assertEqual(self.chat.calls, [])

    async def test_head_not_modified_is_tolerated(self):
        self.head.edit_errors.append(errors.MessageNotModifiedError(request=None))

        await self.edit(_blocks("A", "B"))

        self.assertEqual(self.chat.ops()[-1], ("reply", self.head.id, "B" * 10))
        self.assertEqual(self.state().last_text, _blocks("A", "B"))

    async def test_child_not_modified_keeps_the_child(self):
        await self.edit(_blocks("A", "B"))
        (child_b,) = self.state().children
        child_b.text = "stale"
        child_b.edit_errors.append(errors.MessageNotModifiedError(request=None))

        await self.edit(_blocks("A", "B", "C"))

        self.assertEqual(self.state().children[0], child_b)
        self.assertEqual(self.chat.ops()[-1], ("reply", child_b.id, "C" * 10))

    async def test_head_edit_failure_aborts_and_loses_the_text(self):
        await self.edit("A" * 10)
        self.head.edit_errors.append(errors.FloodWaitError(request=None, capture=120))
        self.chat.calls.clear()

        await self.edit(_blocks("B", "C"))

        self.assertEqual(self.chat.ops(), [("edit", self.head.id, "B" * 10)])
        self.assertEqual(self.state().last_text, "A" * 10)

    async def test_a_same_id_in_another_chat_starts_its_own_chain(self):
        await self.edit(_blocks("A", "B"))
        other_chat = _Chat(chat_id=-1002)
        other_head = other_chat.message("...")
        self.assertEqual(other_head.id, self.head.id)
        self.chat.calls.clear()

        await util.edit_message(other_head, "hi", parse_mode="md", max_len=10)

        self.assertEqual(self.chat.calls, [])
        self.assertEqual(other_chat.ops(), [("edit", other_head.id, "hi")])
        self.assertEqual(len(self.state().children), 1)

    async def test_child_edit_failure_stops_the_chain(self):
        await self.edit(_blocks("A", "B", "C"))
        child_b, child_c = self.state().children
        child_b.edit_errors.append(RuntimeError("boom"))
        self.chat.calls.clear()

        await self.edit(_blocks("A", "X", "Y", "Z"))

        self.assertEqual(self.chat.ops()[-1], ("edit", child_b.id, "X" * 10))


class EditMessageLastSentTests(_EditChainCase):
    """Edits are skipped by comparing against the chunk last sent, not `.text`."""

    def head_edits(self):
        return [
            call[2] for call in self.chat.calls if call[:2] == ("edit", self.head.id)
        ]

    async def test_repeated_text_skips_the_head_edit(self):
        await self.edit("same")
        await self.edit("same")

        self.assertEqual(self.head_edits(), ["same"])

    async def test_text_equal_to_the_original_placeholder_is_still_sent(self):
        await self.edit("answer")
        await self.edit("...")

        self.assertEqual(self.head_edits(), ["answer", "..."])

    async def test_first_edit_still_compares_the_placeholder_text(self):
        await self.edit("...")

        self.assertEqual(self.chat.calls, [])

    async def test_parse_mode_change_is_sent(self):
        await self.edit("same", parse_mode="md")
        await self.edit("same", parse_mode="html")

        self.assertEqual(self.head_edits(), ["same", "same"])

    async def test_unchanged_child_is_skipped_even_when_its_text_differs(self):
        await self.edit(_blocks("A", "B"))
        (child_b,) = self.state().children
        #: What `.text` holds after Telethon unparses the entities it parsed.
        child_b.text = "unparsed"
        self.chat.calls.clear()

        await self.edit(_blocks("A", "B"))

        self.assertEqual(self.chat.calls, [])

    async def test_failed_head_edit_is_retried(self):
        self.head.edit_errors.append(RuntimeError("boom"))

        await self.edit("answer")
        await self.edit("answer")

        self.assertEqual(self.head_edits(), ["answer", "answer"])

    async def test_not_modified_head_is_recorded(self):
        self.head.edit_errors.append(errors.MessageNotModifiedError(request=None))

        await self.edit("answer")
        await self.edit("answer")

        self.assertEqual(self.head_edits(), ["answer"])

    async def test_empty_placeholder_is_sent_once(self):
        await self.edit("answer")
        await self.edit("")
        await self.edit("  ")
        await self.edit("answer")

        self.assertEqual(self.head_edits(), ["answer", "__[empty]__", "answer"])

    async def test_sent_as_file_placeholder_is_sent_once(self):
        file_mode = {
            "send_file_mode": util.SendFileMode.ONLY,
            "file_length_threshold": 5,
        }
        await self.edit("x" * 9, **file_mode)
        await self.edit("y" * 9, **file_mode)
        await self.edit("short")

        self.assertEqual(self.head_edits(), ["__[sent as file]__", "short"])
        self.assertEqual(self.send_file.await_count, 2)

    async def test_records_follow_the_chain(self):
        await self.edit(_blocks("A", "B", "C"))
        self.assertEqual(
            {chunk.text for chunk in self.state().sent.values()},
            {"A" * 10, "B" * 10, "C" * 10},
        )

        await self.edit("A" * 10)

        self.assertEqual(
            self.state().sent, {self.head.id: util.SentChunk("A" * 10, "md")}
        )


class EditMessageFileModeTests(_EditChainCase):
    def assert_file_sent(self, text, **kwargs):
        self.send_file.assert_awaited_once()
        sent = self.send_file.await_args.kwargs
        self.assertEqual(sent["text"], text)
        self.assertIs(sent["message_obj"], self.head)
        for key, value in kwargs.items():
            self.assertEqual(sent[key], value)

    async def test_only_mode_replaces_the_chain_with_a_file(self):
        await self.edit(_blocks("A", "B"))
        (child_b,) = self.state().children
        self.chat.calls.clear()
        user_message = object()

        await self.edit(
            "x" * 30,
            send_file_mode=util.SendFileMode.ONLY,
            file_length_threshold=20,
            reply_to=user_message,
            file_name_mode="llm",
        )

        self.assertEqual(
            self.chat.calls,
            [
                ("delete", child_b.id, None, {}),
                ("edit", self.head.id, "__[sent as file]__", {"parse_mode": "md"}),
            ],
        )
        self.assert_file_sent(
            "x" * 30, reply_to=user_message, file_name_mode="llm", parse_mode="md"
        )
        self.assertNotIn("caption_prefix", self.send_file.await_args.kwargs)
        self.chat.calls.clear()
        await self.edit("later", append_p=True)
        self.assertEqual(self.chat.ops(), [("edit", self.head.id, "later")])

    async def test_only_mode_below_the_threshold_edits_text(self):
        await self.edit(
            "short",
            send_file_mode=util.SendFileMode.ONLY,
            file_length_threshold=20,
        )

        self.assertEqual(self.chat.ops(), [("edit", self.head.id, "short")])
        self.send_file.assert_not_awaited()

    async def test_also_mode_sends_the_file_after_the_text(self):
        order = []
        self.send_file.side_effect = lambda **kwargs: order.append("file")
        original_edit = self.head.edit

        async def edit(text, **kwargs):
            order.append("edit")
            return await original_edit(text, **kwargs)

        self.head.edit = edit

        await self.edit(
            "x" * 5,
            send_file_mode=util.SendFileMode.ALSO,
            file_length_threshold=3,
        )

        self.assertEqual(order, ["edit", "file"])
        self.assert_file_sent("x" * 5)

    async def test_also_if_less_than_sends_both_below_the_file_only_threshold(self):
        await self.edit(
            "x" * 5,
            send_file_mode=util.SendFileMode.ALSO_IF_LESS_THAN,
            file_length_threshold=3,
            file_only_threshold=8,
        )

        self.assertEqual(self.chat.ops(), [("edit", self.head.id, "x" * 5)])
        self.assert_file_sent("x" * 5, caption_prefix=TWIN_FILE_MARKER)

    async def test_a_twin_of_a_multi_message_answer_is_marked(self):
        await self.edit(
            _blocks("A", "B", "C"),
            send_file_mode=util.SendFileMode.ALSO,
            file_length_threshold=3,
        )

        self.assertEqual(len(self.state().children), 2)
        self.assert_file_sent(_blocks("A", "B", "C"), caption_prefix=TWIN_FILE_MARKER)

    async def test_a_twin_of_a_partly_sent_answer_is_not_marked(self):
        self.head.reply_errors.append(errors.FloodWaitError(request=None, capture=1))

        await self.edit(
            _blocks("A", "B"),
            send_file_mode=util.SendFileMode.ALSO,
            file_length_threshold=3,
        )

        self.assertEqual(self.state().children, [])
        self.assert_file_sent(_blocks("A", "B"), caption_prefix="")

    async def test_marking_can_be_turned_off(self):
        await self.edit(
            "x" * 5,
            send_file_mode=util.SendFileMode.ALSO,
            file_length_threshold=3,
            twin_file_marker="",
        )

        self.assert_file_sent("x" * 5, caption_prefix="")

    async def test_also_if_less_than_sends_only_a_file_past_the_file_only_threshold(
        self,
    ):
        await self.edit(
            "x" * 9,
            send_file_mode=util.SendFileMode.ALSO_IF_LESS_THAN,
            file_length_threshold=3,
            file_only_threshold=8,
        )

        self.assertEqual(
            self.chat.ops(), [("edit", self.head.id, "__[sent as file]__")]
        )
        self.assert_file_sent("x" * 9)
        self.assertNotIn("caption_prefix", self.send_file.await_args.kwargs)

    async def test_file_is_still_sent_when_the_head_edit_fails(self):
        self.head.edit_errors.append(errors.FloodWaitError(request=None, capture=120))

        await self.edit(
            "x" * 5,
            send_file_mode=util.SendFileMode.ALSO,
            file_length_threshold=3,
        )

        #: The text never showed, so the file is the only copy.
        self.assert_file_sent("x" * 5, caption_prefix="")

    async def test_never_mode_sends_no_file(self):
        await self.edit("x" * 50, file_length_threshold=3)

        self.send_file.assert_not_awaited()


def _flood_wait():
    return errors.FloodWaitError(request=None, capture=120)


class EditMessageHeadFailureTests(_EditChainCase):
    """`send_new_on_head_failure` keeps the final text when the head cannot be edited."""

    async def deliver(self, text, **kwargs):
        kwargs.setdefault("send_new_on_head_failure", True)
        await self.edit(text, **kwargs)

    def new_head(self):
        return self.state().head

    async def test_flood_wait_sends_the_text_as_a_new_chain(self):
        user_message = self.chat.message("question")
        await self.edit("A" * 5)
        self.head.edit_errors.append(_flood_wait())
        self.chat.calls.clear()

        await self.deliver(_blocks("A", "B", "C"), reply_to=user_message)

        new_head = self.new_head()
        child_b, child_c = self.state().children
        self.assertEqual(
            self.chat.calls,
            [
                (
                    "edit",
                    self.head.id,
                    "A" * 10,
                    {"parse_mode": "md", "link_preview": False},
                ),
                (
                    "respond",
                    self.head.id,
                    "A" * 10,
                    {
                        "reply_to": user_message,
                        "parse_mode": "md",
                        "link_preview": False,
                    },
                ),
                ("reply", new_head.id, "B" * 10, {"parse_mode": "md"}),
                ("reply", child_b.id, "C" * 10, {"parse_mode": "md"}),
                ("delete", self.head.id, None, {}),
            ],
        )
        self.assertEqual(new_head.reply_to_msg_id, user_message.id)
        self.assertEqual(child_c.reply_to_msg_id, child_b.id)
        self.assertEqual(self.state().last_text, _blocks("A", "B", "C"))

    async def test_stale_children_are_deleted_after_the_new_chain_is_sent(self):
        await self.edit(_blocks("A", "B"))
        (stale_b,) = self.state().children
        self.head.edit_errors.append(_flood_wait())
        self.chat.calls.clear()

        await self.deliver(_blocks("X", "Y"))

        new_head = self.new_head()
        self.assertEqual(
            self.chat.ops(),
            [
                ("edit", self.head.id, "X" * 10),
                ("respond", self.head.id, "X" * 10),
                ("reply", new_head.id, "Y" * 10),
                ("delete", stale_b.id, None),
                ("delete", self.head.id, None),
            ],
        )
        self.assertEqual(
            {chunk.text for chunk in self.state().sent.values()}, {"X" * 10, "Y" * 10}
        )

    async def test_later_calls_edit_the_new_chain(self):
        self.head.edit_errors.append(_flood_wait())
        await self.deliver("answer")
        new_head = self.new_head()
        self.chat.calls.clear()

        await util.edit_message(self.head, "error", parse_mode="md", append_p=True)

        expected = f"answer\n\n{BOT_META_INFO_LINE}\nerror"
        self.assertEqual(self.chat.ops(), [("edit", new_head.id, expected)])
        self.assertEqual(self.state().last_text, expected)

    async def test_blank_text_after_a_resend_empties_the_new_head(self):
        self.head.edit_errors.append(_flood_wait())
        await self.deliver("answer")
        new_head = self.new_head()
        self.chat.calls.clear()

        await self.edit("")

        self.assertEqual(self.chat.ops(), [("edit", new_head.id, "__[empty]__")])

    async def test_a_new_head_that_floods_is_replaced_in_turn(self):
        self.head.edit_errors.append(_flood_wait())
        await self.deliver("first")
        first_replacement = self.new_head()
        first_replacement.edit_errors.append(_flood_wait())
        self.chat.calls.clear()

        await self.deliver("second")

        second_replacement = self.new_head()
        self.assertIsNot(second_replacement, first_replacement)
        self.assertEqual(
            self.chat.ops(),
            [
                ("edit", first_replacement.id, "second"),
                ("respond", first_replacement.id, "second"),
                ("delete", first_replacement.id, None),
            ],
        )

    async def test_message_gone_errors_also_resend(self):
        for error in (
            errors.MessageIdInvalidError(request=None),
            errors.MessageAuthorRequiredError(request=None),
            errors.MessageEditTimeExpiredError(request=None),
        ):
            with self.subTest(error=type(error).__name__):
                util.EDIT_CHAINS.clear()
                self.head = self.chat.message("...")
                self.head.edit_errors.append(error)
                self.chat.calls.clear()

                await self.deliver("answer")

                self.assertIn(("respond", self.head.id, "answer"), self.chat.ops())
                self.assertIsNotNone(self.new_head())

    async def test_a_head_that_is_not_ours_is_kept(self):
        await self.edit(_blocks("A", "B"))
        (stale_b,) = self.state().children
        self.head.edit_errors.append(errors.MessageAuthorRequiredError(request=None))
        self.chat.calls.clear()

        await self.deliver(_blocks("X", "Y"))

        self.assertIn(("delete", stale_b.id, None), self.chat.ops())
        self.assertNotIn(("delete", self.head.id, None), self.chat.ops())
        self.assertIsNotNone(self.new_head())

    async def stream_then_flood_the_last_child(self):
        """A streamed partial whose head chunk the final text leaves unchanged."""
        await self.edit(_blocks("A", "B") + "C" * 5 + "▌")
        child_b, stale_c = self.state().children
        stale_c.edit_errors.append(_flood_wait())
        self.chat.calls.clear()
        return child_b, stale_c

    async def test_a_flooded_child_is_replaced_with_the_rest_of_the_text(self):
        child_b, stale_c = await self.stream_then_flood_the_last_child()

        await self.deliver(_blocks("A", "B", "C", "D"))

        kept_b, new_c, new_d = self.state().children
        self.assertIs(kept_b, child_b)
        self.assertEqual(
            self.chat.ops(),
            [
                ("edit", stale_c.id, "C" * 10),
                ("reply", child_b.id, "C" * 10),
                ("reply", new_c.id, "D" * 10),
                ("delete", stale_c.id, None),
            ],
        )
        self.assertIsNone(self.new_head())
        self.assertEqual(self.state().last_text, _blocks("A", "B", "C", "D"))
        self.assertNotIn(stale_c.id, self.state().sent)

    async def test_a_flooded_child_still_stops_the_chain_by_default(self):
        child_b, stale_c = await self.stream_then_flood_the_last_child()

        await self.edit(_blocks("A", "B", "C", "D"))

        self.assertEqual(self.chat.ops(), [("edit", stale_c.id, "C" * 10)])
        self.assertEqual(self.state().children, [child_b])

    async def test_other_child_errors_still_stop_the_chain(self):
        await self.edit(_blocks("A", "B"))
        (child_b,) = self.state().children
        child_b.edit_errors.append(RuntimeError("boom"))
        self.chat.calls.clear()

        await self.deliver(_blocks("A", "X", "Y"))

        self.assertEqual(self.chat.ops(), [("edit", child_b.id, "X" * 10)])
        self.assertEqual(self.state().children, [])

    async def test_other_errors_still_abort(self):
        self.head.edit_errors.append(RuntimeError("boom"))

        await self.deliver("answer")

        self.assertEqual(self.chat.ops(), [("edit", self.head.id, "answer")])
        self.assertIsNone(self.state())

    async def test_default_keeps_the_stale_head(self):
        self.head.edit_errors.append(_flood_wait())

        await self.edit("answer")

        self.assertEqual(self.chat.ops(), [("edit", self.head.id, "answer")])

    async def test_without_reply_to_the_new_head_replies_where_the_head_did(self):
        self.head = self.chat.message("...", reply_to_msg_id=42)
        self.head.edit_errors.append(_flood_wait())

        await self.deliver("answer")

        self.assertEqual(self.new_head().reply_to_msg_id, 42)

    async def test_failed_resend_leaves_the_chain_as_it_was(self):
        await self.edit("partial")
        self.head.edit_errors.append(_flood_wait())
        self.head.respond_errors.append(_flood_wait())
        self.chat.calls.clear()

        await self.deliver("answer")

        self.assertEqual(
            self.chat.ops(),
            [("edit", self.head.id, "answer"), ("respond", self.head.id, "answer")],
        )
        self.assertIsNone(self.state().head)
        self.assertEqual(self.state().last_text, "partial")

    async def test_an_undeletable_stale_head_does_not_undo_the_resend(self):
        self.head.edit_errors.append(_flood_wait())
        self.head.delete_errors.append(errors.MessageDeleteForbiddenError(request=None))

        await self.deliver("answer")

        self.assertIsNotNone(self.new_head())
        self.assertEqual(self.state().last_text, "answer")

    async def test_final_delivery_shape_still_sends_the_file(self):
        user_message = self.chat.message("question")
        self.head.edit_errors.append(_flood_wait())

        await self.deliver(
            "x" * 5,
            max_len=4096,
            reply_to=user_message,
            send_file_mode=util.SendFileMode.ALSO_IF_LESS_THAN,
            file_length_threshold=3,
            file_only_threshold=8,
        )

        self.assertIn(("respond", self.head.id, "x" * 5), self.chat.ops())
        self.send_file.assert_awaited_once()
        self.assertIs(self.send_file.await_args.kwargs["reply_to"], user_message)
        self.assertEqual(
            self.send_file.await_args.kwargs["caption_prefix"], TWIN_FILE_MARKER
        )


class _FakeAction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class SendAsFileCaptionTests(unittest.IsolatedAsyncioTestCase):
    async def send(self, **kwargs):
        sent = mock.AsyncMock(return_value="sent")
        file_data = util.FileGeneration(
            filename="t.md", caption="**Title**", extension=".md"
        )
        borg = mock.Mock(action=lambda *args, **kw: _FakeAction())
        with mock.patch.object(
            util, "_generate_file_data", mock.AsyncMock(return_value=file_data)
        ), mock.patch.object(util, "send_text_as_file", sent), mock.patch.object(
            util, "borg", borg, create=True
        ):
            result = await util.send_as_file_with_filename(
                text="x",
                parse_mode="md",
                file_name_mode="llm",
                message_obj=_Chat().message(),
                **kwargs,
            )
        self.assertEqual(result, "sent")
        return sent.await_args.kwargs["caption"]

    async def test_the_prefix_goes_before_the_generated_caption(self):
        caption = await self.send(caption_prefix=TWIN_FILE_MARKER)

        self.assertEqual(caption, f"{TWIN_FILE_MARKER}**Title**")

    async def test_without_a_prefix_the_caption_is_unchanged(self):
        self.assertEqual(await self.send(), "**Title**")


if __name__ == "__main__":
    unittest.main()
