"""Terminal I/O, ANSI colors and text helpers."""

import fcntl
import functools
import os
import re
import select
import signal
import struct
import sys
import termios
import time
import tty
import unicodedata

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
_SPACES = re.compile(r"\s+")
# What draw() blanks: control characters other than ESC (which starts the
# app's own color escapes) and lone surrogates, which can't be printed.
_UNPRINTABLE = re.compile("[\x00-\x1a\x1c-\x1f\x7f-\x9f\ud800-\udfff]")


def printable(s):
    """s made safe to put on screen. Line breaks, tabs and odd spaces become
    one space (ESPN writes some plays as "bad pass\\nturnover"); control and
    format characters are dropped: the ESC of an escape sequence, bidi
    overrides, and lone surrogates (from a "\\ud800" in JSON)."""
    if s.isprintable():
        return s
    s = _SPACES.sub(" ", s)
    return "".join(ch for ch in s if unicodedata.category(ch)[0] != "C")


def visible_len(s):
    if "\x1b" not in s:
        return len(s)
    return _styled_len(s)


@functools.lru_cache(maxsize=1024)
def _styled_len(s):
    # Cached because every frame measures the same styled lines (logo rows
    # most of all) again, as new but equal strings.
    return len(_SGR.sub("", s))


def shorten(text, width):
    """Plain text in at most width columns: cut between words, with "…"."""
    if len(text) <= width:
        return text
    if width < 2:
        return "…"[:width]
    cut = text[:width - 1]
    if cut[-1] != " " and text[width - 1] != " " and " " in cut:   # mid-word: back to its start
        cut = cut[:cut.rfind(" ")]
    return cut.rstrip(" ·,") + "…"


def center(line, width):
    pad = max(0, (width - visible_len(line)) // 2)
    return " " * pad + line


# Synchronized output: the terminal shows the frame all at once instead of
# while it arrives. Terminals without it ignore these like any unknown mode.
SYNC_BEGIN, SYNC_END = "\x1b[?2026h", "\x1b[?2026l"
FULL_REDRAW_EVERY = 10.0   # seconds; repaints anything else that wrote to the screen


# --------------------------------------------------------------------------
# Symbol sets. The app draws with full Unicode; a frame is translated as it's
# written for terminals whose fonts lack some of it. "console" is for the
# Linux text console: its fonts have light box drawing, blocks and Latin-1,
# but quietly map other symbols to look-alikes (◦ and ● come out the same).
# "ascii" is for terminals without Unicode: solid blocks become reverse-video
# spaces, which look the same in color. Every substitute is one column wide,
# so nothing moves. The play-by-play markers (shots, goals, the ball) are
# picked per set by live.py, since one symbol means different things there.

SYMBOL_SETS = ("full", "console", "ascii")
_SOLID = REVERSE_ON, REVERSE_OFF = "\x1b[7m", "\x1b[27m"
_CONSOLE = {"●": "*", "◦": "+", "○": "o", "◯": "U", "◉": "@", "▶": ">", "◀": "<", "▲": "^", "▼": "v", "■": "#",
            "‹": "<", "›": ">", "…": ".", "←": "<", "→": ">", "↑": "^", "↓": "v",
            "━": "─", "┃": "│", "╭": "┌", "╮": "┐", "╰": "└", "╯": "┘"}
_ASCII = dict(_CONSOLE, **{"─": "-", "━": "=", "│": "|", "┃": "|", "·": ".", "▌": "[", "▐": "]",
                           "█": REVERSE_ON + " " + REVERSE_OFF, "▀": REVERSE_ON + " " + REVERSE_OFF,
                           "▄": REVERSE_ON + " " + REVERSE_OFF},
              **{c: "+" for c in "┌┐└┘┬┴├┤┼╭╮╰╯"})
_TABLES = {"full": None, "console": str.maketrans(_CONSOLE), "ascii": str.maketrans(_ASCII)}
symbols = "full"   # the set in use: set_symbols()


def detect_symbols():
    """The symbol set for this terminal."""
    encoding = (getattr(sys.stdout, "encoding", "") or "").lower().replace("-", "")
    term = os.environ.get("TERM", "")
    if encoding != "utf8" or term == "dumb":
        return "ascii"
    if term == "linux" or term.startswith(("vt", "cons")):
        return "console"   # the Linux text console, and its relatives
    return "full"


def set_symbols(mode):
    """Use a symbol set: one of SYMBOL_SETS, or "auto" to detect it."""
    global symbols
    symbols = detect_symbols() if mode == "auto" else mode if mode in SYMBOL_SETS else "full"


def _ascii_letter(ch):
    base = unicodedata.normalize("NFKD", ch).encode("ascii", "ignore").decode()
    return base[:1] or "?"


def translate(line):
    """line in the symbol set in use; it keeps its width."""
    table = _TABLES[symbols]
    if table is None or line.isascii():
        return line
    line = line.translate(table)
    if symbols == "ascii" and not line.isascii():
        line = "".join(ch if ch.isascii() else _ascii_letter(ch) for ch in line)   # é -> e
    return line


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


ESC_WAIT = 0.05   # seconds for the rest of an escape sequence; an Esc alone has none
# The keys escape sequences stand for: CSI ones by their last byte (Shift- and
# Ctrl-arrows have parameters, and count as arrows) or, ending in ~, their first
# parameter; SS3 ones by their one byte. Anything else is no key at all.
_CSI_KEYS = {b"A": "up", b"B": "down", b"C": "right", b"D": "left", b"H": "home", b"F": "end"}
_TILDE_KEYS = {b"1": "home", b"7": "home", b"4": "end", b"8": "end", b"5": "pgup", b"6": "pgdn"}
_SS3_KEYS = {b"A": "up", b"B": "down", b"C": "right", b"D": "left", b"H": "home", b"F": "end"}
# Signals that end the app: the terminal must be handed back first.
_QUIT_SIGNALS = (signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT)


def _quit(signum, frame):
    raise SystemExit(128 + signum)   # unwinds through Terminal.__exit__, like Ctrl-C


class Terminal:
    def __init__(self):
        self.fd = sys.stdin.fileno()
        self.old = None
        self._shown = None       # the rows on screen, as last drawn (None: unknown)
        self._shown_size = None  # (cols, rows) they were drawn at
        self._full_at = 0.0      # time.monotonic() of the last full redraw
        self._wake = None        # (read, write) ends of the pipe signals wake read_key with
        self._old_wakeup = -1    # the wakeup fd set before
        self._signal = None      # SIGTSTP or SIGCONT, to act on in read_key
        self._handlers = {}      # signal -> the handler it had before

    def __enter__(self):
        self.old = termios.tcgetattr(self.fd)
        if hasattr(sys.stdout, "reconfigure"):   # a character stdout can't encode is a ?, not a crash
            sys.stdout.reconfigure(errors="replace")
        try:
            self._take()
            # Ctrl-Z (SIGTSTP) and fg (SIGCONT) are acted on in read_key, not
            # in the handler, which could interrupt a write to stdout. The
            # wakeup pipe ends read_key's wait as soon as one arrives.
            self._wake = os.pipe()
            for end in self._wake:
                os.set_blocking(end, False)
            self._old_wakeup = signal.set_wakeup_fd(self._wake[1])
            for sig in _QUIT_SIGNALS + (signal.SIGTSTP, signal.SIGCONT):
                self._handlers[sig] = signal.signal(sig, _quit if sig in _QUIT_SIGNALS else self._note)
        except BaseException:
            self.__exit__()
            raise
        return self

    def __exit__(self, *exc):
        try:
            self._give_back()
        finally:
            for sig, old in self._handlers.items():
                signal.signal(sig, old)
            self._handlers = {}
            if self._wake:
                signal.set_wakeup_fd(self._old_wakeup)
                for end in self._wake:
                    os.close(end)
            self._wake = None

    def _take(self):
        """Take over the terminal: keys one at a time, unechoed, on the
        alternate screen with the cursor hidden. Ctrl-S and Ctrl-Q are just
        keys (no IXON): pausing the output would only stall the screen."""
        tty.setcbreak(self.fd)
        mode = termios.tcgetattr(self.fd)
        mode[0] &= ~termios.IXON
        termios.tcsetattr(self.fd, termios.TCSAFLUSH, mode)
        sys.stdout.write("\x1b[?1049h\x1b[?25l\x1b[2J")
        sys.stdout.flush()
        self.invalidate()

    def _give_back(self):
        """Leave the terminal as it was. The writes go through stdout (a frame
        still buffered there goes out first, onto the alternate screen); the
        settings are restored even if they fail, as when the terminal is gone.
        Keys typed but not read are dropped, not left for the shell."""
        try:
            sys.stdout.write(SYNC_END + RESET + "\x1b[?25h\x1b[?1049l")
            sys.stdout.flush()
        except (OSError, ValueError):
            pass
        finally:
            try:
                termios.tcsetattr(self.fd, termios.TCSAFLUSH, self.old)
            except (OSError, termios.error):
                pass

    def _note(self, signum, frame):
        self._signal = signum

    def _job_control(self):
        """Ctrl-Z: give the terminal back and stop; take it again on fg. A
        SIGCONT alone (after kill -STOP) just takes it again: the shell may
        have reset it while the app was stopped."""
        signum, self._signal = self._signal, None
        if signum == signal.SIGTSTP:
            self._give_back()
            signal.signal(signal.SIGTSTP, signal.SIG_DFL)
            try:
                os.kill(os.getpid(), signal.SIGTSTP)   # stopped here until fg
            finally:
                signal.signal(signal.SIGTSTP, self._note)
            self._signal = None   # the SIGCONT that woke it
            # The settings to restore at the end: the shell's, as fg left them.
            self.old = termios.tcgetattr(self.fd)
        self._take()

    def invalidate(self):
        """Make the next draw repaint every row."""
        self._shown = None

    def size(self):
        """(columns, rows) from the terminal itself: not $COLUMNS/$LINES, which
        would pin the size for good, and never 0 (an unsized pty, a serial
        console), which Python 3.10's shutil passes on."""
        try:
            cols, rows = os.get_terminal_size(sys.stdout.fileno())
        except (OSError, ValueError):
            cols = rows = 0
        return cols or 80, rows or 24

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
        wake = [self._wake[0]] if self._wake else []
        r, _, _ = select.select([self.fd] + wake, [], [], timeout)
        if wake and wake[0] in r:
            try:
                os.read(wake[0], 64)   # a signal's wake-up byte
            except BlockingIOError:
                pass
        if self._signal is not None:
            self._job_control()
            return None
        if self.fd not in r:
            return None
        ch = os.read(self.fd, 1)
        if ch == b"\x1b":
            return self._escape()
        if ch in (b"\r", b"\n"):
            return "enter"
        return ch.decode(errors="ignore")

    def _escape(self):
        """What follows an ESC: a key's escape sequence, read to its end so no
        part of it is left to read as keys (or for the shell), or nothing (Esc
        itself). Keys the app has no use for (F5, Alt-x) give None, not "esc",
        which would quit the list."""
        more = lambda: select.select([self.fd], [], [], ESC_WAIT)[0]
        if not more():
            return "esc"
        intro = os.read(self.fd, 1)
        if intro == b"[":   # CSI: parameter and intermediate bytes, then a final one
            seq = b""
            while len(seq) < 16 and more():
                seq += os.read(self.fd, 1)
                if 0x40 <= seq[-1] <= 0x7e:
                    break
            if seq.endswith(b"~"):
                return _TILDE_KEYS.get(seq[:-1].split(b";")[0])
            return _CSI_KEYS.get(seq[-1:])
        if intro == b"O":   # SS3: one more byte
            return _SS3_KEYS.get(os.read(self.fd, 1)) if more() else None
        return "esc" if intro == b"\x1b" else None   # Esc twice, or Alt and a key

    def draw(self, lines):
        """Show lines[i] on row i, blanking the rows past the end.

        Only rows that differ from the previous frame are rewritten; the
        whole screen is repainted on the first draw, after a resize or
        invalidate(), and every FULL_REDRAW_EVERY seconds.
        """
        cols, rows = self.size()
        n = len(lines)
        # Blanking stray control characters keeps every row one row: a
        # newline would scroll the screen. (Feed text is made printable as it
        # arrives; this is the backstop.)
        new = [clip(translate(_UNPRINTABLE.sub(" ", lines[i])), cols) if i < n else "" for i in range(rows)]
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
