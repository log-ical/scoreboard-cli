"""Drawing: half-block logos and the big block-digit font."""

from .term import RESET

_UNSET = object()        # color state at the start of a line: whatever came before
_logo_cache = {}         # (id(grid), color mode) -> (grid, lines)
_LOGO_CACHE_MAX = 32


def logo_lines(grid, colors):
    """Render a pixel grid (two pixel rows per text row) with half blocks.

    Each line ends with a reset. Grids are treated as immutable (LogoStore
    keeps them for the session), so the lines are cached per grid and mode.
    """
    key = (id(grid), colors.mode)
    hit = _logo_cache.get(key)
    if hit is not None and hit[0] is grid:   # holding grid keeps its id unique
        return list(hit[1])
    lines = _render_logo(grid, colors)
    if len(_logo_cache) >= _LOGO_CACHE_MAX:
        _logo_cache.pop(next(iter(_logo_cache)), None)   # the oldest
    _logo_cache[key] = (grid, lines)
    return list(lines)


def _render_logo(grid, colors):
    """Escapes are only written when the cell needs a different color than
    the terminal already has: the cells look exactly as if each one set its
    own (reset to default where a half is transparent), but a run of one
    color costs one escape instead of one per cell."""
    fg, bg = colors.fg, colors.bg
    lines = []
    for y in range(0, len(grid), 2):
        top = grid[y]
        bot = grid[y + 1] if y + 1 < len(grid) else [None] * len(top)
        s = []
        cur_fg = cur_bg = _UNSET   # None once reset to the default colors
        for t, b in zip(top, bot):
            if t is not None and b is not None:
                code = fg(t)
                if code != cur_fg:
                    s.append(code)
                    cur_fg = code
                code = bg(b)
                if code != cur_bg:
                    s.append(code)
                    cur_bg = code
                s.append("▀")
                continue
            # Transparent cell or half: needs the default background (and,
            # as after a reset, no other attributes).
            if cur_bg is not None:
                s.append(RESET)
                cur_fg = cur_bg = None
            if t is None and b is None:
                s.append(" ")   # the foreground color doesn't show on a space
                continue
            code = fg(t if b is None else b)
            if code != cur_fg:
                s.append(code)
                cur_fg = code
            s.append("▀" if b is None else "▄")
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
