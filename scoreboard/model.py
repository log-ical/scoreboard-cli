"""League-neutral game model shared by every league adapter and screen."""

import calendar
import time
from collections import namedtuple
from dataclasses import dataclass, field

# A play or event as the play-by-play views see it, the game's in order:
# seq (its order, and what a delayed view cuts at); the NBA's game seconds
# elapsed and score after it, and for a field-goal try (x, y, side, made) in
# feet; its period and "Q3 4:12"; the team ("away", "home" or None), the
# league's kind of event and what happened; where (NHL: rink feet, home end
# on the right; NBA: court feet, the basket at the top), or None; and for
# the NFL the ball at the snap and after the play (as field_position gives
# them), the points it scored, the yards it gained, and the down after it.
Event = namedtuple("Event", "seq elapsed away home shot period when side kind text xy start end points yards"
                            " after", defaults=(None,) * 11)


@dataclass
class Team:
    abbrev: str
    name: str                      # full name, e.g. "San Jose Sharks"
    score: int = 0
    id: str = ""
    logo_url: str = ""
    color: tuple = None            # team color from the league data, if any
    alt_color: tuple = None
    record: str = ""               # season record, e.g. "12-4" or "8-2-1" (NHL: W-L-OTL)
    stats: list = field(default_factory=list)   # e.g. ["SOG 28"], ["TIMEOUTS 3"]
    tags: list = field(default_factory=list)    # situation, e.g. ["POWER PLAY 1:23"]


@dataclass
class Game:
    league: str
    id: str
    state: str                     # "pre", "live" or "final"
    away: Team
    home: Team
    period: int = 0
    period_label: str = ""         # "3rd PERIOD", "4th QUARTER", "OT"
    short_period: str = ""         # "3rd", "Q4", "OT"
    break_label: str = ""          # set during breaks: "HALFTIME", "INTERMISSION · 2nd PERIOD"
    break_short: str = ""          # same, for the game list: "HALF", "INT 2nd"
    clock_secs: float = None
    clock_running: bool = False
    clock_anchor: float = 0.0      # real time at which clock_secs was correct
    clock_cap: float = None        # never run the clock more than this past the anchor
    start_utc: str = ""
    venue: str = ""
    final_label: str = "FINAL"     # "FINAL", "FINAL/OT", "FINAL/SO"
    last_play: str = ""
    plays: list = field(default_factory=list)   # [(seq, text)], newest first; seq is None for NHL
    # The last play or event this snapshot may show (ESPN: the one behind
    # last_play; NHL: of the play-by-play's, while that view is on).
    play_seq: int = None
    # For the play-by-play view. NFL: (side with the ball, "away" or "home";
    # the ball and the first-down line, or None, in yards from the away goal line).
    ball: tuple = None
    note: str = ""                 # extra live line, e.g. NFL down & distance
    ref: dict = field(default_factory=dict)   # league-specific ids for polling


def text(value):
    """A feed value as text: "" for null, str() of anything that isn't a
    string, so a changed API can't crash the drawing with a number or null."""
    return "" if value is None else value if isinstance(value, str) else str(value)


def parse_utc(s):
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%MZ"):
        try:
            return calendar.timegm(time.strptime(s, fmt))
        except (ValueError, TypeError):
            pass
    return None


def fmt_start(game, h24=False):
    ts = parse_utc(game.start_utc)
    if ts is None:
        return ""
    local = time.localtime(ts)
    text = time.strftime("%H:%M", local) if h24 else time.strftime("%I:%M %p", local).lstrip("0")
    # ESPN's NFL scoreboard covers the whole week, so name the day if needed.
    if local[:3] != time.localtime()[:3]:
        text = time.strftime("%a ", local) + text
    return text


def fmt_clock(secs):
    return "%02d:%02d" % divmod(int(secs), 60)


def until_start(game, now=None):
    """Seconds until the scheduled start (never negative), or None if unknown."""
    ts = parse_utc(game.start_utc)
    return None if ts is None else max(0, ts - (time.time() if now is None else now))


def fmt_countdown(secs):
    """Big-clock countdown: 04:59, 1:04:59, or 2D 03:15 (days, hours, minutes)."""
    secs = int(secs)
    m, s = divmod(secs, 60)
    h, m = divmod(m, 60)
    if h >= 24:
        return "%dD %02d:%02d" % (h // 24, h % 24, m)
    return "%d:%02d:%02d" % (h, m, s) if h else "%02d:%02d" % (m, s)


def fmt_countdown_short(secs):
    """Game-list countdown: "in 2h 05m", "in 45m", "in <1m"."""
    m = int(secs) // 60
    if m < 1:
        return "in <1m"
    return "in %dh %02dm" % divmod(m, 60) if m >= 60 else "in %dm" % m


def ordinal(n):
    return {1: "1st", 2: "2nd", 3: "3rd"}.get(n, "%dth" % n)


def headline(game):
    return game.break_label or game.period_label


def list_status(game, hide=False, h24=False):
    if game.state == "live":
        if hide:   # any period, break or clock would show which games went to overtime
            return "LIVE"
        if game.break_short:
            return game.break_short
        clock = fmt_clock(game.clock_secs) if game.clock_secs is not None else ""
        return "%s  %s" % (game.short_period, clock)
    if game.state == "final":
        return "FINAL" if hide else game.final_label   # don't reveal OT when hiding
    return fmt_start(game, h24)


def progress_key(game):
    """Orders snapshots by game progress, to spot stale copies from a CDN."""
    rank = {"pre": 0, "live": 1, "final": 2}.get(game.state, 0)
    return (rank, game.period or 0, bool(game.break_label), -(game.clock_secs or 0))


def is_stale(new, last):
    """True if `new` is an old cached copy rather than a real update.

    Small upward clock corrections (video reviews) and anything with new
    points are always accepted.
    """
    if new.away.score + new.home.score > last.away.score + last.home.score:
        return False
    kn, kl = progress_key(new), progress_key(last)
    if kn[:3] != kl[:3]:
        return kn < kl
    return -kn[3] - -kl[3] > 3   # new shows 3+ s more on the same clock
