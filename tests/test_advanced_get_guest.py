"""The guest shell in `stdplugins/advanced_get.py`: `@bot .a CMD` from a guest chat.

Commands here are inert `printf`s run through `simple_run_capture` (`.aa`).
"""

import asyncio
import datetime
import importlib.util
import logging
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from telethon import types
from telethon._updates import EntityCache

from uniborg import guest_util, telethon_safety, tg_raw, util

BOT_USERNAME = "julia_bot"
BOT_ID = 999
ADMIN = 195391705
STRANGER = 555
NOW = datetime.datetime(2026, 9, 30, 12, 0, tzinfo=datetime.timezone.utc)
PLUGIN_PATH = Path(__file__).resolve().parents[1] / "stdplugins" / "advanced_get.py"


class _FakeBorg:
    def __init__(self):
        self.me = types.User(
            id=BOT_ID, username=BOT_USERNAME, bot=True, is_self=True, first_name="J"
        )
        self.handlers = []
        self.guest_handlers = []
        self.sent = []

    def on(self, builder):
        def decorator(fn):
            self.handlers.append((builder, fn))
            return fn

        return decorator

    def add_event_handler(self, callback, builder):
        self.guest_handlers.append((builder, callback))

    async def send_message(self, chat, text, **kwargs):
        self.sent.append((chat, text))
        return SimpleNamespace(id=len(self.sent))


def _load_plugin(borg):
    previous = util.borg
    util.borg = borg
    try:
        spec = importlib.util.spec_from_file_location("_test_advanced_get", PLUGIN_PATH)
        mod = importlib.util.module_from_spec(spec)
        mod.borg = borg
        mod.logger = logging.getLogger("test.advanced_get")
        spec.loader.exec_module(mod)
        return mod
    finally:
        util.borg = previous


def _query(text, *, caller=ADMIN, entities=None, reference_grouped_id=None):
    trigger = types.Message(
        id=10,
        peer_id=types.PeerUser(STRANGER),
        date=NOW,
        message=text,
        from_id=types.PeerUser(caller),
        out=True,
        entities=entities,
    )
    client = SimpleNamespace(_self_id=BOT_ID, _mb_entity_cache=EntityCache())
    references = []
    if reference_grouped_id is not None:
        references.append(
            types.Message(
                id=9,
                peer_id=types.PeerUser(STRANGER),
                date=NOW,
                message="",
                from_id=types.PeerUser(STRANGER),
                grouped_id=reference_grouped_id,
            )
        )
    update = SimpleNamespace(
        query_id=77, message=trigger, reference_messages=references
    )
    update._entities = {caller: types.User(id=caller, first_name="C")}
    return guest_util.guest_query_from_update(update, client=client)


def _document_media(i):
    return types.MessageMediaDocument(
        document=types.Document(
            id=100 + i,
            access_hash=7,
            file_reference=b"ref",
            date=NOW,
            mime_type="application/octet-stream",
            size=4,
            dc_id=2,
            attributes=[],
        )
    )


class _FakeEditor:
    edits = None
    fail_media = False

    def __init__(self, client, inline_id):
        self.inline_id = inline_id

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def edit(self, **kwargs):
        type(self).edits.append(kwargs)
        if kwargs.get("media") is not None and self.fail_media:
            raise RuntimeError("MEDIA_INVALID")
        return True


class GuestShellTests(unittest.TestCase):
    def setUp(self):
        self.borg = _FakeBorg()
        self.plugin = _load_plugin(self.borg)
        self.answers = []
        self.uploads = []
        _FakeEditor.edits = self.edits = []
        _FakeEditor.fail_media = False
        self.dm_media = False
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dl_base = tmp.name + "/"

        async def answer_guest(client, *, query_id, title, text=None, **kwargs):
            self.answers.append(text)
            return types.InputBotInlineMessageID(dc_id=2, id=1, access_hash=3)

        async def upload_output_files(chat, files, *, album_mode, reply_to, on_error):
            files = list(files)
            self.uploads.append((chat, sorted(Path(f).name for f in files)))
            return [
                SimpleNamespace(
                    id=i, media=_document_media(i) if self.dm_media else None
                )
                for i, _ in enumerate(files)
            ]

        self.answer_guest = AsyncMock(side_effect=answer_guest)
        for target, name, value in (
            (tg_raw, "answer_guest", self.answer_guest),
            (tg_raw, "InlineEditor", _FakeEditor),
            (util, "is_admin_by_id", lambda user_id: user_id == ADMIN),
            (util, "upload_output_files", upload_output_files),
            (util, "dl_base", self.dl_base),
            (util, "borg", self.borg),
        ):
            patcher = patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _run(self, query):
        asyncio.run(self.plugin.guest_shell(query))

    def test_registers_a_guest_handler_on_a_bot(self):
        if not hasattr(types, "UpdateBotGuestChatQuery"):
            self.skipTest("no guest types in this Telethon")
        self.assertEqual(len(self.borg.guest_handlers), 1)

    def test_an_admin_command_runs_and_its_output_is_edited_in(self):
        self._run(_query(f"@{BOT_USERNAME} .aa printf 'hi there'"))

        self.assertEqual(self.answers, ["⏳ Running…"])
        (final,) = self.edits
        self.assertEqual(final["text"], "hi there")
        self.assertFalse(final.get("entities"))
        self.assertIsNone(final.get("parse_mode"))
        self.assertEqual(self.uploads, [])
        self.assertEqual(list(Path(self.dl_base).iterdir()), [])

    def test_a_reply_to_an_album_item_says_only_that_item_was_fetched(self):
        self._run(
            _query(f"@{BOT_USERNAME} .aa printf 'hi there'", reference_grouped_id=7)
        )

        (final,) = self.edits
        self.assertEqual(
            final["text"], f"hi there\n\n{guest_util.ALBUM_REFERENCE_NOTE}"
        )

    def test_a_nonzero_exit_and_empty_output_are_reported(self):
        self._run(_query(f"@{BOT_USERNAME} .aa exit 3"))

        (final,) = self.edits
        self.assertEqual(final["text"], "The process exited 3.\n\nexit 3")

    def test_files_go_to_the_callers_dm_never_the_guest_chat(self):
        self._run(_query(f"@{BOT_USERNAME} .aa printf data > out.bin; printf done"))

        self.assertEqual(self.uploads, [(ADMIN, ["out.bin"])])
        self.assertEqual([chat for chat, _text in self.borg.sent], [ADMIN])
        self.assertIn("1 file(s) sent to your DM", self.edits[-1]["text"])

    def test_a_single_file_is_attached_to_the_answer_with_a_caption(self):
        self.dm_media = True

        self._run(_query(f"@{BOT_USERNAME} .aa printf 'y%.0s' $(seq 900); : > f"))

        (final,) = self.edits
        self.assertIsInstance(final["media"], types.InputMediaDocument)
        self.assertEqual(final["media"].id.id, 100)
        self.assertLessEqual(len(final["text"].encode("utf-16-le")) // 2, 1024)
        self.assertFalse(final.get("entities"))
        self.assertTrue(final["text"].startswith("y" * 900))

    def test_output_too_long_for_a_caption_stays_a_whole_text_answer(self):
        self.dm_media = True

        self._run(_query(f"@{BOT_USERNAME} .aa printf 'y%.0s' $(seq 2000); : > f"))

        (final,) = self.edits
        self.assertNotIn("media", final)
        self.assertTrue(final["text"].startswith("y" * 2000))
        self.assertIn("1 file(s) sent to your DM", final["text"])

    def test_the_whole_output_never_replaces_the_commands_own_output_txt(self):
        self._run(
            _query(
                f"@{BOT_USERNAME} .aa printf mine > output.txt;"
                " printf 'y%.0s' $(seq 5000)"
            )
        )

        ((caller, names),) = self.uploads
        self.assertEqual(caller, ADMIN)
        self.assertEqual(len(names), 2)
        self.assertIn("output.txt", names)
        self.assertTrue(
            any(n.startswith("output-") and n.endswith(".txt") for n in names)
        )

    def test_a_failed_attachment_falls_back_to_the_text_answer(self):
        self.dm_media = True
        _FakeEditor.fail_media = True

        self._run(_query(f"@{BOT_USERNAME} .aa printf done > f; printf done"))

        attach, final = self.edits
        self.assertIsNotNone(attach["media"])
        self.assertNotIn("media", final)
        self.assertTrue(final["text"].startswith("done"))

    def test_several_files_are_not_attached(self):
        self.dm_media = True

        self._run(_query(f"@{BOT_USERNAME} .aa printf a > a; printf b > b; printf ok"))

        (final,) = self.edits
        self.assertNotIn("media", final)
        self.assertIn("2 file(s) sent to your DM", final["text"])

    def test_long_output_is_truncated_and_sent_whole_as_a_file(self):
        self._run(_query(f"@{BOT_USERNAME} .aa printf 'x%.0s' $(seq 5000)"))

        text = self.edits[-1]["text"]
        self.assertLessEqual(len(text.encode("utf-16-le")) // 2, 4096)
        self.assertIn("Output truncated", text)
        self.assertEqual(self.uploads, [(ADMIN, ["output.txt"])])

    def test_a_non_admin_is_told_and_nothing_runs(self):
        self._run(_query(f"@{BOT_USERNAME} .aa printf pwned > pwned", caller=STRANGER))

        self.assertEqual(self.answers, ["Not available here."])
        self.assertEqual(self.edits, [])
        self.assertEqual(list(Path(self.dl_base).iterdir()), [])

    def test_an_implicit_trigger_gets_no_answer(self):
        self._run(_query("thanks"))

        self.answer_guest.assert_not_called()

    def test_a_mention_not_leading_a_command_gets_the_usage_line(self):
        for text in (
            f".aa printf x @{BOT_USERNAME}",
            f"see @{BOT_USERNAME} .aa printf x",
            f"@{BOT_USERNAME} hello",
            #: Telegram sees a mention in each, but only whitespace may
            #: separate it from the command.
            f"@{BOT_USERNAME}: .aa printf x",
            f"@{BOT_USERNAME}, .aa printf x",
            f"@{BOT_USERNAME}.aa printf x",
        ):
            with self.subTest(text=text):
                self.answers.clear()
                self._run(_query(text))

                (answer,) = self.answers
                self.assertTrue(answer.startswith("Usage:"))
        self.assertEqual(self.edits, [])

    def test_a_trigger_inside_a_code_block_does_not_run(self):
        text = f"  @{BOT_USERNAME} .aa printf x"
        code = types.MessageEntityPre(offset=0, length=len(text), language="")

        self._run(_query(text, entities=[code]))

        (answer,) = self.answers
        self.assertTrue(answer.startswith("Usage:"))
        self.assertEqual(self.edits, [])

    def test_a_failed_answer_runs_nothing(self):
        self.answer_guest.side_effect = telethon_safety.DeliveryUnknownError("gone")
        ran = []

        async def capture(**kwargs):
            ran.append(kwargs)

        with patch.object(util, "simple_run_capture", capture):
            self._run(_query(f"@{BOT_USERNAME} .aa printf x"))

        self.assertEqual(ran, [])
        self.assertEqual(self.edits, [])

    def test_an_exception_becomes_the_final_edit(self):
        async def capture(**kwargs):
            raise RuntimeError("boom")

        with patch.object(util, "simple_run_capture", capture):
            self._run(_query(f"@{BOT_USERNAME} .aa printf x"))

        self.assertIn("RuntimeError: boom", self.edits[-1]["text"])
        self.assertEqual(list(Path(self.dl_base).iterdir()), [])

    def test_a_brish_command_runs_on_the_shell_pool(self):
        seen = []

        async def capture(**kwargs):
            seen.append(kwargs)
            return util.CommandResult(output="hi", retcode=0)

        with patch.object(util, "brishz_capture", capture):
            self._run(_query(f"@{BOT_USERNAME} .a printf hi"))

        (call,) = seen
        self.assertIs(call["brish"], util.persistent_brish)
        self.assertEqual(self.edits[-1]["text"], "hi")


class ShellHandlerEchoTests(unittest.TestCase):
    def test_the_dot_a_handler_ignores_an_echoed_guest_answer(self):
        borg = _FakeBorg()
        _load_plugin(borg)
        (_builder, handler), *_rest = borg.handlers
        echo = SimpleNamespace(
            out=True, guestchat_via_from=types.PeerUser(ADMIN), forward=None
        )

        with patch.object(util, "isAdmin", AsyncMock(side_effect=AssertionError)):
            asyncio.run(handler(SimpleNamespace(message=echo)))


class ShellHandlerPoolTests(unittest.TestCase):
    def test_dot_a_runs_on_the_shell_pool_and_dot_aa_on_no_pool(self):
        borg = _FakeBorg()
        plugin = _load_plugin(borg)
        (_builder, handler), *_rest = borg.handlers
        runs = []

        async def run_and_upload(*, event, to_await, album_mode):
            runs.append(to_await)

        for name, value in (
            ("isAdmin", AsyncMock(return_value=True)),
            ("run_and_upload", run_and_upload),
        ):
            patcher = patch.object(util, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

        for text in (".a printf hi", ".aa printf hi"):
            event = SimpleNamespace(
                message=SimpleNamespace(out=False, forward=None),
                pattern_match=plugin.pattern_a.match(text),
            )
            asyncio.run(handler(event))

        brish_run, plain_run = runs
        self.assertIs(brish_run.func, util.brishz)
        self.assertIs(brish_run.keywords["brish"], util.persistent_brish)
        self.assertIs(plain_run.func, util.simple_run)


if __name__ == "__main__":
    unittest.main()
