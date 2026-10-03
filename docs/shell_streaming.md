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
  and the plugin passes it explicitly (`brish=util.persistent_brish`). It
  reads the global when the command runs, after the replied-to files have
  downloaded: a `.x` during the download retires the pool of when the
  message came, and a retired pool refuses commands
  (UninitializedBrishException).
- The **plugin pool**, `util.plugin_brish()`: `borg_plugin_brish_count`
  workers (4 by default), started on its first use. `brishz`,
  `brishz_capture` and `brishz_helper` default to it, so
  `jlib_plugins/jlib2.py`, `stdplugins/tex2png.py`, `stdplugins/ptv.py`
  (through `aget_brishz`) and `stdplugins/ebook_processor.py` run here.

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
  When only blanks follow that newline (the tail is the end of one long line,
  such as a minified JSON body), the tail is kept whole, so a preview never
  goes empty.

It is not a terminal emulator: there is no screen, no cursor addressing
beyond the above, and every character is one column wide (tabs and wide
characters are not expanded). Commands still run without a terminal; nothing
fakes one (no `script`, `unbuffer` or pseudo-terminal).

Cost: linear in the text. The line under the cursor is kept as an array of
code points, so a write, an overwrite after `\r` or `\b`, and an erase cost
what they change, not the length of the line; only moving to another line
(a newline or a cursor-up) costs the length of the lines involved. On the
development machine, per MiB: about 0.02 s for ordinary lines, 0.06 s for
dense `\r` frames (tqdm), 0.09 s for coloured lines, 0.2 s for one long
coloured line (`jq -C -c`) and 0.35 s for one long line of
`grep --color=always` matches. The cap allows 32 MiB per stream, so a final
render of a large output can take seconds, and belongs in a thread, not on
the event loop.

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
live output, state and stop reason. Its `id` is a process-wide counter. Every
field is a keyword argument, so two ids (owner and chat, say) cannot be
swapped by position.

The states (`JobState`): QUEUED (waiting for a shell), RUNNING, STOPPING
(asked to stop; the command may still be ending), ENDED (the command has
ended, and the consumer may still be sending its final or its files) and DONE
(all of it is delivered). Every match on `JobState`, `StopReason` (USER or
SHUTDOWN) and `CancelOutcome` names each member and raises on anything else.

- `try_start()` is called by the producer once it holds a shell. It returns
  False when the job was stopped while it waited; the producer then frees the
  shell and runs nothing. A running job answers True again, for a producer
  that retries. `ran` records that the command was let run, and a job
  stopped before that is **dropped** (`dropped`): its command never runs, so
  the consumer delivers its final at once instead of waiting for a shell.
- `attach(kill)` stores the kill hook and calls it at once when the job is
  already stopped. `cancel(*, reason)` marks the job stopped and then calls
  the hook it finds. Both take the job's lock, so whichever runs second sees
  the other, and a cancel that lands between `popen` and `attach` still kills
  the command (popen-api.md, the Job pattern). The producer calls `detach()`
  once the command has ended: it drops the hook and makes the job ENDED. A
  stopped job keeps its `stop_reason` there.
- `cancel` returns STOPPING (the hook was called), NOT_STARTED (it was still
  queued and will never run; the consumer can deliver its final at once),
  ALREADY_STOPPING or FINISHED (ENDED or DONE: the command has ended by
  itself or after an earlier stop, and nothing is done, so a `.k` during the
  upload of the files does not claim a stop). Only the first cancel counts.
  It returns at once; the command may take seconds to end.
- Every state change sets `output.changed`, so a preview header can follow.

The registry is a dict, `shell_stream.JOBS`, used from the event loop only:
`register(job)`, `finish(job)` (state DONE, sets `job.done`, forgets it),
`find(*, chat_id, message_id)` (the job whose command or preview that message
is), `visible(*, chat_id, caller_id, thread_key=None)` (a chat's jobs; in the
caller's private chat with the bot also the caller's guest jobs; with a
`thread_key`, the caller's jobs of that guest thread) and
`stop_all(*, reason, timeout)`, which cancels every job, waits up to `timeout`
seconds for them to finish and returns how many had not.

## The brish producer (`.a`, `.af`)

`util.brishz_capture(*, cwd, cmd, fork=True, job=None, brish=None)`. Without a
job it runs as it always did, through `brishz_helper` and `send_cmd`. With a
job, on a brish that has `Brish.popen` (0.4.0 and later; `util.BRISH_POPEN`
says whether the installed one does):

1. An executor thread takes a worker through `util._on_brish_worker`, the
   session `brishz_helper` also uses: the worker lock, `$jd`, `cd` and
   `jinit`, the command, `cd /tmp`. Without `brish=`, that thread also looks
   up the plugin pool, whose first use (and first use after `.x`) boots its
   workers for seconds; the event loop never waits for it. A non-fork
   command (`.af`) takes worker 0, which keeps its state between commands,
   as without a job: while another command holds worker 0 it waits, QUEUED,
   even when other workers are free.
2. Right after taking the lock it asks `job.try_start()`. False (the job was
   stopped while it waited for a free worker) frees the lock, runs nothing,
   and the result is None.
3. The command runs as `popen('{ eval "$(< /dev/stdin)"; } 2>&1', fork=...,
   cmd_stdin=cmd)`. The popen's `kill` is attached as the job's kill hook,
   and every chunk goes to `job.output.write(chunk, stream=...)`.
4. The loop never breaks on a stop. What a dying command still prints
   arrives (a trap's goodbye, say), and brish takes kill steps 2 to 4 inside
   these reads, which stay fast because `write` never blocks. No brish call
   happens inside the loop, so BrishWorkerBusyException cannot occur.
5. `cd /tmp` runs after the `with` block. After `exit N` in a non-fork
   command, or a 9001 (a worker that brish had to SIGKILL), it raises
   BrishWorkerDiedException and the result stands; a worker that was gone
   before the command ran gets one retry (6b60222), which is safe because
   nothing streamed.
6. The result is `CommandResult(output=job.output.final_text(render=False),
   retcode=...)`, the same text as without a job. The output decodes with the
   brish's own `encoding` and `decoding_errors`.

If the awaiting task is cancelled (the client disconnecting), the job is
cancelled with StopReason.SHUTDOWN and the CancelledError propagates; the
executor thread is not awaited, and ends once the command does.

What a stop gives, in the tests (default `kill_grace` of 2 s): `sleep 100`
ends with 130 within about 0.1 s, and a non-fork command keeps the worker's
state; a command that traps INT runs its trap (its output arrives) and
returns the trap's status; a fork command that ignores INT (`trap '' INT`)
gets SIGTERM at step 2, about 2.1 s later, and returns 143 (non-fork: its
`sleep` gets the SIGTERM, and the command goes on). Under a stubborn command
brish reaches step 4 (SIGKILL of the worker, 9001) after about 5 to 13 s;
with brish 0.4.0 the pool then restarts, waiting for its other commands.

**Old brish fallback.** A brish without `popen` (eva ran 0.3.5) runs the
command through `send_cmd`, still asking `try_start` first, and writes its
whole result into the job at the end, which makes the job ENDED. It cannot
be stopped once it runs: no kill hook is attached. The consumer should not
offer a stop (or a preview) for `.a` there; `.aa` streams on any brish.

## The `.aa` producer

`util.simple_run_capture(*, cwd, command, shell=True, job=None)`. Without a
job it runs as before, through `subprocess.run(shell=True, executable="zsh",
text=True)` on an executor thread. A job needs `shell=True` (anything else
raises ValueError). With one, `util._stream_zsh`:

1. asks `job.try_start()`; False gives None and runs nothing;
2. starts `asyncio.create_subprocess_exec("zsh", "-c", command, cwd=cwd,
   stdin=DEVNULL, stdout=PIPE, stderr=STDOUT, start_new_session=True)`. The
   argv is the one `executable="zsh"` gave, and the input stays empty;
3. reads `stdout.read(65536)` on the event loop into `job.output`, so a
   running `.aa` holds no executor thread and stays the escape hatch when
   every thread is busy;
4. returns `CommandResult(output=job.output.final_text(render=False),
   retcode=...)`. That is UTF-8 with `\r\n` and `\r` turned into `\n`, as
   `text=True` gave for valid output; invalid bytes now become `\xNN`
   escapes, as in `.a`, where `text=True` raised UnicodeDecodeError. The exit
   status of a command killed by a signal is negative (`-15`), as before.

The command runs in a session, and so a process group, of its own. Its kill
hook (`util._ProcessGroupKiller`) sends the whole group SIGINT, then SIGTERM,
then SIGKILL, `ZSH_KILL_GRACE` (2 s) apart through `loop.call_later`, and
stops as soon as the group is empty. The group includes background jobs:
those of a non-interactive zsh ignore SIGINT, so they end at SIGTERM, and a
`.aa` whose background job holds the output open ends then too. A command
that ignores SIGINT (`trap '' INT`) ends at SIGTERM, 2 s after the stop.

When the command's own process has been reaped, the producer checks the
group at once. If it is empty, the pending step is cancelled: the group's id
(the command's process id) is then free, and could name a new group, such as
another streamed `.aa`. A background job left in the group keeps the id
reserved and gets the later steps.

A cancelled await (the client disconnecting) cancels the job with
StopReason.SHUTDOWN, sends the group SIGKILL at once and re-raises.

A side effect of the own session: a streamed `.aa` command has no controlling
terminal, like a brish command, and a terminal Ctrl-C on the bot no longer
reaches it.

## Settings and the kill switch

`uniborg/shell_settings.py` (a core module) keeps each admin's shell settings
in `UserStorage(purpose="shell")`, one JSON file per user under
`~/.borg/shell/`. `ShellSettings.get(user_id)` gives a `ShellPrefs`, and
`ShellSettings.set(user_id, prefs)` stores only the values that differ from
the defaults, so a changed default reaches everyone who never chose. A stored
value this version does not know is logged and read as its default. The
storage is injected (`ShellSettings(storage=...)`), so the tests use a temp
dir.

The settings, with their defaults:
- `stream_private`: how a preview shows in private chats, Drafts. The field
  names and values are the chat bot's (`stream_driver.StreamMode`), and so
  are the helpers that read and write them per scope
  (`stream_driver.stream_mode`, `set_stream_mode`).
- `stream_groups`: the same in groups, Edits.
- `final_mode` (`shell_settings.FinalMode`): what a command that showed a
  preview sends when it ends, EDIT_PREVIEW (the preview becomes the final) or
  NEW_REPLY (a new reply, as before live output).
- `render`: show output as a terminal would (`term_render`), on.

The **kill switch** is the environment variable `borg_shell_streaming`, read
once when the module is first imported: "1" or unset is on, "0" is off, and
any other value raises at startup rather than guess. Off, `.a`, `.af` and
`.aa` run exactly as before live output: no job, no preview and no renderer.

## What phase C builds on this

Nothing calls the producers with a job yet. The chat side (the preview and
its pace, the final in its two modes, `.k`, the Stop button, `/settings`,
guest answers, stopping jobs before a shutdown, and the
`borg_shell_streaming` kill switch) is the next phase, and will extend this
file.
