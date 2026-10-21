"""Terminal I/O, ANSI colors and text helpers."""

import os
import re
import select
import shutil
import sys
import termios
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
    return min(range(16), key=lambda i: sum((a - b) ** 2 for a, b in zip(rgb, BASIC16[i])))


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


class Colors:
    def __init__(self, mode):
        self.mode = mode

    def fg(self, rgb):
        if self.mode == "truecolor":
            return "\x1b[38;2;%d;%d;%dm" % rgb
        if self.mode == "256":
            return "\x1b[38;5;%dm" % _to256(rgb)
        i = _nearest16(rgb)
        return "\x1b[%dm" % (30 + i if i < 8 else 90 + i - 8)

    def bg(self, rgb):
        if self.mode == "truecolor":
            return "\x1b[48;2;%d;%d;%dm" % rgb
        if self.mode == "256":
            return "\x1b[48;5;%dm" % _to256(rgb)
        i = _nearest16(rgb)
        return "\x1b[%dm" % (40 + i if i < 8 else 100 + i - 8)


RESET = "\x1b[0m"
BOLD = "\x1b[1m"
DIM = "\x1b[2m"
REVERSE = "\x1b[7m"


def visible_len(s):
    return len(re.sub(r"\x1b\[[0-9;]*m", "", s))


def center(line, width):
    pad = max(0, (width - visible_len(line)) // 2)
    return " " * pad + line


class Terminal:
    def __init__(self):
        self.fd = sys.stdin.fileno()
        self.old = None

    def __enter__(self):
        self.old = termios.tcgetattr(self.fd)
        tty.setcbreak(self.fd)
        sys.stdout.write("\x1b[?1049h\x1b[?25l\x1b[2J")
        sys.stdout.flush()
        return self

    def __exit__(self, *exc):
        sys.stdout.write(RESET + "\x1b[?25h\x1b[?1049l")
        sys.stdout.flush()
        termios.tcsetattr(self.fd, termios.TCSADRAIN, self.old)

    def size(self):
        s = shutil.get_terminal_size((80, 24))
        return s.columns, s.lines

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
        cols, rows = self.size()
        out = ["\x1b[H"]
        for i in range(rows):
            line = lines[i] if i < len(lines) else ""
            out.append(line + RESET + "\x1b[K")
            if i < rows - 1:
                out.append("\r\n")
        sys.stdout.write("".join(out))
        sys.stdout.flush()
