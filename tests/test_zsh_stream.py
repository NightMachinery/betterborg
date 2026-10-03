"""Streaming `.aa` commands: `util.simple_run_capture(job=...)`.

Commands run in a real zsh, in a temporary directory, and are inert (printf,
print, sleep, trap, cat on an empty input). Background `sleep`s are short, so
a failed test leaves nothing running for long. `zsh -c` reads the user's
startup files, which can take seconds on a loaded machine, so the waits for a
command to start are long; what is timed starts at the stop.
"""

import asyncio
import os
from pathlib import Path
import signal
import tempfile
import time
import unittest
from unittest.mock import patch

from uniborg import shell_stream, util
from uniborg.shell_stream import CancelOutcome, JobState, StopReason


def _job():
    return shell_stream.ShellJob(owner_id=1, chat_id=1, command="")


async def _wait_for_output(job, predicate, *, timeout=30.0):
    deadline = time.monotonic() + timeout
    while True:
        text = job.output.tail_text(max_units=1000, render=False)
        if predicate(text):
            return text
        left = deadline - time.monotonic()
        if left <= 0:
            raise AssertionError(f"timed out waiting; output so far: {text!r}")
        job.output.changed.clear()
        try:
            await asyncio.wait_for(job.output.changed.wait(), min(left, 0.1))
        except asyncio.TimeoutError:
            pass


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _wait_gone(pid, *, timeout=3.0):
    deadline = time.monotonic() + timeout
    while _alive(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    return not _alive(pid)


class ZshStreamTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cwd = tmp.name

    async def capture(self, command, *, job=None):
        return await util.simple_run_capture(cwd=self.cwd, command=command, job=job)

    def test_valid_output_gives_the_same_result_as_without_a_job(self):
        command = r"printf 'h\xc3\xa9\r\nx\ry\n'; print -u2 e; exit 3"

        async def main():
            job = _job()
            return await self.capture(command), await self.capture(command, job=job)

        plain, streamed = asyncio.run(main())

        self.assertEqual(plain, util.CommandResult(output="hé\nx\ny\ne\n", retcode=3))
        self.assertEqual(streamed, plain)

    def run_job(self, command):
        async def main():
            return await asyncio.wait_for(self.capture(command, job=_job()), 30)

        return asyncio.run(main())

    def test_invalid_bytes_become_escapes(self):
        result = self.run_job(r"printf 'a\xffb'")

        self.assertEqual(result, util.CommandResult(output="a\\xffb", retcode=0))

    def test_the_command_gets_an_empty_input(self):
        result = self.run_job("cat")

        self.assertEqual(result, util.CommandResult(output="", retcode=0))

    def test_output_arrives_while_the_command_runs(self):
        async def main():
            job = _job()
            task = asyncio.create_task(
                self.capture("print a; sleep 1; print b", job=job)
            )
            await _wait_for_output(job, lambda text: "a" in text)
            return not task.done(), await task

        running, result = asyncio.run(main())

        self.assertTrue(running)
        self.assertEqual(result, util.CommandResult(output="a\nb\n", retcode=0))

    def test_a_job_stopped_before_it_started_runs_nothing(self):
        async def main():
            job = _job()
            job.cancel(reason=StopReason.USER)
            return await self.capture("printf x > ran", job=job)

        self.assertIsNone(asyncio.run(main()))
        self.assertFalse(Path(self.cwd, "ran").exists())

    def test_a_job_needs_a_shell(self):
        async def main():
            await util.simple_run_capture(
                cwd=self.cwd, command="true", shell=False, job=_job()
            )

        with self.assertRaises(ValueError):
            asyncio.run(main())

    def stop(self, command, *, ready):
        """Runs COMMAND as a job and stops it once READY(output) holds."""

        async def main():
            job = _job()
            task = asyncio.create_task(self.capture(command, job=job))
            text = await _wait_for_output(job, ready)
            stopped_at = time.monotonic()
            outcome = job.cancel(reason=StopReason.USER)
            result = await asyncio.wait_for(task, 30)
            return text, outcome, result, time.monotonic() - stopped_at

        return asyncio.run(main())

    def test_a_stop_interrupts_the_command(self):
        _text, outcome, result, took = self.stop(
            "print started; sleep 30", ready=lambda text: "started" in text
        )

        self.assertEqual(outcome, CancelOutcome.STOPPING)
        self.assertIn(result.retcode, (-2, 130))
        self.assertLess(took, 1.5)

    def test_a_stop_reaches_a_background_grandchild(self):
        #: Background jobs of a non-interactive zsh ignore SIGINT, so this
        #: one lives until the SIGTERM step, holding the output open.
        text, _outcome, result, took = self.stop(
            "sleep 30 & print $!; wait", ready=lambda text: text.strip().isdigit()
        )
        pid = int(text)

        self.assertTrue(_wait_gone(pid))
        self.assertGreater(took, 1.5)
        self.assertLess(took, 5)

    def test_the_steps_stop_once_the_group_is_gone(self):
        #: Its process group id is free again, and could name another group.
        sent = []
        signal_group = util._signal_group

        def spy(pgid, sig):
            delivered = signal_group(pgid, sig)
            sent.append((time.monotonic(), sig, delivered))
            return delivered

        async def main():
            with patch.object(util, "_signal_group", spy):
                job = _job()
                task = asyncio.create_task(
                    self.capture("print started; sleep 30", job=job)
                )
                await _wait_for_output(job, lambda text: "started" in text)
                job.cancel(reason=StopReason.USER)
                await asyncio.wait_for(task, 30)
                ended_at = time.monotonic()
                await asyncio.sleep(util.ZSH_KILL_GRACE + 0.5)
            return ended_at

        ended_at = asyncio.run(main())

        signals_after_the_end = [
            sig for at, sig, _delivered in sent if at > ended_at and sig != 0
        ]
        self.assertEqual(signals_after_the_end, [])
        self.assertIn(signal.SIGINT, [sig for _at, sig, _delivered in sent])

    def test_the_steps_go_on_for_a_background_job_left_in_the_group(self):
        #: Its output goes elsewhere, so the command ends at the interrupt,
        #: and the background job, which ignores it, ends at the SIGTERM.
        async def main():
            job = _job()
            task = asyncio.create_task(
                self.capture("sleep 30 >/dev/null 2>&1 & print $!; sleep 30", job=job)
            )
            text = await _wait_for_output(job, lambda text: text.strip().isdigit())
            stopped_at = time.monotonic()
            job.cancel(reason=StopReason.USER)
            await asyncio.wait_for(task, 30)
            took = time.monotonic() - stopped_at
            pid = int(text)
            #: The later steps run on this loop, so it must keep running.
            deadline = time.monotonic() + util.ZSH_KILL_GRACE + 2
            while _alive(pid) and time.monotonic() < deadline:
                await asyncio.sleep(0.05)
            return took, _alive(pid)

        took, alive = asyncio.run(main())

        self.assertLess(took, 1.5)
        self.assertFalse(alive)

    def test_a_command_that_ignores_the_interrupt_ends_at_sigterm(self):
        _text, _outcome, result, took = self.stop(
            "trap '' INT; print started; sleep 30",
            ready=lambda text: "started" in text,
        )

        self.assertEqual(result.retcode, -15)
        self.assertGreater(took, 1.5)
        self.assertLess(took, 5)

    def test_a_cancelled_await_kills_the_group_and_stops_the_job(self):
        async def main():
            job = _job()
            task = asyncio.create_task(
                self.capture("sleep 30 & print $!; sleep 30", job=job)
            )
            text = await _wait_for_output(job, lambda text: text.strip().isdigit())
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            return int(text), job

        pid, job = asyncio.run(main())

        self.assertTrue(_wait_gone(pid, timeout=2))
        self.assertEqual(job.stop_reason, StopReason.SHUTDOWN)
        self.assertEqual(job.state, JobState.ENDED)


if __name__ == "__main__":
    unittest.main()
