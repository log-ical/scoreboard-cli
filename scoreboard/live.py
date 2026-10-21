"""The play-by-play views under the game clock: a field, court or rink.

Each league has a few styles, the first the default; `v` cycles them. A view
is a block of equal-width lines, the last one a caption naming the style.
"""

import math

from .term import BOLD, DIM, RESET

STYLES = {
    "nfl": (("bar", "yard bar"), ("field", "full field")),
    "nba": (("lead", "lead tracker"), ("shots", "shot chart")),
    "nhl": (("rink", "shot map"), ("zones", "zone strip")),
}
MIN_WIDTH, MAX_WIDTH = 36, 72
# The tallest view (with its caption) is 9 rows, so a 40-row window fits any
# view and both teams in the vertical layout.
YELLOW, RED, BLUE = "\x1b[33m", "\x1b[31m", "\x1b[94m"
SHOTS_SHOWN = 10   # the NBA shot chart's most recent shots


def style_of(league, chosen):
    """The style to show: the chosen one if the league has it, else its default."""
    keys = [k for k, _ in STYLES.get(league, ())]
    return chosen if chosen in keys else (keys[0] if keys else None)


def next_style(league, current):
    keys = [k for k, _ in STYLES.get(league, ())]
    return keys[(keys.index(current) + 1) % len(keys)] if current in keys else (keys[0] if keys else None)


def view(league, style, g, plays, width, colors, tints, rules=None):
    """The view's lines, at most width wide; [] if it doesn't fit or the league has none.

    plays: the NBA views' play details (already cut at the view's last play);
    rules: the league, for its period lengths.
    """
    if league not in STYLES or width < MIN_WIDTH:
        return []
    style = style_of(league, style)
    w = min(width, MAX_WIDTH)
    teams = Teams(g, colors, tints)
    label = dict(STYLES[league])[style]
    if league == "nfl":
        return (yard_bar if style == "bar" else full_field)(g, w, teams, label)
    if league == "nba":
        return (lead_tracker(g, plays, w, teams, label, rules) if style == "lead"
                else shot_chart(plays, w, teams, label))
    return (shot_map if style == "rink" else zone_strip)(g, w, teams, label)


class Teams:
    """Each side's abbreviation and colors."""

    def __init__(self, g, colors, tints):
        self.abbrev = {"away": g.away.abbrev, "home": g.home.abbrev}
        self.fg = {"away": colors.fg(tints[0]), "home": colors.fg(tints[1])}
        self.bg = {"away": colors.bg(tints[0]), "home": colors.bg(tints[1])}

    def name(self, side):
        return self.fg[side] + BOLD + self.abbrev[side] + RESET


class Grid:
    """Characters with a style each, joined into lines with the fewest escapes."""

    def __init__(self, w, h):
        self.w, self.h = w, h
        self.cells = [[(" ", "")] * w for _ in range(h)]

    def put(self, x, y, ch, style=""):
        if 0 <= x < self.w and 0 <= y < self.h:
            self.cells[y][x] = (ch, style)

    def text(self, x, y, s, style=""):
        for i, ch in enumerate(s):
            self.put(x + i, y, ch, style)

    def lines(self):
        out = []
        for row in self.cells:
            parts, cur = [], ""
            for ch, style in row:
                if style != cur:
                    parts.append(RESET + style)
                    cur = style
                parts.append(ch)
            out.append("".join(parts) + RESET)
        return out


def caption(w, left, label, left_len, *shorter):
    """A last line: `left` (styled, left_len visible) and the style's name on
    the right. `shorter`: (text, length) pairs to fall back on, longest first."""
    right = "%s · v" % label
    for left, left_len in ((left, left_len),) + shorter + (("", 0),):
        if left_len <= w - len(right) - 2:
            break
    return left + " " * (w - left_len - len(right)) + DIM + right + RESET


# --------------------------------------------------------------------------
# NFL: the field, 120 yards with the end zones; the away team attacks right.

def _yard_col(w):
    return lambda yards: round(yards * (w - 1) / 120)   # yards from the back of the away end zone


def _ball_text(g, teams):
    if not g.ball:
        return "", 0
    side = g.ball[0]
    return teams.name(side) + DIM + " ball" + RESET, len(teams.abbrev[side]) + 5


def yard_bar(g, w, teams, label):
    col, grid = _yard_col(w), Grid(w, 2)
    for x in range(w):
        side = "away" if x < col(10) else "home" if x > col(110) else None
        if side:
            for y in (0, 1):
                grid.put(x, y, "█", teams.fg[side])
        else:
            grid.put(x, 1, "━", DIM)
    for yd in range(10, 100, 10):
        s = str(yd if yd <= 50 else 100 - yd)
        grid.text(col(yd + 10) - len(s) // 2, 0, s, DIM)
    _ball(grid, g, col, 1, teams)
    text, n = _ball_text(g, teams)
    return grid.lines() + [caption(w, text, label, n)]


def full_field(g, w, teams, label):
    col, grid = _yard_col(w), Grid(w, 5)
    lines = [col(10)] + [col(yd + 10) for yd in range(10, 100, 10)] + [col(110)]
    for x in range(w):
        top, bottom = ("┬", "┴") if x in lines else ("─", "─")
        grid.put(x, 0, "┌" if x == 0 else "┐" if x == w - 1 else top, DIM)
        grid.put(x, 4, "└" if x == 0 else "┘" if x == w - 1 else bottom, DIM)
    for y in (1, 2, 3):
        for x in (0, w - 1, col(10), col(110)):
            grid.put(x, y, "│", DIM)
    # End zones in team colors, the abbreviation down the middle.
    for side, x0, x1 in (("away", 1, col(10) - 1), ("home", col(110) + 1, w - 2)):
        letters = teams.abbrev[side][:3].center(3)
        for y in (1, 2, 3):
            for x in range(x0, x1 + 1):
                ch = letters[y - 1] if x == (x0 + x1) // 2 else " "
                grid.put(x, y, ch, teams.bg[side] + BOLD)
    for yd in range(10, 100, 10):
        s = str(yd if yd <= 50 else 100 - yd)
        grid.text(col(yd + 10) - len(s) // 2, 1, s, DIM)
    if g.ball and g.ball[2] is not None:
        grid.put(col(g.ball[2] + 10), 3, "┃", YELLOW + BOLD)
    _ball(grid, g, col, 2, teams)
    text, n = _ball_text(g, teams)
    return grid.lines() + [caption(w, text, label, n)]


def _ball(grid, g, col, y, teams):
    """The first-down line, the ball and the way the offense is going."""
    if not g.ball:
        return
    side, ball, first = g.ball
    x = col(ball + 10)
    grid.put(x + (1 if side == "away" else -1), y, "▶" if side == "away" else "◀", teams.fg[side])
    if first is not None:   # over the arrow when it's that close
        grid.put(col(first + 10), y, "┃", YELLOW + BOLD)
    grid.put(x, y, "●", teams.fg[side] + BOLD)


# --------------------------------------------------------------------------
# NBA

def lead_tracker(g, plays, w, teams, label, rules):
    """The away team's lead above the line, the home team's below, over the game."""
    lw = max(len(a) for a in teams.abbrev.values()) + 1
    cw = w - lw
    regulation = rules.periods * rules.period_secs
    total = max(regulation, rules.elapsed(g.period, 0) if g.period > rules.periods else 0)
    points = [(0, 0)] + [(p.elapsed, p.away - p.home) for p in plays]
    if g.state == "live" and g.clock_secs is not None:
        upto = rules.elapsed(g.period, g.clock_secs)
    else:
        upto = total if g.state == "final" else points[-1][0]
    scale = max(10, max(abs(m) for _, m in points))
    grid = Grid(w, 5)
    for i, side in ((0, "away"), (4, "home")):
        grid.text(0, i, teams.abbrev[side], teams.fg[side] + BOLD)
    j = margin = 0
    for c in range(cw):
        t = (c + 1) * total / cw
        if t > upto + total / cw:
            break
        while j < len(points) and points[j][0] <= t:
            margin = points[j][1]
            j += 1
        level = 0 if margin == 0 else max(1, min(4, round(abs(margin) / scale * 4)))
        side, rows, half = ("away", (1, 0), "▄") if margin > 0 else ("home", (3, 4), "▀")
        for k, y in enumerate(rows):   # nearest the line first
            ch = "█" if level >= 2 * (k + 1) else half if level == 2 * k + 1 else None
            if ch:
                grid.put(lw + c, y, ch, teams.fg[side])
    for c in range(cw):
        grid.put(lw + c, 2, "─", DIM)
    periods = max(rules.periods, g.period or 0)
    for p in range(1, periods + 1):
        t = rules.elapsed(p, rules.period_secs if p <= rules.periods else rules.ot_secs)
        grid.text(lw + round(t / total * cw), 2, "Q%d" % p if p <= rules.periods else "OT", DIM)
    # Who leads now, by how much at most, and how often it changed hands.
    now = points[-1][1]
    signs = [m > 0 for _, m in points if m]
    changes = sum(a != b for a, b in zip(signs, signs[1:]))
    best = {"away": max(0, max(m for _, m in points)), "home": max(0, -min(m for _, m in points))}
    if now:
        lead = "away" if now > 0 else "home"
        text, n = teams.name(lead) + " +%d" % abs(now), len(teams.abbrev[lead]) + 2 + len(str(abs(now)))
    else:
        text, n = "Tied", 4
    extra = " · largest %s %d, %s %d · %d lead change%s" % (
        teams.abbrev["away"], best["away"], teams.abbrev["home"], best["home"], changes, "" if changes == 1 else "s")
    short = " · %d lead change%s" % (changes, "" if changes == 1 else "s")
    return grid.lines() + [caption(w, text + DIM + extra + RESET, label, n + len(extra),
                                   (text + DIM + short + RESET, n + len(short)), (text, n))]


def shot_chart(plays, w, teams, label):
    """The last shots on a half court (basket at the top): ● made, ○ missed."""
    h, depth = 8, 30.0   # rows, and feet out from the basket they cover
    grid = Grid(w, h)
    col = lambda x: round(x / 50 * (w - 1))
    row = lambda y: round(y / depth * (h - 1))
    for x in range(w):
        grid.put(x, 0, "┌" if x == 0 else "┐" if x == w - 1 else "─", DIM)
    for y in range(1, h):
        grid.put(0, y, "│", DIM)
        grid.put(w - 1, y, "│", DIM)
    for y in range(0, row(14) + 1):   # the lane, to the free-throw line
        grid.put(col(17), y, "·", DIM)
        grid.put(col(33), y, "·", DIM)
    for x in range(col(17), col(33) + 1):
        grid.put(x, row(14), "·", DIM)
    for y in range(0, row(8.9) + 1):   # the corner threes, then the arc
        grid.put(col(3), y, "·", DIM)
        grid.put(col(47), y, "·", DIM)
    for i in range(181):
        a = math.radians(i)
        x, y = 25 + 23.75 * math.cos(a), 23.75 * math.sin(a)
        if y >= 8.9:
            grid.put(col(x), row(y), "·", DIM)
    grid.put(col(25), 0, "◯", BOLD)
    shots = [p.shot for p in plays if p.shot][-SHOTS_SHOWN:]
    for i, (x, y, side, made) in enumerate(shots):
        newest = BOLD if i == len(shots) - 1 else ""
        grid.put(col(x), min(h - 1, max(1, row(y))), "●" if made else "○", teams.fg[side] + newest)
    key = "● made ○ missed  " + teams.fg["away"] + "■" + RESET + " %s " % teams.abbrev["away"] \
        + teams.fg["home"] + "■" + RESET + " %s" % teams.abbrev["home"]
    n = 19 + len(teams.abbrev["away"]) + len(teams.abbrev["home"]) + 3
    return grid.lines() + [caption(w, key, label, n)]


# --------------------------------------------------------------------------
# NHL: rink feet, x -100..100 with the home end on the right, y -42.5..42.5.

def _rink_col(w):
    return lambda x: round((max(-100, min(100, x)) + 100) / 200 * (w - 1))


def _shot_counts(g, teams):
    count = {s: sum(1 for *_, side, _ in g.rink if side == s) for s in ("away", "home")}
    text = teams.name("away") + " %d · " % count["away"] + teams.name("home") + " %d" % count["home"]
    n = len(teams.abbrev["away"]) + len(teams.abbrev["home"]) + len(str(count["away"])) + len(str(count["home"])) + 5
    return text, n


def _last_event(g, w, zone=False):
    if not g.rink_last:
        return DIM + "no events yet".ljust(w) + RESET
    text, x, _ = g.rink_last
    if zone:
        where = "neutral zone" if -25 <= x <= 25 else "%s end" % (g.home.abbrev if x > 0 else g.away.abbrev)
        text += " · " + where
    return DIM + ("last: " + text)[:w].ljust(w) + RESET


def shot_map(g, w, teams, label):
    """This period's shots (◦) and goals (●), each team attacking the far end."""
    h = max(5, min(7, round(w * 85 / 400)))
    col, grid = _rink_col(w), Grid(w, h)
    row = lambda y: round((42.5 - max(-42.5, min(42.5, y))) / 85 * (h - 1))
    blue = (col(-25), col(25))
    for x in range(w):
        edge = "┬" if x in blue else "─"
        grid.put(x, 0, "╭" if x == 0 else "╮" if x == w - 1 else edge, DIM)
        grid.put(x, h - 1, "╰" if x == 0 else "╯" if x == w - 1 else ("┴" if x in blue else "─"), DIM)
    for y in range(1, h - 1):
        grid.put(0, y, "│", DIM)
        grid.put(w - 1, y, "│", DIM)
        grid.put(col(0), y, "┃", RED)
        for x in blue:
            grid.put(x, y, "│", BLUE)
    for x, y in ((-20, 22), (-20, -22), (20, 22), (20, -22)):
        grid.put(col(x), row(y), "·", DIM)
    grid.put(col(-89), row(0), "▌", DIM)
    grid.put(col(89), row(0), "▐", DIM)
    for x, y, side, goal in g.rink:
        grid.put(col(x), min(h - 2, max(1, row(y))), "●" if goal else "◦", teams.fg[side] + (BOLD if goal else ""))
    counts, n = _shot_counts(g, teams)
    legend = "◦ shot ● goal   " + counts
    return grid.lines() + [caption(w, legend, label, 16 + n), _last_event(g, w)]


def zone_strip(g, w, teams, label):
    """Where the latest event happened, and this period's shots."""
    col, grid = _rink_col(w), Grid(w, 2)
    for y in (0, 1):
        grid.put(0, y, "┃", DIM)
        grid.put(w - 1, y, "┃", DIM)
        for x in (col(-25), col(25)):
            grid.put(x, y, "│", BLUE)
    grid.put(1, 0, "▌", DIM)
    grid.put(w - 2, 0, "▐", DIM)
    zones = ((col(-100) + 2, col(-25), "%s end" % teams.abbrev["away"]),
             (col(-25) + 1, col(25), "neutral"), (col(25) + 1, col(100) - 2, "%s end" % teams.abbrev["home"]))
    for x0, x1, name in zones:
        grid.text(x0 + max(0, (x1 - x0 - len(name)) // 2), 0, name[:max(0, x1 - x0)], DIM)
    if g.rink_last:
        _, x, side = g.rink_last
        grid.put(min(w - 2, max(1, col(x))), 1, "●", teams.fg[side] + BOLD)
    counts, n = _shot_counts(g, teams)
    return grid.lines() + [_last_event(g, w, zone=True), caption(w, DIM + "shots this period: " + RESET + counts,
                                                                 label, 19 + n)]
