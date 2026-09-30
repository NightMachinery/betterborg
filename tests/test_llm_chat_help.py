"""`/help` and the chunked replies it is sent with.

Telegram rejects a message longer than 4096 UTF-16 units after parsing, and
`/help` outgrew that: a single `event.reply` failed, and `/help` sent nothing.
"""

import asyncio
import builtins
import importlib
from contextlib import ExitStack
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from telethon.extensions import markdown

from uniborg import util
from uniborg.constants import BOT_META_INFO_PREFIX

TELEGRAM_LIMIT = 4096


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


class _Replyable:
    """A message whose replies are recorded in the shared `log`."""

    def __init__(self, log, *, text=""):
        self.log = log
        self.text = text

    async def reply(self, text, **kwargs):
        message = _Replyable(self.log, text=text)
        self.log.append((self, message, kwargs))
        return message


def _units(text):
    return len(text.encode("utf-16-le")) // 2


class SplitParagraphsTests(unittest.TestCase):
    def test_short_text_stays_one_chunk(self):
        self.assertEqual(util.split_paragraphs("a\n\nb", max_units=100), ["a\n\nb"])

    def test_paragraphs_are_packed_and_never_cut(self):
        paragraphs = [f"**p{i}**\n" + "x" * 30 for i in range(10)]

        chunks = util.split_paragraphs("\n\n".join(paragraphs), max_units=100)

        self.assertTrue(all(_units(c) <= 100 for c in chunks))
        self.assertEqual("\n\n".join(chunks), "\n\n".join(paragraphs))

    def test_an_oversized_paragraph_is_split_within_the_limit(self):
        chunks = util.split_paragraphs("head\n\n" + "word " * 100, max_units=60)

        self.assertEqual(chunks[0], "head")
        self.assertTrue(all(_units(c) <= 60 for c in chunks))
        self.assertEqual(" ".join(chunks[1:]).split(), ["word"] * 100)


class ReplyInChunksTests(unittest.TestCase):
    def test_chains_the_replies_and_prefixes_each_one(self):
        log = []
        event = _Replyable(log)

        sent = asyncio.run(
            util.reply_in_chunks(
                event, "one\n\ntwo", prefix="» ", parse_mode="md", max_units=6
            )
        )

        self.assertEqual([m.text for m in sent], ["» one", "» two"])
        self.assertEqual([parent for parent, _m, _kw in log], [event, sent[0]])
        self.assertEqual(log[0][2], {"parse_mode": "md", "link_preview": False})


class HelpTests(unittest.TestCase):
    def _help_messages(self, *, access):
        log = []
        prefs = SimpleNamespace(
            group_activation_mode="mention_and_reply",
            model="gemini/gemini-3-flash-preview",
        )
        with ExitStack() as stack:
            stack.enter_context(patch.object(plugin, "BOT_USERNAME", "@vlm_chat_bot"))
            stack.enter_context(
                patch.object(plugin.llm_db, "is_awaiting_key", return_value=False)
            )
            stack.enter_context(patch.object(plugin, "cancel_input_flow"))
            stack.enter_context(
                patch.object(plugin.user_manager, "get_prefs", return_value=prefs)
            )
            stack.enter_context(patch.object(plugin.llm_chat_config, "load_config"))
            for name in ("can_use_codex", "can_use_codex_imagegen"):
                stack.enter_context(
                    patch.object(
                        plugin.llm_chat_config,
                        name,
                        AsyncMock(return_value=access),
                    )
                )
            stack.enter_context(
                patch.object(plugin.util, "isAdmin", AsyncMock(return_value=access))
            )
            event = _Replyable(log)
            event.sender_id = 1
            asyncio.run(plugin.help_handler(event))
        return [message.text for _parent, message, _kw in log]

    def test_every_help_message_fits_and_is_marked_as_meta(self):
        for access in (True, False):
            with self.subTest(access=access):
                messages = self._help_messages(access=access)

                self.assertGreater(len(messages), 1)
                for text in messages:
                    self.assertTrue(text.startswith(BOT_META_INFO_PREFIX))
                    parsed, _entities = markdown.parse(text)
                    self.assertLessEqual(_units(parsed), TELEGRAM_LIMIT)
                    #: A cut inside `**…**` would leave literal asterisks.
                    self.assertNotIn("**", parsed)
                joined = "\n".join(messages)
                self.assertIn("**Available Commands:**", joined)
                self.assertIn("to make a level stick.", joined.replace("\n", " "))


if __name__ == "__main__":
    unittest.main()
