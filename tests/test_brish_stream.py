"""Streaming `.a` and `.af` commands: `util.brishz_capture(job=...)`.

Each test runs a real one-worker Brish, injected with `brish=`. Commands are
inert (printf, print, sleep, trap) and run in a temporary directory.
"""

import asyncio
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from brish import Brish

from uniborg import shell_stream, util
from uniborg.shell_stream import CancelOutcome, JobState, StopReason


def _job():
    return shell_stream.ShellJob(owner_id=1, chat_id=1, command="")


async def _wait_until(predicate, *, job, timeout=5.0):
    """Waits on JOB's output until PREDICATE() holds."""
    deadline = time.monotonic() + timeout
    while not predicate():
        left = deadline - time.monotonic()
        if left <= 0:
            raise AssertionError("timed out waiting")
        job.output.changed.clear()
        try:
            await asyncio.wait_for(job.output.changed.wait(), min(left, 0.1))
        except asyncio.TimeoutError:
            pass


class _BrishTestCase(unittest.TestCase):
    brish_class = Brish
    server_count = 1

    def setUp(self):
        self.brish = self.brish_class(server_count=self.server_count)
        self.addCleanup(self.brish.cleanup)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cwd = tmp.name + "/"

    async def capture(self, cmd, *, job=None, fork=True, cwd=None):
        return await util.brishz_capture(
            cwd=cwd or self.cwd, cmd=cmd, fork=fork, job=job, brish=self.brish
        )


@unittest.skipUnless(util.BRISH_POPEN, "brish before popen")
class BrishStreamTests(_BrishTestCase):
    def test_the_result_is_the_same_as_without_a_job(self):
        commands = (
            r"printf '\xc3'; sleep 0.2; printf '\xa9\n'",
            r"printf '\xff'",
            "print -u2 e",
            "print o; print -u2 e; print o2",
            "return 3",
            "",
        )

        async def both(cmd, fork):
            plain = await self.capture(cmd, fork=fork)
            job = _job()
            streamed = await self.capture(cmd, fork=fork, job=job)
            return plain, streamed, job

        for fork in (True, False):
            for cmd in commands:
                with self.subTest(cmd=cmd, fork=fork):
                    plain, streamed, job = asyncio.run(both(cmd, fork))
                    self.assertEqual(streamed, plain)
                    self.assertEqual(job.state, JobState.ENDED)
        self.assertEqual(plain, util.CommandResult(output="", retcode=0))

    def test_output_arrives_while_the_command_runs(self):
        async def main():
            job = _job()
            task = asyncio.create_task(
                self.capture("print a; sleep 1; print b", job=job)
            )
            await _wait_until(
                lambda: "a" in job.output.tail_text(max_units=100, render=False),
                job=job,
            )
            running = not task.done()
            return running, await task

        running, result = asyncio.run(main())

        self.assertTrue(running)
        self.assertEqual(result, util.CommandResult(output="a\nb\n", retcode=0))

    def stop_after(self, cmd, *, delay=0.3, fork=True):
        """Runs CMD as a job, stops it DELAY s after it started; times the end."""

        async def main():
            job = _job()
            task = asyncio.create_task(self.capture(cmd, job=job, fork=fork))
            await _wait_until(lambda: job.state is JobState.RUNNING, job=job)
            await asyncio.sleep(delay)
            stopped_at = time.monotonic()
            outcome = job.cancel(reason=StopReason.USER)
            result = await asyncio.wait_for(task, 20)
            return outcome, result, time.monotonic() - stopped_at

        return asyncio.run(main())

    def test_a_stop_ends_the_command_with_130_and_keeps_the_worker_state(self):
        asyncio.run(self.capture("typeset -g kept=yes", fork=False))

        outcome, result, took = self.stop_after("sleep 100", fork=False)

        self.assertEqual(outcome, CancelOutcome.STOPPING)
        self.assertEqual(result.retcode, 130)
        self.assertLess(took, 2)
        after = asyncio.run(self.capture("print -r -- $kept", fork=False))
        self.assertEqual(after, util.CommandResult(output="yes\n", retcode=0))

    def test_what_a_stopped_command_prints_on_its_way_out_arrives(self):
        outcome, result, took = self.stop_after(
            "trap 'print bye; return 1' INT; sleep 100"
        )

        self.assertIn("bye", result.output)
        self.assertLess(took, 2)

    def test_a_command_that_ignores_the_interrupt_ends_at_the_next_step(self):
        outcome, result, took = self.stop_after(
            "trap '' INT; print -r x; sleep 100; print -r after $?"
        )

        #: Step 2 SIGTERMs the fork command's subshell: 143, the worker kept.
        self.assertEqual(result, util.CommandResult(output="x\n", retcode=143))
        #: Step 2 (SIGTERM) comes one grace (2 s) after the interrupt.
        self.assertGreater(took, 1.5)
        self.assertLess(took, 8)

    def test_a_job_stopped_while_queued_never_runs(self):
        other = tempfile.TemporaryDirectory()
        self.addCleanup(other.cleanup)

        async def main():
            busy, queued = _job(), _job()
            first = asyncio.create_task(self.capture("sleep 1", job=busy))
            await _wait_until(lambda: busy.state is JobState.RUNNING, job=busy)
            second = asyncio.create_task(
                self.capture("printf x > ran", job=queued, cwd=other.name + "/")
            )
            await asyncio.sleep(0.2)
            outcome = queued.cancel(reason=StopReason.USER)
            return outcome, await first, await second

        outcome, first, second = asyncio.run(main())

        self.assertEqual(outcome, CancelOutcome.NOT_STARTED)
        self.assertEqual(first.retcode, 0)
        self.assertIsNone(second)
        self.assertFalse(Path(other.name, "ran").exists())

    def test_exit_in_a_non_fork_command_reports_and_the_pool_recovers(self):
        async def main():
            gone = await self.capture("exit 3", job=_job(), fork=False)
            again = await self.capture("print again", job=_job(), fork=False)
            return gone, again

        gone, again = asyncio.run(main())

        self.assertEqual(gone.retcode, 3)
        self.assertEqual(again, util.CommandResult(output="again\n", retcode=0))

    def test_a_worker_left_under_emulate_sh_still_streams(self):
        async def main():
            await self.capture("emulate sh", job=_job(), fork=False)
            return await self.capture("print -r hello", job=_job(), fork=False)

        self.assertEqual(
            asyncio.run(main()), util.CommandResult(output="hello\n", retcode=0)
        )

    def test_a_cancelled_await_stops_the_command(self):
        async def main():
            job = _job()
            task = asyncio.create_task(self.capture("sleep 100", job=job))
            await _wait_until(lambda: job.state is JobState.RUNNING, job=job)
            await asyncio.sleep(0.3)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            started = time.monotonic()
            #: The one worker is free again once the stopped command ended.
            after = await asyncio.wait_for(self.capture("print free"), 5)
            return job, after, time.monotonic() - started

        job, after, took = asyncio.run(main())

        self.assertEqual(job.stop_reason, StopReason.SHUTDOWN)
        self.assertEqual(after, util.CommandResult(output="free\n", retcode=0))
        self.assertLess(took, 3)


class WorkerZeroTests(_BrishTestCase):
    server_count = 2

    def test_a_non_fork_command_runs_on_worker_zero(self):
        #: `.af` keeps a REPL's state on worker 0; a free worker 1 has other
        #: state, and must not be taken instead while worker 0 is busy.
        self.brish.send_cmd("typeset -g kept=zero", server_index=0)
        self.brish.send_cmd("typeset -g kept=one", server_index=1)

        async def main(streamed):
            job = _job() if streamed else None
            lock, _index = self.brish.acquire_lock(server_index=0)
            try:
                task = asyncio.create_task(
                    self.capture("print -r -- $kept", job=job, fork=False)
                )
                await asyncio.sleep(0.5)
                waited = not task.done()
            finally:
                lock.release()
            return waited, await asyncio.wait_for(task, 10)

        for streamed in (False, True):
            with self.subTest(streamed=streamed):
                waited, result = asyncio.run(main(streamed))
                self.assertTrue(waited)
                self.assertEqual(result, util.CommandResult(output="zero\n", retcode=0))


class _BrishWithoutCancelled(Brish):
    """A brish whose waits for a worker cannot be called off, as before 0.4.1."""

    def acquire_lock(self, server_index=None, lock_sleep=1):
        return super().acquire_lock(server_index=server_index, lock_sleep=lock_sleep)


class _QueuedStopTests(_BrishTestCase):
    """An `.af` job queued behind a busy worker 0, and stopped while it waits."""

    server_count = 2

    def stop_while_queued(self):
        """Returns how long the queued job's producer took to return after the
        stop, whether the busy command still ran then, and both results."""
        other = tempfile.TemporaryDirectory()
        self.addCleanup(other.cleanup)
        self.ran = Path(other.name, "ran")

        async def main():
            busy, queued = _job(), _job()
            first = asyncio.create_task(
                self.capture("sleep 3; print done", job=busy, fork=False)
            )
            await _wait_until(lambda: busy.state is JobState.RUNNING, job=busy)
            second = asyncio.create_task(
                self.capture(
                    "printf x > ran", job=queued, fork=False, cwd=other.name + "/"
                )
            )
            await asyncio.sleep(0.3)
            stopped_at = time.monotonic()
            outcome = queued.cancel(reason=StopReason.USER)
            second_result = await asyncio.wait_for(second, 20)
            took = time.monotonic() - stopped_at
            busy_running = not first.done()
            first_result = await asyncio.wait_for(first, 20)
            return outcome, took, busy_running, first_result, second_result

        return asyncio.run(main())


@unittest.skipUnless(
    util.BRISH_POPEN and util.BRISH_CANCELLED, "brish before cancelled="
)
class QueuedStopTests(_QueuedStopTests):
    def test_a_stop_frees_the_queued_jobs_thread_at_once(self):
        outcome, took, busy_running, first, second = self.stop_while_queued()

        self.assertEqual(outcome, CancelOutcome.NOT_STARTED)
        self.assertIsNone(second)
        #: Brish polls `cancelled` every 0.05 s, and the stop took 0.03 to
        #: 0.09 s in runs; the bound leaves room for a loaded machine, and
        #: worker 0 is busy for 3 s.
        self.assertTrue(busy_running)
        self.assertLess(took, 0.4)
        self.assertFalse(self.ran.exists())
        self.assertEqual(first, util.CommandResult(output="done\n", retcode=0))


@unittest.skipUnless(util.BRISH_POPEN, "brish before popen")
class QueuedStopWithoutCancelledTests(_QueuedStopTests):
    brish_class = _BrishWithoutCancelled

    def test_the_queued_job_waits_for_the_worker_then_runs_nothing(self):
        outcome, took, busy_running, first, second = self.stop_while_queued()

        self.assertEqual(outcome, CancelOutcome.NOT_STARTED)
        self.assertIsNone(second)
        self.assertFalse(busy_running)
        self.assertFalse(self.ran.exists())
        self.assertEqual(first, util.CommandResult(output="done\n", retcode=0))


class PluginPoolTests(_BrishTestCase):
    def test_the_plugin_pool_is_looked_up_off_the_event_loop(self):
        #: Its first use boots a pool of zsh workers, which takes seconds.
        threads = []

        def plugin_brish():
            threads.append(threading.current_thread())
            return self.brish

        async def main():
            job = _job()
            result = await util.brishz_capture(cwd=self.cwd, cmd="print hi", job=job)
            return result, threading.current_thread()

        with patch.object(util, "plugin_brish", plugin_brish):
            result, loop_thread = asyncio.run(main())

        self.assertEqual(result, util.CommandResult(output="hi\n", retcode=0))
        (thread,) = threads
        self.assertIsNot(thread, loop_thread)


class _BrishWithoutPopen(Brish):
    """A brish as before 0.4.0, as far as streaming is concerned."""

    @property
    def popen(self):
        raise AttributeError("popen")


class BrishFallbackTests(_BrishTestCase):
    brish_class = _BrishWithoutPopen

    def test_a_brish_without_popen_gives_the_same_result_through_the_job(self):
        cmd = r"printf 'a\xffb'; print -u2 e; return 4"

        async def main():
            plain = await self.capture(cmd)
            job = _job()
            streamed = await self.capture(cmd, job=job)
            return plain, streamed, job

        plain, streamed, job = asyncio.run(main())

        self.assertFalse(hasattr(self.brish, "popen"))
        self.assertEqual(streamed, plain)
        self.assertEqual(job.output.final_text(render=False), plain.output)
        self.assertEqual(job.state, JobState.ENDED)

    def test_a_job_stopped_before_it_ran_runs_nothing(self):
        async def main():
            job = _job()
            job.cancel(reason=StopReason.USER)
            return await self.capture("printf x > ran", job=job)

        self.assertIsNone(asyncio.run(main()))
        self.assertFalse(Path(self.cwd, "ran").exists())


class PluginPoolFallbackTests(PluginPoolTests):
    brish_class = _BrishWithoutPopen


if __name__ == "__main__":
    unittest.main()
