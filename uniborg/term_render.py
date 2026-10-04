"""Shows a program's output as a terminal would, as plain text.

Programs that draw progress bars write control characters that only mean
something on a terminal: a carriage return (`\\r`) to redraw the current line,
a backspace (`\\b`), and ANSI escape sequences for colours, erasing and moving
the cursor. Shown raw in a message, a progress bar becomes every one of its
frames in a row, wrapped in escape codes.

`render` applies them, as far as plain text can:
- `\\r` moves to column 0, and later characters overwrite from there
  (`abcdef\\rXY` renders `XYcdef`);
- `\\n` ends the line, moving down a line and to column 0 (onto a line that
  already exists after a cursor-up, otherwise a new one);
- `\\b` moves back one column, never past column 0;
- of the CSI sequences (`ESC [ ... final`): SGR (`m`, colours and styles) is
  removed; `K` erases to the end of the line (`ESC[K`, `ESC[0K`), from its
  start through the cursor (`ESC[1K`, as blanks) or all of it (`ESC[2K`); `A`
  moves the cursor up N lines (default 1, never above the first), as
  multi-bar tqdm does. Every other CSI sequence is dropped.
- Other escape sequences (OSC strings such as a window title, and the short
  ones such as `ESC (B` that `tput sgr0` writes) are dropped.

Every other character is written as it is, one column each: tabs and wide
characters are not expanded. There is no screen and no cursor addressing
beyond this; text without `\\r`, `\\b` or ESC is returned unchanged.

A *tail* of a longer output can start in the middle of a line, where a `\\r`
or a cursor-up would act on text that is not there. `line_aligned` cuts such
a tail to its first whole line, so rendering it stays inside the tail; a tail
that holds only the end of one line is kept whole.

Rendering takes time linear in the text: a line written to is kept as an
array of code points, so writing, overwriting and erasing in it cost what
they change, not the length of the line. The arrays of the last
`ACTIVE_LINES` lines written to are kept, so moving between them (a newline
or a cursor-up) costs nothing, however long they are, and a redraw under a
long line does not copy it each time. Only a program that keeps rewriting
more lines than that in turn pays the length of each line it comes back to.

This module imports only the standard library.
"""

from array import array
import re
import sys

_CSI = r"\x1b\[(?P<params>[0-?]*)[ -/]*(?P<final>[@-~])?"
_OSC = r"\x1b\][^\x07\x1b]*(?P<osc_end>\x07|\x1b\\)?"
_ESCAPE = r"\x1b[ -/]*(?P<esc_final>[0-~])?"
#: Within one line: `feed` splits its text at newlines first.
_TOKEN = re.compile(f"{_CSI}|{_OSC}|{_ESCAPE}|[\r\b]")
_NEEDS_RENDERING = re.compile(r"[\r\b\x1b]")

#: How many lines keep their arrays; a multi-bar progress display redraws
#: a few lines in turn.
ACTIVE_LINES = 16

#: An array type of 4-byte items: one code point each.
_CODE_POINT_TYPE = next(code for code in "IL" if array(code).itemsize == 4)
_UTF32 = "utf-32-le" if sys.byteorder == "little" else "utf-32-be"


def _code_points(text: str) -> array:
    """TEXT as an array of its code points; lone surrogates pass."""
    points = array(_CODE_POINT_TYPE)
    points.frombytes(text.encode(_UTF32, "surrogatepass"))
    return points


def _text_of(points: array) -> str:
    return points.tobytes().decode(_UTF32, "surrogatepass")


class TerminalRenderer:
    """Renders output fed to it piece by piece.

    `feed` takes text in any pieces, even one that ends inside an escape
    sequence (that part waits for the next piece), and `text` gives what is
    rendered so far.
    """

    def __init__(self):
        self._lines = [""]
        self._row = 0
        self._col = 0
        self._pending = ""
        #: Rows written to lately, as code points, the latest last; a row's
        #: entry in `_lines` is out of date while it is here.
        self._active = {}

    def feed(self, text: str) -> "TerminalRenderer":
        text = self._pending + text
        self._pending = ""
        segments = text.split("\n")
        last = len(segments) - 1
        for i, segment in enumerate(segments):
            if i:
                self._newline()
            if _NEEDS_RENDERING.search(segment):
                self._feed_controls(segment, at_end=(i == last))
            elif segment:
                self._write(segment)
        return self

    def text(self) -> str:
        for row, points in self._active.items():
            self._lines[row] = _text_of(points)
        self._active.clear()
        return "\n".join(self._lines)

    def _line(self) -> array:
        """The cursor's line, as code points to change in place."""
        row = self._row
        points = self._active.get(row)
        if points is None:
            if len(self._active) >= ACTIVE_LINES:
                oldest = next(iter(self._active))
                self._lines[oldest] = _text_of(self._active.pop(oldest))
            points = self._active[row] = _code_points(self._lines[row])
        elif next(reversed(self._active)) != row:
            #: The latest last: the oldest is the one to give up.
            self._active[row] = self._active.pop(row)
        return points

    def _write(self, run: str) -> None:
        col = self._col
        self._col = col + len(run)
        if col == 0 and self._row not in self._active and not self._lines[self._row]:
            #: A fresh line, the common case, needs no array.
            self._lines[self._row] = run
            return
        line = self._line()
        if col > len(line):
            line.extend(_code_points(" " * (col - len(line))))
        line[col : col + len(run)] = _code_points(run)

    def _feed_controls(self, segment: str, *, at_end: bool) -> None:
        """Feeds SEGMENT, a piece of one line with control characters in it."""
        pos = 0
        for match in _TOKEN.finditer(segment):
            if match.start() > pos:
                self._write(segment[pos : match.start()])
            pos = match.end()
            if at_end and pos == len(segment) and _unfinished(match):
                self._pending = match.group()
                return
            self._apply(match)
        if pos < len(segment):
            self._write(segment[pos:])

    def _newline(self) -> None:
        self._row += 1
        self._col = 0
        if self._row == len(self._lines):
            self._lines.append("")

    def _apply(self, match) -> None:
        token = match.group()
        if token == "\r":
            self._col = 0
        elif token == "\b":
            self._col = max(0, self._col - 1)
        elif token.startswith("\x1b[") and match.group("final"):
            self._csi(match.group("params"), match.group("final"))

    def _csi(self, params: str, final: str) -> None:
        if final not in "KA":
            return
        if params and not params.isdigit():
            #: A private (`?`) or multi-part parameter: not one of ours.
            return
        n = int(params or 0)
        if final == "A":
            self._row = max(0, self._row - max(n, 1))
            return
        line = self._line()
        col = self._col
        if n == 0:
            del line[col:]
        elif n == 1:
            end = min(col + 1, len(line))
            line[:end] = _code_points(" " * end)
        elif n == 2:
            del line[:]


def _unfinished(match) -> bool:
    """Whether MATCH is an escape sequence cut off before its end."""
    token = match.group()
    if not token.startswith("\x1b"):
        return False
    if token.startswith("\x1b["):
        return match.group("final") is None
    if token.startswith("\x1b]"):
        return match.group("osc_end") is None
    return match.group("esc_final") is None


def render(text: str) -> str:
    """TEXT as a terminal would show it; see the module docstring."""
    if not _NEEDS_RENDERING.search(text):
        return text
    renderer = TerminalRenderer().feed(text)
    #: A sequence still unfinished at the very end is never completed now.
    return renderer.text()


def line_aligned(text: str) -> str:
    """TEXT from the start of its first whole line.

    For a tail cut from a longer output: its first line is usually the end of
    a longer one. When nothing but blanks follows that line (TEXT is the end
    of one long line, with or without its newline), TEXT is returned whole,
    as the best there is.
    """
    newline = text.find("\n")
    if newline < 0:
        return text
    rest = text[newline + 1 :]
    return rest if rest.strip() else text
