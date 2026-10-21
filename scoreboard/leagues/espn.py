"""NBA and NFL via ESPN's public APIs.

The game list comes from the site scoreboard. While watching a game we poll
three tiny "core" API documents instead (~3 KB in total): status (clock and
period), situation (timeouts, fouls, down & distance) and the last play.

ESPN has no "clock running" flag, so it is inferred: the last play's type
tells whether it stopped the clock (timeout, foul, incomplete pass, ...), and
its wallclock timestamp says when the clock had the value it shows.
"""

import copy
import re
import time
from urllib.parse import urlparse

from ..model import Game, Team, ordinal, parse_utc
from ..net import fetch_json, fetch_json_timed

SITE = "https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/scoreboard"
CORE = "https://sports.core.api.espn.com/v2/sports/{sport}/leagues/{league}/events/{id}/competitions/{id}"
LOGO = "https://a.espncdn.com/combiner/i?img={path}&h=200&w=200"
PLAY_HISTORY = 25      # plays fetched for the expanded play list
PLAY_REFRESH = 20      # seconds between refetches of that list while it's open


def _hex(s):
    try:
        return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4)) if s and len(s) == 6 else None
    except ValueError:
        return None


def _logo(url):
    # The scoreboard links a small "scoreboard" variant; use the full logo.
    path = urlparse(url).path.replace("/scoreboard/", "/") if url else ""
    return LOGO.format(path=path) if path else ""


def _team(c):
    t = c["team"]
    try:
        score = int(float(c.get("score") or 0))
    except (TypeError, ValueError):
        score = 0
    return Team(
        abbrev=t.get("abbreviation", "?"),
        name=t.get("displayName", t.get("abbreviation", "")),
        score=score,
        id=str(t.get("id", "")),
        logo_url=_logo(t.get("logo", "")),
        color=_hex(t.get("color")),
        alt_color=_hex(t.get("alternateColor")),
    )


def _ref_id(ref):
    m = re.search(r"/teams/(\d+)", (ref or {}).get("$ref", ""))
    return m.group(1) if m else None


class EspnLeague:
    key = name = sport = league = None
    periods = 4
    period_secs = 12 * 60
    poll_every = 4         # seconds between polls while watching
    clock_cap = 26         # max seconds the clock can run without a new play
    stop_words = ()

    # -- labels ------------------------------------------------------------

    def labels(self, period):
        if period > self.periods:
            n = period - self.periods
            ot = "OT" if n == 1 else "%dOT" % n
            return ot, ot
        return ordinal(period) + " QUARTER", "Q%d" % period

    def apply_status(self, g, st):
        """Fill state, period and clock fields from an ESPN status object."""
        stype = st.get("type") or {}
        g.state = {"in": "live", "post": "final"}.get(stype.get("state"), "pre")
        g.period = st.get("period") or 0
        g.period_label, g.short_period = self.labels(max(1, g.period))
        clock = st.get("clock")
        g.clock_secs = float(clock) if clock is not None else None
        name = stype.get("name", "")
        g.break_label = g.break_short = ""
        if g.state == "live":
            if name == "STATUS_HALFTIME":
                g.break_label, g.break_short = "HALFTIME", "HALF"
            elif name == "STATUS_END_PERIOD":
                g.break_label = "END OF " + g.period_label
                g.break_short = "END " + g.short_period
            elif name != "STATUS_IN_PROGRESS":
                g.break_label = g.break_short = (stype.get("description") or "").upper()
        elif g.state == "final":
            g.final_label = (stype.get("shortDetail") or "Final").upper()
        else:
            g.clock_secs = self.period_secs

    # -- game list ---------------------------------------------------------

    def parse_event(self, e):
        c = e["competitions"][0]
        sides = {x.get("homeAway"): x for x in c["competitors"]}
        g = Game(
            league=self.key,
            id=str(e["id"]),
            state="pre",
            away=_team(sides["away"]),
            home=_team(sides["home"]),
            start_utc=e.get("date", ""),
            venue=(c.get("venue") or {}).get("fullName", ""),
            ref={"id": str(e["id"])},
        )
        self.apply_status(g, e.get("status") or {})
        return g

    def list_games(self):
        data = fetch_json(SITE.format(sport=self.sport, league=self.league))
        return [self.parse_event(e) for e in data.get("events", [])]

    def open(self, game):
        return Poller(self, game)

    # -- clock inference ---------------------------------------------------

    def stops_clock(self, play, secs, period):
        kind = ((play.get("type") or {}).get("text") or "").lower()
        return any(w in kind for w in self.stop_words)

    def decorate(self, g, situation, play):
        """Add league-specific stats, tags and notes."""


class NBA(EspnLeague):
    key, name = "nba", "NBA"
    sport, league = "basketball", "nba"
    period_secs = 12 * 60
    poll_every = 3
    clock_cap = 26         # something happens within a shot clock (24 s)
    stop_words = ("timeout", "foul", "free throw", "violation", "end period", "end game",
                  "end of", "substitution", "replay", "ejection", "technical", "delay",
                  "traveling", "kicked ball", "double dribble")

    def stops_clock(self, play, secs, period):
        if super().stops_clock(play, secs, period):
            return True
        kind = ((play.get("type") or {}).get("text") or "").lower()
        text = (play.get("text") or "").lower()
        if "turnover" in kind and "steal" not in text:
            return True    # out of bounds, shot clock, ...
        # Made baskets stop the clock in the last 2 minutes of the 4th and OT.
        return bool(play.get("scoringPlay")) and period >= 4 and secs <= 120

    def decorate(self, g, situation, play):
        for team, side in ((g.away, "away"), (g.home, "home")):
            to = (situation.get(side + "Timeouts") or {}).get("timeoutsRemainingCurrent")
            fouls = situation.get(side + "Fouls") or {}
            if to is not None:
                team.stats.append("TIMEOUTS %s" % to)
            if fouls.get("teamFoulsCurrent") is not None:
                team.stats.append("FOULS %s" % fouls["teamFoulsCurrent"])
            bonus = fouls.get("bonusState")
            if g.state == "live" and bonus and bonus != "NONE":
                team.tags.append("BONUS")
        if play:
            g.last_play = "Last play: " + (play.get("text") or "")

    def score_alert(self, points):
        return None        # baskets are too frequent to flash


class NFL(EspnLeague):
    key, name = "nfl", "NFL"
    sport, league = "football", "nfl"
    period_secs = 15 * 60
    poll_every = 5
    clock_cap = 50         # 40 s play clock plus the play itself
    stop_words = ("incompletion", "timeout", "two-minute", "end period", "end of",
                  "touchdown", "field goal", "extra point", "two-point", "safety",
                  "penalty", "kickoff", "punt", "coin toss", "interception",
                  "fumble recovery (opponent)", "blocked", "missed", "spike")

    def stops_clock(self, play, secs, period):
        if super().stops_clock(play, secs, period):
            return True
        text = " %s " % (play.get("text") or "").lower()
        out = "out of bounds" in text or " ob " in text or "ob at" in text
        # Out of bounds only stops the clock late in each half.
        return out and ((period == 2 and secs <= 120) or (period >= 4 and secs <= 300))

    def decorate(self, g, situation, play):
        for team, side in ((g.away, "away"), (g.home, "home")):
            to = situation.get(side + "Timeouts")
            if to is not None:
                team.stats.append("TIMEOUTS %s" % to)
        if not play:
            return
        g.last_play = "Last play: " + (play.get("text") or "")
        if g.state != "live" or g.break_label:
            return
        end = play.get("end") or {}
        has_ball = _ref_id(end.get("team"))
        for team in (g.away, g.home):
            if has_ball and team.id == has_ball:
                team.tags.append("● BALL")
                if situation.get("isRedZone"):
                    team.tags.append("RED ZONE")
        if (end.get("down") or 0) > 0 and end.get("downDistanceText"):
            g.note = end["downDistanceText"]

    def score_alert(self, points):
        return {6: "TOUCHDOWN!", 7: "TOUCHDOWN!", 8: "TOUCHDOWN!", 3: "FIELD GOAL!",
                2: "TWO POINTS!", 1: "EXTRA POINT!"}.get(points, "SCORE!")


class Poller:
    def __init__(self, league, game):
        self.league = league
        self.base = game
        self.core = CORE.format(sport=league.sport, league=league.league, id=game.ref["id"])
        self.play_ref, self.play = None, None
        self.status_val, self.status_seen = None, 0.0
        self.play_status_val = None
        self.want_plays = False     # set by the game view while the play list is open
        self.plays = {}             # seq -> text
        self.plays_fetched, self.plays_ref = 0.0, False

    def play_entry(self, play):
        """(seq, "Q4  6:36  text") for the expanded play list."""
        try:
            seq = int(play.get("sequenceNumber"))
        except (TypeError, ValueError):
            return None
        period = (play.get("period") or {}).get("number") or 1
        clock = (play.get("clock") or {}).get("displayValue", "")
        return seq, "%-3s %5s  %s" % (self.league.labels(period)[1], clock, play.get("text") or "")

    def fetch_plays(self):
        """The last PLAY_HISTORY plays (the API pages from the first play on)."""
        url = self.core + "/plays?limit=%d&page=%%d" % PLAY_HISTORY
        page = fetch_json(url % 1)
        last = page.get("pageCount") or 1
        items = []
        if last > 1:
            if last > 2:
                items = fetch_json(url % (last - 1)).get("items", [])
            page = fetch_json(url % last)
        for p in items + page.get("items", []):
            entry = self.play_entry(p)
            if entry:
                self.plays[entry[0]] = entry[1]
        self.plays_fetched, self.plays_ref = time.time(), self.play_ref

    def poll(self):
        """Return (game, data_time, seconds_until_next_poll)."""
        L = self.league
        now = time.time()
        status, age, max_age = fetch_json_timed(self.core + "/status")
        if age is None:
            age = max_age / 2   # ESPN sends no Age: assume a half-expired copy
        try:
            situation = fetch_json(self.core + "/situation")
        except Exception:
            situation = {}     # not published before the game
        ref = (situation.get("lastPlay") or {}).get("$ref")
        if ref and ref != self.play_ref:
            self.play = fetch_json(ref)
            self.play_ref = ref
            self.play_status_val = None
            entry = self.play_entry(self.play)
            if entry:
                self.plays[entry[0]] = entry[1]
        # Polling only sees the latest play, so refetch the list now and then
        # to fill in the plays between polls.
        if self.want_plays and self.plays_ref != self.play_ref and now - self.plays_fetched > PLAY_REFRESH:
            try:
                self.fetch_plays()
            except Exception:
                self.plays_fetched = now   # retry after PLAY_REFRESH
        for seq in sorted(self.plays)[:-2 * PLAY_HISTORY]:
            del self.plays[seq]

        g = copy.deepcopy(self.base)
        g.away.stats, g.away.tags, g.home.stats, g.home.tags = [], [], [], []
        L.apply_status(g, status)
        play = self.play
        if play and play.get("awayScore") is not None:
            g.away.score = int(play["awayScore"])
            g.home.score = int(play["homeScore"])

        data_t = now - age
        val = (g.period, g.clock_secs)
        if val != self.status_val:
            self.status_val, self.status_seen = val, data_t
        if self.play_status_val is None:
            self.play_status_val = val

        if g.state == "live" and not g.break_label and g.clock_secs is not None:
            # The clock moved since the last play arrived: it is running.
            moved = val[0] == self.play_status_val[0] and val[1] < self.play_status_val[1]
            g.clock_running = moved or (play is not None and not L.stops_clock(play, g.clock_secs, g.period))
            # Anchor at the play's wallclock when the status shows the play's
            # clock; otherwise at when we first saw this status value.
            g.clock_anchor = self.status_seen
            if play and not moved:
                pclock = (play.get("clock") or {}).get("value")
                wall = parse_utc(play.get("wallclock"))
                same_period = (play.get("period") or {}).get("number") == g.period
                if wall and same_period and pclock is not None and abs(pclock - g.clock_secs) < 1:
                    g.clock_anchor = min(wall, data_t)
            g.clock_cap = L.clock_cap
        L.decorate(g, situation, play)
        entry = self.play_entry(play) if play else None
        g.play_seq = entry and entry[0]
        g.plays = sorted(self.plays.items(), reverse=True)
        return g, data_t, L.poll_every
