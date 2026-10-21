"""Drawing: half-block logos and the big block-digit font."""

from .term import RESET


def logo_lines(grid, colors):
    """Render a pixel grid (two pixel rows per text row) with half blocks."""
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


BIG_FONT = {
    "0": ["███", "█ █", "█ █", "█ █", "███"],
    "1": [" █ ", "██ ", " █ ", " █ ", "███"],
    "2": ["███", "  █", "███", "█  ", "███"],
    "3": ["███", "  █", "███", "  █", "███"],
    "4": ["█ █", "█ █", "███", "  █", "  █"],
    "5": ["███", "█  ", "███", "  █", "███"],
    "6": ["███", "█  ", "███", "█ █", "███"],
    "7": ["███", "  █", "  █", "  █", "  █"],
    "8": ["███", "█ █", "███", "█ █", "███"],
    "9": ["███", "█ █", "███", "  █", "███"],
    ":": [" ", "█", " ", "█", " "],
    "G": ["███", "█  ", "█ █", "█ █", "███"],
    "O": ["███", "█ █", "█ █", "█ █", "███"],
    "A": ["███", "█ █", "███", "█ █", "█ █"],
    "D": ["██ ", "█ █", "█ █", "█ █", "██ "],
    "L": ["█  ", "█  ", "█  ", "█  ", "███"],
    "!": ["█", "█", "█", " ", "█"],
    "-": ["   ", "   ", "███", "   ", "   "],
    " ": [" ", " ", " ", " ", " "],
}


def big_text(text, scale=1):
    """Render digits in the block font; scale=2 doubles every pixel."""
    rows = []
    for r in range(5):
        parts = []
        for ch in text:
            glyph = BIG_FONT.get(ch, BIG_FONT[" "])[r]
            parts.append("".join(c * (2 * scale) for c in glyph))
        line = "  ".join(parts) if scale == 1 else "   ".join(parts)
        rows.extend([line] * scale)
    return rows


def place(lines, block, lo, hi):
    """Center block vertically within rows lo..hi-1, clipping if needed."""
    start = lo + max(0, (hi - lo - len(block)) // 2)
    for i, b in enumerate(block):
        if start + i < hi:
            lines[start + i] = b
