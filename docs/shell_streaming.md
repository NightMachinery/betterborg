# Shell Streaming

How the shell (`.a`, `.af`, `.aa` and the guest shell in
`stdplugins/advanced_get.py`) runs commands so that their output can be shown
while they run, and stopped. This file describes the producers: the code that
runs a command and collects its bytes. The chat side (the live preview, `.k`,
the Stop button and `/settings`) builds on them and is not written yet.

## Terms

- **Job**: one `.a`, `.af` or `.aa` command, from the moment it waits for a
  shell until its final is delivered (`shell_stream.ShellJob`).
- **Producer**: the code that runs the command and writes its bytes into the
  job's live output: `util.brishz_capture` for `.a` and `.af`,
  `util.simple_run_capture` for `.aa`, each given `job=`.
- **Consumer**: the chat side, which shows the output while the command runs
  and sends the final text when it ends.
- **Live output** (`shell_stream.LiveOutput`): what the command wrote so far,
  written from any thread and read on the event loop.
- **Kill hook**: the callable that stops the running command
  (`BrishPopen.kill` for brish, the process-group escalation for `.aa`). The
  producer attaches it to the job, and `ShellJob.cancel` calls it.

## Shell pools

Brish keeps long-lived zsh workers ready; a set of them in one `Brish` object
is a **pool**. The bot has two:

- The **shell pool**, `util.persistent_brish`: `borg_brish_count` workers (16
  by default), started at import. `.a`, `.af` and the guest shell run here,
  and the plugin passes it explicitly (`brish=util.persistent_brish`).
- The **plugin pool**, `util.plugin_brish()`: `borg_plugin_brish_count`
  workers (4 by default), started on its first use. `brishz`,
  `brishz_capture` and `brishz_helper` default to it, so `jlib_plugins/jlib2.py`,
  `stdplugins/tex2png.py`, `stdplugins/ptv.py` (through `aget_brishz`) and
  `stdplugins/ebook_processor.py` run here.

Both pools boot with `util.BRISH_BOOT_CMD` (`JBRISH=y`).

Why two: with brish 0.4.0, a pool restarts after one of its workers dies (a
stopped command that needed SIGKILL, or `exit N` in a non-fork `.af`), and
that restart waits for the lock of every worker of the pool, so for every
command still running there, an endless one included. On one shared pool, a
stubborn `.a tail -f` would block `.tex` until it ended. With two pools, a
restart of either never stalls the other.

`.x`, `.sbb` and `.xf` call `util.init_brishes`, which starts a new shell pool
and retires both old pools on `util.executor`. The plugin pool is not started
again there; the next plugin command starts it. A retired pool shuts down once
the commands still running on it end.

Nothing here depends on whole-pool restarts. Brish 0.4.1 is announced to
restart only the dead worker; the separate plugin pool is still worth keeping
until the bot runs it.

## The terminal renderer

`uniborg/term_render.py` shows output as a terminal would, as plain text, so a
progress bar shows its last frame instead of every frame in a row. It is a
pure module (standard library only) and works on any `str`, streamed or not.

- `render(text)` applies `\r` (back to column 0; later characters overwrite),
  `\n`, `\b` (back one column, not past 0) and these CSI sequences: SGR
  (`ESC[...m`) is removed, `ESC[K` with 0, 1 or 2 erases in the line, and
  `ESC[nA` moves up n lines, as multi-bar tqdm does. Every other escape
  sequence (other CSI, OSC titles and links, `ESC (B`) is dropped. Text with
  no `\r`, `\b` or ESC is returned unchanged, as the same object.
- `TerminalRenderer` does the same piece by piece (`feed`, then `text`); an
  escape sequence cut at the end of one piece waits for the next.
- `line_aligned(text)` drops everything up to the first newline. A tail cut
  from a longer output usually starts mid-line, where a `\r` or a cursor-up
  would act on text that is not there; aligned, rendering stays inside it.

It is not a terminal emulator: there is no screen, no cursor addressing
beyond the above, and every character is one column wide (tabs and wide
characters are not expanded). Commands still run without a terminal; nothing
fakes one (no `script`, `unbuffer` or pseudo-terminal).

Cost: about 0.05 s per MiB of ordinary lines and 0.4 s per MiB of dense `\r`
frames on the development machine, so a final render of a large output
belongs in a thread, not on the event loop.

## Live output and memory caps

`uniborg/shell_stream.py` is a core module: a plugin reload never re-executes
it. It imports only `term_render` and `tg_format`.

`LiveOutput.write(chunk, stream="out" | "err")` works from any thread and
never blocks, so a producer reads as fast as the command writes and brish's
backpressure never engages; a slow consumer only ever sees a later tail.
After a write, `changed` (an `asyncio.Event`) is set on the loop through
`call_soon_threadsafe`, with at most one wake pending, so a flood of writes
costs the loop one callback per burst. A closed loop is ignored.

What is kept, per stream:
- the first `HEAD_BYTES` (16 MiB) and the last `TAIL_BYTES` (16 MiB), with a
  line `[… N bytes not kept …]` between them when anything was dropped. The
  cuts never split a UTF-8 character;
- after a stop (`mark_stopped`, called by `ShellJob.cancel`), at most
  `AFTER_STOP_BYTES` (256 KiB, brish's read-ahead after its first signal)
  more over both streams; the rest is counted and reported in a last line,
  `[… N bytes written after the stop not kept …]`;
- separately, the last `PREVIEW_BYTES` (16 KiB) of both streams together, in
  arrival order, for previews.

Reading:
- `tail_text(*, max_units, render)` is for a preview. It decodes the recent
  window with `errors="replace"` (so invalid bytes show as U+FFFD), leaves out
  a character cut at either end, and returns at most `max_units` UTF-16 units
  (`tg_format.tail_utf16`). With `render`, a window cut from a longer output
  starts at its first whole line (`term_render.line_aligned`) and is rendered.
- `final_text(*, render)` is for the final. It decodes stdout and stderr each
  whole, with the producer's `decoding` (`shell_stream.Decoding`), and joins
  them stdout first. Without `render` this is exactly what capturing the same
  bytes gave before: brish's `CmdResult.outerr`, or for `.aa`
  `subprocess.run(text=True)` with `\r\n` and `\r` turned into `\n`. With
  `render`, `term_render.render` replaces that newline translation.

## Jobs and the registry

A `ShellJob` holds its owner, chat, command message, guest `thread_key`,
live output, state and stop reason. Its `id` is a process-wide counter.

The states (`JobState`): QUEUED (waiting for a shell), RUNNING, STOPPING
(asked to stop; the command may still be ending) and DONE (its final is
delivered). Every match on `JobState`, `StopReason` (USER or SHUTDOWN) and
`CancelOutcome` names each member and raises on anything else.

- `try_start()` is called by the producer once it holds a shell. It returns
  False when the job was stopped while it waited; the producer then frees the
  shell and runs nothing. A running job answers True again, for a producer
  that retries.
- `attach(kill)` stores the kill hook and calls it at once when the job is
  already stopped. `cancel(*, reason)` marks the job stopped and then calls
  the hook it finds. Both take the job's lock, so whichever runs second sees
  the other, and a cancel that lands between `popen` and `attach` still kills
  the command (popen-api.md, the Job pattern). `detach()` drops the hook once
  the command has ended.
- `cancel` returns STOPPING (the hook was called), NOT_STARTED (it was still
  queued and will never run; the consumer can deliver its final at once),
  ALREADY_STOPPING or FINISHED. Only the first cancel counts. It returns at
  once; the command may take seconds to end.
- Every state change sets `output.changed`, so a preview header can follow.

The registry is a dict, `shell_stream.JOBS`, used from the event loop only:
`register(job)`, `finish(job)` (state DONE, sets `job.done`, forgets it),
`find(*, chat_id, message_id)` (the job whose command or preview that message
is), `visible(*, chat_id, caller_id, thread_key=None)` (a chat's jobs; in the
caller's private chat with the bot also the caller's guest jobs; with a
`thread_key`, the caller's jobs of that guest thread) and
`stop_all(*, reason, timeout)`, which cancels every job, waits up to `timeout`
seconds for them to finish and returns how many had not.
