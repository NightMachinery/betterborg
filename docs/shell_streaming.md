# Shell Streaming

How the shell (`.a`, `.af`, `.aa` and the guest shell in
`stdplugins/advanced_get.py`) runs commands so that their output can be shown
while they run, and stopped. This file describes the producers: the code that
runs a command and collects its bytes. The chat side (the live preview, `.k`,
the Stop button and `/settings`) builds on them and is not written yet.

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
