"""`uniborg/term_render.py`: output rendered as a terminal would show it."""

import time
import unittest
from unittest.mock import patch

from uniborg import term_render
from uniborg.term_render import TerminalRenderer, line_aligned, render

ESC = "\x1b"


class RenderTests(unittest.TestCase):
    def test_plain_text_is_unchanged(self):
        for text in ("", "a", "a\nb\n", "\n\n", "tab\there", "é😀\n"):
            with self.subTest(text=text):
                self.assertIs(render(text), text)

    def test_a_carriage_return_overwrites_from_column_zero(self):
        self.assertEqual(render("abcdef\rXY"), "XYcdef")

    def test_a_progress_bar_keeps_its_last_frame(self):
        self.assertEqual(render(" 10%\r 50%\r100%\ndone\n"), "100%\ndone\n")

    def test_crlf_is_a_newline_and_a_trailing_cr_changes_nothing(self):
        self.assertEqual(render("a\r\nb\r\n"), "a\nb\n")
        self.assertEqual(render("abc\r"), "abc")

    def test_backspace_moves_back_but_not_past_column_zero(self):
        self.assertEqual(render("abc\bX"), "abX")
        self.assertEqual(render("a\b\b\bX"), "X")
        self.assertEqual(render("ab\b\b\n"), "ab\n")

    def test_colours_are_removed(self):
        self.assertEqual(render(f"{ESC}[1;31mred{ESC}[0m {ESC}[mx"), "red x")

    def test_erase_in_line(self):
        self.assertEqual(render(f"abcdef\r{ESC}[Kxy"), "xy")
        self.assertEqual(render(f"abcdef\rab{ESC}[0K"), "ab")
        self.assertEqual(render(f"abcdef\rab{ESC}[1K"), "   def")
        self.assertEqual(render(f"abcdef{ESC}[2K\rxy"), "xy")
        #: The cursor stays where it was, so later text starts there.
        self.assertEqual(render(f"abc{ESC}[2Kd"), "   d")

    def test_cursor_up_redraws_earlier_lines(self):
        frames = (
            "bar 1:  0%\nbar 2:  0%"
            f"\r{ESC}[A\rbar 1: 50%\nbar 2: 50%"
            f"\r{ESC}[1A\rbar 1:100%\nbar 2:100%\n"
        )
        self.assertEqual(render(frames), "bar 1:100%\nbar 2:100%\n")

    def test_cursor_up_stops_at_the_first_line(self):
        self.assertEqual(render(f"a\nb{ESC}[5A\rX"), "X\nb")
        self.assertEqual(render(f"a\nb{ESC}[0A\rX"), "X\nb")

    def test_moving_onto_a_shorter_line_pads_with_blanks(self):
        self.assertEqual(render(f"a\nlonger{ESC}[Ax"), "a     x\nlonger")

    def test_other_sequences_are_dropped(self):
        for text in (
            f"{ESC}[?25lhidden{ESC}[?25h",
            f"{ESC}[2Jhidden",
            f"{ESC}[10;5Hhidden",
            f"{ESC}]0;a title\x07hidden",
            f"{ESC}]8;;http://x{ESC}\\hidden{ESC}]8;;{ESC}\\",
            f"{ESC}(B{ESC}[mhidden",
            f"{ESC}7hidden{ESC}8",
        ):
            with self.subTest(text=text):
                self.assertEqual(render(text), "hidden")

    def test_broken_sequences_do_not_leak_their_parameters(self):
        self.assertEqual(render(f"a{ESC}[31\nb"), "a\nb")
        self.assertEqual(render(f"a{ESC}\nb"), "a\nb")
        self.assertEqual(render(f"a{ESC}[3"), "a")
        self.assertEqual(render(f"a{ESC}"), "a")

    def test_multi_part_and_private_parameters_are_not_misread(self):
        self.assertEqual(render(f"abc\r{ESC}[?1Kx"), "xbc")
        self.assertEqual(render(f"a\nb{ESC}[1;2A\rX"), "a\nX")

    def test_lone_surrogates_pass_through(self):
        #: As `surrogateescape` decoding leaves invalid bytes.
        self.assertEqual(render("a\udcffb\rX"), "X\udcffb")


def _best_time(text, *, runs=3):
    best = float("inf")
    for _ in range(runs):
        started = time.perf_counter()
        render(text)
        best = min(best, time.perf_counter() - started)
    return best


class CostTests(unittest.TestCase):
    def test_the_cost_grows_linearly_along_one_long_coloured_line(self):
        #: `jq -C -c` output: one line, a colour code every few characters.
        unit = f'{ESC}[1;34m"key"{ESC}[0m:{ESC}[0;32m"value"{ESC}[0m,'
        small = _best_time(unit * 4000)
        large = _best_time(unit * 32000)

        #: 8 times the text: about 8 times the time when linear, about 27
        #: when every write copies the line.
        self.assertLess(large / small, 16)

    def test_redrawing_after_a_long_prefix_costs_what_is_redrawn(self):
        frames = "".join(f"\r{i:5}%" for i in range(100000)) + f"{ESC}[K\n"
        self.assertEqual(render("x" * 10 + frames), "99999%\n")

        short = _best_time("x" * 2**16 + frames, runs=2)
        long = _best_time("x" * 2**19 + frames, runs=2)

        #: The same frames after a prefix 8 times as long: about the same
        #: time, not about 6 times as much.
        self.assertLess(long / short, 2)

    def test_a_redraw_under_a_long_line_does_not_copy_it(self):
        """Counted in code points converted, so a busy machine cannot fail it."""
        converted = []

        def counting(convert):
            def wrapper(value):
                converted.append(len(value))
                return convert(value)

            return wrapper

        long = "x" * 100_000
        with patch.object(
            term_render, "_code_points", counting(term_render._code_points)
        ), patch.object(term_render, "_text_of", counting(term_render._text_of)):
            text = render(long + f"{ESC}[1Ay\n" * 1000)

        self.assertEqual(text, "y" + long[1:] + "y\n")
        #: The long line once each way, not twice per redraw (2e8).
        self.assertLess(sum(converted), 3 * len(long))

    def test_lines_rewritten_in_turn_keep_their_text(self):
        """More lines than keep their arrays, so some are given up and
        taken again."""
        rows = term_render.ACTIVE_LINES + 4
        text = "".join(f"{row}:0\n" for row in range(rows)) + "".join(
            f"{ESC}[{rows}A" + "".join(f"\r{row}:{step}\n" for row in range(rows))
            for step in (1, 2)
        )

        self.assertEqual(render(text), "".join(f"{row}:2\n" for row in range(rows)))


class IncrementalTests(unittest.TestCase):
    def test_pieces_render_like_the_whole(self):
        text = f"one\r{ESC}[1;32mtwo{ESC}[0m\nthree\b\bX{ESC}[K\n{ESC}[A\rZ"
        for size in (1, 2, 3, 5):
            with self.subTest(size=size):
                renderer = TerminalRenderer()
                for i in range(0, len(text), size):
                    renderer.feed(text[i : i + size])
                self.assertEqual(renderer.text(), render(text))

    def test_a_sequence_split_across_pieces_waits_for_its_end(self):
        renderer = TerminalRenderer().feed(f"x{ESC}[3")
        self.assertEqual(renderer.text(), "x")
        self.assertEqual(renderer.feed("1my").text(), "xy")


class LineAlignedTests(unittest.TestCase):
    def test_starts_at_the_first_whole_line(self):
        self.assertEqual(line_aligned("ar 40%\rbar 90%\nnext\n"), "next\n")
        self.assertEqual(line_aligned("\nall"), "all")

    def test_a_tail_without_a_newline_is_kept(self):
        self.assertEqual(line_aligned("no newline\rX"), "no newline\rX")

    def test_a_tail_with_nothing_after_its_first_newline_is_kept(self):
        #: The end of one long line: dropping it would leave nothing.
        self.assertEqual(line_aligned('vvv"}\n'), 'vvv"}\n')
        self.assertEqual(line_aligned("vvv\n\n  \n"), "vvv\n\n  \n")

    def test_a_cursor_up_in_an_aligned_tail_stays_inside_it(self):
        tail = line_aligned(f"cut\nkept\nlast{ESC}[9A\rK")
        self.assertEqual(render(tail), "Kept\nlast")


if __name__ == "__main__":
    unittest.main()
