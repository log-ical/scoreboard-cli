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
from scoreboard.leagues.espn import Detail
from scoreboard.model import Game, Team
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
        self.assertEqual([live.style_of(k, None) for k in ("nfl", "nba", "nhl")], ["bar", "lead", "rink"])
        self.assertEqual(live.style_of("nba", "shots"), "shots")
        self.assertEqual(live.style_of("nba", "rink"), "lead")   # not an NBA style
        self.assertEqual(live.next_style("nfl", "bar"), "field")
        self.assertEqual(live.next_style("nfl", "field"), "bar")


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
        plays = [Detail(1, 600, 10, 0, None), Detail(2, 1500, 10, 20, None), Detail(3, 2800, 30, 24, None)]
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
        plays = [Detail(i, i, 0, 0, (i * 3, 20, "away", i % 2 == 0)) for i in range(15)]
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
        details = [Detail(10, 1, 0, 0, None), Detail(11, 2, 2, 0, None), Detail(12, 3, 2, 3, None)]
        self.assertEqual([d.seq for d in screens.visible_details(g, details)], [10, 11])
        self.assertEqual(screens.visible_details(teams_game("nba"), details), [])


class NhlTest(unittest.TestCase):
    def pbp(self):
        def event(kind, x, y, team, period, side="right", **details):
            return {"typeDescKey": kind, "periodDescriptor": {"number": period}, "homeTeamDefendingSide": side,
                    "details": dict(xCoord=x, yCoord=y, eventOwnerTeamId=team, **details)}
        return {"awayTeam": {"id": 10}, "homeTeam": {"id": 20},
                "rosterSpots": [{"playerId": 7, "lastName": {"default": "Horvat"}}],
                "plays": [event("shot-on-goal", 80, 5, 10, 1),                   # an old period
                          event("shot-on-goal", 70, 10, 10, 2),                  # away shoots right
                          event("goal", 75, -10, 20, 2, side="left", scoringPlayerId=7),   # flipped
                          event("hit", -40, 20, 10, 2, hittingPlayerId=99),
                          {"typeDescKey": "stoppage", "periodDescriptor": {"number": 2}}]}

    def test_rink_events_this_period_home_end_on_the_right(self):
        g = teams_game("nhl", "ANA", "NYI")
        shots, last = nhl.rink_events(self.pbp(), g)
        self.assertEqual(shots, [(70, 10, "away", False), (-75, 10, "home", True)])
        self.assertEqual(last, ("ANA hit", -40, "away"))
        data = self.pbp()
        data["plays"] = data["plays"][:3]
        self.assertEqual(nhl.rink_events(data, g)[1], ("NYI GOAL · Horvat", -75, "home"))

    def test_poller_fetches_play_by_play_only_when_on(self):
        urls = []
        saved = nhl.fetch_json, nhl.fetch_json_timed
        nhl.fetch_json = lambda url, timeout=10: urls.append(url) or self.pbp()
        nhl.fetch_json_timed = lambda url, timeout=10: (dict(fixture("nhl_landing.json"), gameState="LIVE"), 0.0, 19)
        try:
            poller = nhl.Poller(1)
            g, _, _ = poller.poll()
            self.assertEqual((urls, g.rink), ([], []))
            poller.want_live = True
            g, _, _ = poller.poll()
            self.assertEqual(len(urls), 1)
            self.assertTrue(urls[0].endswith("/gamecenter/1/play-by-play"))
        finally:
            nhl.fetch_json, nhl.fetch_json_timed = saved

    def test_shot_map_and_zone_strip(self):
        g = teams_game("nhl", "ANA", "NYI", rink=[(80, 0, "away", False), (-80, 0, "home", True)],
                       rink_last=("NYI GOAL · Horvat", -80, "home"))
        for style in ("rink", "zones"):
            lines = live.view("nhl", style, g, [], 60, COLORS, TINTS)
            self.assertEqual({visible_len(l) for l in lines}, {60})
            text = "\n".join(plain(l) for l in lines)
            self.assertIn("ANA 1 · NYI 1", text)
            self.assertIn("last: NYI GOAL · Horvat", text)
        rink = [plain(l) for l in live.view("nhl", "rink", g, [], 60, COLORS, TINTS)]
        middle = next(r for r in rink if "◦" in r)
        self.assertGreater(middle.index("◦"), 30)        # ANA's shot at the NYI (right) end
        self.assertLess(middle.index("●"), 30)
        self.assertIn("ANA end", plain(live.view("nhl", "zones", g, [], 60, COLORS, TINTS)[2]))


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

    def test_on_in_options_and_v_switches_the_style(self):
        class Poller:
            want_live, polls = False, []

            def poll(self):
                self.polls.append(self.want_live)
                g = teams_game("nhl", "ANA", "NYI", rink=[(80, 0, "away", False)], rink_last=("ANA hit", 10, "away"))
                return g, time.time(), 3600
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
            self.assertTrue(wait_for(lambda: term.shown("shot map · v")))
            self.assertEqual(poller.polls[0], True)   # the first poll already fetches the view's data
            self.assertTrue(term.shown("v view"))
            term.keys.put("v")
            self.assertTrue(wait_for(lambda: term.shown("zone strip · v")))
            self.assertEqual(self.saves, [{"nhl": "zones"}])
            settings["play_by_play"] = False   # as if turned off in Options
            n = len(term.draws)
            self.assertTrue(wait_for(lambda: len(term.draws) > n + 3))
            self.assertNotIn("zone strip", term.draws[-1][1])
            self.assertNotIn("v view", term.draws[-1][1])
            self.assertTrue(wait_for(lambda: not poller.want_live))
        finally:
            term.keys.put("q")
            t.join(1)


if __name__ == "__main__":
    unittest.main()
