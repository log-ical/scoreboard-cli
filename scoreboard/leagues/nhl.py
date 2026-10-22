"""NHL via the public NHL web API (api-web.nhle.com)."""

import datetime
import threading
import time

from ..model import Event, Game, Team, ordinal, text
from ..net import fetch_json, fetch_json_timed

API = "https://api-web.nhle.com/v1"
LOGO_URL = "https://a.espncdn.com/combiner/i?img=/i/teamlogos/nhl/500/{}.png&h=200&w=200"

# NHL abbreviations that differ from ESPN's logo filenames.
ESPN_ABBREV = {"SJS": "sj", "TBL": "tb", "NJD": "nj", "LAK": "la", "UTA": "utah"}

STANDINGS_REFRESH = 300   # seconds between standings fetches (for team records)
SLATE_DAYS = 7            # the game list covers the NHL's current day and the six after it
EARLIER_DAYS = 14         # how far back "earlier games" looks for a day with games

LIVE_STATES = ("LIVE", "CRIT")
FINAL_STATES = ("FINAL", "OFF")

# Season records by abbreviation, from the standings. Requested with the
# game list, never while watching, so a game that ends in real time can't
# show up in a record before a delayed view reaches the final. The standings
# are slow, so they load in the background instead of holding up the list,
# and show up once they arrive (the request still dates from the list).
records, records_fetched = {}, 0.0
_records_lock = threading.Lock()


def refresh_records():
    """Start loading the standings if due; return the thread loading them, or None."""
    global records_fetched
    with _records_lock:   # list loads for two screens can overlap
        if time.time() - records_fetched < STANDINGS_REFRESH:
            return None
        records_fetched = time.time()
    t = threading.Thread(target=_load_records, daemon=True)
    t.start()
    return t


def _load_records():
    try:
        loaded = {}
        for t in fetch_json(API + "/standings/now").get("standings", []):
            abbrev = (t.get("teamAbbrev") or {}).get("default")
            if abbrev:
                loaded[abbrev] = "%d-%d-%d" % (t.get("wins", 0), t.get("losses", 0), t.get("otLosses", 0))
        records.update(loaded)   # one step, for readers on other threads
    except Exception:
        pass


def _period_labels(pd):
    pd = pd or {}
    num = pd.get("number") or 0
    ptype = pd.get("periodType", "REG")
    if ptype == "SO":
        return "SHOOTOUT", "SO"
    if ptype == "OT":
        n = num - pd.get("maxRegulationPeriods", 3)
        label = "OT" if n <= 1 else "%dOT" % n
        return label, label
    return ordinal(num) + " PERIOD", ordinal(num)


def _team(t):
    abbrev = text(t["abbrev"])
    name = t.get("commonName") or t.get("name") or {}
    place = text((t.get("placeName") or {}).get("default"))
    return Team(
        abbrev=abbrev,
        name=("%s %s" % (place, text(name.get("default", abbrev)))).strip(),
        score=int(t.get("score") or 0),   # here, not in the drawing, if it isn't a number
        id=str(t.get("id", "")),
        record=records.get(abbrev, ""),
        logo_url=LOGO_URL.format(ESPN_ABBREV.get(abbrev, abbrev.lower())),
    )


def _strength_tag(data, side):
    sit = data.get("situation") or {}
    desc = (sit.get(side) or {}).get("situationDescriptions") or []
    if "PP" in desc:
        return "POWER PLAY %s" % sit.get("timeRemaining", "")
    if "EN" in desc:
        return "EMPTY NET"
    return ""


def _goal_text(g):
    desc = "%s %s" % ((g.get("teamAbbrev") or {}).get("default", ""), (g.get("name") or {}).get("default", ""))
    assists = [(a.get("name") or {}).get("default", "") for a in g.get("assists", [])]
    if assists:
        desc += " · " + ", ".join(assists)
    if g.get("strength") in ("pp", "sh"):
        desc += " · " + g["strength"].upper()
    return desc


def _last_goal(data):
    goals = [(p, g) for p in (data.get("summary") or {}).get("scoring", []) for g in p.get("goals", [])]
    if not goals:
        return ""
    p, g = goals[-1]
    name, _, rest = _goal_text(g).partition(" · ")
    return "Last goal: %s (%s %s)%s" % (name, _period_labels(p.get("periodDescriptor"))[1],
                                       g.get("timeInPeriod", ""), " · " + rest if rest else "")


def _plays(data):
    """Goals and penalties, newest first, for the expanded play list."""
    summary = data.get("summary") or {}
    events = []
    for kind, items in (("GOAL", "goals"), ("PENALTY", "penalties")):
        for p in summary.get("scoring" if kind == "GOAL" else "penalties", []):
            pd = p.get("periodDescriptor") or {}
            for e in p.get(items, []):
                if kind == "GOAL":
                    entry = _goal_text(e)
                else:
                    who = e.get("committedByPlayer") or {}
                    first = text((who.get("firstName") or {}).get("default"))
                    last = text((who.get("lastName") or {}).get("default"))
                    # "B. Tkachuk", as goals name players: brothers on a team are two people.
                    name = ("%s. %s" % (first[0], last) if first and last else last) or "bench"
                    entry = "%s %s · %s (%s min)" % (
                        (e.get("teamAbbrev") or {}).get("default", ""), name,
                        (e.get("descKey") or "").replace("-", " "), e.get("duration", ""))
                t = e.get("timeInPeriod", "")
                events.append(((pd.get("number") or 0, t.zfill(5)),
                               "%-4s %5s  %-7s %s" % (_period_labels(pd)[1], t, kind, entry)))
    events.sort(key=lambda x: x[0], reverse=True)
    return [(None, entry) for _, entry in events]


def parse_game(data, data_t=0.0, detail=False):
    """Build a Game from a score/now entry or (detail=True) a landing response."""
    state = data.get("gameState", "")
    clock = data.get("clock") or {}
    secs = clock.get("secondsRemaining")
    pd = data.get("periodDescriptor") or {}
    label, short = _period_labels(pd)
    g = Game(
        league="nhl",
        id=str(data["id"]),
        state="live" if state in LIVE_STATES else ("final" if state in FINAL_STATES else "pre"),
        away=_team(data["awayTeam"]),
        home=_team(data["homeTeam"]),
        period=pd.get("number") or 0,
        period_label=label,
        short_period=short,
        clock_secs=float(secs) if secs is not None else None,
        # The intermission clock runs even though "running" is false.
        clock_running=bool(clock.get("running") or clock.get("inIntermission")),
        clock_anchor=data_t,
        start_utc=data.get("startTimeUTC", ""),
        venue=text((data.get("venue") or {}).get("default")),
        ref={"id": data["id"]},
    )
    if clock.get("inIntermission"):
        g.break_label = "INTERMISSION · " + label
        g.break_short = "INT " + short
    if g.state == "final":
        g.final_label = "FINAL" + ("" if pd.get("periodType", "REG") == "REG" else "/" + pd["periodType"])
    if g.state == "pre":
        g.clock_secs = 20 * 60
    if detail:
        for team, side in ((g.away, "awayTeam"), (g.home, "homeTeam")):
            if "sog" in data[side]:
                team.stats.append("SOG %s" % data[side]["sog"])
            tag = _strength_tag(data, side)
            if tag:
                team.tags.append(tag)
        g.last_play = _last_goal(data)
        g.plays = _plays(data)
    return g


# The events the play-by-play views draw, and how each is put in words
# from its details (players by id, from the roster).
EVENT_KINDS = ("faceoff", "hit", "giveaway", "takeaway", "shot-on-goal", "missed-shot", "blocked-shot", "goal",
               "penalty")


def _event_text(kind, d, name):
    """What happened, after the team: "Reinhart (wrist) · saved by Ullmark"."""
    shot = " (%s)" % d["shotType"] if d.get("shotType") else ""
    if kind == "faceoff":
        return "%s wins it" % name("winningPlayerId")
    if kind == "hit":
        return "%s on %s" % (name("hittingPlayerId"), name("hitteePlayerId"))
    if kind in ("giveaway", "takeaway"):
        return name("playerId")
    if kind == "shot-on-goal":
        return "%s%s · saved by %s" % (name("shootingPlayerId"), shot, name("goalieInNetId"))
    if kind == "missed-shot":
        return "%s%s · %s" % (name("shootingPlayerId"), shot, text(d.get("reason")).replace("-", " "))
    if kind == "blocked-shot":   # the blocking team's event
        return "%s blocks %s" % (name("blockingPlayerId"), name("shootingPlayerId"))
    if kind == "goal":
        assists = [name(k) for k in ("assist1PlayerId", "assist2PlayerId") if d.get(k)]
        return name("scoringPlayerId") + shot + " · " + (", ".join(assists) if assists else "unassisted")
    return "%s · %s (%s min)" % (name("committedByPlayerId"), text(d.get("descKey")).replace("-", " "),
                                 d.get("duration", ""))


def _played_by(p, upto):
    """True if play p happened by `upto`: (period, seconds left on its clock)."""
    period = (p.get("periodDescriptor") or {}).get("number") or 0
    if period != upto[0]:
        return period < upto[0]
    m, _, s = (p.get("timeRemaining") or "").partition(":")
    return m.isdigit() and s.isdigit() and int(m) * 60 + int(s) >= upto[1]


def rink_events(data, upto=None):
    """([Event] of the whole game, oldest first; the seq of the last one
    played by `upto`, (period, seconds left), or of the last one at all)
    from a play-by-play response. Only events with a place on the ice.

    The NHL's coordinates are the arena's, and teams change ends each period,
    so flip them to keep the home end on the right.
    """
    sides = {(data.get(k) or {}).get("id"): s for k, s in (("awayTeam", "away"), ("homeTeam", "home"))}
    names = {}   # "M. Tkachuk": brothers can be teammates
    for p in data.get("rosterSpots", []):
        first, last = text((p.get("firstName") or {}).get("default")), text((p.get("lastName") or {}).get("default"))
        names[p.get("playerId")] = "%s. %s" % (first[0], last) if first and last else last
    events, cut, past = [], None, False
    for i, p in enumerate(data.get("plays", [])):
        d, kind = p.get("details") or {}, p.get("typeDescKey")
        side = sides.get(d.get("eventOwnerTeamId"))
        if kind not in EVENT_KINDS or not side or not isinstance(d.get("xCoord"), (int, float)):
            continue
        flip = -1 if p.get("homeTeamDefendingSide") == "left" else 1
        y = d.get("yCoord") if isinstance(d.get("yCoord"), (int, float)) else 0
        pd = p.get("periodDescriptor") or {}
        seq = p.get("sortOrder") if isinstance(p.get("sortOrder"), int) else i
        events.append(Event(seq, None, None, None, None, period=pd.get("number") or 0,
                            when="%s %s" % (_period_labels(pd)[1], text(p.get("timeInPeriod"))), side=side, kind=kind,
                            text=_event_text(kind, d, lambda k: names.get(d.get(k)) or "?"),
                            xy=(d["xCoord"] * flip, y * flip)))
        # The cut is before the first event after `upto`: nothing past it
        # shows, even listed with an earlier time.
        past = past or bool(upto and not _played_by(p, upto))
        if not past:
            cut = seq
    return events, cut


class Poller:
    def __init__(self, game_id):
        self.url = "%s/gamecenter/%s/landing" % (API, game_id)
        self.pbp = "%s/gamecenter/%s/play-by-play" % (API, game_id)
        self.want_live = False      # set by the game view while the play-by-play view is on
        self.events, self.cut = [], None   # the play-by-play's events, and the last one played

    def poll(self):
        """Return (game, data_time, seconds_until_next_poll)."""
        now = time.time()
        data, age, max_age = fetch_json_timed(self.url)
        age = age or 0.0    # no Age header: fresh from the NHL's servers
        # Timestamp the snapshot when its clock was read, not when we got it.
        data_t = now - age
        g = parse_game(data, data_t, detail=True)
        if self.want_live and g.state != "pre":
            # The CDN caches the play-by-play apart from the landing, and its
            # copy can be the newer one: the snapshot shows events up to the
            # landing's game time only, or a delayed view could show a goal
            # before its score does. (Breaks and finals: the period is over.)
            live = g.state == "live" and not g.break_label and g.clock_secs is not None
            try:   # ~11 KB gzipped: only while the view is on
                self.events, self.cut = rink_events(fetch_json(self.pbp), (g.period, g.clock_secs) if live else None)
            except Exception:
                pass   # the last ones fetched stay, cut where they were
            g.play_seq = self.cut
        # The API reports the clock but not between copies, so the local clock
        # runs on for at most a copy's lifetime and a poll; without a cap, a
        # stalled network or a laptop's sleep would run it down to 0:00.
        g.clock_cap = max_age + 20
        # The CDN caches for ~19 s; poll just after the copy expires so every
        # request brings new data (but at least every minute, whatever it says).
        return g, data_t, min(60, max(2, max_age - age + 1))

    def play_details(self):
        """[Event] for the play-by-play views, a copy for the game view."""
        return list(self.events)


def upcoming(current, seen):
    """Games on the days after the NHL's current day, to fill out the week.

    score/now has one day with live detail (clocks); scoreboard/now has the
    days around it, but no clocks, so it only adds the days after. The NHL's
    "current day" lasts into the next morning, so late at night tonight's
    games come from here too.
    """
    try:
        last = (datetime.date.fromisoformat(current) + datetime.timedelta(days=SLATE_DAYS - 1)).isoformat()
        days = fetch_json(API + "/scoreboard/now").get("gamesByDate", [])
    except Exception:
        return []   # no week: the current day still shows
    return [parse_game(g) for day in days if current < day.get("date", "") <= last
            for g in day.get("games", []) if str(g.get("id")) not in seen]


class NHL:
    key = "nhl"
    name = "NHL"
    first_day = None   # the date the game list starts on ("YYYY-MM-DD"), once it has loaded
    dated_copies = True   # snapshots are dated by their copy's Age: a later one is newer data

    def list_games(self):
        refresh_records()   # in the background: the list doesn't wait for it
        data = fetch_json(API + "/score/now")
        games = [parse_game(g) for g in data.get("games", [])]
        self.first_day = data.get("currentDate") or self.first_day
        return games + upcoming(data.get("currentDate"), {g.id for g in games})

    def earlier(self, day):
        """(the last day before `day` with games, its games), looking back
        EARLIER_DAYS days; (None, []) if there were none."""
        d = datetime.date.fromisoformat(day)
        for _ in range(EARLIER_DAYS):
            d -= datetime.timedelta(days=1)
            games = [parse_game(g) for g in fetch_json(API + "/score/" + d.isoformat()).get("games", [])]
            if games:
                return d.isoformat(), games
        return None, []

    def open(self, game):
        return Poller(game.ref["id"])

    def score_alert(self, points):
        return "GOAL!"
