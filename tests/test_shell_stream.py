"""`uniborg/shell_stream.py`: live output, jobs and their registry."""

import asyncio
import threading
import time
import unittest
from unittest.mock import patch

from uniborg import shell_stream
from uniborg.shell_stream import (
    CancelOutcome,
    Decoding,
    JobState,
    LiveOutput,
    ShellJob,
    StopReason,
)


class _FakeLoop:
    """Records wakes instead of running them; can act closed."""

    def __init__(self, *, closed=False):
        self.callbacks = []
        self.closed = closed

    def call_soon_threadsafe(self, callback):
        if self.closed:
            raise RuntimeError("Event loop is closed")
        self.callbacks.append(callback)

    def run_pending(self):
        callbacks, self.callbacks = self.callbacks, []
        for callback in callbacks:
            callback()


def _output(**kwargs):
    return LiveOutput(_FakeLoop(), **kwargs)


def _write_all(output, chunks, *, stream="out"):
    for chunk in chunks:
        output.write(chunk, stream=stream)


class FinalTextTests(unittest.TestCase):
    def test_a_character_split_across_writes_decodes_as_bytes_decode_does(self):
        cases = (
            [b"\xc3", b"\xa9\n"],
            [b"a\xff", b"b"],
            [b"\xf0\x9f", b"\x98", b"\x80!"],
            [b"\xe2\x82"],
        )
        for chunks in cases:
            with self.subTest(chunks=chunks):
                output = _output()
                _write_all(output, chunks)
                expected = b"".join(chunks).decode("utf-8", "backslashreplace")
                self.assertEqual(output.final_text(render=False), expected)

    def test_stdout_comes_first_and_each_stream_decodes_alone(self):
        output = _output()
        output.write(b"e\xa9", stream="err")
        output.write(b"o\xc3", stream="out")

        #: As brish's CmdResult.outerr: out + err, decoded separately.
        self.assertEqual(output.final_text(render=False), "o\\xc3e\\xa9")

    def test_the_decoding_follows_the_producer(self):
        output = _output()
        output.decoding = Decoding(encoding="latin-1", errors="strict")
        output.write(b"\xe9")

        self.assertEqual(output.final_text(render=False), "é")

    def test_newline_translation_is_text_mode_s(self):
        output = _output(decoding=Decoding(translate_newlines=True))
        output.write(b"a\r\nb\rc\n")

        self.assertEqual(output.final_text(render=False), "a\nb\nc\n")
        #: Rendering replaces the translation: the \r redraws the line.
        self.assertEqual(output.final_text(render=True), "a\nc\n")

    def test_render_applies_the_terminal_renderer(self):
        output = _output()
        output.write(b"10%\r\x1b[32m100%\x1b[0m\n")

        self.assertEqual(output.final_text(render=False), "10%\r\x1b[32m100%\x1b[0m\n")
        self.assertEqual(output.final_text(render=True), "100%\n")

    def test_render_keeps_each_stream_to_itself(self):
        #: The streams are joined one after the other, not as they arrived,
        #: so stderr's \r or cursor-up must not reach stdout's lines.
        output = _output()
        output.write(b"answer: 42", stream="out")
        output.write(b"\r  0%|   |\r100%|###|\n", stream="err")
        self.assertEqual(output.final_text(render=True), "answer: 42100%|###|\n")

        output = _output()
        output.write(b"Downloading\n", stream="out")
        output.write(b"\x1b[1Awarning: x\n", stream="err")
        self.assertEqual(output.final_text(render=True), "Downloading\nwarning: x\n")

    def test_nothing_written_is_empty(self):
        self.assertEqual(_output().final_text(render=True), "")


class MemoryCapTests(unittest.TestCase):
    def test_the_head_and_tail_are_kept_with_a_marker_between(self):
        output = _output(head_bytes=10, tail_bytes=10)
        _write_all(output, [b"0123456", b"789abc", b"defghij", b"KLMNOPQRST"])

        self.assertEqual(
            output.final_text(render=False),
            "0123456789\n[… 10 bytes not kept …]\nKLMNOPQRST",
        )
        self.assertEqual(output.written, 30)

    def test_each_stream_has_its_own_cap(self):
        output = _output(head_bytes=2, tail_bytes=2)
        _write_all(output, [b"abcdef"])
        _write_all(output, [b"12"], stream="err")

        self.assertEqual(
            output.final_text(render=False), "ab\n[… 2 bytes not kept …]\nef12"
        )

    def test_the_cuts_never_split_a_character(self):
        output = _output(head_bytes=3, tail_bytes=2)
        output.write("aaé".encode() + b"x" * 10 + "éb".encode())

        self.assertEqual(
            output.final_text(render=False), "aa\n[… 14 bytes not kept …]\nb"
        )

    def test_after_a_stop_only_a_little_more_is_kept(self):
        output = _output(after_stop_bytes=4)
        output.write(b"before\n")
        output.mark_stopped()
        output.write(b"bye", stream="err")
        output.write(b"!!!")

        self.assertEqual(
            output.final_text(render=False),
            "before\n!bye\n[… 2 bytes written after the stop not kept …]\n",
        )


class TailTextTests(unittest.TestCase):
    def test_the_tail_is_in_arrival_order_and_within_its_bound(self):
        output = _output()
        output.write(b"out1 ")
        output.write(b"err ", stream="err")
        output.write(b"out2")

        self.assertEqual(output.tail_text(max_units=100, render=False), "out1 err out2")
        self.assertEqual(output.tail_text(max_units=4, render=False), "out2")

    def test_a_long_output_keeps_only_a_recent_window(self):
        output = _output(preview_bytes=8)
        output.write(b"x" * 100 + b"\nabc")

        self.assertEqual(output.tail_text(max_units=100, render=False), "xxxx\nabc")
        #: Rendered, it starts at the first whole line.
        self.assertEqual(output.tail_text(max_units=100, render=True), "abc")

    def test_one_long_last_line_still_shows_rendered(self):
        output = _output(preview_bytes=100)
        output.write(b'{"k":"' + b"v" * 200 + b'"}\n')

        tail = output.tail_text(max_units=30, render=True)
        self.assertEqual(tail, "v" * 27 + '"}\n')

    def test_a_character_cut_at_either_end_is_left_out(self):
        output = _output(preview_bytes=5)
        output.write("éé".encode())
        output.write(b"ab\xc3")

        self.assertEqual(output.tail_text(max_units=100, render=False), "éab")
        output.write(b"\xa9")
        self.assertEqual(output.tail_text(max_units=100, render=False), "abé")

    def test_invalid_bytes_show_as_replacement_characters(self):
        output = _output()
        output.write(b"a\xffb")

        self.assertEqual(output.tail_text(max_units=100, render=False), "a�b")

    def test_a_rendered_tail_cannot_reach_above_its_first_line(self):
        output = _output(preview_bytes=20)
        output.write(b"z" * 30 + b"\nkept\nlast\x1b[9A\rK")

        self.assertEqual(output.tail_text(max_units=100, render=True), "Kept\nlast")

    def test_the_tail_is_cut_in_utf16_units(self):
        output = _output()
        output.write("a😀b".encode())

        self.assertEqual(output.tail_text(max_units=2, render=False), "b")
        self.assertEqual(output.tail_text(max_units=3, render=False), "😀b")


class WakeTests(unittest.TestCase):
    def test_a_burst_of_writes_wakes_the_loop_once(self):
        loop = _FakeLoop()
        output = LiveOutput(loop)
        for _ in range(100):
            output.write(b"x")
        output.notify()
        self.assertEqual(len(loop.callbacks), 1)

        loop.run_pending()
        self.assertTrue(output.changed.is_set())
        output.changed.clear()
        output.write(b"y")
        self.assertEqual(len(loop.callbacks), 1)

    def test_a_closed_loop_is_ignored(self):
        output = LiveOutput(_FakeLoop(closed=True))
        output.write(b"x")
        output.notify()

        self.assertEqual(output.final_text(render=False), "x")

    def test_a_writer_thread_wakes_a_real_loop(self):
        async def main():
            output = LiveOutput()
            thread = threading.Thread(
                target=lambda: [output.write(b"x") for _ in range(1000)]
            )
            thread.start()
            await asyncio.wait_for(output.changed.wait(), 2)
            thread.join()
            await asyncio.sleep(0)
            return output

        output = asyncio.run(main())
        self.assertEqual(output.final_text(render=False), "x" * 1000)


class _KillCounter:
    def __init__(self):
        self.calls = 0
        self.lock = threading.Lock()

    def __call__(self):
        with self.lock:
            self.calls += 1


def _job(**kwargs):
    kwargs.setdefault("owner_id", 1)
    kwargs.setdefault("chat_id", 10)
    kwargs.setdefault("command", "sleep 100")
    kwargs.setdefault("output", _output())
    return ShellJob(**kwargs)


class ShellJobTests(unittest.TestCase):
    def test_a_job_starts_queued_and_runs_once_started(self):
        job = _job()
        self.assertEqual(job.state, JobState.QUEUED)

        self.assertFalse(job.ran)
        self.assertTrue(job.try_start())
        self.assertEqual(job.state, JobState.RUNNING)
        self.assertTrue(job.ran)
        #: A producer that retries asks again.
        self.assertTrue(job.try_start())
        job.cancel(reason=StopReason.USER)
        self.assertFalse(job.dropped)

    def test_a_job_stopped_while_queued_never_starts(self):
        job = _job()

        self.assertFalse(job.dropped)
        self.assertEqual(job.cancel(reason=StopReason.USER), CancelOutcome.NOT_STARTED)
        self.assertTrue(job.dropped)
        self.assertFalse(job.try_start())
        self.assertEqual(job.state, JobState.STOPPING)
        self.assertEqual(job.stop_reason, StopReason.USER)
        self.assertFalse(job.ran)

    def test_attach_then_cancel_kills(self):
        job = _job()
        kill = _KillCounter()
        job.try_start()
        job.attach(kill)

        self.assertEqual(job.cancel(reason=StopReason.USER), CancelOutcome.STOPPING)
        self.assertEqual(kill.calls, 1)

    def test_cancel_then_attach_kills(self):
        job = _job()
        kill = _KillCounter()
        job.try_start()

        self.assertEqual(job.cancel(reason=StopReason.USER), CancelOutcome.STOPPING)
        self.assertEqual(kill.calls, 0)
        job.attach(kill)
        self.assertEqual(kill.calls, 1)

    def test_a_racing_cancel_and_attach_always_kill(self):
        for _ in range(200):
            job = _job()
            job.try_start()
            kill = _KillCounter()
            barrier = threading.Barrier(2)

            def attach():
                barrier.wait()
                job.attach(kill)

            thread = threading.Thread(target=attach)
            thread.start()
            barrier.wait()
            job.cancel(reason=StopReason.USER)
            thread.join()
            self.assertGreaterEqual(kill.calls, 1)

    def test_only_the_first_cancel_counts(self):
        job = _job()
        kill = _KillCounter()
        job.try_start()
        job.attach(kill)
        job.cancel(reason=StopReason.USER)

        self.assertEqual(
            job.cancel(reason=StopReason.SHUTDOWN), CancelOutcome.ALREADY_STOPPING
        )
        self.assertEqual((kill.calls, job.stop_reason), (1, StopReason.USER))

    def test_a_detached_or_finished_job_kills_nothing(self):
        job = _job(output=_output(after_stop_bytes=0))
        kill = _KillCounter()
        job.try_start()
        job.attach(kill)
        job.detach()

        self.assertEqual(job.state, JobState.ENDED)
        #: The final or the files may still be on their way: nothing to stop.
        self.assertEqual(job.cancel(reason=StopReason.USER), CancelOutcome.FINISHED)
        self.assertEqual((kill.calls, job.stopped), (0, False))
        self.assertFalse(job.try_start())
        job.output.write(b"late")
        self.assertEqual(job.output.final_text(render=False), "late")

        done = _job()
        done.try_start()
        shell_stream.finish(done)
        self.assertEqual(done.cancel(reason=StopReason.USER), CancelOutcome.FINISHED)
        self.assertFalse(done.stopped)

    def test_a_stopped_job_that_ended_keeps_its_reason(self):
        job = _job()
        job.try_start()
        job.attach(_KillCounter())
        job.cancel(reason=StopReason.USER)
        job.detach()

        self.assertEqual(job.state, JobState.ENDED)
        self.assertEqual(job.cancel(reason=StopReason.SHUTDOWN), CancelOutcome.FINISHED)
        self.assertEqual(job.stop_reason, StopReason.USER)

    def test_detach_wakes_the_consumer_and_needs_a_started_job(self):
        loop = _FakeLoop()
        job = _job(output=LiveOutput(loop))
        with self.assertRaises(ValueError):
            job.detach()
        job.try_start()
        loop.run_pending()
        job.detach()
        self.assertEqual(len(loop.callbacks), 1)

    def test_a_cancel_caps_the_output(self):
        job = _job(output=_output(after_stop_bytes=1))
        job.try_start()
        job.cancel(reason=StopReason.USER)
        job.output.write(b"abc")

        self.assertIn(
            "2 bytes written after the stop", job.output.final_text(render=False)
        )

    def test_state_changes_wake_the_consumer(self):
        loop = _FakeLoop()
        job = _job(output=LiveOutput(loop))
        job.try_start()
        self.assertEqual(len(loop.callbacks), 1)
        loop.run_pending()
        job.cancel(reason=StopReason.USER)
        self.assertEqual(len(loop.callbacks), 1)

    def test_an_unknown_state_raises(self):
        job = _job()
        job.state = "lost"

        with self.assertRaises(ValueError):
            job.cancel(reason=StopReason.USER)
        with self.assertRaises(ValueError):
            job.try_start()

    def test_ids_are_unique_and_increasing(self):
        first, second = _job(), _job()
        self.assertLess(first.id, second.id)

    def test_every_field_is_a_keyword(self):
        #: Two ids in swapped places would make a job of the wrong chat.
        with self.assertRaises(TypeError):
            ShellJob(1, 10, "sleep 100", output=_output())


class RegistryTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.dict(shell_stream.JOBS, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_register_find_and_finish(self):
        job = shell_stream.register(_job(chat_id=10, message_id=5))
        job.preview_id = 6

        self.assertIs(shell_stream.find(chat_id=10, message_id=5), job)
        self.assertIs(shell_stream.find(chat_id=10, message_id=6), job)
        self.assertIsNone(shell_stream.find(chat_id=11, message_id=5))

        shell_stream.finish(job)
        self.assertIsNone(shell_stream.find(chat_id=10, message_id=5))
        self.assertTrue(job.done.is_set())
        self.assertEqual(job.state, JobState.DONE)

    def test_visibility(self):
        admin, other = 1, 2
        here = shell_stream.register(_job(owner_id=other, chat_id=10))
        elsewhere = shell_stream.register(_job(owner_id=admin, chat_id=11))
        dm = shell_stream.register(_job(owner_id=admin, chat_id=admin))
        guest = shell_stream.register(
            _job(owner_id=admin, chat_id=None, thread_key=("t", 1))
        )
        others_guest = shell_stream.register(
            _job(owner_id=other, chat_id=None, thread_key=("t", 1))
        )

        self.assertEqual(shell_stream.visible(chat_id=10, caller_id=admin), [here])
        self.assertEqual(
            shell_stream.visible(chat_id=admin, caller_id=admin), [dm, guest]
        )
        self.assertEqual(
            shell_stream.visible(chat_id=None, caller_id=admin, thread_key=("t", 1)),
            [guest],
        )
        self.assertEqual(
            shell_stream.visible(chat_id=None, caller_id=admin, thread_key=("t", 2)),
            [],
        )
        self.assertNotIn(elsewhere, shell_stream.visible(chat_id=10, caller_id=admin))
        self.assertNotIn(
            others_guest, shell_stream.visible(chat_id=admin, caller_id=admin)
        )

    def test_stop_all_stops_every_job_and_respects_its_timeout(self):
        async def main():
            quick = shell_stream.register(_job(output=LiveOutput()))
            stuck = shell_stream.register(_job(output=LiveOutput()))
            queued = shell_stream.register(_job(output=LiveOutput()))
            loop = asyncio.get_running_loop()
            for job in (quick, stuck):
                job.try_start()
            quick.attach(lambda: loop.call_soon(shell_stream.finish, quick))
            stuck.attach(lambda: None)

            started = time.monotonic()
            left = await shell_stream.stop_all(reason=StopReason.SHUTDOWN, timeout=0.3)
            elapsed = time.monotonic() - started
            #: The stuck and the queued job are still registered.
            again = await shell_stream.stop_all(reason=StopReason.USER, timeout=0.1)
            return left, elapsed, again, (quick, stuck, queued)

        left, elapsed, again, jobs = asyncio.run(main())

        self.assertEqual((left, again), (2, 2))
        self.assertLess(elapsed, 1.5)
        self.assertTrue(all(job.stop_reason is StopReason.SHUTDOWN for job in jobs))

    def test_a_job_registered_while_stopping_all_never_runs_and_is_waited_for(self):
        """A shutdown keeps taking commands until it disconnects."""
        seen = {}

        async def main():
            loop = asyncio.get_running_loop()
            first = shell_stream.register(_job(output=LiveOutput()))
            first.try_start()
            first.attach(lambda: loop.call_later(0.2, shell_stream.finish, first))

            def arrive():
                #: As a consumer does: a dropped job's final goes out at once.
                late = seen["late"] = shell_stream.register(_job(output=LiveOutput()))
                if late.try_start():
                    late.attach(lambda: loop.call_later(0.1, shell_stream.finish, late))
                else:
                    loop.call_later(0.3, shell_stream.finish, late)

            loop.call_later(0.1, arrive)
            seen["left"] = await shell_stream.stop_all(
                reason=StopReason.SHUTDOWN, timeout=5
            )
            seen["after"] = shell_stream.register(_job(output=LiveOutput()))

        asyncio.run(main())

        self.assertEqual(seen["left"], 0)
        self.assertIs(seen["late"].stop_reason, StopReason.SHUTDOWN)
        self.assertFalse(seen["late"].ran)
        self.assertTrue(seen["late"].done.is_set())
        self.assertFalse(seen["after"].stopped)

    def test_stop_all_counts_a_late_job_that_did_not_finish(self):
        async def main():
            loop = asyncio.get_running_loop()
            quick = shell_stream.register(_job(output=LiveOutput()))
            quick.try_start()
            quick.attach(lambda: loop.call_later(0.1, shell_stream.finish, quick))
            loop.call_later(0.05, shell_stream.register, _job(output=LiveOutput()))
            return await shell_stream.stop_all(reason=StopReason.SHUTDOWN, timeout=0.3)

        self.assertEqual(asyncio.run(main()), 1)

    def test_stop_all_with_no_jobs(self):
        self.assertEqual(
            asyncio.run(shell_stream.stop_all(reason=StopReason.SHUTDOWN, timeout=1)),
            0,
        )

    def test_a_shutdown_disconnects_once_the_finals_are_out(self):
        """And after its timeout when a job does not finish."""
        calls = []

        class _Client:
            async def disconnect(self):
                calls.append(("disconnect", sorted(shell_stream.JOBS)))

        async def main():
            loop = asyncio.get_running_loop()
            quick = shell_stream.register(_job(output=LiveOutput()))
            quick.try_start()
            #: Its final goes out a moment after the stop.
            quick.attach(lambda: loop.call_later(0.1, shell_stream.finish, quick))
            await shell_stream.stop_all_and_disconnect(_Client(), timeout=5)

            stuck = shell_stream.register(_job(output=LiveOutput()))
            stuck.try_start()
            stuck.attach(lambda: None)
            with self.assertLogs("uniborg.shell_stream", "WARNING"):
                await shell_stream.stop_all_and_disconnect(_Client(), timeout=0.1)
            return quick, stuck

        quick, stuck = asyncio.run(main())

        self.assertEqual(calls, [("disconnect", []), ("disconnect", [stuck.id])])
        self.assertIs(quick.stop_reason, StopReason.SHUTDOWN)
        self.assertIs(stuck.stop_reason, StopReason.SHUTDOWN)

    def test_a_job_registered_while_disconnecting_never_runs(self):
        seen = {}

        class _Client:
            async def disconnect(self):
                seen["during"] = shell_stream.register(_job(output=LiveOutput()))

        async def main():
            await shell_stream.stop_all_and_disconnect(_Client(), timeout=1)
            seen["after"] = shell_stream.register(_job(output=LiveOutput()))

        asyncio.run(main())

        self.assertIs(seen["during"].stop_reason, StopReason.SHUTDOWN)
        self.assertFalse(seen["during"].try_start())
        self.assertFalse(seen["after"].stopped)


if __name__ == "__main__":
    unittest.main()
