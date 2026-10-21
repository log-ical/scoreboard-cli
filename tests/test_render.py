import io
import random
import re
import time
import unittest
from unittest import mock

from tests.helpers import ROOT  # noqa: F401  (sets sys.path)
from scoreboard import render, term
from scoreboard.render import logo_lines
from scoreboard.term import BASIC16, RESET, Colors, Terminal, center, visible_len

MODES = ("truecolor", "256", "16")


# --------------------------------------------------------------------------
# What the code did before it was optimized, to compare against.

def reference_nearest16(rgb):
    return min(range(16), key=lambda i: sum((a - b) ** 2 for a, b in zip(rgb, BASIC16[i])))


def reference_logo_lines(grid, colors):
    lines = []
    for y in range(0, len(grid), 2):
        top = grid[y]
        bot = grid[y + 1] if y + 1 < len(grid) else [None] * len(top)
        s = []
        for t, b in zip(top, bot):
            if t is None and b is None:
                s.append(RESET + " ")
            elif b is None:
                s.append(RESET + colors.fg(t) + "▀")
            elif t is None:
                s.append(RESET + colors.fg(b) + "▄")
            else:
                s.append(colors.fg(t) + colors.bg(b) + "▀")
        lines.append("".join(s) + RESET)
    return lines


def reference_clip(line, cols):
    """line cut to cols visible cells, one character or escape at a time."""
    out, n = [], 0
    for m in re.finditer(r"\x1b\[[0-9;]*m|.", line, re.S):
        if not m.group().startswith("\x1b"):
            if n == cols:
                break
            n += 1
        out.append(m.group())
    return "".join(out)


def reference_draw(lines, rows):
    out = ["\x1b[H"]
    for i in range(rows):
        out.append((lines[i] if i < len(lines) else "") + RESET + "\x1b[K")
        if i < rows - 1:
            out.append("\r\n")
    return "".join(out)


# --------------------------------------------------------------------------
# Just enough of a terminal to see what ends up on screen.

SGR_OR_CHAR = re.compile(r"\x1b\[([0-9;]*)m|(.)", re.S)


def cells(text, state=None):
    """Per-cell (char, fg, bg, attrs) for styled text, and the state after it.
    A space's foreground doesn't show, so it is left out."""
    fg, bg, attrs = state or (None, None, frozenset())
    out = []
    for m in SGR_OR_CHAR.finditer(text):
        if m.group(2) is not None:
            ch = m.group(2)
            out.append((ch, None if ch == " " and "reverse" not in attrs else fg, bg, attrs))
            continue
        params = [int(p or 0) for p in m.group(1).split(";")]
        i = 0
        while i < len(params):
            p = params[i]
            if p == 0:
                fg, bg, attrs = None, None, frozenset()
            elif p in (1, 2, 7):
                attrs = attrs | {{1: "bold", 2: "dim", 7: "reverse"}[p]}
            elif p in (38, 48):
                n = 5 if params[i + 1] == 2 else 3
                value, i = tuple(params[i + 1:i + n]), i + n - 1
                fg, bg = (value, bg) if p == 38 else (fg, value)
            elif 30 <= p <= 37 or 90 <= p <= 97:
                fg = p
            elif 40 <= p <= 47 or 100 <= p <= 107:
                bg = p
            else:
                raise ValueError(p)
            i += 1
    return out, (fg, bg, attrs)


class Screen:
    CSI = re.compile(r"\x1b\[(\??)([0-9;]*)([A-Za-z])")

    def __init__(self, cols, rows, fill="#"):
        self.cols, self.rows = cols, rows
        self.grid = [[(fill, "")] * cols for _ in range(rows)]
        self.r = self.c = 0
        self.sgr = ""

    def feed(self, data):
        i = 0
        while i < len(data):
            m = self.CSI.match(data, i)
            if m:
                private, params, final = m.groups()
                i = m.end()
                if private:
                    continue   # modes such as synchronized output
                if final == "m":
                    self.sgr = "" if params in ("", "0") else self.sgr + params + ";"
                elif final == "H":
                    r, c = (params.split(";") + ["", ""])[:2]
                    self.r, self.c = int(r or 1) - 1, int(c or 1) - 1
                elif final == "K":
                    for x in range(self.c, self.cols):
                        self.grid[self.r][x] = (" ", self.sgr)
                else:
                    raise ValueError(m.group(0))
                continue
            ch = data[i]
            i += 1
            if ch == "\r":
                self.c = 0
            elif ch == "\n":
                assert self.r < self.rows - 1, "the screen scrolled"
                self.r += 1
            else:
                assert self.c < self.cols, "a line wrapped"
                self.grid[self.r][self.c] = (ch, self.sgr)
                self.c += 1


class FakeTerminal(Terminal):
    def __init__(self, cols, rows):
        with mock.patch("sys.stdin", mock.Mock(fileno=lambda: 0)):
            super().__init__()
        self.cols, self.rows = cols, rows

    def size(self):
        return self.cols, self.rows

    def draw_captured(self, lines):
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            self.draw(lines)
        return out.getvalue()


# --------------------------------------------------------------------------

class DrawTest(unittest.TestCase):
    def test_first_frame_is_the_full_screen(self):
        t = FakeTerminal(20, 4)
        lines = ["one", "\x1b[1mtwo" + RESET]
        self.assertEqual(t.draw_captured(lines),
                         term.SYNC_BEGIN + reference_draw(lines, 4) + term.SYNC_END)

    def test_only_changed_rows_are_written(self):
        t = FakeTerminal(20, 5)
        t.draw_captured(["title", "", "1:00", "logo", "footer"])
        self.assertEqual(t.draw_captured(["title", "", "1:00", "logo", "footer"]), "")
        out = t.draw_captured(["title", "", "0:59", "logo", "footer"])
        self.assertEqual(out, term.SYNC_BEGIN + "\x1b[3;1H0:59" + RESET + "\x1b[K" + term.SYNC_END)

    def test_blanking_and_short_frames_clear_old_rows(self):
        t = FakeTerminal(10, 3)
        t.draw_captured(["a", "", "c"])
        out = t.draw_captured([])
        self.assertEqual(out, term.SYNC_BEGIN + "\x1b[1;1H" + RESET + "\x1b[K"
                         + "\x1b[3;1H" + RESET + "\x1b[K" + term.SYNC_END)

    def test_resize_invalidate_and_time_repaint_everything(self):
        t = FakeTerminal(10, 3)
        t.draw_captured(["a", "b"])
        t.cols = 12
        self.assertEqual(t.draw_captured(["a", "b"]), term.SYNC_BEGIN + reference_draw(["a", "b"], 3) + term.SYNC_END)
        t.invalidate()
        self.assertEqual(t.draw_captured(["a", "b"]), term.SYNC_BEGIN + reference_draw(["a", "b"], 3) + term.SYNC_END)
        self.assertEqual(t.draw_captured(["a", "b"]), "")
        t._full_at = time.monotonic() - term.FULL_REDRAW_EVERY - 1
        self.assertEqual(t.draw_captured(["a", "b"]), term.SYNC_BEGIN + reference_draw(["a", "b"], 3) + term.SYNC_END)

    def test_full_width_rows_keep_their_last_cell(self):
        # ESC[K after a full-width row would erase its last cell (the cursor
        # is parked there), so it's only sent when there's something to clear.
        t = FakeTerminal(4, 2)
        self.assertEqual(t.draw_captured(["abcd", "ab"]),
                         term.SYNC_BEGIN + "\x1b[Habcd" + RESET + "\r\nab" + RESET + "\x1b[K" + term.SYNC_END)
        self.assertEqual(t.draw_captured(["wxyz", "ab"]), term.SYNC_BEGIN + "\x1b[1;1Hwxyz" + RESET + term.SYNC_END)

    def test_lines_wider_than_the_screen_are_clipped(self):
        # A wider line would wrap, and on the last row scroll the whole screen.
        self.assertEqual(term.clip("\x1b[1mabcdef" + RESET, 3), "\x1b[1mabc")
        self.assertEqual(term.clip("ab\x1b[2mcd" + RESET + "ef", 4), "ab\x1b[2mcd")
        line = "short \x1b[1mone" + RESET
        self.assertIs(term.clip(line, 20), line)
        t, screen = FakeTerminal(6, 3), Screen(6, 3)
        screen.feed(t.draw_captured(["top", "", "a footer much too long"]))
        screen.feed(t.draw_captured(["TOP", "", "a footer much too long"]))
        expected = Screen(6, 3)
        expected.feed(reference_draw(["TOP", "", "a foot"], 3))
        self.assertEqual(screen.grid, expected.grid)

    def test_screen_always_matches_a_full_redraw(self):
        rnd = random.Random(7)
        pool = ["", "x", "hello", "  centered", "\x1b[1mbold" + RESET, "\x1b[38;5;9m▀▄\x1b[48;5;3m▀" + RESET,
                "\x1b[2m12:00" + RESET + " tail", "\x1b[7m rev ", "wide line here",
                "a footer much wider than any screen here", "\x1b[1m" + "x" * 17 + RESET + "y",
                "\x1b[38;5;9m" + "▀" * 16 + RESET]
        cols, rows = 16, 6
        t = FakeTerminal(cols, rows)
        screen = Screen(cols, rows)
        for frame in range(300):
            if rnd.random() < 0.05:   # resized: the terminal shows something else now
                cols, rows = rnd.choice([s for s in [(16, 6), (18, 4), (16, 8)] if s != (cols, rows)])
                t.cols, t.rows = cols, rows
                screen = Screen(cols, rows, fill="?")
            lines = [rnd.choice(pool) for _ in range(rnd.randint(0, rows + 2))]
            if rnd.random() < 0.3 and frame:
                lines = list(prev)
                if lines:
                    lines[rnd.randrange(len(lines))] = rnd.choice(pool)
            prev = lines
            screen.feed(t.draw_captured(lines))
            expected = Screen(cols, rows)
            expected.feed(reference_draw([reference_clip(l, cols) for l in lines], rows))
            self.assertEqual(screen.grid, expected.grid, frame)


class ColorsTest(unittest.TestCase):
    def test_nearest16_matches_the_original(self):
        for r in range(0, 256, 15):
            for g in range(0, 256, 15):
                for b in range(0, 256, 15):
                    self.assertEqual(term._nearest16((r, g, b)), reference_nearest16((r, g, b)))
        for c in BASIC16:   # exact ties at the palette colors themselves
            self.assertEqual(term._nearest16(c), reference_nearest16(c))

    def test_cached_escapes_follow_mode_changes(self):
        colors = Colors("truecolor")
        rgb = (200, 30, 40)
        self.assertEqual(colors.fg(rgb), "\x1b[38;2;200;30;40m")
        self.assertEqual(colors.bg(rgb), "\x1b[48;2;200;30;40m")
        colors.mode = "256"
        self.assertEqual(colors.mode, "256")
        self.assertEqual(colors.fg(rgb), "\x1b[38;5;%dm" % term._to256(rgb))
        colors.mode = "16"
        i = reference_nearest16(rgb)
        self.assertEqual(colors.fg(rgb), "\x1b[%dm" % (30 + i if i < 8 else 90 + i - 8))
        self.assertEqual(colors.bg(rgb), "\x1b[%dm" % (40 + i if i < 8 else 100 + i - 8))
        colors.mode = "truecolor"
        self.assertEqual(colors.fg(rgb), "\x1b[38;2;200;30;40m")
        self.assertEqual(Colors("256").fg([1, 2, 3]), Colors("256").fg((1, 2, 3)))   # unhashable


class LogoLinesTest(unittest.TestCase):
    def random_grid(self, rnd):
        palette = [(rnd.randrange(256), rnd.randrange(256), rnd.randrange(256)) for _ in range(4)]
        w, h = rnd.randint(1, 12), rnd.randint(1, 9)

        def px():
            if rnd.random() < 0.3:
                return None
            return rnd.choice(palette) if rnd.random() < 0.8 else tuple(rnd.randrange(256) for _ in range(3))
        return [[px() for _ in range(w)] for _ in range(h)]

    def test_looks_the_same_as_one_escape_per_cell(self):
        rnd = random.Random(3)
        before = "\x1b[1m\x1b[7m\x1b[38;5;3m\x1b[48;5;4m"   # whatever the line follows
        for _ in range(300):
            grid = self.random_grid(rnd)
            for mode in MODES:
                colors = Colors(mode)
                new, old = logo_lines(grid, colors), reference_logo_lines(grid, colors)
                self.assertEqual(len(new), len(old))
                for a, b in zip(new, old):
                    self.assertLessEqual(len(a), len(b))
                    for start in (None, cells(before)[1]):
                        self.assertEqual(cells(a, start), cells(b, start))

    def test_cached_per_grid_and_mode(self):
        grid = [[(255, 0, 0), None], [(0, 0, 255), (0, 0, 255)]]
        colors = Colors("truecolor")
        first = logo_lines(grid, colors)
        again = logo_lines(grid, colors)
        self.assertEqual(first, again)
        self.assertIsNot(first, again)   # callers may change their copy
        again.append("x")
        self.assertEqual(logo_lines(grid, colors), first)
        colors.mode = "16"
        self.assertEqual(logo_lines(grid, colors), reference_logo_lines(grid, colors))
        other = [[(0, 255, 0), None], [(0, 0, 255), (0, 0, 255)]]   # a different grid
        self.assertEqual(logo_lines(other, colors), reference_logo_lines(other, colors))

    def test_cache_is_bounded(self):
        colors = Colors("256")
        for i in range(render._LOGO_CACHE_MAX * 2):
            logo_lines([[(i, 0, 0)]], colors)
        self.assertLessEqual(len(render._logo_cache), render._LOGO_CACHE_MAX)


class TextTest(unittest.TestCase):
    def test_visible_len(self):
        self.assertEqual(visible_len("plain"), 5)
        self.assertEqual(visible_len(""), 0)
        self.assertEqual(visible_len("\x1b[1m\x1b[38;2;1;2;3mab" + RESET + "▀"), 3)
        self.assertEqual(visible_len("\x1b[1mab" + RESET), 2)   # again, from the cache
        self.assertEqual(visible_len("a\x1b[Kb"), 5)   # only color/style escapes are invisible
        self.assertEqual(center("\x1b[1mab" + RESET, 6), "  \x1b[1mab" + RESET)
        self.assertEqual(center("abcdefgh", 6), "abcdefgh")


if __name__ == "__main__":
    unittest.main()
