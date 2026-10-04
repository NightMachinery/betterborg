# Shell Streaming

How the shell (`.a`, `.af`, `.aa` and the guest shell in
`stdplugins/advanced_get.py`) runs commands so that their output can be shown
while they run, and stopped. It describes the producers (the code that runs a
command and collects its bytes), the settings and their `/settings` panel,
the live output of `.a`, `.af` and `.aa` in chats and in guest answers, and
how a running command is stopped: `.k`, `@bot .k` and the previews' Stop
buttons.

## Terms

- **Job**: one `.a`, `.af` or `.aa` command, in a chat or from a guest
  chat, from the moment it waits for a shell until its final is delivered
  (`shell_stream.ShellJob`).
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
  (UninitializedBrishException). A job still waiting for a worker when `.x`
  retires its pool waits until that pool shuts down (once the commands that
  hold its workers end); the pool then refuses the wait, nothing has run,
  and the job moves to the current pool (`_capture_on_shell_pool`). A job
  whose command had started is never run twice: the error stands.
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
restart of either never stalls the other. Brish 0.4.1 (released) replaces
only the dead worker, and its `restart()` never waits for a worker in use,
so there the two pools only keep the shell's commands and the plugins'
apart; the bot runs on either version.

`.x`, `.sbb` and `.xf` call `util.init_brishes`, which starts a new shell pool
and retires both old pools on `util.executor`. The plugin pool is not started
again there; the next plugin command starts it. A retired pool shuts down once
the commands still running on it end. A restart stops none of them, so the
reply (`old_pool_note` in the plugin) adds a line such as "2 commands still
run on old pools; .k stops them." when jobs that are still running (QUEUED,
RUNNING or STOPPING) have a retired pool as their `ShellJob.pool`: any pool
but the current `util.persistent_brish`, so one that an earlier restart
retired counts too. The consumer sets that field when it picks the pool,
after the downloads; `.aa` jobs and jobs still downloading have none, and an
old-brish `.a` is no job, so none of these is counted. Only the jobs that a
`.k` in the chat of the restart can see (`shell_stream.visible`) get that
line. The others get a second line, "1 command in another chat still runs on
an old pool; .k in that chat stops it.", since a `.k` here would not find
them, and the caller's own guest jobs among them a third, "1 guest command
still runs on an old pool; @bot .k in its chat stops it." A fourth line, "1
command of another admin still runs on an old pool; they can stop it.",
counts the jobs that the caller cannot reach at all: another admin's guest
jobs (`@bot .k` sees only its caller's own) and their jobs in a private chat
(a positive chat id), which on a bot only they can write in. Another admin's
job in a group counts on the second line, since any admin there can stop it.
A guest job records its pool as a chat job does.

Nothing here depends on whole-pool restarts, so the pools work the same on
brish 0.4.0 and 0.4.1. On 0.4.1 a stopped job also gives up its wait for a
worker at once, through brish's `cancelled=` argument; on 0.4.0 it waits
until a worker is free, then runs nothing (see "Stopping a queued job" under
Limits).

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

Cost: linear in the text. A line written to is kept as an array of code
points, so a write, an overwrite after `\r` or `\b`, and an erase cost what
they change, not the length of the line. The last `ACTIVE_LINES` (16) lines
written to keep their arrays, so moving between them (a newline or a
cursor-up) costs nothing, and a progress display redrawn under a long line
does not copy that line at each redraw (a line of 1 MiB with 2000 redraws
under it renders in about 0.06 s). Only a program that keeps rewriting more
than 16 lines in turn pays the length of each line it comes back to. On the
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
  `render`, `term_render.render` replaces that newline translation, and
  renders each stream on its own: they are joined one after the other, not
  in the order they arrived, so a `\r` or a cursor-up at the start of
  stderr must not overwrite the end of stdout. (Both producers send the
  command's stderr into its stdout, `2>&1` for brish and `stderr=STDOUT` for
  `.aa`, so the stderr stream is usually empty.)

## Jobs and the registry

A `ShellJob` holds its owner, chat, command message, guest `thread_key`,
live output, state and stop reason, and the brish pool it runs on (`pool`).
Its `id` is a process-wide counter. Every field is a keyword argument, so
two ids (owner and chat, say) cannot be swapped by position.

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
- `stop_requested()` says whether the job was asked to stop (`stopped`, as
  a callable). Brish 0.4.1 calls it every 0.05 s from the executor thread
  while the job waits for a worker; it takes no lock.
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
   and the result is None. With brish 0.4.1 the wait itself can be called
   off: `acquire_lock` gets `cancelled=job.stop_requested`, which brish
   calls every 0.05 s while the thread waits, and once more when it has the
   worker. Once the job is stopped, brish frees the worker and raises
   BrishCancelledException: nothing ran, so the result is None, as for a
   refused `try_start`, with no retry. `util.BRISH_CANCELLED` says whether
   the installed brish takes the argument; `_on_brish_worker` asks each
   pool's class (`_lock_takes_cancelled`, once per class), so a pool whose
   `acquire_lock` does not take it (0.4.0) gets only `try_start`.
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
with brish 0.4.0 the pool then restarts, waiting for its other commands,
while 0.4.1 replaces only that worker.

**Old brish fallback.** A brish without `popen` (before 0.4.0) runs the
command through `send_cmd`, still asking `try_start` first, and writes its
whole result into the job at the end, which makes the job ENDED. It cannot
be stopped once it runs: no kill hook is attached. So the plugin runs `.a`
and `.af` there with no job at all (no preview, nothing for `.k` to find),
in chats and in guest answers; `.aa` streams on any brish.

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
StopReason.SHUTDOWN, sends the group SIGKILL at once and re-raises. Every way
out closes the subprocess transport: a process that left the group (a
daemon) can hold the output open past the SIGKILL, and a transport left open
would be closed by its `__del__` after the loop had closed ("Event loop is
closed").

A side effect of the own session: a streamed `.aa` command has no controlling
terminal, like a brish command, and a terminal Ctrl-C on the bot no longer
reaches it.

## Settings and the kill switch

`uniborg/shell_settings.py` (a core module) keeps each admin's shell settings
in `UserStorage(purpose="shell")`, one JSON file per user under
`~/.borg/shell/`. `ShellSettings.get(user_id)` gives a `ShellPrefs`, and
`ShellSettings.set(user_id, prefs)` stores only the values that differ from
the defaults, so a changed default reaches everyone who never chose. A stored
value this version does not know is logged and read as its default, and a
file that holds valid JSON other than an object (`["drafts"]`, after a hand
edit) is logged and read as all defaults, so `.a` still answers. The storage
is injected (`ShellSettings(storage=...)`), so the tests use a temp dir.

The settings, with their defaults:
- `stream_private`: how a preview shows in private chats, Drafts. The field
  names and values are the chat bot's (`stream_driver.StreamMode`), and so
  are the helpers that read and write them per scope
  (`stream_driver.stream_mode`, `set_stream_mode`).
- `stream_groups`: the same in groups, Edits.
- `final_mode` (`shell_settings.FinalMode`): what a command that showed a
  preview sends when it ends, EDIT_PREVIEW (the preview becomes the final) or
  NEW_REPLY (a new reply, as before live output).
- `render`: show output as a terminal would (`term_render`), on. It is the
  only setting that also applies to the admin's guest answers.

### The `/settings` panel

On a bot, `/settings` (in `stdplugins/advanced_get.py`) shows an admin's
settings and changes them. It has the gate of `.a` (an admin's message, not
forwarded, not an echoed guest answer), and a non-admin gets no reply.

- In a private chat with the bot it replies with the **panel**: a Markdown
  message that says what each setting does and what it costs, its current
  value, and a row of inline buttons per setting. The rows of the two
  live-output scopes are the chat bot's `/stream` rows
  (`stream_driver.stream_mode_rows`); the others are "When it ends" (Edit
  the preview, New reply) and "Renderer" (On, Off).
- In a group it replies that the panel is in the private chat with the bot,
  and changes nothing.
- Text forms change one setting, then show the panel: `/settings private
  drafts|edits`, `/settings groups drafts|edits`, `/settings final
  edit|reply` and `/settings render on|off`, in any letter case, with any
  blanks between the words, line breaks included. Anything else gets the
  usage line. `/settings@thisbot` works too.
- A button's callback data is `shs:` and the same words joined by ":"
  (`shs:private:edits`, `shs:final:reply`, `shs:render:off`), so a press and
  a text form go through one parser, `setting_change`. Its handler takes
  only data that starts with `shs:`, and is wrapped in
  `callback_util.hold_bare_answers`, so its toast always shows. A press is
  answered first, with a toast that names the new value ("Groups:
  Drafts."), and then the panel is redrawn in place. A press by a non-admin
  gets the toast "Only the bot's admins can do that." and changes nothing;
  data the parser does not know (a button from an older panel) gets a toast
  that asks for a new `/settings`.
- A setting that cannot be saved (the user file's lock timed out) gets "Could
  not save that setting; try again.", by reply or toast.

The panel's text names the costs of each choice: while a bot's draft is
live, Telegram for Android disables the send button, so under Drafts a long
command keeps you from typing (press Stop, or choose Edits; where the
bot's Telethon cannot give a draft a Stop button, the panel says so, since
`.k` cannot be sent either); Edit the preview finishes an edited preview
silently, with no notification, while a draft becomes a new message, and
New reply notifies.

`/help` lists the shell's commands. Both are in the plugin's `BOT_COMMANDS`,
which `bot_util.register_bot_commands` sets as the bot's command menu at
startup; no other plugin of `stdplugins/` sets one, so it replaces nothing.
On a userbot neither command exists: the account's own `/settings`, typed
to another bot, would be answered there too. A userbot still reads the
settings files under `~/.borg/shell/`, so it follows what an admin set
through a bot that runs as the same OS user, and otherwise the defaults (it
cannot show drafts, so its previews are edited messages).

The **kill switch** is the environment variable `borg_shell_streaming`, read
once when the module is first imported. It takes the words of the bot's other
switches (`borg_guest_trigger_guard`, `borg_tg_safety_nets`), through their one
parser, `uniborg/env_switch.py`: unset, empty, "1", "true", "yes" or "on" is on,
"0", "false", "no" or "off" is off, in any letter case and with blanks around
them, and any other value raises rather than guess. `uniborg/uniborg.py` imports
the module, so that error stops the bot at startup. Were the shell plugin the
first to import it, the plugin loader would only log the error and skip the
plugin, and the bot would run on with `.a` answering nothing. Off, `.a`, `.af`
and `.aa` run exactly as before live output: no job, no preview and no renderer.

## Live output in chats

`.a`, `.af` and `.aa` in a chat (private or group, on a bot or a userbot) show
a running command's output while it runs. The code is in
`stdplugins/advanced_get.py`. Terms:

- **Preview**: the message, or draft, that shows the end of a running
  command's output and changes as the output grows.
- **Final**: the output text once the command has ended, built as before live
  output (`util.shell_output_text`, which `send_output` uses too): trimmed, or
  "The process exited N." when empty. A stopped job's final gets a stop note
  after a blank line.
- **Pump**: `_run_live`, which runs the producer and moves its latest output
  into the preview.

### The flow

1. The `.a` handler keeps its gate (an admin's own, unforwarded message, not
   an echoed guest answer), reads the caller's settings, registers a
   `ShellJob` and runs `util.run_and_upload` with `_run_in_chat` as its work.
   So the read receipt, the downloads, the files sent back and `handle_exc`
   stay as they were. The job is finished (`shell_stream.finish`) only after
   the files are sent, so it can be found until then.
2. `_run_in_chat` starts the producer (`util.brishz_capture` on the shell
   pool, or `util.simple_run_capture` for `.aa`, each with `job=`) and runs
   the pump.
3. A command that ends within the preview delay (2 s) shows no preview: its
   final goes out exactly as before (one plain-text reply, or the `.txt` file
   at 4000 characters or more), then its files.
4. Otherwise the preview opens, and `stream_driver.follow` shows the latest
   output in it through a `PacedEditor`, whenever the output or the job's
   state changes, at the preview's pace, until the command ends.
5. The final takes the preview's place, as the caller's final mode says
   (below), and the files follow.

The pump is the only reader of `job.output.changed`; it passes each change on
to `follow`. That way it also sees a job **dropped** while it waited for a
shell, and delivers its final ("⏹ Stopped before it ran.", see the stop
notes below) at once, while the producer frees its shell whenever it gets
one and runs nothing.

### The preview

- **Text**: a header, a blank line, the output's end and the cursor "▌",
  within 3496 UTF-16 units (`PREVIEW_UNITS`). A message holds 4096, but
  `util.edit_message` splits a text into a chain of messages where it can
  break it well: it looks for a newline in the last
  `util.SPLIT_SEARCH_CHARS` (600) characters below that limit, so a text of
  lines longer than 3496 characters would become two messages, and the
  second would outlive the preview. A shorter text is never split. The room
  left also fits a draft's heartbeat suffix ("⏳ 42s"). It is plain text
  (`parse_mode=None`) with no link preview. The first message is sent
  silently, and edits never notify.
- **Header**: "⏳ #3" while the command runs, "⏳ #3 waiting for a free shell"
  while the job is QUEUED (every shell of the pool is busy), and "⏹ #3
  stopping…" once it is stopped. A preview with no Stop button adds
  " · .k to stop": an edited message on a userbot, and a draft on a Telethon
  that cannot build the button (before 1.45). That draft is the worse case:
  on Android it disables the send button for as long as it lives.
- **Kind**: a draft (`draft_stream.DraftAnswerMessage`, opened through
  `stream_driver.open_stream_target` with `parse_mode=None`) when the account
  is a bot, the installed Telethon can send drafts, and the caller's setting
  for this kind of chat is Drafts (private chats, by default). Otherwise, and
  when Telegram refuses the draft (it does in groups), it is a message sent as
  a reply to the command and edited. In a private topic the draft shows in
  that topic.
- **Stop**: a draft has a Stop button on Telethon 1.45
  (`draft_stream.STOP_SUPPORTED`; `_shows_stop_button` asks both). It shows
  on Telegram 10.3 clients. `stream_driver.stop_wired` points it at
  `job.cancel(reason=USER)`, and the plugin registers the press handler with
  `stream_driver.register_draft_stop(borg, module=__name__)`, so a plugin
  reload removes it. An edited preview on a bot has an inline "⏹ Stop"
  button (`_stop_buttons`, built with `tg_compat.callback_button`, so it
  works on both Telethon versions), whose callback data is `shk:` and the
  job id. Edits keep it, since `Message.edit` reuses a message's reply
  markup when `buttons` is left out (and a message sent in a private chat,
  which Telethon builds itself from `UpdateShortSentMessage`, keeps the
  markup of its request, `client/messages.py` in `send_message`); the
  final's edit removes it (`buttons=None`), and so does deleting the
  preview. See "The Stop button" below.
- **Pace**: an edited preview changes at most every 2 s, then every 5 s once
  the command has run 30 s; in groups every 4 s, then every 10 s
  (`stream_driver.tiered_pace`). That is about 10 edits in a group's first
  minute, within its edit budget. A draft keeps the draft worker's own pace,
  about one draft a second, plus a heartbeat every 20 s of quiet so it does
  not expire.

The costs of drafts, which the settings panel names: while a bot's draft is
live, Telegram for Android disables the send button, so under Drafts a long
command keeps you from typing until you press Stop (or choose Edits). An
edited preview that becomes the final changes silently, with no
notification; a new reply notifies.

### The final

A command that showed no preview gets the final exactly as before, in both
modes. After a preview, `ShellPrefs.final_mode` decides:

- **Edit the preview** (EDIT_PREVIEW, the default).
  - A final under 4000 characters (`util.discreet_sends_file`, the rule of
    `discreet_send`) replaces the preview (`stream_driver.show_final`). An
    edited preview is edited in place as plain text, with `buttons=None`:
    Telethon's `Message.edit` reuses the old reply markup when `buttons` is
    left out (`telethon/tl/custom/message.py`, in `Message.edit`, on both
    1.43.2 and 1.45). That edit does not notify. A draft ends its stream, and
    the final is sent as a real reply to the command after a sync draft, the
    way the chat bot's draft becomes its answer; that reply notifies.
  - A final of 4000 characters or more turns the preview into its end, as
    much as fits one message (4096 UTF-16 units), under the first line
    "✂️ The full output is in the file below.". The same `.txt` file as
    before follows, as a reply to that message.
  - If the preview cannot be edited (it was deleted, say), the final is sent
    anew, as before, and the preview is removed.
- **New reply** (NEW_REPLY): the final exactly as before, as a new reply to
  the command, then the edited preview is deleted. A preview that cannot be
  deleted (a bot cannot delete its messages in a group after 48 hours) is
  edited to "Finished; output below." A draft's stream ends without its text
  being sent; before a final sent as text, a sync draft with that text lets
  the client adopt the draft into the reply.

How a draft goes away (docs/telegram_ai_apis.md, section 2.1): no call clears
one. A client drops a draft 30 s after its last update, or replaces it with a
message from the bot that adopts it. Telegram Desktop adopts only a message
that starts like the last draft, hence the sync draft; Android adopts only
while the chat is open. So under New reply, a final sent only as a `.txt`
file has no text to adopt the draft, which can stay up to 30 s, as after the
chat bot's image-only answers.

Stop notes: "⏹ Stopped (exit 130)." after a stop by the user (`.k` or a
Stop button), and "⏹ Stopped: the bot is going offline (exit N)." after a
shutdown. A dropped job's final is the note alone: "⏹ Stopped before it
ran." after a stop by the user, "⏹ Stopped before it ran: the bot is going
offline." after a shutdown. A shutdown note says "going offline" because
`.restart`, `.shutdown` and a server stop all take the bot offline, for a
while or for good, and it names no bot. The job records why it was
stopped, not who stopped it, so the note does not name anyone either.

### The renderer in chats

With the caller's `render` setting on (the default), what a chat shows of
the output is rendered as a terminal would show it (`term_render`, above):
the preview (`LiveOutput.tail_text(render=True)`), the final, and the `.txt`
file of a long final, for `.a`, `.af` and `.aa` alike, and for guest answers
(see "Live guest answers"). So a progress bar shows
its last frame, colours are dropped, and for `.aa` rendering replaces the old
translation of `\r` into a newline (`\r\n` stays a newline). Output with
none of `\r`, `\b` or ESC is unchanged. Off, the final is the output as
before: raw for brish, newline-translated for `.aa`.

A final over `RENDER_ON_LOOP_BYTES` (64 KiB) is rendered in a thread of the
default executor: at the worst rate measured above (0.35 s per MiB), 64 KiB
holds the event loop for about 20 ms, while the 32 MiB a job may keep could
hold it for seconds. A preview renders at most `PREVIEW_BYTES` (16 KiB) and
stays on the loop.

On a brish without `popen`, `.a` renders its captured output the same way.

### Failures and fallbacks

- **The producer raises**: the preview is removed, and `handle_exc` posts the
  traceback, as before.
- **The preview cannot be sent**: logged; the command runs on, and its final
  is sent as if there had been no preview.
- **A preview edit fails**: `follow` waits a second, or the preview's pace
  when that is longer (4 s in a group), before the next try, and doubles the
  wait after each further failure in a row, up to a minute
  (`stream_driver.retry_wait`). A preview that someone deleted, or one in a
  group the bot was removed from, so costs a few edits in its first minute
  and then one a minute, not one a second for as long as the command runs.
  An edit that works starts the count again.
- **The handler's task is cancelled** (the client disconnecting, or a
  standalone bot's loop shutting down; see "Shutdown and restarts"): the
  job is stopped with StopReason.SHUTDOWN, the producer is cancelled, and
  the cancel propagates. A cancel while the files are sent propagates too
  (`util.upload_output_files` and `send_files` re-raise it), rather than
  being reported as a failed upload through a closing client. So does one
  during the read receipt that `util.run_and_upload` sends first, and the
  command then never runs; a receipt that fails is still ignored.
- `util.forget_edit_chain(preview)` drops `util.edit_message`'s record of the
  preview once the pump is done, so it does not outlive the command.
- **`borg_shell_streaming=0`**: `.a`, `.af` and `.aa` run exactly as before:
  no job, no preview, no renderer.
- **A brish without `Brish.popen`**: `.a` and `.af` run as before, with no job
  and no preview, since they could not be stopped (only the renderer
  applies); `.aa` still streams.

Tests: `tests/test_advanced_get_shell.py` drives the handler with a fake chat
and a fake producer that follows a script, with the timings injected
(`LIVE_TIMING`); a few tests run inert commands in a real zsh. The test run
(`tests/conftest.py`) points ZDOTDIR at an empty directory, so no zsh it
starts, `.aa`'s or a brish worker's, reads the user's startup files, whose
output would otherwise land in a command's. The bot leaves ZDOTDIR alone.

## Stopping a command: `.k`

`.k` (`kill_handler` in `stdplugins/advanced_get.py`) stops a job. It is
registered right after the `.a` handler, has the same gate (a non-admin, a
forwarded `.k` and an echoed guest answer get nothing), and its pattern,
`.k` alone or followed by whitespace and any text, never matches
`pattern_a`. Every `.k` from an admin gets a plain-text reply: the pattern
takes any text after `.k`, so a form it does not know (`.k 3 5`) gets the
usage line rather than silence.

The jobs it can see are the **visible** jobs (`shell_stream.visible`): the
jobs of this chat, any admin's, and in an admin's private chat with the bot
also that admin's guest jobs. Of those, only the **running** ones count:
QUEUED, RUNNING or STOPPING. An ENDED job, whose final or files are still
being sent, has nothing left to stop.

- **As a reply** to a command, or to its preview: stops that job
  (`shell_stream.find` matches either message). In a topic, where every
  message carries a reply header, only a real reply counts
  (`topics.resolve_reply_target`); a reply to any other message says "That
  message has no running command."
- **Alone**: stops the only running job; with several, lists them instead of
  guessing; with none, says "No running command here."
- **`.k N`** (or `.k #N`): stops job N, if it is visible and running.
- **`.k all`**: stops every running visible job, one reply line each.
- **`.k ls`**: lists them, a line each: id, age, "waiting" or "stopping"
  when it is, "guest" for a guest job, and the first 60 characters of the
  command on one line.
- Anything else gets the usage line.

A stop is `job.cancel(reason=StopReason.USER)`. Its reply follows the
`CancelOutcome`: "⏹ Stopping #3…" (STOPPING, or NOT_STARTED for a job that
was still waiting for a shell and will now never run), "#3 is already
stopping." or "#3 has already ended." The command may take seconds to end
(see the producers above); the preview's header says "⏹ #3 stopping…" until
it does, and the final ends with the stop note.

`.k` sees only jobs, so a reply that finds nothing says why a command might
still be running unseen: with `borg_shell_streaming=0` no command is a job
("Live output is off (borg_shell_streaming=0), so no command can be
stopped."), and on a brish without `popen` `.a` and `.af` are not jobs
(".a cannot be stopped until brish is upgraded; .aa can.").

The registry lives in the core module `shell_stream`, so a `.k` from a
reloaded plugin still sees the jobs that started on the old code.

### `@bot .k` in a guest chat

`@<bot> .k`, sent in a guest chat (docs/guest_mode.md), stops the caller's
own running guest jobs of that chat: `shell_stream.visible` with the query's
`thread_key`, which matches only jobs that this caller started from this
guest chat. It takes the trigger of the guest shell (the mention first, then
whitespace, then `.k`; `guest_util.shell_command_after_mention`), so a
non-admin gets "Not available here." and a loose form gets the usage line.
The forms are those of `.k` (`kill_text` serves both) except a reply, which
names no job here: alone it stops the only running job or lists several,
and `N`, `all` and `ls` work as above. The lists and the usage line name
`@<bot> .k` rather than `.k`. Each call is answered with a guest note
(`guest_util.answer_note`), so it costs one message in the chat. In the
caller's private chat with the bot, a plain `.k` sees the same jobs.

A userbot's trigger guard (`guest_util.OutgoingTriggerGuardMixin`) defangs
`@somebot .k` as it does `@somebot .a`, so text the userbot relays cannot
stop its owner's guest commands.

### The Stop button

`stop_press_handler` takes the presses whose data starts with `shk:` (a
CallbackQuery pattern), on bots only, and is wrapped in
`callback_util.hold_bare_answers`, so its toast always shows. `buttons_test`
no longer answers presses that are not its own, whatever order the plugins
load in.

- A press by anyone but an admin (`util.isAdmin`, as for `.a`) gets the
  toast "Only the bot's admins can do that." and stops nothing.
- Otherwise the job is `shell_stream.JOBS[id]`, if it is in the chat of the
  press and the pressed message is its preview (`job.preview_id`), and it is
  stopped as by `.k`: the toast is the same text ("⏹ Stopping #3…", "#3 is
  already stopping.", "#3 has already ended."), and the header turns to "⏹
  #3 stopping…" at the preview's next edit.
- A job that is no longer registered (it finished, or the bot restarted)
  gets "#3 has already ended.", and data whose id is not ASCII digits "That
  button is out of date." The preview check matters after a restart: job ids
  start again at 1 in each process, and a preview that a shutdown or a crash
  left with its button would otherwise stop the newer job that got the same
  id.

## Live guest answers

`@<bot> .a CMD` from a guest chat (docs/guest_mode.md) runs as a job too,
and its answer shows the output live. The code is `_run_guest_shell` and
`_run_guest_live` in `stdplugins/advanced_get.py`.

- **The job** is registered before the replied-to files download, with the
  caller as its owner, no chat (`chat_id=None`) and the query's
  `thread_key`, so `@<bot> .k` in that guest chat and `.k` in the caller's
  private chat with the bot see it. It is finished after the answer's last
  edit. With `borg_shell_streaming=0`, or for `.a` on a brish without
  `popen`, there is no job and the answer is as before live output.
- **The preview** is the guest answer itself: it says "⏳ Running…" when
  posted, as before, and a command still running after the preview delay
  (2 s) turns it into the header, the output's end and the cursor, as in a
  chat, through the same pump (`_run_live`) and the same text
  (`_preview_text`, within `PREVIEW_UNITS`). It is never a draft, and the
  settings' preview kinds and final modes do not apply. It changes at the
  edit pace of a private chat or of a group, after the kind of the guest
  chat, and `guest_util.GuestAnswerMessage` keeps its own edits at least
  1.2 s apart and skips them during a flood wait. A guest answer has no
  Stop button, so the header always names the command that stops it:
  "⏳ #3 · @bot .k to stop".
- **The final** is the answer's last edit (`finalize`), built as before:
  the output as plain text, cut to fit, with the lines under it. A command
  that ends within the preview delay still makes exactly one edit. After a
  stop, a line "⏹ Stopped" (or "⏹ Stopped: the bot is going offline" on a
  shutdown) follows "exit N". A job stopped while it waited for a shell
  says "⏹ Stopped before it ran." alone (on a shutdown, "⏹ Stopped before
  it ran: the bot is going offline.").
- **The renderer** follows the caller's `render` setting (the default, on,
  when they never chose), for the preview, the final and the `output.txt`
  of long output. Off, and with live output off, the output is raw as
  before; on a brish without `popen`, `.a` is rendered from its captured
  output, as in a chat.
- **Failures**: a producer's error leaves the preview in place (a guest
  answer cannot be deleted), and the traceback replaces it, as before.

## Shutdown and restarts

A shutdown stops every job first, while the bot is still connected, so each
stopped command's final goes out: "⏹ Stopped: the bot is going offline (exit
N)." in a chat, "⏹ Stopped: the bot is going offline" under the exit code of a
guest answer. `shell_stream.stop_all_and_disconnect(client)` cancels every job
with StopReason.SHUTDOWN, waits up to `SHUTDOWN_TIMEOUT` (15 s) for them to
finish (their finals and files delivered), logs how many had not, and then
disconnects. The bot still takes commands while it waits, so until the
disconnect is done, `shell_stream.register` stops each new job at once: its
command never runs, and `stop_all` waits for its final ("⏹ Stopped before it
ran: the bot is going offline.") as for the others; only a job that arrives
during the disconnect itself may lose its final. A command that is no job (live
output off, or `.a` on an old brish) is not stopped. Three paths call it:

- **Under uvicorn** (`start_server.py`), the server's shutdown event
  (`stdborg.shutdown_event`) runs on SIGINT or SIGTERM, while the event loop
  still runs.
- **`.restart` and `.shutdown`** (`stdplugins/power_tools.py`) reply
  ("Restarted.", "Turning off ..."), run it, then re-execute the bot or exit.
  Each must be the whole message (any letter case, blanks around it
  allowed): Telethon matches a pattern from the start only, so a bare
  `.restart` also took "/restart", "#restart" or ".restarted", which on a
  userbot can be any of the owner's own messages.
  Both reply rather than edit the command, since a bot cannot edit a message
  it did not write. They run it in a task of their own: Telethon's
  `disconnect` cancels every running event handler, the one that calls it
  included, so code after it in the handler would never run.
- **Leftovers**: a job that has not finished when the time runs out has its
  handler cancelled by the disconnect (`_disconnect_coro` in Telethon's
  `telegrambaseclient.py` cancels and awaits its event handler tasks). The
  pump's cancel path then kills the command, with no final.

**Standalone** (`python3 stdborg.py`), Ctrl-C ends `asyncio.run`, which
cancels every task before it closes the loop; there is no time to send
anything. Each job's pump takes its cancel path: the job is stopped with
SHUTDOWN, a streamed `.aa` gets SIGKILL for its process group at once, and a
brish command gets its popen's kill. The loop's default executor then waits
for the brish threads, which end once their commands do (a command that
ignores SIGINT ends at a later step of brish's kill, within about 13 s), so
a `tail -f` no longer keeps the process from exiting.

## Limits

- **Block-buffered programs.** Commands run with pipes, not a terminal, and
  nothing fakes one, so a program that buffers its output when it is not
  writing to a terminal (C stdio, Python) shows it in blocks of a few KiB,
  or only when it exits. Ask it to flush each line: `python3 -u`, `stdbuf
  -oL CMD` (for C programs that use stdio), `grep --line-buffered`. The bot
  does not set `PYTHONUNBUFFERED` for you.
- **Daemons survive a stop.** A stop reaches only the command's own
  processes: for brish, the worker's descendants at each kill step
  (popen-api.md: `kill()` signals nothing that has left the worker's
  process tree); for `.aa`, the command's process group. A program that
  detached itself (a daemon that forked twice and was reparented, or one
  started with `setsid`) is not signalled, and outlives the stop and a
  shutdown, as before live output.
- **Binary output.** A preview shows invalid UTF-8 as U+FFFD; the final shows
  it as `\xNN` escapes, for `.aa` too. With brish, an output line that holds
  only a NUL still ends the stream early (popen-api.md, legacy mode), as it
  did before live output. Write binary data to a file.
- **Two drafts in one chat.** Clients keep one draft per sender and thread,
  so two commands previewed as drafts at once in a private chat overwrite
  each other's draft until one ends (docs/draft_streaming.md, "Known
  limits"). Their finals are unaffected; Edits avoids it.
- **No time limit, and endless output.** A command runs until it ends or is
  stopped. One that writes without pause (`yes`) keeps the bot reading at
  full speed until it is stopped; memory stays within the caps above.
- **Stopping a queued job.** A job that waits for a worker (every worker
  busy, or worker 0 for `.af`) is dropped at once by `.k`, and its final
  goes out. With brish 0.4.1 its executor thread gives up the wait too,
  within about 0.05 s (brish's poll of `cancelled=`). The tests require an
  `.af` queued behind a busy worker 0 to return within 0.4 s of the stop,
  while worker 0 stays busy; it took 0.03 to 0.09 s in runs on the
  development machine. With 0.4.0 that thread still waits until it gets a
  worker, then runs nothing, so a job queued behind an endless command holds
  one thread of the event loop's default executor until that command is
  stopped.
