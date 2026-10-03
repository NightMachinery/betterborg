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
from functools import partial
from unittest.mock import AsyncMock, patch

from telethon import types
from telethon._updates import EntityCache

from uniborg import (
    guest_util,
    shell_settings,
    shell_stream,
    telethon_safety,
    tg_raw,
    util,
)

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
        #: The names of the coroutines the plugin scheduled; none of them runs.
        self.scheduled = []
        self.loop = SimpleNamespace(create_task=self._create_task)

    def _create_task(self, coro):
        self.scheduled.append(coro.__qualname__)
        coro.close()

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


class _GuestTestCase(unittest.TestCase):
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


class GuestShellTests(_GuestTestCase):
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


class GuestKillTests(_GuestTestCase):
    """`@bot .k`: stops the caller's guest commands of the guest chat."""

    def setUp(self):
        super().setUp()
        patcher = patch.dict(shell_stream.JOBS, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.thread = _query("").thread_key

    def kill(self, text, *, jobs=(), caller=ADMIN):
        """Sends TEXT as a guest query once JOBS are running; a job is a dict
        of `ShellJob` fields over a guest job of the caller in this chat.
        Returns the jobs and the ids of those whose kill hook ran."""
        killed = []
        shell_stream.JOBS.clear()

        async def main():
            made = []
            for fields in jobs:
                job = shell_stream.register(
                    shell_stream.ShellJob(
                        **{
                            "owner_id": ADMIN,
                            "chat_id": None,
                            "command": "sleep 100",
                            "thread_key": self.thread,
                            **fields,
                        }
                    )
                )
                job.try_start()
                job.attach(partial(killed.append, job.id))
                made.append(job)
            await self.plugin.guest_shell(_query(text, caller=caller))
            return made

        return asyncio.run(main()), killed

    def test_nothing_running(self):
        self.kill(f"@{BOT_USERNAME} .k")

        self.assertEqual(self.answers, [self.plugin.NO_RUNNING_JOB])
        self.assertEqual(self.edits, [])

    def test_bare_stops_the_callers_only_command_here(self):
        (job,), killed = self.kill(f"@{BOT_USERNAME} .k", jobs=[{}])

        self.assertEqual(self.answers, [f"⏹ Stopping #{job.id}…"])
        self.assertEqual(killed, [job.id])

    def test_only_the_callers_jobs_of_this_chat_are_visible(self):
        _jobs, killed = self.kill(
            f"@{BOT_USERNAME} .k all",
            jobs=[
                {"thread_key": "chat:-100"},
                {"owner_id": 42},
                {"thread_key": None, "chat_id": ADMIN},
            ],
        )

        self.assertEqual(self.answers, [self.plugin.NO_RUNNING_JOB])
        self.assertEqual(killed, [])

    def test_its_forms_name_the_guest_command(self):
        jobs, killed = self.kill(f"@{BOT_USERNAME} .k ls", jobs=[{}, {}])
        (listing,) = self.answers
        self.assertIn(f"@{BOT_USERNAME} .k N stops one", listing)
        self.assertEqual(killed, [])

        self.answers.clear()
        jobs, killed = self.kill(f"@{BOT_USERNAME} .k", jobs=[{}, {}])
        self.assertTrue(self.answers[0].startswith("Running here:"))
        self.assertEqual(killed, [])

        self.answers.clear()
        jobs, killed = self.kill(f"@{BOT_USERNAME} .K #{0}", jobs=[{}])
        self.assertEqual(self.answers, ["No running command #0 here."])

        self.answers.clear()
        jobs, killed = self.kill(f"@{BOT_USERNAME} .k all", jobs=[{}, {}])
        self.assertEqual(
            self.answers, ["\n".join(f"⏹ Stopping #{job.id}…" for job in jobs)]
        )
        self.assertEqual(killed, [job.id for job in jobs])

    def test_an_unknown_form_gets_the_usage(self):
        _jobs, killed = self.kill(f"@{BOT_USERNAME} .k 3 5", jobs=[{}])

        (answer,) = self.answers
        self.assertTrue(answer.startswith(f"Usage: @{BOT_USERNAME} .k "))
        self.assertEqual(killed, [])

    def test_a_non_admin_is_told_and_nothing_stops(self):
        _jobs, killed = self.kill(f"@{BOT_USERNAME} .k all", jobs=[{}], caller=STRANGER)

        self.assertEqual(self.answers, ["Not available here."])
        self.assertEqual(killed, [])

    def test_only_a_strict_trigger_stops(self):
        for text in (f"@{BOT_USERNAME}: .k", f"see @{BOT_USERNAME} .k"):
            with self.subTest(text=text):
                self.answers.clear()
                _jobs, killed = self.kill(text, jobs=[{}])

                (answer,) = self.answers
                self.assertTrue(answer.startswith("Usage:"))
                self.assertIn(f"@{BOT_USERNAME} .k stops it", answer)
                self.assertEqual(killed, [])


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
    """The `.a` handler with live output switched off, as before it existed.

    The live path is in test_advanced_get_shell.py.
    """

    def setUp(self):
        borg = _FakeBorg()
        self.plugin = _load_plugin(borg)
        (_builder, self.handler), *_rest = borg.handlers
        self.runs = []

        async def run_and_upload(*, event, to_await, album_mode):
            self.runs.append(to_await)

        for target, name, value in (
            (util, "isAdmin", AsyncMock(return_value=True)),
            (util, "run_and_upload", run_and_upload),
            (shell_settings, "SHELL_STREAMING", False),
        ):
            patcher = patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _to_await(self, text):
        event = SimpleNamespace(
            message=SimpleNamespace(out=False, forward=None),
            pattern_match=self.plugin.pattern_a.match(text),
        )
        asyncio.run(self.handler(event))
        (to_await,) = self.runs
        return to_await, event

    def test_dot_a_runs_on_the_shell_pool_of_when_the_command_runs(self):
        to_await, event = self._to_await(".af printf hi")
        seen = []

        async def capture(**kwargs):
            seen.append(kwargs)
            return util.CommandResult(output="hi", retcode=0)

        #: `.x` while the replied-to files download: the old pool is retired.
        new_pool = object()
        with patch.object(util, "persistent_brish", new_pool), patch.object(
            util, "brishz_capture", capture
        ), patch.object(util, "send_output", AsyncMock()):
            asyncio.run(to_await(cwd="/tmp/x/", event=event))

        (call,) = seen
        self.assertIs(call["brish"], new_pool)
        self.assertEqual(
            (call["cwd"], call["cmd"], call["fork"]), ("/tmp/x/", "printf hi", False)
        )

    def test_dot_aa_runs_on_no_pool(self):
        to_await, _event = self._to_await(".aa printf hi")

        self.assertIs(to_await.func, util.simple_run)


if __name__ == "__main__":
    unittest.main()
