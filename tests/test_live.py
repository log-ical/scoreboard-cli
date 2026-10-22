"""The play-by-play view: its data from each league, and how it's drawn."""

import copy
import re
import threading
import time
import unittest

from tests.helpers import ROOT, fixture  # noqa: F401
from tests.test_concurrency import FakeLeague, FakeLogos, FakeMenu, FakeTerm, wait_for
from tests.test_leagues import LAST_PLAY, FakeApi, live_status
from scoreboard import live, screens
from scoreboard.leagues import espn, nhl
from scoreboard.model import Event, Game, Team
from scoreboard.settings import settings
from scoreboard.term import Colors, visible_len

COLORS, TINTS = Colors("16"), ((0, 80, 200), (0, 120, 60))
plain = lambda s: re.sub(r"\x1b\[[0-9;]*m", "", s)


def teams_game(league="nfl", away="BUF", home="NYJ", **kw):
    return Game(league, "1", "live", Team(away, away, 0, id="1"), Team(home, home, 0, id="2"), **kw)


def spot(team, to_go, down=1, distance=10):
    return {"team": {"$ref": "http://x/teams/%s" % team}, "yardsToEndzone": to_go, "down": down, "distance": distance}


class StylesTest(unittest.TestCase):
    def test_picks_are_the_defaults_and_v_cycles(self):
        self.assertEqual([live.style_of(k, None) for k in ("nfl", "nba", "nhl")], ["latest"] * 3)
        self.assertEqual(live.style_of("nba", "shots"), "shots")
        self.assertEqual(live.style_of("nba", "rink"), "latest")   # not an NBA style
        self.assertEqual(live.next_style("nfl", "bar"), "field")
        self.assertEqual(live.next_style("nfl", "field"), "latest")


class NflTest(unittest.TestCase):
    def test_field_position_for_either_offense(self):
        g = teams_game()
        self.assertEqual(espn.field_position(spot("1", 47, 3, 2), g), ("away", 53, 55))   # BUF at the NYJ 47
        self.assertEqual(espn.field_position(spot("2", 20), g), ("home", 20, 10))          # NYJ at the BUF 20
        self.assertEqual(espn.field_position(spot("1", 4, 1, 4), g), ("away", 96, 100))    # goal to go
        self.assertEqual(espn.field_position(spot("1", 35, 0, 0), g), ("away", 65, None))  # a kickoff: no line
        self.assertIsNone(espn.field_position(spot("9", 30), g))

    def test_live_play_sets_the_ball(self):
        play = fixture("nfl_play_running.json")    # SF ball, as in test_leagues
        g0 = espn.NFL().parse_event(fixture("nfl_scoreboard.json")["events"][0])
        g0.away.id, g0.home.id = "25", "28"
        with FakeApi(live_status(3, play["clock"]["value"]), dict(LAST_PLAY), play):
            g, _, _ = espn.NFL().open(g0).poll()
        end = play["end"]
        self.assertEqual(g.ball, ("away", 100 - end["yardsToEndzone"], 100 - end["yardsToEndzone"] + end["distance"]))

    def test_yard_bar_and_full_field(self):
        g = teams_game(ball=("away", 50, 60))
        for style in ("bar", "field"):
            lines = live.view("nfl", style, g, [], 61, COLORS, TINTS)
            self.assertEqual({visible_len(l) for l in lines}, {61})
            text = "\n".join(plain(l) for l in lines)
            self.assertEqual((text.count("●"), text.count("▶"), text.count("◀")), (1, 1, 0))
            self.assertIn("┃", text)
            self.assertIn("BUF ball", plain(lines[-1]))
        row = plain(live.view("nfl", "bar", g, [], 61, COLORS, TINTS)[1])
        self.assertEqual(row.index("●"), 30)                  # midfield
        self.assertGreater(row.index("┃"), row.index("●"))   # BUF goes right
        g.ball = ("home", 50, 40)
        row = plain(live.view("nfl", "bar", g, [], 61, COLORS, TINTS)[1])
        self.assertLess(row.index("┃"), row.index("●"))
        self.assertIn("◀", row)

    def test_too_narrow_shows_nothing(self):
        self.assertEqual(live.view("nfl", "bar", teams_game(), [], live.MIN_WIDTH - 1, COLORS, TINTS), [])


class NbaTest(unittest.TestCase):
    def test_lead_tracker(self):
        nba = espn.NBA()
        g = teams_game("nba", "PHI", "NY", period=4, clock_secs=0.0)
        g.state = "final"
        plays = [Event(1, 600, 10, 0, None), Event(2, 1500, 10, 20, None), Event(3, 2800, 30, 24, None)]
        lines = live.view("nba", "lead", g, plays, 60, COLORS, TINTS, nba)
        self.assertEqual({visible_len(l) for l in lines}, {60})
        rows = [plain(l) for l in lines]
        self.assertIn("Q1", rows[2])
        self.assertIn("Q4", rows[2])
        self.assertTrue(set(rows[1][5:]) & set("▄█"))   # PHI led: bars above the line
        self.assertTrue(set(rows[3][5:]) & set("▀█"))   # NY led: below
        self.assertTrue(rows[-1].startswith("PHI +6"))
        self.assertIn("2 lead changes", rows[-1])

    def test_shot_chart_shows_the_last_shots(self):
        g = teams_game("nba", "PHI", "NY")
        plays = [Event(i, i, 0, 0, (i * 3, 20, "away", i % 2 == 0)) for i in range(15)]
        lines = [plain(l) for l in live.view("nba", "shots", g, plays, 50, COLORS, TINTS, espn.NBA())]
        court = "\n".join(lines[:-1])
        self.assertEqual(court.count("●") + court.count("○"), live.SHOTS_SHOWN)   # the 10 most recent
        self.assertNotIn("●", court.split("\n")[5][:8])   # the oldest (x 0-12 ft) are gone
        self.assertIn("shot chart · v", lines[-1])

    def test_poller_backfills_once_and_skips_free_throw_junk(self):
        shot = dict(fixture("nba_play.json"), sequenceNumber="5", shootingPlay=True, scoringPlay=True,
                    coordinate={"x": 30, "y": 20}, team={"$ref": "http://x/teams/%s" % "AWAY"})
        free = dict(shot, sequenceNumber="6", coordinate={"x": -214748340, "y": -214748365})
        play = copy.deepcopy(fixture("nba_play.json"))
        g0 = espn.NBA().parse_event(fixture("nba_scoreboard.json")["events"][0])
        shot["team"] = {"$ref": "http://x/teams/%s" % g0.away.id}
        free["team"] = shot["team"]
        with FakeApi(fixture("nba_status.json"), dict(LAST_PLAY), play) as api:
            urls, plain_fetch = [], api.fetch_json

            def fetch(url, timeout=10):
                urls.append(url)
                if url.endswith("/plays?limit=1000"):
                    return {"count": 2, "pageCount": 1, "items": [shot, free]}
                return plain_fetch(url)
            espn.fetch_json = fetch
            poller = espn.NBA().open(g0)
            poller.poll()
            self.assertFalse(any("limit=1000" in u for u in urls))   # off: no extra requests
            poller.want_live = True
            poller.poll()
            poller.poll()
        self.assertEqual(sum("limit=1000" in u for u in urls), 1)
        details = poller.play_details()
        self.assertEqual([d.seq for d in details], [5, 6, int(play["sequenceNumber"])])
        self.assertEqual(details[0].shot, (30, 20, "away", True))
        self.assertIsNone(details[1].shot)
        self.assertEqual([s for s, _ in poller.play_history()[0]][-2:], [6, 5])   # the play list has them too

    def test_delayed_view_cuts_the_plays(self):
        g = teams_game("nba", play_seq=11)
        details = [Event(10, 1, 0, 0, None), Event(11, 2, 2, 0, None), Event(12, 3, 2, 3, None)]
        self.assertEqual([d.seq for d in screens.visible_details(g, details)], [10, 11])
        self.assertEqual(screens.visible_details(teams_game("nba"), details), [])


class NhlTest(unittest.TestCase):
    def pbp(self):
        def event(kind, x, y, team, period, side="right", **details):
            return {"typeDescKey": kind, "periodDescriptor": {"number": period}, "homeTeamDefendingSide": side,
                    "timeInPeriod": "05:00", "details": dict(xCoord=x, yCoord=y, eventOwnerTeamId=team, **details)}
        return {"awayTeam": {"id": 10}, "homeTeam": {"id": 20},
                "rosterSpots": [{"playerId": 7, "lastName": {"default": "Horvat"}},
                                {"playerId": 8, "lastName": {"default": "Sorokin"}}],
                "plays": [event("shot-on-goal", 80, 5, 10, 1, goalieInNetId=8),     # an old period
                          event("shot-on-goal", 70, 10, 10, 2),                     # away shoots right
                          event("goal", 75, -10, 20, 2, side="left", scoringPlayerId=7),   # flipped
                          event("hit", -40, 20, 10, 2, hittingPlayerId=99),
                          {"typeDescKey": "stoppage", "periodDescriptor": {"number": 2}}]}

    def events(self):
        return nhl.rink_events(self.pbp())[0]

    def test_rink_events_home_end_on_the_right(self):
        events, cut = nhl.rink_events(self.pbp())
        self.assertEqual([(e.kind, e.side, e.xy, e.period) for e in events],
                         [("shot-on-goal", "away", (80, 5), 1), ("shot-on-goal", "away", (70, 10), 2),
                          ("goal", "home", (-75, 10), 2), ("hit", "away", (-40, 20), 2)])   # no stoppage: no place
        self.assertEqual(cut, events[-1].seq)
        self.assertEqual((events[0].text, events[2].text, events[0].when), ("? · saved by Sorokin", "Horvat · unassisted", "1st 05:00"))

    def test_poller_keeps_play_by_play_to_the_landings_time(self):
        """The play-by-play copy can be newer than the landing that dates the
        snapshot: its later events would show ahead of the delayed view."""
        data = self.pbp()
        for p, left in zip(data["plays"], ("05:00", "12:00", "09:30", "09:00", "09:00")):
            p["timeRemaining"] = left   # the goal (2nd, 9:30 left) and the hit came after the landing's 10:00
        landing = dict(fixture("nhl_landing.json"), gameState="LIVE",
                       periodDescriptor={"number": 2, "periodType": "REG"},
                       clock={"secondsRemaining": 600, "running": True, "inIntermission": False})
        saved = nhl.fetch_json, nhl.fetch_json_timed
        nhl.fetch_json = lambda url, timeout=10: data
        nhl.fetch_json_timed = lambda url, timeout=10: (landing, 18.0, 19)
        try:
            poller = nhl.Poller(1)
            poller.want_live = True
            g, _, _ = poller.poll()
            shown = screens.visible_details(g, poller.play_details())
            self.assertEqual([e.kind for e in shown], ["shot-on-goal", "shot-on-goal"])   # not the goal, nor after it
            landing["clock"] = {"secondsRemaining": 1000, "running": False, "inIntermission": True}
            g, _, _ = poller.poll()   # the period is over: all of it shows
            self.assertEqual(len(screens.visible_details(g, poller.play_details())), 4)
        finally:
            nhl.fetch_json, nhl.fetch_json_timed = saved

    def test_poller_fetches_play_by_play_only_when_on(self):
        urls = []
        saved = nhl.fetch_json, nhl.fetch_json_timed
        nhl.fetch_json = lambda url, timeout=10: urls.append(url) or self.pbp()
        nhl.fetch_json_timed = lambda url, timeout=10: (dict(fixture("nhl_landing.json"), gameState="LIVE"), 0.0, 19)
        try:
            poller = nhl.Poller(1)
            g, _, _ = poller.poll()
            self.assertEqual((urls, poller.play_details(), g.play_seq), ([], [], None))
            poller.want_live = True
            g, _, _ = poller.poll()
            self.assertEqual(len(urls), 1)
            self.assertTrue(urls[0].endswith("/gamecenter/1/play-by-play"))
        finally:
            nhl.fetch_json, nhl.fetch_json_timed = saved

    def test_goals_are_not_hidden_by_later_shots(self):
        goal = Event(1, None, None, None, None, period=1, when="1st 05:00", side="away", kind="goal", text="x", xy=(80, 0))
        events = [goal, goal._replace(seq=2, kind="shot-on-goal")]
        rink = "\n".join(plain(l) for l in live.view("nhl", "rink", teams_game("nhl"), events, 60, COLORS, TINTS)[:-2])
        self.assertIn("●", rink)
        self.assertNotIn("◦", rink)   # the shot after it, in the same cell, doesn't cover it

    def test_shot_map_and_zone_strip(self):
        g = teams_game("nhl", "ANA", "NYI")
        events = self.events()[:3]   # ANA's 2nd-period shot, then NYI's goal
        for style in ("rink", "zones"):
            lines = live.view("nhl", style, g, events, 60, COLORS, TINTS)
            self.assertEqual({visible_len(l) for l in lines}, {60})
            text = "\n".join(plain(l) for l in lines)
            self.assertIn("shots in 2nd: ANA 1 · NYI 1", text)     # the shown event's period only
            self.assertIn("GOAL · NYI Horvat", text)
        rink = [plain(l) for l in live.view("nhl", "rink", g, events, 60, COLORS, TINTS)]
        middle = next(r for r in rink if "◦" in r)
        self.assertGreater(middle.index("◦"), 30)        # ANA's shot at the NYI (right) end
        self.assertLess(middle.index("●"), 30)
        self.assertIn("ANA end", plain(live.view("nhl", "zones", g, events, 60, COLORS, TINTS)[2]))

    def test_latest_event_is_drawn_where_it_happened(self):
        g = teams_game("nhl", "ANA", "NYI")
        events = self.events()
        lines = [plain(l) for l in live.view("nhl", "latest", g, events, 60, COLORS, TINTS)]
        self.assertEqual({len(l) for l in lines}, {60})
        rink, words, recent = "\n".join(lines[:-2]), lines[-2], lines[-1]
        self.assertIn("◉", rink)
        self.assertIn("HIT", rink)
        self.assertTrue(words.startswith("HIT · ANA ? on ?"))
        self.assertTrue(words.rstrip().endswith("2nd 05:00"))
        self.assertIn("1 NYI goal · 2 ANA shot", recent)        # the two before it, numbered
        self.assertIn("1", rink)
        self.assertIn("2", rink)
        goal = [plain(l) for l in live.view("nhl", "latest", g, events[:3], 60, COLORS, TINTS)]
        row = next(r for r in goal[:-2] if "●" in r)
        self.assertIn("◀── GOAL!", row)   # NYI attack the left net: the arrow points at it (no room left of it)
        save = [plain(l) for l in live.view("nhl", "latest", g, events[:2], 72, COLORS, TINTS)]
        self.assertIn("◉ ──▶ SAVE", next(r for r in save[:-2] if "◉" in r))   # ANA at the right net

    def test_going_back_shows_that_events_period_as_it_stood(self):
        g = teams_game("nhl", "ANA", "NYI")
        events = self.events()
        lines = [plain(l) for l in live.view("nhl", "rink", g, events, 72, COLORS, TINTS, at=0)]
        # The note takes the legend's place; the counts by it still say they're shots, not goals.
        self.assertIn("◀ 3 back · → to live  shots in 1st: ANA 1 · NYI 0", lines[-2])
        self.assertNotIn("●", "\n".join(lines[:-2]))     # the 2nd-period goal hasn't happened yet
        for style in ("rink", "zones"):   # narrower, the note is cut short to keep them
            lines = [plain(l) for l in live.view("nhl", style, g, events, 56, COLORS, TINTS, at=0)]
            self.assertEqual({len(l) for l in lines}, {56})
            self.assertIn("◀ 3 back  shots in 1st: ANA 1 · NYI 0", "\n".join(lines[-2:]))


class StaticFeed:
    """game_view's feed with one snapshot and an ESPN-like play list, no thread."""
    has_plays = True

    def __init__(self, g, live_on):
        self.want_live = live_on
        plays = [(n, "Q3  %d:00  play %d" % (n % 12, n)) for n in range(40, 0, -1)]
        self.waiting = [("game", g, time.time() - 1), ("plays", plays, False)]

    def updates(self):
        out, self.waiting = self.waiting, []
        return out

    def plays_pending(self):
        return False

    def show_plays(self, on):
        pass

    def show_live(self, on):
        pass

    def load_older(self):
        pass


class SizedTerm(FakeTerm):
    """A FakeTerm of any size that types the keys given, then q."""

    def __init__(self, cols, rows, keys):
        super().__init__()
        self.cols, self.rows, self.script = cols, rows, list(keys)

    def size(self):
        return self.cols, self.rows

    def read_key(self, timeout):
        return self.script.pop(0) if self.script else "q"


def score_readable(frame, team):
    """The score's block digits whole, or (slim) in plain digits by the name."""
    rows = [plain(r) for r in frame.split("\n")]
    if any(team.name.upper() in r and r.rstrip().endswith(str(team.score)) for r in rows):
        return True
    for scale in (1, 2):
        digits = screens.big_text(str(team.score), scale)
        if any(all(d in rows[i + k] for k, d in enumerate(digits)) for i in range(len(rows) - len(digits) + 1)):
            return True
    return False


class LayoutTest(unittest.TestCase):
    """The vertical game view at every height: never an exception, the title
    and footer kept, and at normal sizes both scores readable."""

    def setUp(self):
        self.saved = dict(settings)
        settings.update(delay=0, layout="vertical", show_last_play=True, live_styles={})

    def tearDown(self):
        settings.clear()
        settings.update(self.saved)

    def frames(self, cols, rows, league, plays_open, live_on):
        g = teams_game(league, "AAA", "BBB", period=3, period_label="3rd QUARTER", short_period="Q3",
                       clock_secs=300.0, clock_running=True, note="3rd & 7" if league == "nfl" else "",
                       last_play="Last play: a long pass to the left", play_seq=40, venue="Arena")
        g.away.score, g.home.score = 108, 112
        settings["play_by_play"] = live_on
        term = SizedTerm(cols, rows, [None, "p", None] if plays_open else [None, None])
        screens.game_view(term, FakeLeague(None, league), StaticFeed(g, live_on), FakeLogos(), COLORS, FakeMenu())
        return g, [d for _, d in term.draws]

    def test_play_list_pages_back_to_the_delayed_views_play(self):
        """With a long delay every play loaded first is after the view's:
        the list pages back by itself instead of saying "No plays yet"."""
        g = teams_game("nba", "AAA", "BBB", period=2, short_period="Q2", clock_secs=300.0, play_seq=100)

        class Paged(StaticFeed):
            asked, oldest = 0, 251

            def __init__(self):
                super().__init__(g, False)
                self.waiting[1] = ("plays", self.history(), True)

            def history(self):
                return [(n, "Q2  5:00  play %d" % n) for n in range(300, self.oldest - 1, -1)]

            def load_older(self):
                self.asked += 1
                self.oldest -= 25
                self.waiting.append(("plays", self.history(), self.oldest > 1))
        feed, term = Paged(), SizedTerm(100, 40, [None, "p"] + [None] * 12)
        settings["play_by_play"] = False
        screens.game_view(term, FakeLeague(None, "nba"), feed, FakeLogos(), COLORS, FakeMenu())
        self.assertIn("play 100", plain(term.draws[-1][1]))
        self.assertEqual(feed.asked, 7)   # 251 -> 76: the pages it took, and no more

    def test_every_height(self):
        for league in ("nhl", "nfl"):
            for cols in (20, 80, 100):
                for rows in range(1, 46):
                    for plays_open in (False, True):
                        for live_on in (False, True):
                            case = (league, cols, rows, plays_open, live_on)
                            g, frames = self.frames(cols, rows, league, plays_open, live_on)
                            last = frames[-1].split("\n")
                            self.assertEqual(len(last), rows, case)
                            if rows >= 3:
                                self.assertIn("AAA @ BBB", plain(last[0]), case)
                                self.assertIn("quit", plain(last[-1]), case)
                            if rows >= 24 and cols >= 80:
                                for team in (g.away, g.home):
                                    self.assertTrue(score_readable(frames[-1], team), case)


class ScreenTest(unittest.TestCase):
    def setUp(self):
        self.saved = dict(settings), screens.save_settings
        self.saves = []
        screens.save_settings = lambda: self.saves.append(dict(settings["live_styles"]))
        settings.update(delay=0, layout="vertical", play_by_play=True, live_styles={})

    def tearDown(self):
        settings.clear()
        settings.update(self.saved[0])
        screens.save_settings = self.saved[1]

    def test_on_in_options_v_switches_the_style_and_arrows_go_back(self):
        events = NhlTest().events()

        class Poller:
            want_live, polls = False, []

            def poll(self):
                self.polls.append(self.want_live)
                return teams_game("nhl", "ANA", "NYI", play_seq=events[-1].seq), time.time(), 3600

            def play_details(self):
                return list(events)
        poller = Poller()

        class Nhl(FakeLeague):
            key = "nhl"
        league = Nhl(poller)
        league.key = "nhl"
        term = FakeTerm()
        t = threading.Thread(target=lambda: screens.game_screen(term, league, None, FakeLogos(), COLORS, FakeMenu()),
                             daemon=True)
        t.start()
        try:
            self.assertTrue(wait_for(lambda: term.shown("latest event · v")))
            self.assertEqual(poller.polls[0], True)   # the first poll already fetches the view's data
            self.assertTrue(term.shown("v view  ←/→ rewind"))
            term.keys.put("left")
            self.assertTrue(wait_for(lambda: "◀ 1 back · → to live" in term.draws[-1][1]))
            self.assertIn("GOAL · NYI Horvat", plain(term.draws[-1][1]))    # the event before the hit
            term.keys.put("right")
            self.assertTrue(wait_for(lambda: "HIT · ANA" in plain(term.draws[-1][1])))
            self.assertNotIn("back · → to live", term.draws[-1][1])
            term.keys.put("home")    # the first event, then [ ] from score to score
            self.assertTrue(wait_for(lambda: "◀ 3 back" in term.draws[-1][1]))
            term.keys.put("]")
            self.assertTrue(wait_for(lambda: "GOAL · NYI" in plain(term.draws[-1][1])))
            term.keys.put("end")
            self.assertTrue(wait_for(lambda: "HIT · ANA" in plain(term.draws[-1][1])))
            term.keys.put("v")
            self.assertTrue(wait_for(lambda: term.shown("shot map · v")))
            self.assertEqual(self.saves, [{"nhl": "rink"}])
            settings["play_by_play"] = False   # as if turned off in Options
            n = len(term.draws)
            self.assertTrue(wait_for(lambda: len(term.draws) > n + 3))
            self.assertNotIn("shot map", term.draws[-1][1])
            self.assertNotIn("v view", term.draws[-1][1])
            self.assertTrue(wait_for(lambda: not poller.want_live))
        finally:
            term.keys.put("q")
            t.join(1)

if __name__ == "__main__":
    unittest.main()
