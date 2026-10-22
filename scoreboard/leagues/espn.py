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
from urllib.parse import quote, urlparse

from ..model import Event, Game, Team, ordinal, parse_utc, text
from ..net import fetch_json, fetch_json_timed

SITE = "https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/scoreboard"
CORE = "https://sports.core.api.espn.com/v2/sports/{sport}/leagues/{league}/events/{id}/competitions/{id}"
LOGO = "https://a.espncdn.com/combiner/i?img={path}&h=200&w=200"
SLATE_REFRESH = 600    # seconds between refetches of the later days in a week's game list
EARLIER_DAYS = 14      # how far back "earlier games" looks for a day with games
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
    # Quoted, so a path can't add its own &h=&w= (the combiner takes the first).
    return LOGO.format(path=quote(path)) if path else ""


def _team(c):
    t = c["team"]
    try:
        score = int(float(c.get("score") or 0))
    except (TypeError, ValueError):
        score = 0
    abbrev = text(t.get("abbreviation")) or "?"
    return Team(
        abbrev=abbrev,
        name=text(t.get("displayName")) or abbrev,
        score=score,
        id=str(t.get("id", "")),
        logo_url=_logo(text(t.get("logo"))),
        record=next((text(r.get("summary")) for r in c.get("records") or [] if r.get("type") == "total"), ""),
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


def field_position(spot, g):
    """(side with the ball, ball, first-down line or None) from a play's
    start or end, in yards from the away goal line, or None. The away team
    attacks the home end zone, drawn on the right."""
    has_ball = _ref_id(spot.get("team"))
    side = "away" if has_ball == g.away.id else "home" if has_ball == g.home.id else None
    to_go = spot.get("yardsToEndzone")
    if side is None or not isinstance(to_go, (int, float)) or not 0 <= to_go <= 100:
        return None
    ball = 100 - to_go if side == "away" else to_go
    first, dist = None, spot.get("distance") or 0
    if (spot.get("down") or 0) > 0 and dist > 0:
        first = min(100, ball + dist) if side == "away" else max(0, ball - dist)
    return side, ball, first


def _before(val, play):
    """True if a status (period, clock) is a copy from before `play`: still in
    an earlier period, or with more time on the clock than the play's (an NBA
    play's clock is when it happened, an NFL play's at its snap; either way
    the clock can only be lower after it)."""
    if play is None or val[1] is None:
        return False
    pclock, period = (play.get("clock") or {}).get("value"), (play.get("period") or {}).get("number")
    if not isinstance(pclock, (int, float)) or not isinstance(period, int):
        return False
    return val[0] < period or (val[0] == period and val[1] > pclock + 0.5)


def _eastern_day(events):
    """The US Eastern date of the first game: what ESPN's `dates` takes.
    Games start between ~9:30 and 23:00 Eastern, so six hours back from UTC
    always lands on the right day."""
    starts = [t for t in (parse_utc(e.get("date")) for e in events) if t is not None]
    if not starts:
        return None
    return datetime.datetime.fromtimestamp(min(starts) - 6 * 3600, datetime.timezone.utc).date().isoformat()


class EspnLeague:
    key = name = sport = league = None
    periods = 4
    period_secs = 12 * 60
    poll_every = 4         # seconds between polls while watching
    clock_cap = 26         # max seconds the clock can run without a new play
    stop_words = ()
    slate_days = 1         # days in the game list: the scoreboard's day and the ones after it
    ot_secs = 10 * 60
    track_plays = False    # the play-by-play view needs every play (to step through, the NBA's charts)

    def __init__(self):
        self.later = {}    # "YYYYMMDD" -> (fetched at, events) for the days after the scoreboard's
        self.first_day = None   # the (US Eastern) date the game list starts on, "YYYY-MM-DD"

    # -- labels ------------------------------------------------------------

    def labels(self, period):
        if period > self.periods:
            n = period - self.periods
            ot = "OT" if n == 1 else "%dOT" % n
            return ot, ot
        return ordinal(period) + " QUARTER", "Q%d" % period

    def elapsed(self, period, clock):
        """Game seconds played when `clock` seconds are left in `period`."""
        regular = min(period - 1, self.periods) * self.period_secs
        overtime = max(0, period - 1 - self.periods) * self.ot_secs
        length = self.period_secs if period <= self.periods else self.ot_secs
        return regular + overtime + max(0, length - clock)

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
                g.break_label = g.break_short = text(stype.get("description")).upper()
        elif g.state == "final":
            g.final_label = (text(stype.get("shortDetail")) or "Final").upper()
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
            venue=text((c.get("venue") or {}).get("fullName")),
            ref={"id": str(e["id"])},
        )
        self.apply_status(g, e.get("status") or {})
        return g

    def list_games(self):
        url = SITE.format(sport=self.sport, league=self.league)
        data = fetch_json(url)
        events = data.get("events", [])
        self.first_day = (data.get("day") or {}).get("date") or _eastern_day(events) or self.first_day
        if self.slate_days > 1:
            events = events + self.later_events(url, (data.get("day") or {}).get("date"))
        games, seen = [], set()
        for e in events:
            if str(e.get("id")) not in seen:   # a late game can be listed on two days
                seen.add(str(e.get("id")))
                games.append(self.parse_event(e))
        return games

    def earlier(self, day):
        """(the last day before `day` with games, its games), looking back
        EARLIER_DAYS days; (None, []) if there were none."""
        url = SITE.format(sport=self.sport, league=self.league) + "?dates="
        d = datetime.date.fromisoformat(day)
        for _ in range(EARLIER_DAYS):
            d -= datetime.timedelta(days=1)
            events = fetch_json(url + d.strftime("%Y%m%d")).get("events", [])
            if events:
                return d.isoformat(), [self.parse_event(e) for e in events]
        return None, []

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
        later = dict(self.later)   # a copy: list loads for two screens can overlap
        due = [d for d in dates if now - later.get(d, (0.0, None))[0] > SLATE_REFRESH]

        for d in due:
            try:
                later[d] = (now, fetch_json(url + "?dates=" + d).get("events", []))
            except Exception:
                pass
        self.later = later = {d: v for d, v in later.items() if d in dates}
        return [e for d in dates for e in later.get(d, (0.0, []))[1]]

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
    ot_secs = 5 * 60
    track_plays = True
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
            g.last_play = "Last play: " + text(play.get("text"))

    def score_alert(self, points):
        return None        # baskets are too frequent to flash


class NFL(EspnLeague):
    key, name = "nfl", "NFL"
    sport, league = "football", "nfl"
    period_secs = 15 * 60
    poll_every = 5
    clock_cap = 50         # 40 s play clock plus the play itself
    track_plays = True     # the play-by-play view steps through them
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
        g.last_play = "Last play: " + text(play.get("text"))
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
            g.note = text(end["downDistanceText"])
        g.ball = field_position(end, g)   # where the next snap is

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
        self.play_status_val = None   # the first status after the last play (from after it)
        self.want_plays = False     # set by the game view while the play list is open
        self.want_live = False      # ... and while the play-by-play view is on
        self.plays = {}             # seq -> text
        self.details = {}           # seq -> Event, for the play-by-play view (track_plays)
        self.backfilled = False
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

    def play_detail(self, play, seq):
        """What the play-by-play views need from a play: an Event."""
        L, period = self.league, (play.get("period") or {}).get("number") or 1
        clock = play.get("clock") or {}
        team = _ref_id(play.get("team"))
        side = "away" if team == self.base.away.id else "home" if team == self.base.home.id else None
        shot, xy = None, play.get("coordinate") or {}
        x, y = xy.get("x"), xy.get("y")
        # Free throws carry junk coordinates; real ones are feet on a half court.
        spot = (x, y) if isinstance(x, (int, float)) and isinstance(y, (int, float)) \
            and 0 <= x <= 50 and 0 <= y <= 47 else None
        if play.get("shootingPlay") and side and spot:
            shot = (x, y, side, bool(play.get("scoringPlay")))
        end = play.get("end") or {}
        return Event(seq, L.elapsed(period, clock.get("value") or 0), int(play.get("awayScore") or 0),
                     int(play.get("homeScore") or 0), shot, period=period,
                     when="%s %s" % (L.labels(period)[1], text(clock.get("displayValue"))), side=side,
                     kind=text((play.get("type") or {}).get("text")), text=text(play.get("text")), xy=spot,
                     start=field_position(play.get("start") or {}, self.base), end=field_position(end, self.base),
                     points=play.get("scoreValue") if play.get("scoringPlay") else 0,
                     yards=play.get("statYardage") if isinstance(play.get("statYardage"), int) else None,
                     after=text(end.get("downDistanceText")) if (end.get("down") or 0) > 0 else "")

    def _record(self, play):
        """Keep a play for the play list (and the play-by-play view); its (seq, text)."""
        entry = self.play_entry(play)
        if entry:
            self.plays[entry[0]] = entry[1]
            if self.league.track_plays:
                self.details[entry[0]] = self.play_detail(play, entry[0])
        return entry

    def _entries(self, page):
        return [e for e in map(self._record, page.get("items", [])) if e]

    def backfill(self):
        """Every play so far, once, for the play-by-play view: a whole game is
        one request (~40 KB for the NBA). After it the play list has everything, too."""
        page = fetch_json(self.core + "/plays?limit=1000")
        for p in page.get("items", []):
            self._record(p)
        self.backfilled = True
        if (page.get("pageCount") or 1) == 1:
            count = page.get("count") or len(page.get("items", []))
            self.oldest_page, self.newest_page = 1, max(1, -(-count // PLAY_HISTORY))
            self.plays_fetched, self.plays_ref = time.time(), self.play_ref

    def play_details(self):
        """[Event] in play order, a copy for the game view."""
        return [self.details[seq] for seq in sorted(self.details)]

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
        if restart:
            # Keep only plays newer than these pages (seen by polling).
            newest = max((e[0] for n in need for e in map(self.play_entry, pages[n].get("items", [])) if e),
                         default=0)
            self.plays = {seq: t for seq, t in self.plays.items() if seq > newest}
            self.oldest_page = want[0]
        for n in need:
            self._entries(pages[n])
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
            self._record(self.play)
        live = self.want_live and L.track_plays
        if live and not self.backfilled:
            try:
                self.backfill()
            except Exception:
                pass   # tried again next poll
        # Polling only sees the latest play, so refetch the list now and then
        # to fill in the plays between polls.
        if (self.want_plays or live) and self.plays_ref != self.play_ref and now - self.plays_fetched > PLAY_REFRESH:
            try:
                self.fetch_plays()
            except Exception:
                self.plays_fetched = now   # retry after PLAY_REFRESH

        # Dated when it all came in, not when the status copy was made: that's
        # unknown (ESPN's max-age counts down to the copy's expiry), and the
        # play and its score are often fresher than the status. A delayed view
        # must not show a play before its time. The guess is for the clock.
        arrived = time.time()
        clock_t = now - age     # about when the status's clock was read

        g = copy.deepcopy(self.base)
        g.away.stats, g.away.tags, g.home.stats, g.home.tags = [], [], [], []
        L.apply_status(g, status)
        play = self.play
        if play and play.get("awayScore") is not None:
            g.away.score = int(play["awayScore"])
            g.home.score = int(play["homeScore"])

        val = (g.period, g.clock_secs)
        if val != self.status_val:
            self.status_val, self.status_seen = val, clock_t
        if self.play_status_val is None and not _before(val, play):
            self.play_status_val = val

        if g.state == "live" and not g.break_label and g.clock_secs is not None:
            # The clock moved since the last play arrived: it is running. Not
            # counting status copies from before the play, which can come with
            # it: the next, fresh one would look like the clock moved.
            base = self.play_status_val
            moved = base is not None and val[0] == base[0] and val[1] < base[1]
            g.clock_running = moved or (play is not None and not L.stops_clock(play, g.clock_secs, g.period))
            # Anchor at the play's wallclock when the status shows the play's
            # clock; otherwise at when we first saw this status value.
            g.clock_anchor = self.status_seen
            if play and not moved:
                pclock = (play.get("clock") or {}).get("value")
                wall = parse_utc(play.get("wallclock"))
                same_period = (play.get("period") or {}).get("number") == g.period
                if wall and same_period and pclock is not None and abs(pclock - g.clock_secs) < 1:
                    g.clock_anchor = min(wall, clock_t)
            g.clock_cap = L.clock_cap
        L.decorate(g, situation, play)
        entry = self.play_entry(play) if play else None
        g.play_seq = entry and entry[0]
        return g, arrived, L.poll_every
