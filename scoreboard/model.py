"""League-neutral game model shared by every league adapter and screen."""

import calendar
import time
from dataclasses import dataclass, field


@dataclass
class Team:
    abbrev: str
    name: str                      # full name, e.g. "San Jose Sharks"
    score: int = 0
    id: str = ""
    logo_url: str = ""
    color: tuple = None            # team color from the league data, if any
    alt_color: tuple = None
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
    note: str = ""                 # extra live line, e.g. NFL down & distance
    ref: dict = field(default_factory=dict)   # league-specific ids for polling


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


def ordinal(n):
    return {1: "1st", 2: "2nd", 3: "3rd"}.get(n, "%dth" % n)


def headline(game):
    return game.break_label or game.period_label


def list_status(game, hide=False, h24=False):
    if game.state == "live":
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
