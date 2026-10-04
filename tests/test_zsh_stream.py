"""Streaming `.aa` commands: `util.simple_run_capture(job=...)`.

Commands run in a real zsh, in a temporary directory, and are inert (printf,
print, sleep, trap, cat on an empty input). Background `sleep`s are short, so
a failed test leaves nothing running for long. conftest.py points ZDOTDIR at
an empty directory, so `zsh -c` reads no user startup file. Starting zsh can
still take seconds on a loaded machine, so the waits for a command to start
are long; what is timed starts at the stop.
"""

import asyncio
import contextlib
import gc
import os
from pathlib import Path
import signal
import sys
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


@contextlib.contextmanager
def _signals_sent():
    """Records each `util._signal_group` call as (time.monotonic(), signal).

    Signal 0 only checks whether the group is empty; `_steps` leaves it out.
    """
    sent = []
    signal_group = util._signal_group

    def spy(pgid, sig):
        sent.append((time.monotonic(), sig))
        return signal_group(pgid, sig)

    with patch.object(util, "_signal_group", spy):
        yield sent


def _steps(sent, *, until=float("inf")):
    """The kill steps among SENT, up to the time UNTIL."""
    return [sig for at, sig in sent if sig != 0 and at <= until]


def _wait_gone(pid, *, timeout=10.0):
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

    def test_zsh_reads_no_user_startup_file(self):
        #: A startup file's output (a rebuild notice, say) would land in the
        #: command's; without any, zsh defines no function.
        result = self.run_job("print -r -- ${#functions}")

        self.assertEqual(result, util.CommandResult(output="0\n", retcode=0))

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
        #: Which step ended it, not how long that took on a loaded machine:
        #: with the next step 10 s away, a command that outlived the
        #: interrupt would show SIGTERM among the steps.
        with _signals_sent() as sent, patch.object(util, "ZSH_KILL_GRACE", 10):
            _text, outcome, result, took = self.stop(
                "print started; sleep 30", ready=lambda text: "started" in text
            )

        self.assertEqual(outcome, CancelOutcome.STOPPING)
        self.assertIn(result.retcode, (-2, 130))
        self.assertEqual(_steps(sent), [signal.SIGINT])
        self.assertLess(took, 10)

    def test_a_stop_reaches_a_background_grandchild(self):
        #: Background jobs of a non-interactive zsh ignore SIGINT, so this
        #: one lives until the SIGTERM step, holding the output open. Which
        #: steps ended it, not how long they took on a loaded machine: a
        #: SIGKILL would come a whole grace after the SIGTERM.
        grace = 3
        with _signals_sent() as sent, patch.object(util, "ZSH_KILL_GRACE", grace):
            text, _outcome, result, took = self.stop(
                "sleep 30 & print $!; wait", ready=lambda text: text.strip().isdigit()
            )
        pid = int(text)

        self.assertTrue(_wait_gone(pid, timeout=10))
        self.assertEqual(_steps(sent), [signal.SIGINT, signal.SIGTERM])
        self.assertGreater(took, grace - 0.5)

    def test_the_steps_stop_once_the_group_is_gone(self):
        #: Its process group id is free again, and could name another group.
        async def main():
            job = _job()
            task = asyncio.create_task(self.capture("print started; sleep 30", job=job))
            await _wait_for_output(job, lambda text: "started" in text)
            job.cancel(reason=StopReason.USER)
            await asyncio.wait_for(task, 30)
            ended_at = time.monotonic()
            await asyncio.sleep(util.ZSH_KILL_GRACE + 0.5)
            return ended_at

        with _signals_sent() as sent:
            ended_at = asyncio.run(main())

        self.assertEqual(_steps(sent), _steps(sent, until=ended_at))
        self.assertIn(signal.SIGINT, _steps(sent))

    def test_the_steps_go_on_for_a_background_job_left_in_the_group(self):
        #: Its output goes elsewhere, so the command ends at the interrupt,
        #: and the background job, which ignores it, ends at the SIGTERM.
        #: Checked by the order of the steps and the end, with the SIGTERM a
        #: 4 s grace after the interrupt, so load cannot blur the two.
        grace = 4

        async def main():
            job = _job()
            task = asyncio.create_task(
                self.capture("sleep 30 >/dev/null 2>&1 & print $!; sleep 30", job=job)
            )
            text = await _wait_for_output(job, lambda text: text.strip().isdigit())
            job.cancel(reason=StopReason.USER)
            await asyncio.wait_for(task, 30)
            ended_at = time.monotonic()
            pid = int(text)
            #: The later steps run on this loop, so it must keep running.
            deadline = time.monotonic() + grace + 2
            while _alive(pid) and time.monotonic() < deadline:
                await asyncio.sleep(0.05)
            return ended_at, _alive(pid)

        with _signals_sent() as sent, patch.object(util, "ZSH_KILL_GRACE", grace):
            ended_at, alive = asyncio.run(main())

        self.assertEqual(_steps(sent, until=ended_at), [signal.SIGINT])
        self.assertEqual(_steps(sent), [signal.SIGINT, signal.SIGTERM])
        self.assertFalse(alive)

    def test_a_command_that_ignores_the_interrupt_ends_at_sigterm(self):
        grace = 3
        with _signals_sent() as sent, patch.object(util, "ZSH_KILL_GRACE", grace):
            _text, _outcome, result, took = self.stop(
                "trap '' INT; print started; sleep 30",
                ready=lambda text: "started" in text,
            )

        self.assertEqual(result.retcode, -15)
        self.assertEqual(_steps(sent), [signal.SIGINT, signal.SIGTERM])
        self.assertGreater(took, grace - 0.5)

    def stop_with_the_output_held(self, *, group_signals):
        """Stops `print $$; sleep 30` while it stands in for a daemon that
        left the group and holds the output open: no group signal is
        delivered, and GROUP_SIGNALS(sig) says whether the group still has
        a process. Returns the result and the job."""
        withheld = []
        pids = []

        def unreachable(pgid, sig):
            withheld.append(sig)
            return group_signals(sig)

        async def main():
            job = _job()
            task = asyncio.create_task(self.capture("print $$; sleep 30", job=job))
            text = await _wait_for_output(job, lambda text: text.strip().isdigit())
            pids.append(int(text))
            job.cancel(reason=StopReason.USER)
            return await asyncio.wait_for(task, 10), job

        def kill_the_stand_in():
            for pid in pids:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(pid, signal.SIGKILL)

        self.addCleanup(kill_the_stand_in)
        with patch.object(util, "_signal_group", unreachable), patch.object(
            util, "ZSH_KILL_GRACE", 0.2
        ):
            result, job = asyncio.run(main())
        return result, job, withheld

    def test_a_stop_ends_a_job_whose_output_a_daemon_holds(self):
        #: The group is empty at the interrupt (the command has exited), yet
        #: the output stays open; before, the read waited for good.
        result, job, withheld = self.stop_with_the_output_held(
            group_signals=lambda sig: False
        )

        self.assertIsNotNone(result)
        self.assertEqual(job.state, JobState.ENDED)
        self.assertEqual(job.stop_reason, StopReason.USER)
        self.assertEqual(withheld[0], signal.SIGINT)

    def test_after_the_last_step_the_group_is_checked_once_more(self):
        #: Every step finds the group alive, the check after SIGKILL finds
        #: it empty.
        result, job, withheld = self.stop_with_the_output_held(
            group_signals=lambda sig: sig != 0
        )

        self.assertIsNotNone(result)
        self.assertEqual(job.state, JobState.ENDED)
        self.assertEqual(
            [sig for sig in withheld if sig != 0][:3],
            [signal.SIGINT, signal.SIGTERM, signal.SIGKILL],
        )

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

        self.assertTrue(_wait_gone(pid, timeout=10))
        self.assertEqual(job.stop_reason, StopReason.SHUTDOWN)
        self.assertEqual(job.state, JobState.ENDED)

    def test_a_cancelled_await_leaves_no_transport_open(self):
        #: A process that left the command's group (a daemon) holds the
        #: output open past the SIGKILL, so the transport is still open when
        #: `asyncio.run` closes the loop. Left to its `__del__`, it would
        #: close there, and raise "Event loop is closed". Here no group
        #: signal is delivered, so the command itself stands in for that
        #: process.
        unraisable = []
        withheld = []
        signal_group = util._signal_group

        def unreachable(pgid, sig):
            if sig == 0:
                return signal_group(pgid, sig)
            withheld.append(sig)
            return True

        async def main():
            job = _job()
            task = asyncio.create_task(self.capture("print $$; sleep 2", job=job))
            text = await _wait_for_output(job, lambda text: text.strip().isdigit())
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            return int(text)

        with patch.object(sys, "unraisablehook", unraisable.append), patch.object(
            util, "_signal_group", unreachable
        ):
            pid = asyncio.run(main())
            self.assertTrue(_wait_gone(pid, timeout=10))
            gc.collect()

        self.assertIn(signal.SIGKILL, withheld)
        self.assertEqual(unraisable, [])


if __name__ == "__main__":
    unittest.main()
