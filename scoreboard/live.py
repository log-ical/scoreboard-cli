"""The play-by-play views under the game clock: a field, court or rink.

Each league has a few styles, the first the default; `v` cycles them. A view
is a block of equal-width lines, the last one a caption naming the style.
A view draws the game as of one of its events (model.Event): the latest, or
one the viewer has gone back to with ←/→, the maps showing that event's
period as it stood then.
"""

import math
import re

from . import term
from .term import BOLD, DIM, RESET, shorten

STYLES = {
    "nfl": (("latest", "latest play"), ("bar", "yard bar"), ("field", "full field")),
    "nba": (("latest", "latest play"), ("lead", "lead tracker"), ("shots", "shot chart")),
    "nhl": (("latest", "latest event"), ("rink", "shot map"), ("zones", "zone strip")),
}
MIN_WIDTH, MAX_WIDTH = 36, 72
# The tallest view (with its caption) is 9 rows, so a 40-row window fits any
# view and both teams in the vertical layout.
YELLOW, RED, BLUE = "\x1b[33m", "\x1b[31m", "\x1b[94m"
SHOTS_SHOWN = 10   # the NBA shot chart's most recent shots
# Markers per symbol set (see term.py): limited fonts draw ◦ and ● alike.
MARKS = {
    "full": {"shot": "◦", "goal": "●", "made": "●", "missed": "○", "ball": "●", "basket": "◯", "event": "◉",
             "snap": "○"},
    "plain": {"shot": "+", "goal": "@", "made": "O", "missed": "x", "ball": "@", "basket": "U", "event": "*",
              "snap": "o"},
}


def marks():
    return MARKS["full" if term.symbols == "full" else "plain"]


def style_of(league, chosen):
    """The style to show: the chosen one if the league has it, else its default."""
    keys = [k for k, _ in STYLES.get(league, ())]
    return chosen if chosen in keys else (keys[0] if keys else None)


def next_style(league, current):
    keys = [k for k, _ in STYLES.get(league, ())]
    return keys[(keys.index(current) + 1) % len(keys)] if current in keys else (keys[0] if keys else None)


def view(league, style, g, events, width, colors, tints, rules=None, at=None, earlier=False):
    """The view's lines, at most width wide; [] if it doesn't fit or the league has none.

    events: the game's events so far (already cut at the view's last play),
    oldest first; at: the index of the one to show, None for the latest.
    rules: the league, for its period lengths. earlier: the NBA and NHL
    latest views also number the two before the shown one (the game view
    sets it while the play list is open).
    """
    if league not in STYLES or width < MIN_WIDTH:
        return []
    style = style_of(league, style)
    w = min(width, MAX_WIDTH)
    teams = Teams(g, colors, tints)
    label = dict(STYLES[league])[style]
    at = len(events) - 1 if at is None else at
    upto, back = events[:at + 1], len(events) - 1 - at
    # Gone back in time: the caption says so, in place of its legend.
    args = (g, upto, w, teams, label, back or None)
    if league == "nfl":
        return {"latest": nfl_latest, "bar": yard_bar, "field": full_field}[style](*args)
    if league == "nba":
        if style == "latest":
            return nba_latest(*args, earlier)
        return lead_tracker(*args, rules) if style == "lead" else shot_chart(*args)
    return nhl_latest(*args, earlier) if style == "latest" else {"rink": shot_map, "zones": zone_strip}[style](*args)


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


def _caption(w, note, left, label, left_len, *shorter, keep=None):
    """caption(), with a note on having gone back `note` events in time in
    place of the legend, and by it `keep` (styled, length): what the legend
    says of the time shown, kept with a shorter note if need be."""
    if not note:
        return caption(w, left, label, left_len, *shorter)
    full = "◀ %d back · → to live" % note
    tries = [(YELLOW + t + RESET + "  " + keep[0], len(t) + 2 + keep[1]) for t in (full, "◀ %d back" % note)] \
        if keep else []
    tries += [(YELLOW + t + RESET, len(t)) for t in (full, "◀ → live")]
    return caption(w, tries[0][0], label, tries[0][1], *tries[1:])


def _line(w, left, left_len, rest, right=""):
    """One line of w columns: `left` (styled, left_len visible), then `rest`
    cut between words to fit, and `right` (dim) at the end."""
    rest = shorten(rest, max(0, w - left_len - (len(right) + 1 if right else 0)))
    return left + rest + " " * max(0, w - left_len - len(rest) - len(right)) + DIM + right + RESET


def _before(grid, x, y, digit, style, here):
    """An earlier event's number at (x, y), unless right by the shown one's
    cell `here`, where it would run into its marker and words."""
    if not here or y != here[1] or abs(x - here[0]) > 1:
        grid.put(x, y, digit, style)


def _place_tag(grid, x, y, options, style):
    """Write the first of options, (side, text), that fits beside column x on
    row y, inside the frame: a space from x, to its right or left."""
    for side, text in options:
        x0 = x + 2 if side == "right" else x - 1 - len(text)
        if x0 >= 1 and x0 + len(text) <= grid.w - 1:
            grid.text(x0, y, text, style)
            return


# --------------------------------------------------------------------------
# NFL: the field, 120 yards with the end zones; the away team attacks right.

NFL_KINDS = {"pass reception": "PASS", "pass incompletion": "INCOMPLETE", "rush": "RUSH", "sack": "SACK",
             "passing touchdown": "TOUCHDOWN", "rushing touchdown": "TOUCHDOWN", "kickoff": "KICKOFF",
             "punt": "PUNT", "field goal good": "FIELD GOAL", "field goal missed": "MISSED FIELD GOAL",
             "extra point good": "EXTRA POINT", "penalty": "PENALTY", "interception return": "INTERCEPTION",
             "pass interception return": "INTERCEPTION", "fumble recovery (opponent)": "FUMBLE",
             "timeout": "TIMEOUT", "official timeout": "TIMEOUT", "end period": "END OF QUARTER",
             "two-minute warning": "TWO-MINUTE WARNING"}
NFL_GAINS = ("PASS", "RUSH", "SACK", "TOUCHDOWN")   # plays whose yards to show


def _yard_col(w):
    return lambda yards: round(yards * (w - 1) / 120)   # yards from the back of the away end zone


def _shown_ball(g, upto, note):
    """(side, ball, first-down line) to draw: where the shown play left it
    when gone back in time, else the game's."""
    if note and upto and upto[-1].end:
        return upto[-1].end
    return g.ball


def _ball_text(ball, teams):
    if not ball:
        return "", 0
    return teams.name(ball[0]) + DIM + " ball" + RESET, len(teams.abbrev[ball[0]]) + 5


def yard_bar(g, upto, w, teams, label, note):
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
    ball = _shown_ball(g, upto, note)
    _ball(grid, ball, col, 1, teams)
    text, n = _ball_text(ball, teams)
    return grid.lines() + [_caption(w, note, text, label, n)]


def _field(w, teams):
    """The field, empty: sidelines, yard lines and numbers, end zones in team
    colors with the abbreviations down the middle."""
    col, grid = _yard_col(w), Grid(w, 5)
    lines = [col(10)] + [col(yd + 10) for yd in range(10, 100, 10)] + [col(110)]
    for x in range(w):
        top, bottom = ("┬", "┴") if x in lines else ("─", "─")
        grid.put(x, 0, "┌" if x == 0 else "┐" if x == w - 1 else top, DIM)
        grid.put(x, 4, "└" if x == 0 else "┘" if x == w - 1 else bottom, DIM)
    for y in (1, 2, 3):
        for x in (0, w - 1, col(10), col(110)):
            grid.put(x, y, "│", DIM)
    for side, x0, x1 in (("away", 1, col(10) - 1), ("home", col(110) + 1, w - 2)):
        letters = teams.abbrev[side][:3].center(3)
        for y in (1, 2, 3):
            for x in range(x0, x1 + 1):
                ch = letters[y - 1] if x == (x0 + x1) // 2 else " "
                grid.put(x, y, ch, teams.bg[side] + BOLD)
    for yd in range(10, 100, 10):
        s = str(yd if yd <= 50 else 100 - yd)
        grid.text(col(yd + 10) - len(s) // 2, 1, s, DIM)
    return col, grid


def full_field(g, upto, w, teams, label, note):
    col, grid = _field(w, teams)
    ball = _shown_ball(g, upto, note)
    if ball and ball[2] is not None:
        grid.put(col(ball[2] + 10), 3, "┃", YELLOW + BOLD)
    _ball(grid, ball, col, 2, teams)
    text, n = _ball_text(ball, teams)
    return grid.lines() + [_caption(w, note, text, label, n)]


def _ball(grid, ball, col, y, teams):
    """The first-down line, the ball and the way the offense is going."""
    if not ball:
        return
    side, at, first = ball
    x = col(at + 10)
    grid.put(x + (1 if side == "away" else -1), y, "▶" if side == "away" else "◀", teams.fg[side])
    if first is not None:   # over the arrow when it's that close
        grid.put(col(first + 10), y, "┃", YELLOW + BOLD)
    grid.put(x, y, marks()["ball"], teams.fg[side] + BOLD)


def nfl_latest(g, upto, w, teams, label, note):
    """The shown play on the field: from the snap (○) to where it ended (●),
    the line it was going for (┃), and what happened."""
    col, grid = _field(w, teams)
    cur = upto[-1] if upto else None
    if cur is None:
        return grid.lines() + [DIM + "no plays yet".ljust(w) + RESET, _caption(w, note, "", label, 0)]
    m = marks()
    if cur.start and cur.end:
        side, a, first = cur.start
        x0, x1 = col(a + 10), col(cur.end[1] + 10)
        if first is not None:
            grid.put(col(first + 10), 3, "┃", YELLOW + BOLD)
        for x in range(min(x0, x1) + 1, max(x0, x1)):
            grid.put(x, 2, "━", teams.fg[side])
        grid.put(x0, 2, m["snap"], teams.fg[side])
        if x1 != x0:
            grid.put(x1 + (1 if x1 > x0 else -1), 2, "▶" if x1 > x0 else "◀", teams.fg[cur.end[0]])
        grid.put(x1, 2, m["ball"], teams.fg[cur.end[0]] + BOLD)
    else:
        _ball(grid, g.ball, col, 2, teams)
    kind = NFL_KINDS.get((cur.kind or "").lower(), (cur.kind or "").upper())
    if kind in NFL_GAINS and cur.yards is not None:
        kind += " " + ("%+d" % cur.yards if cur.yards else "no gain")
    words = re.sub(r"^\([^()]*\)\s*", "", cur.text)   # less "(Shotgun)" and the like
    first_line = _line(w, BOLD + kind + RESET + " · ", len(kind) + 3, words, cur.when)
    return grid.lines() + [first_line, _caption(w, note, DIM + (cur.after or "") + RESET, label, len(cur.after or ""))]


# --------------------------------------------------------------------------
# NBA

def lead_tracker(g, upto, w, teams, label, note, rules):
    """The away team's lead above the line, the home team's below, over the game."""
    lw = max(len(a) for a in teams.abbrev.values()) + 1
    cw = w - lw
    regulation = rules.periods * rules.period_secs
    total = max(regulation, rules.elapsed(g.period, 0) if g.period > rules.periods else 0)
    points = [(0, 0)] + [(p.elapsed, p.away - p.home) for p in upto]
    if note:
        until = points[-1][0]   # gone back: the game as it stood then
    elif g.state == "live" and g.clock_secs is not None:
        until = rules.elapsed(g.period, g.clock_secs)
    else:
        until = total if g.state == "final" else points[-1][0]
    scale = max(10, max(abs(m) for _, m in points))
    grid = Grid(w, 5)
    for i, side in ((0, "away"), (4, "home")):
        grid.text(0, i, teams.abbrev[side], teams.fg[side] + BOLD)
    j = margin = 0
    for c in range(cw):
        t = (c + 1) * total / cw
        if t > until + total / cw:
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
    return grid.lines() + [_caption(w, note, text + DIM + extra + RESET, label, n + len(extra),
                                    (text + DIM + short + RESET, n + len(short)), (text, n), keep=(text, n))]


def _court(w, h, every=1):
    """A half court, the basket at the top: (grid, column, row) for court feet.
    The lines are dotted; `every` thins the arc's dots."""
    depth = 30.0   # feet out from the basket the rows cover
    grid = Grid(w, h)
    col = lambda x: round(x / 50 * (w - 1))
    row = lambda y: min(h - 1, max(1, round(y / depth * (h - 1))))
    for x in range(w):
        grid.put(x, 0, "┌" if x == 0 else "┐" if x == w - 1 else "─", DIM)
    for y in range(1, h):
        grid.put(0, y, "│", DIM)
        grid.put(w - 1, y, "│", DIM)
    for y in range(0, round(14 / depth * (h - 1)) + 1):   # the lane, to the free-throw line
        grid.put(col(17), y, "·", DIM)
        grid.put(col(33), y, "·", DIM)
    for x in range(col(17), col(33) + 1, 1 if every == 1 else 2):
        grid.put(x, round(14 / depth * (h - 1)), "·", DIM)
    for y in range(0, round(8.9 / depth * (h - 1)) + 1):   # the corner threes, then the arc
        grid.put(col(3), y, "·", DIM)
        grid.put(col(47), y, "·", DIM)
    for i in range(0, 181, every):
        a = math.radians(i)
        x, y = 25 + 23.75 * math.cos(a), 23.75 * math.sin(a)
        if y >= 8.9:
            grid.put(col(x), round(y / depth * (h - 1)), "·", DIM)
    grid.put(col(25), 0, marks()["basket"], BOLD)
    return grid, col, row


def shot_chart(g, upto, w, teams, label, note):
    """The last shots on a half court (basket at the top): ● made, ○ missed."""
    grid, col, row = _court(w, 8)
    m = marks()
    shots = [p.shot for p in upto if p.shot][-SHOTS_SHOWN:]
    for i, (x, y, side, made) in enumerate(shots):
        newest = BOLD if i == len(shots) - 1 else ""
        grid.put(col(x), row(y), m["made"] if made else m["missed"], teams.fg[side] + newest)
    key = "%s made %s missed  " % (m["made"], m["missed"]) + teams.fg["away"] + "■" + RESET + " %s " % teams.abbrev["away"] \
        + teams.fg["home"] + "■" + RESET + " %s" % teams.abbrev["home"]
    n = 19 + len(teams.abbrev["away"]) + len(teams.abbrev["home"]) + 3
    return grid.lines() + [_caption(w, note, key, label, n)]


def _nba_tag(e):
    """A word or two for a play, by the spot it happened."""
    kind = (e.kind or "").lower()
    if e.shot:
        return "+%d" % (e.points or 2) if e.shot[3] else "MISS"
    if "free throw" in kind:
        return "FT +1" if e.points else "FT MISS"
    for word, tag in (("rebound", "REBOUND"), ("foul", "FOUL"), ("turnover", "TURNOVER"), ("block", "BLOCK"),
                      ("steal", "STEAL"), ("violation", "VIOLATION"), ("jump ball", "JUMP BALL")):
        if word in kind:
            return tag
    return ""


def nba_latest(g, upto, w, teams, label, note, earlier=False):
    """The shown play where it happened on the court, with what it was and
    the score after it; with earlier, the two shots before it too (numbered,
    faded)."""
    grid, col, row = _court(w, 7, every=4)
    cur = upto[-1] if upto else None
    if cur is None:
        return grid.lines() + [DIM + "no plays yet".ljust(w) + RESET, _caption(w, note, "", label, 0)]
    m = marks()
    free_throw = "free throw" in (cur.kind or "").lower()
    spot = cur.xy or ((25, 14) if free_throw else None)   # free throws' own coordinates are junk
    here = (col(spot[0]), row(spot[1])) if spot and cur.side else None
    before = [e for e in upto[:-1] if e.shot][-2:] if earlier else []
    for n, e in enumerate(reversed(before), 1):
        x, y, side, _ = e.shot
        _before(grid, col(x), row(y), str(n), teams.fg[side] + DIM, here)
    if here:
        x, y = here
        style = teams.fg[cur.side] + BOLD
        tag = _nba_tag(cur)
        _place_tag(grid, x, y, (("right", tag), ("left", tag)), style)
        made = cur.shot[3] if cur.shot else bool(cur.points) if free_throw else None
        grid.put(x, y, m["event"] if made is None else m["made"] if made else m["missed"], style)
    who, n = (teams.name(cur.side) + " ", len(teams.abbrev[cur.side]) + 1) if cur.side else ("", 0)
    score = teams.name("away") + " %d · " % cur.away + teams.name("home") + " %d" % cur.home
    n_score = len(teams.abbrev["away"]) + len(teams.abbrev["home"]) + len(str(cur.away)) + len(str(cur.home)) + 5
    return grid.lines() + [_line(w, who, n, cur.text, cur.when), _caption(w, note, score, label, n_score)]


# --------------------------------------------------------------------------
# NHL: rink feet, x -100..100 with the home end on the right, y -42.5..42.5.

NHL_NAMES = {"faceoff": "FACEOFF", "hit": "HIT", "giveaway": "GIVEAWAY", "takeaway": "TAKEAWAY",
             "shot-on-goal": "SHOT", "missed-shot": "MISSED SHOT", "blocked-shot": "BLOCKED SHOT", "goal": "GOAL",
             "penalty": "PENALTY"}
NHL_TAGS = dict(NHL_NAMES, **{"shot-on-goal": "SAVE", "missed-shot": "MISS", "blocked-shot": "BLOCK",
                              "goal": "GOAL!"})
NHL_SHOTS = ("shot-on-goal", "missed-shot", "goal")   # at a net: the away team's at the right one


def _rink_col(w):
    return lambda x: round((max(-100, min(100, x)) + 100) / 200 * (w - 1))


def _rink(w):
    """The rink, empty: its boards, center and blue lines, faceoff dots and
    nets; with (column, row) for rink feet."""
    h = max(5, min(7, round(w * 85 / 400)))
    col, grid = _rink_col(w), Grid(w, h)
    row = lambda y: min(h - 2, max(1, round((42.5 - max(-42.5, min(42.5, y))) / 85 * (h - 1))))
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
    return grid, col, row


def _this_period(g, upto):
    """(the shown event or None, the events of its period up to it, the period's name)."""
    cur = upto[-1] if upto else None
    period = cur.period if cur else g.period
    return cur, [e for e in upto if e.period == period], cur.when.rsplit(" ", 1)[0] if cur else g.short_period


def _event_line(cur, w, teams, extra=""):
    """The shown event in words: "SHOT · FLA Reinhart (wrist) · saved by Ullmark   2nd 14:12"."""
    if cur is None:
        return DIM + "no events yet".ljust(w) + RESET
    name = NHL_NAMES.get(cur.kind, cur.kind.upper())
    head = BOLD + name + RESET + " · " + teams.name(cur.side) + " "
    return _line(w, head, len(name) + 4 + len(teams.abbrev[cur.side]), cur.text + extra, cur.when)


def _shot_counts(shots, teams, period):
    """The period's shots by team, "shots in 2nd: FLA 9 · OTT 6". Always
    labeled: bare, as when gone back in time, they read like the score."""
    count = {s: sum(1 for e in shots if e.side == s) for s in ("away", "home")}
    head = "shots in %s: " % period
    text = DIM + head + RESET + teams.name("away") + " %d · " % count["away"] \
        + teams.name("home") + " %d" % count["home"]
    n = len(head) + len(teams.abbrev["away"]) + len(teams.abbrev["home"]) + len(str(count["away"])) \
        + len(str(count["home"])) + 5
    return text, n


def shot_map(g, upto, w, teams, label, note):
    """The shots (◦) and goals (●) of the shown event's period up to it, each
    team attacking the far end."""
    grid, col, row = _rink(w)
    cur, period, name = _this_period(g, upto)
    shots = [e for e in period if e.kind in ("shot-on-goal", "goal")]
    m = marks()
    # Goals last: shots cluster by the nets, and a later one in a goal's cell would hide it.
    for e in sorted(shots, key=lambda e: e.kind == "goal"):
        goal = e.kind == "goal"
        grid.put(col(e.xy[0]), row(e.xy[1]), m["goal"] if goal else m["shot"], teams.fg[e.side] + (BOLD if goal else ""))
    counts, n = _shot_counts(shots, teams, name)
    legend = "%s shot %s goal   " % (m["shot"], m["goal"]) + counts
    return grid.lines() + [_caption(w, note, legend, label, 16 + n, (counts, n), keep=(counts, n)),
                           _event_line(cur, w, teams)]


def zone_strip(g, upto, w, teams, label, note):
    """Where the shown event happened, and its period's shots up to it."""
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
    cur, period, name = _this_period(g, upto)
    where = ""
    if cur:
        x = cur.xy[0]
        grid.put(min(w - 2, max(1, col(x))), 1, marks()["event"], teams.fg[cur.side] + BOLD)
        where = " · " + ("neutral zone" if -25 <= x <= 25 else "%s end" % (g.home.abbrev if x > 0 else g.away.abbrev))
    counts, n = _shot_counts([e for e in period if e.kind in ("shot-on-goal", "goal")], teams, name)
    return grid.lines() + [_event_line(cur, w, teams, where),
                           _caption(w, note, counts, label, n, keep=(counts, n))]


def nhl_latest(g, upto, w, teams, label, note, earlier=False):
    """The shown event where it happened, with what it was (a shot's arrow
    toward the net it went at); with earlier, the two before it in its
    period too, numbered and faded, and named in the caption."""
    grid, col, row = _rink(w)
    cur, period, _ = _this_period(g, upto)
    if cur is None:
        return grid.lines() + [DIM + "no events yet".ljust(w) + RESET, _caption(w, note, "", label, 0)]
    m = marks()
    recent = []
    x, y, style = col(cur.xy[0]), row(cur.xy[1]), teams.fg[cur.side] + BOLD
    for n, e in enumerate(reversed(period[-3:-1] if earlier else []), 1):
        _before(grid, col(e.xy[0]), row(e.xy[1]), str(n), teams.fg[e.side] + DIM, (x, y))
        recent.append("%d %s %s" % (n, teams.abbrev[e.side], NHL_NAMES.get(e.kind, e.kind).lower()))
    tag = NHL_TAGS.get(cur.kind, cur.kind.upper())
    if cur.kind in NHL_SHOTS:   # an arrow at the net it went at, beyond the marker if there's room
        if cur.side == "away":
            options = (("right", "──▶ " + tag), ("left", tag + " ──▶"), ("right", tag), ("left", tag))
        else:
            options = (("left", tag + " ◀──"), ("right", "◀── " + tag), ("left", tag), ("right", tag))
    else:
        options = (("right", tag), ("left", tag))
    _place_tag(grid, x, y, options, style)
    grid.put(x, y, m["goal"] if cur.kind == "goal" else m["event"], style)
    recent = " · ".join(recent)
    return grid.lines() + [_event_line(cur, w, teams), _caption(w, note, DIM + recent + RESET, label, len(recent))]
