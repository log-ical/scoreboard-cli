"""Terminal I/O, ANSI colors and text helpers."""

import fcntl
import functools
import os
import re
import select
import shutil
import struct
import sys
import termios
import time
import tty

BASIC16 = [
    (0, 0, 0), (170, 0, 0), (0, 170, 0), (170, 85, 0), (0, 0, 170),
    (170, 0, 170), (0, 170, 170), (170, 170, 170), (85, 85, 85),
    (255, 85, 85), (85, 255, 85), (255, 255, 85), (85, 85, 255),
    (255, 85, 255), (85, 255, 255), (255, 255, 255),
]


def detect_color_mode():
    ct = os.environ.get("COLORTERM", "").lower()
    term = os.environ.get("TERM", "")
    if ct in ("truecolor", "24bit"):
        return "truecolor"
    if "256" in term:
        return "256"
    return "16"


def _nearest16(rgb):
    """Index of the closest BASIC16 color (the first one on ties)."""
    r, g, b = rgb[:3]
    best, best_d = 0, None
    for i, (cr, cg, cb) in enumerate(BASIC16):
        d = (r - cr) ** 2 + (g - cg) ** 2 + (b - cb) ** 2
        if best_d is None or d < best_d:
            best, best_d = i, d
    return best


def _to256(rgb):
    r, g, b = rgb
    if abs(r - g) < 10 and abs(g - b) < 10:
        if r < 8:
            return 16
        if r > 248:
            return 231
        return 232 + round((r - 8) / 247 * 24)
    q = lambda v: 0 if v < 48 else (1 if v < 115 else (v - 35) // 40)
    return 16 + 36 * q(r) + 6 * q(g) + q(b)


def _fg_code(mode, rgb):
    if mode == "truecolor":
        return "\x1b[38;2;%d;%d;%dm" % rgb
    if mode == "256":
        return "\x1b[38;5;%dm" % _to256(rgb)
    i = _nearest16(rgb)
    return "\x1b[%dm" % (30 + i if i < 8 else 90 + i - 8)


def _bg_code(mode, rgb):
    if mode == "truecolor":
        return "\x1b[48;2;%d;%d;%dm" % rgb
    if mode == "256":
        return "\x1b[48;5;%dm" % _to256(rgb)
    i = _nearest16(rgb)
    return "\x1b[%dm" % (40 + i if i < 8 else 100 + i - 8)


_COLOR_CACHE_MAX = 1 << 16   # escapes kept per mode and layer before starting over


class Colors:
    """Color escapes for the current mode ("truecolor", "256" or "16").

    The mode can be changed at any time (the options menu does). Escapes
    are memoized per mode, since logos ask for the same colors again and again.
    """

    def __init__(self, mode):
        self._caches = {}   # mode -> (fg cache, bg cache), each rgb -> escape
        self.mode = mode

    @property
    def mode(self):
        return self._mode

    @mode.setter
    def mode(self, mode):
        self._mode = mode
        self._fg, self._bg = self._caches.setdefault(mode, ({}, {}))

    def fg(self, rgb):
        try:
            return self._fg[rgb]
        except KeyError:
            if len(self._fg) >= _COLOR_CACHE_MAX:
                self._fg.clear()
            code = self._fg[rgb] = _fg_code(self._mode, rgb)
            return code
        except TypeError:   # unhashable, e.g. a list
            return _fg_code(self._mode, rgb)

    def bg(self, rgb):
        try:
            return self._bg[rgb]
        except KeyError:
            if len(self._bg) >= _COLOR_CACHE_MAX:
                self._bg.clear()
            code = self._bg[rgb] = _bg_code(self._mode, rgb)
            return code
        except TypeError:
            return _bg_code(self._mode, rgb)


RESET = "\x1b[0m"
BOLD = "\x1b[1m"
DIM = "\x1b[2m"
REVERSE = "\x1b[7m"

_SGR = re.compile(r"\x1b\[[0-9;]*m")
_SGR_SPLIT = re.compile(r"(\x1b\[[0-9;]*m)")


def visible_len(s):
    if "\x1b" not in s:
        return len(s)
    return _styled_len(s)


@functools.lru_cache(maxsize=1024)
def _styled_len(s):
    # Cached because every frame measures the same styled lines (logo rows
    # most of all) again, as new but equal strings.
    return len(_SGR.sub("", s))


def center(line, width):
    pad = max(0, (width - visible_len(line)) // 2)
    return " " * pad + line


# Synchronized output: the terminal shows the frame all at once instead of
# while it arrives. Terminals without it ignore these like any unknown mode.
SYNC_BEGIN, SYNC_END = "\x1b[?2026h", "\x1b[?2026l"
FULL_REDRAW_EVERY = 10.0   # seconds; repaints anything else that wrote to the screen


def clip(line, cols):
    """line cut to at most cols visible cells, keeping its escapes. A longer
    line would wrap, and on the last row scroll the screen, leaving every
    row drawn since one off."""
    if visible_len(line) <= cols:
        return line
    out, room = [], cols
    for i, part in enumerate(_SGR_SPLIT.split(line)):
        if i % 2:
            out.append(part)   # an escape
        else:
            out.append(part[:room])
            room -= len(out[-1])
            if room <= 0:
                break
    return "".join(out)


def row(line, cols):
    """A screen row: the line, then clear the rest of it. A full-width line
    leaves the cursor on its last cell, where ESC[K would erase that cell,
    and there's nothing left to clear anyway."""
    return line + RESET + ("" if visible_len(line) >= cols else "\x1b[K")


class Terminal:
    def __init__(self):
        self.fd = sys.stdin.fileno()
        self.old = None
        self._shown = None       # the rows on screen, as last drawn (None: unknown)
        self._shown_size = None  # (cols, rows) they were drawn at
        self._full_at = 0.0      # time.monotonic() of the last full redraw

    def __enter__(self):
        self.old = termios.tcgetattr(self.fd)
        tty.setcbreak(self.fd)
        sys.stdout.write("\x1b[?1049h\x1b[?25l\x1b[2J")
        sys.stdout.flush()
        self.invalidate()
        return self

    def __exit__(self, *exc):
        sys.stdout.write(SYNC_END + RESET + "\x1b[?25h\x1b[?1049l")
        sys.stdout.flush()
        termios.tcsetattr(self.fd, termios.TCSADRAIN, self.old)

    def invalidate(self):
        """Make the next draw repaint every row."""
        self._shown = None

    def size(self):
        s = shutil.get_terminal_size((80, 24))
        return s.columns, s.lines

    def pixel_aspect(self):
        """Width / height of one half-block pixel on screen (1.0 if unknown)."""
        try:
            rows, cols, xpx, ypx = struct.unpack("HHHH", fcntl.ioctl(
                sys.stdout.fileno(), termios.TIOCGWINSZ, b"\0" * 8))
            if rows and cols and xpx and ypx:
                return max(0.5, min(2.0, (xpx / cols) / (ypx / rows / 2)))
        except OSError:
            pass
        return 1.0

    def read_key(self, timeout):
        r, _, _ = select.select([self.fd], [], [], timeout)
        if not r:
            return None
        ch = os.read(self.fd, 1)
        if ch == b"\x1b":
            seq = b""
            while select.select([self.fd], [], [], 0.02)[0]:
                seq += os.read(self.fd, 1)
                if len(seq) >= 2:
                    break
            return {b"[A": "up", b"[B": "down", b"[C": "right", b"[D": "left",
                    b"OA": "up", b"OB": "down", b"OC": "right", b"OD": "left"}.get(seq, "esc")
        if ch in (b"\r", b"\n"):
            return "enter"
        return ch.decode(errors="ignore")

    def draw(self, lines):
        """Show lines[i] on row i, blanking the rows past the end.

        Only rows that differ from the previous frame are rewritten; the
        whole screen is repainted on the first draw, after a resize or
        invalidate(), and every FULL_REDRAW_EVERY seconds.
        """
        cols, rows = self.size()
        n = len(lines)
        new = [clip(lines[i], cols) if i < n else "" for i in range(rows)]
        old, now = self._shown, time.monotonic()
        if old is None or self._shown_size != (cols, rows) or now - self._full_at >= FULL_REDRAW_EVERY:
            # Every row, top to bottom, with no newline after the last one
            # so the screen never scrolls.
            out = ["\x1b[H", "\r\n".join(row(line, cols) for line in new)]
            self._full_at = now
        else:
            out = ["\x1b[%d;1H%s" % (i + 1, row(line, cols))
                   for i, line in enumerate(new) if line != old[i]]
        self._shown = None   # unknown if the write fails part way
        if out:
            sys.stdout.write(SYNC_BEGIN + "".join(out) + SYNC_END)
        sys.stdout.flush()   # also sends anything written since (e.g. a bell)
        self._shown, self._shown_size = new, (cols, rows)
