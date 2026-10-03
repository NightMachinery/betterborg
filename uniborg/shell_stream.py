"""Shell commands as jobs whose output can be watched while they run, and stopped.

Terms:
- **Job** (`ShellJob`): one running `.a`, `.af` or `.aa` command, registered
  so it can be listed and stopped.
- **Producer**: the code that runs the command and writes its bytes into the
  job's `LiveOutput` (`util.brishz_capture` and `util.simple_run_capture`,
  given `job=`). It may write from a worker thread.
- **Consumer**: the chat side, which reads the output while the command runs
  (`LiveOutput.tail_text`, woken by `LiveOutput.changed`) and builds the final
  text when it ends (`LiveOutput.final_text`).
- **Kill hook**: what stops the running command (`BrishPopen.kill`, or the
  process-group escalation of `.aa`). The producer attaches it to the job;
  `ShellJob.cancel` calls it.

`write` never blocks, so a producer keeps reading as fast as the command
writes, and a slow consumer only ever sees a later tail. The memory each
stream keeps is bounded: its first `HEAD_BYTES` and last `TAIL_BYTES`, with a
marker line for what was dropped between them, and after a stop at most
`AFTER_STOP_BYTES` more.

The registry (`JOBS`, `register`, `finish`, `find`, `visible`, `stop_all`) is
used from the event loop thread only. This is a core module, so a plugin
reload never re-executes it, and a reloaded plugin still sees the jobs that
started on its old code. More is in docs/shell_streaming.md.
"""

import asyncio
import codecs
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
import itertools
import threading
import time
from typing import Any, Callable, Optional

from uniborg import term_render, tg_format

#: What each stream keeps of its start, and of its end.
HEAD_BYTES = 16 * 2**20
TAIL_BYTES = 16 * 2**20
#: What is kept after a stop, all streams together. A command can print a
#: report on its way out; brish reads this far ahead after its first signal.
AFTER_STOP_BYTES = 256 * 2**10
#: The latest output, in arrival order, that a preview is built from.
PREVIEW_BYTES = 16 * 2**10

STREAM_OUT = "out"
STREAM_ERR = "err"


class JobState(Enum):
    #: Waiting for a free shell.
    QUEUED = "queued"
    RUNNING = "running"
    #: Asked to stop; the command may still be ending.
    STOPPING = "stopping"
    #: The command has ended (`detach`); its final is not delivered yet.
    ENDED = "ended"
    #: Ended, and its final delivered (`finish`).
    DONE = "done"


class StopReason(Enum):
    #: `.k` or the Stop button.
    USER = "user"
    #: The bot is shutting down or restarting.
    SHUTDOWN = "shutdown"


class CancelOutcome(Enum):
    #: The command was running; its kill hook was called.
    STOPPING = "stopping"
    #: It had been asked to stop already; nothing more was done.
    ALREADY_STOPPING = "already_stopping"
    #: It was still waiting for a shell, and will never run.
    NOT_STARTED = "not_started"
    #: It had ended; nothing was done.
    FINISHED = "finished"


@dataclass(frozen=True)
class Decoding:
    """How a producer's bytes become the final text."""

    encoding: str = "utf-8"
    errors: str = "backslashreplace"
    #: `\r\n` and a lone `\r` become `\n`, as `subprocess.run(text=True)` does.
    translate_newlines: bool = False


def _is_continuation(byte: int) -> bool:
    return byte & 0xC0 == 0x80


def _char_start(data, at: int) -> int:
    """AT, moved back to the start of the UTF-8 character it falls inside."""
    start = at
    while start > 0 and at - start < 3 and _is_continuation(data[start]):
        start -= 1
    return start if not _is_continuation(data[start]) else at


def _skip_continuations(data: bytes) -> bytes:
    """DATA without the end of a character cut off at its start."""
    skip = 0
    while skip < min(3, len(data)) and _is_continuation(data[skip]):
        skip += 1
    return data[skip:]


def _not_kept_line(count: int, *, what: str) -> str:
    return f"\n[… {count} bytes {what}not kept …]\n"


class _StreamStore:
    """One stream's bytes: its first HEAD_BYTES and its last TAIL_BYTES."""

    def __init__(self, *, head_bytes: int, tail_bytes: int):
        self.head_bytes = head_bytes
        self.tail_bytes = tail_bytes
        self.head = bytearray()
        self.head_closed = False
        #: Chunks, so dropping from the front never copies the whole tail.
        self.tail = deque()
        self.tail_size = 0
        self.dropped = 0

    def add(self, chunk: bytes) -> None:
        if not self.head_closed:
            room = self.head_bytes - len(self.head)
            if len(chunk) < room:
                self.head += chunk
                return
            cut = _char_start(chunk, room) if room < len(chunk) else room
            self.head += chunk[:cut]
            chunk = chunk[cut:]
            self.head_closed = True
            if not chunk:
                return
        self.tail.append(chunk)
        self.tail_size += len(chunk)
        excess = self.tail_size - self.tail_bytes
        if excess <= 0:
            return
        while excess > 0:
            first = self.tail[0]
            if len(first) <= excess:
                self.tail.popleft()
                taken = len(first)
            else:
                self.tail[0] = first[excess:]
                taken = excess
            self.tail_size -= taken
            self.dropped += taken
            excess -= taken
        if not self.tail:
            return
        first = self.tail[0]
        aligned = _skip_continuations(first)
        if len(aligned) != len(first):
            self.tail[0] = aligned
            self.tail_size -= len(first) - len(aligned)
            self.dropped += len(first) - len(aligned)

    def text(self, decoding: Decoding) -> str:
        tail = b"".join(self.tail)
        if not self.dropped:
            return (bytes(self.head) + tail).decode(decoding.encoding, decoding.errors)
        return (
            self.head.decode(decoding.encoding, decoding.errors)
            + _not_kept_line(self.dropped, what="")
            + tail.decode(decoding.encoding, decoding.errors)
        )


class LiveOutput:
    """What a command wrote so far, written from any thread, read on the loop.

    `changed` is set (on LOOP) after new output or a change of its job's
    state; the consumer clears it before reading. At most one wake is pending
    at a time, so a flood of small writes costs the loop one callback per
    burst. A producer sets `decoding` before its first write.
    """

    def __init__(
        self,
        loop: Optional[asyncio.AbstractEventLoop] = None,
        *,
        head_bytes: int = HEAD_BYTES,
        tail_bytes: int = TAIL_BYTES,
        after_stop_bytes: int = AFTER_STOP_BYTES,
        preview_bytes: int = PREVIEW_BYTES,
        decoding: Decoding = Decoding(),
    ):
        self._loop = loop if loop is not None else asyncio.get_running_loop()
        self.changed = asyncio.Event()
        self.decoding = decoding
        self._lock = threading.Lock()
        self._stores = {
            name: _StreamStore(head_bytes=head_bytes, tail_bytes=tail_bytes)
            for name in (STREAM_OUT, STREAM_ERR)
        }
        self._preview_bytes = preview_bytes
        self._recent = bytearray()
        self._recent_cut = False
        self._after_stop_bytes = after_stop_bytes
        #: None until a stop; then what may still be kept.
        self._after_stop_room = None
        self._not_kept_after_stop = 0
        self._wake_pending = False
        #: Every byte written, kept or not.
        self.written = 0

    def write(self, chunk: bytes, *, stream: str = STREAM_OUT) -> None:
        """Adds CHUNK of STREAM ("out" or "err"); safe from any thread."""
        store = self._stores[stream]
        with self._lock:
            self.written += len(chunk)
            if self._after_stop_room is not None:
                kept = chunk[: self._after_stop_room]
                self._after_stop_room -= len(kept)
                self._not_kept_after_stop += len(chunk) - len(kept)
                chunk = kept
            if not chunk:
                return
            store.add(chunk)
            self._add_recent(chunk)
            wake = self._claim_wake()
        if wake:
            self._send_wake()

    def notify(self) -> None:
        """Sets `changed` without new output (a state change); any thread."""
        with self._lock:
            wake = self._claim_wake()
        if wake:
            self._send_wake()

    def mark_stopped(self) -> None:
        """From now on, keeps at most `after_stop_bytes` more."""
        with self._lock:
            if self._after_stop_room is None:
                self._after_stop_room = self._after_stop_bytes

    def tail_text(self, *, max_units: int, render: bool) -> str:
        """The latest output, in arrival order, within MAX_UNITS UTF-16 units.

        For a preview: a character cut at either end is left out, invalid
        bytes show as U+FFFD, and RENDER applies `term_render` to a tail that
        starts at a line boundary.
        """
        with self._lock:
            data = bytes(self._recent)
            cut = self._recent_cut
        if cut:
            data = _skip_continuations(data)
        decoder = codecs.getincrementaldecoder(self.decoding.encoding)("replace")
        text = decoder.decode(data, final=False)
        if render:
            text = term_render.render(term_render.line_aligned(text) if cut else text)
        return tg_format.tail_utf16(text, max_units)

    def final_text(self, *, render: bool) -> str:
        """Everything kept: stdout, then stderr, each decoded whole.

        Without RENDER this is what capturing the same bytes gives today:
        brish's `CmdResult.outerr`, or `subprocess.run(text=True)` with
        `translate_newlines`. RENDER applies `term_render` instead of the
        newline translation, to each stream on its own: they are joined one
        after the other, not as they arrived, so a `\r` or cursor-up in
        stderr must not overwrite stdout. Markers say what the memory caps
        left out.
        """
        with self._lock:
            parts = [
                self._stores[name].text(self.decoding)
                for name in (STREAM_OUT, STREAM_ERR)
            ]
            not_kept_after_stop = self._not_kept_after_stop
        if render:
            text = "".join(term_render.render(part) for part in parts)
        else:
            text = "".join(parts)
            if self.decoding.translate_newlines:
                text = text.replace("\r\n", "\n").replace("\r", "\n")
        if not_kept_after_stop:
            text += _not_kept_line(not_kept_after_stop, what="written after the stop ")
        return text

    def _add_recent(self, chunk: bytes) -> None:
        self._recent += chunk
        excess = len(self._recent) - self._preview_bytes
        if excess > 0:
            del self._recent[:excess]
            self._recent_cut = True

    def _claim_wake(self) -> bool:
        """Under the lock: whether this caller sends the one pending wake."""
        if self._wake_pending:
            return False
        self._wake_pending = True
        return True

    def _send_wake(self) -> None:
        try:
            self._loop.call_soon_threadsafe(self._on_wake)
        except RuntimeError:
            #: The loop is closed: nobody is watching any more.
            pass

    def _on_wake(self) -> None:
        with self._lock:
            self._wake_pending = False
        self.changed.set()


_job_ids = itertools.count(1)


@dataclass(eq=False, kw_only=True)
class ShellJob:
    """One shell command, from the moment it waits for a shell to its final.

    `cancel` and `attach` take the same lock, so whichever of the two runs
    second sees the other: a stop that comes before the producer attached its
    kill hook kills the command at `attach` (popen-api.md, the Job pattern).
    """

    owner_id: int
    #: The chat of the command; None for a guest job.
    chat_id: Optional[int]
    command: str
    output: LiveOutput = field(default_factory=LiveOutput)
    #: The command's message.
    message_id: Optional[int] = None
    #: A guest job's `GuestQuery.thread_key`.
    thread_key: Optional[Any] = None
    id: int = field(default_factory=lambda: next(_job_ids))
    #: When the job was made (`time.monotonic`), queued time included.
    started_at: float = field(default_factory=time.monotonic)
    state: JobState = JobState.QUEUED
    stop_reason: Optional[StopReason] = None
    #: `try_start` let the command run.
    ran: bool = field(default=False, init=False)
    #: The message that shows the output while it runs.
    preview_id: Optional[int] = None
    #: Set by `finish`.
    done: asyncio.Event = field(default_factory=asyncio.Event)
    _lock: threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False
    )
    _kill: Optional[Callable[[], Any]] = field(default=None, init=False, repr=False)

    @property
    def stopped(self) -> bool:
        return self.stop_reason is not None

    @property
    def dropped(self) -> bool:
        """Stopped while it waited for a shell: its command will never run.

        The consumer can deliver its final at once, without waiting for the
        producer, which runs nothing once it gets a shell.
        """
        return self.stopped and not self.ran

    def try_start(self) -> bool:
        """Called by the producer once it has a shell: False if stopped meanwhile.

        Any thread. A producer that retries (a shell that was gone before the
        command ran) asks again, and a running job says True again.
        """
        with self._lock:
            match self.state:
                case JobState.QUEUED:
                    started = not self.stopped
                    if started:
                        self.state = JobState.RUNNING
                        self.ran = True
                case JobState.RUNNING:
                    started = True
                case JobState.STOPPING | JobState.ENDED | JobState.DONE:
                    started = False
                case _:
                    raise ValueError(f"unknown job state: {self.state!r}")
        self.output.notify()
        return started

    def attach(self, kill: Callable[[], Any]) -> None:
        """Gives the job the hook that stops its command; kills at once if stopped.

        Any thread. KILL must be safe to call from the thread that cancels
        (the event loop) and from this one.
        """
        with self._lock:
            self._kill = kill
            stopped = self.stopped
        if stopped:
            kill()

    def detach(self) -> None:
        """The command has ended: drops the kill hook, and the job is ENDED.

        Any thread. A later cancel finds nothing to stop (FINISHED), even
        while the consumer still sends the final or the files; a job that was
        stopped keeps its `stop_reason`.
        """
        with self._lock:
            self._kill = None
            match self.state:
                case JobState.RUNNING | JobState.STOPPING:
                    self.state = JobState.ENDED
                case JobState.ENDED | JobState.DONE:
                    return
                case JobState.QUEUED:
                    raise ValueError("a job that never started cannot end")
                case _:
                    raise ValueError(f"unknown job state: {self.state!r}")
        self.output.notify()

    def cancel(self, *, reason: StopReason) -> CancelOutcome:
        """Stops the job: kills its command, or keeps it from ever starting.

        Returns at once; the command may take a few seconds to end (brish's
        kill escalates in steps). Only the first cancel counts.
        """
        kill = None
        with self._lock:
            match self.state:
                case JobState.QUEUED:
                    outcome = CancelOutcome.NOT_STARTED
                case JobState.RUNNING:
                    outcome = CancelOutcome.STOPPING
                    kill = self._kill
                case JobState.STOPPING:
                    outcome = CancelOutcome.ALREADY_STOPPING
                case JobState.ENDED | JobState.DONE:
                    outcome = CancelOutcome.FINISHED
                case _:
                    raise ValueError(f"unknown job state: {self.state!r}")
            match outcome:
                case CancelOutcome.NOT_STARTED | CancelOutcome.STOPPING:
                    self.state = JobState.STOPPING
                    self.stop_reason = reason
                case CancelOutcome.ALREADY_STOPPING | CancelOutcome.FINISHED:
                    return outcome
                case _:
                    raise ValueError(f"unknown cancel outcome: {outcome!r}")
        self.output.mark_stopped()
        self.output.notify()
        if kill is not None:
            kill()
        return outcome


#: The jobs not yet finished, by id.
JOBS: dict[int, ShellJob] = {}


def register(job: ShellJob) -> ShellJob:
    JOBS[job.id] = job
    return job


def finish(job: ShellJob) -> None:
    """Marks JOB done once its final is delivered, and forgets it."""
    with job._lock:
        job.state = JobState.DONE
        job._kill = None
    JOBS.pop(job.id, None)
    job.done.set()
    job.output.notify()


def find(*, chat_id: int, message_id: int) -> Optional[ShellJob]:
    """The job of CHAT_ID whose command or preview is MESSAGE_ID."""
    for job in JOBS.values():
        if job.chat_id == chat_id and message_id in (job.message_id, job.preview_id):
            return job
    return None


def visible(
    *, chat_id: Optional[int], caller_id: int, thread_key: Optional[Any] = None
) -> list[ShellJob]:
    """The jobs CALLER_ID may list and stop from here, oldest first.

    With THREAD_KEY (a guest call), the caller's own jobs of that guest
    thread. Otherwise the jobs of CHAT_ID, and in the caller's private chat
    with the bot (CHAT_ID == CALLER_ID) also the caller's guest jobs.
    """
    if thread_key is not None:
        jobs = [
            job
            for job in JOBS.values()
            if job.thread_key == thread_key and job.owner_id == caller_id
        ]
    else:
        jobs = [
            job
            for job in JOBS.values()
            if (job.thread_key is None and job.chat_id == chat_id)
            or (
                job.thread_key is not None
                and chat_id == caller_id
                and job.owner_id == caller_id
            )
        ]
    return sorted(jobs, key=lambda job: job.id)


async def stop_all(*, reason: StopReason, timeout: float) -> int:
    """Cancels every job and waits up to TIMEOUT seconds for them to finish.

    Returns how many had still not finished when the time ran out.
    """
    jobs = list(JOBS.values())
    for job in jobs:
        job.cancel(reason=reason)
    waiters = [asyncio.ensure_future(job.done.wait()) for job in jobs]
    if not waiters:
        return 0
    _done, pending = await asyncio.wait(waiters, timeout=timeout)
    for waiter in pending:
        waiter.cancel()
    return len(pending)
