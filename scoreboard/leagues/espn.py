"""NBA and NFL via ESPN's public APIs.

The game list comes from the site scoreboard. While watching a game we poll
three tiny "core" API documents instead (~3 KB in total): status (clock and
period), situation (timeouts, fouls, down & distance) and the last play.

ESPN has no "clock running" flag, so it is inferred: the last play's type
tells whether it stopped the clock (timeout, foul, incomplete pass, ...), and
its wallclock timestamp says when the clock had the value it shows.
"""

import copy
import datetime
import re
import threading
import time
from functools import partial
from urllib.parse import urlparse

from ..model import Game, Team, ordinal, parse_utc
from ..net import fetch_json, fetch_json_timed

SITE = "https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/scoreboard"
CORE = "https://sports.core.api.espn.com/v2/sports/{sport}/leagues/{league}/events/{id}/competitions/{id}"
LOGO = "https://a.espncdn.com/combiner/i?img={path}&h=200&w=200"
SLATE_REFRESH = 600    # seconds between refetches of the later days in a week's game list
PLAY_HISTORY = 25      # plays per page of the expanded play list (newest two pages load first)
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
        record=next((r.get("summary", "") for r in c.get("records") or [] if r.get("type") == "total"), ""),
        color=_hex(t.get("color")),
        alt_color=_hex(t.get("alternateColor")),
    )


def _ref_id(ref):
    m = re.search(r"/teams/(\d+)", (ref or {}).get("$ref", ""))
    return m.group(1) if m else None


def _parallel(*calls):
    """Run the calls (functions of no arguments) at once and return their
    results in order, raising the first one's error if any failed.

    All but the last run on daemon threads (not an executor, whose threads
    Python waits for at exit), so a hung request can't hold up quitting.
    """
    results, errors = [None] * len(calls), [None] * len(calls)

    def run(i):
        try:
            results[i] = calls[i]()
        except Exception as e:
            errors[i] = e
    threads = [threading.Thread(target=run, args=(i,), daemon=True) for i in range(len(calls) - 1)]
    for t in threads:
        t.start()
    if calls:
        run(len(calls) - 1)
    for t in threads:
        t.join()
    for e in errors:
        if e is not None:
            raise e
    return results


class EspnLeague:
    key = name = sport = league = None
    periods = 4
    period_secs = 12 * 60
    poll_every = 4         # seconds between polls while watching
    clock_cap = 26         # max seconds the clock can run without a new play
    stop_words = ()
    slate_days = 1         # days in the game list: the scoreboard's day and the ones after it

    def __init__(self):
        self.later = {}    # "YYYYMMDD" -> (fetched at, events) for the days after the scoreboard's

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
        url = SITE.format(sport=self.sport, league=self.league)
        data = fetch_json(url)
        events = data.get("events", [])
        if self.slate_days > 1:
            events = events + self.later_events(url, (data.get("day") or {}).get("date"))
        games, seen = [], set()
        for e in events:
            if str(e.get("id")) not in seen:   # a late game can be listed on two days
                seen.add(str(e.get("id")))
                games.append(self.parse_event(e))
        return games

    def later_events(self, url, day):
        """Events on the days after the scoreboard's day, to fill out the week.

        The API takes one date per request, and a schedule days out rarely
        changes, so each day is refetched only every SLATE_REFRESH seconds;
        a day whose fetch fails keeps what it had. The days are fetched one
        after another on the pooled connection (~0.05 s each): opening a
        connection per day at once sometimes stalls them all for seconds.
        """
        try:
            first = datetime.date.fromisoformat(day)
        except (TypeError, ValueError):
            return []
        dates = [(first + datetime.timedelta(days=n)).strftime("%Y%m%d") for n in range(1, self.slate_days)]
        now = time.time()
        due = [d for d in dates if now - self.later.get(d, (0.0, None))[0] > SLATE_REFRESH]

        for d in due:
            try:
                self.later[d] = (now, fetch_json(url + "?dates=" + d).get("events", []))
            except Exception:
                pass
        self.later = {d: v for d, v in self.later.items() if d in dates}
        return [e for d in dates for e in self.later.get(d, (0.0, []))[1]]

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
    slate_days = 7         # the scoreboard has one day; NFL's already has the week
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
    """Not thread-safe: the game view's worker thread is its only user."""

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
        # Pages of the API's play list loaded so far (page 1 is the opening tip).
        self.oldest_page = self.newest_page = None

    def play_entry(self, play):
        """(seq, "Q4  6:36  text") for the expanded play list."""
        try:
            seq = int(play.get("sequenceNumber"))
        except (TypeError, ValueError):
            return None
        period = (play.get("period") or {}).get("number") or 1
        clock = (play.get("clock") or {}).get("displayValue", "")
        return seq, "%-3s %5s  %s" % (self.league.labels(period)[1], clock, play.get("text") or "")

    def _entries(self, page):
        return [e for e in map(self.play_entry, page.get("items", [])) if e]

    def fetch_plays(self):
        """Load the newest two pages of plays (the API pages from the first play on)."""
        url = self.core + "/plays?limit=%d&page=%%d" % PLAY_HISTORY

        def get(nums):
            return dict(zip(nums, _parallel(*[partial(fetch_json, url % n) for n in nums])))
        start = self.newest_page
        if start is None:
            count = fetch_json(self.core + "/plays?limit=1").get("count") or 0
            start = max(1, -(-count // PLAY_HISTORY))
        # A first load shows two pages: fetch the one before `start` alongside it.
        pages = get([start - 1, start] if self.oldest_page is None and start > 1 else [start])
        last = max(start, pages[start].get("pageCount") or 1)
        want = range(max(1, last - 1), last + 1)
        # Nothing loaded yet, or a gap since the last load: start over.
        restart = self.oldest_page is None or want[0] > start + 1
        need = want if restart else range(start, last + 1)
        pages.update(get([n for n in need if n not in pages]))
        entries = [e for n in need for e in self._entries(pages[n])]
        if restart:
            # Keep only plays newer than these pages (seen by polling).
            newest = max((seq for seq, _ in entries), default=0)
            self.plays = {seq: t for seq, t in self.plays.items() if seq > newest}
            self.oldest_page = want[0]
        self.plays.update(entries)
        self.newest_page = last
        self.plays_fetched, self.plays_ref = time.time(), self.play_ref

    def load_older(self):
        """Load the page of plays before the oldest one loaded."""
        if not self.oldest_page or self.oldest_page <= 1:
            return
        url = self.core + "/plays?limit=%d&page=%d" % (PLAY_HISTORY, self.oldest_page - 1)
        try:
            self.plays.update(self._entries(fetch_json(url)))
            self.oldest_page -= 1
        except Exception:
            pass

    def play_history(self):
        """([(seq, text)] newest first, whether older plays can be loaded)."""
        return sorted(self.plays.items(), reverse=True), bool(self.oldest_page and self.oldest_page > 1)

    def _situation(self):
        try:
            return fetch_json(self.core + "/situation")
        except Exception:
            return {}          # not published before the game

    def poll(self):
        """Return (game, data_time, seconds_until_next_poll)."""
        L = self.league
        now = time.time()
        # Independent documents: fetch them at once.
        (status, age, max_age), situation = _parallel(
            lambda: fetch_json_timed(self.core + "/status"), self._situation)
        if age is None:
            age = max_age / 2   # ESPN sends no Age: assume a half-expired copy
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
        return g, data_t, L.poll_every
