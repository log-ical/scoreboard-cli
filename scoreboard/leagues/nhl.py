"""NHL via the public NHL web API (api-web.nhle.com)."""

import time

from ..model import Game, Team, ordinal
from ..net import fetch_json, fetch_json_timed

API = "https://api-web.nhle.com/v1"
LOGO_URL = "https://a.espncdn.com/combiner/i?img=/i/teamlogos/nhl/500/{}.png&h=200&w=200"

# NHL abbreviations that differ from ESPN's logo filenames.
ESPN_ABBREV = {"SJS": "sj", "TBL": "tb", "NJD": "nj", "LAK": "la", "UTA": "utah"}

STANDINGS_REFRESH = 300   # seconds between standings fetches (for team records)

LIVE_STATES = ("LIVE", "CRIT")

# Season records by abbreviation, from the standings. Fetched with the game
# list, never while watching, so a game that ends in real time can't show
# up in a record before a delayed view reaches the final.
records, records_fetched = {}, 0.0


def refresh_records():
    global records_fetched
    if time.time() - records_fetched < STANDINGS_REFRESH:
        return
    records_fetched = time.time()
    try:
        for t in fetch_json(API + "/standings/now").get("standings", []):
            abbrev = (t.get("teamAbbrev") or {}).get("default")
            if abbrev:
                records[abbrev] = "%d-%d-%d" % (t.get("wins", 0), t.get("losses", 0), t.get("otLosses", 0))
    except Exception:
        pass
FINAL_STATES = ("FINAL", "OFF")


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
    abbrev = t["abbrev"]
    name = t.get("commonName") or t.get("name") or {}
    place = (t.get("placeName") or {}).get("default", "")
    return Team(
        abbrev=abbrev,
        name=("%s %s" % (place, name.get("default", abbrev))).strip(),
        score=t.get("score", 0) or 0,
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
                    text = _goal_text(e)
                else:
                    who = (e.get("committedByPlayer") or {}).get("lastName") or {}
                    text = "%s %s · %s (%s min)" % (
                        (e.get("teamAbbrev") or {}).get("default", ""), who.get("default") or "bench",
                        (e.get("descKey") or "").replace("-", " "), e.get("duration", ""))
                t = e.get("timeInPeriod", "")
                events.append(((pd.get("number") or 0, t.zfill(5)),
                               "%-4s %5s  %-7s %s" % (_period_labels(pd)[1], t, kind, text)))
    events.sort(key=lambda x: x[0], reverse=True)
    return [(None, text) for _, text in events]


def parse_game(data, data_t=0.0, detail=False):
    """Build a Game from a score/now entry or (detail=True) a landing response."""
    state = data.get("gameState", "")
    clock = data.get("clock") or {}
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
        clock_secs=clock.get("secondsRemaining"),
        # The intermission clock runs even though "running" is false.
        clock_running=bool(clock.get("running") or clock.get("inIntermission")),
        clock_anchor=data_t,
        start_utc=data.get("startTimeUTC", ""),
        venue=(data.get("venue") or {}).get("default", ""),
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


class Poller:
    def __init__(self, game_id):
        self.url = "%s/gamecenter/%s/landing" % (API, game_id)

    def poll(self):
        """Return (game, data_time, seconds_until_next_poll)."""
        now = time.time()
        data, age, max_age = fetch_json_timed(self.url)
        age = age or 0.0    # no Age header: fresh from the NHL's servers
        # Timestamp the snapshot when its clock was read, not when we got it.
        data_t = now - age
        # The CDN caches for ~19 s; poll just after the copy expires so every
        # request brings new data.
        return parse_game(data, data_t, detail=True), data_t, max(2, max_age - age + 1)


class NHL:
    key = "nhl"
    name = "NHL"

    def list_games(self):
        refresh_records()
        return [parse_game(g) for g in fetch_json(API + "/score/now").get("games", [])]

    def open(self, game):
        return Poller(game.ref["id"])

    def score_alert(self, points):
        return "GOAL!"
