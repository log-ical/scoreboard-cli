import copy
import unittest

from tests.helpers import ROOT, fixture  # noqa: F401
from scoreboard.leagues import espn, nhl
from scoreboard.model import parse_utc


class NhlTest(unittest.TestCase):
    def test_list_and_landing(self):
        games = [nhl.parse_game(g) for g in fixture("nhl_score.json")["games"]]
        self.assertTrue(games)
        g = nhl.parse_game(fixture("nhl_landing.json"), 1000.0, detail=True)
        self.assertTrue(g.away.name and g.home.name)
        self.assertIn("/teamlogos/nhl/500/", g.away.logo_url)
        self.assertTrue(g.away.stats[0].startswith("SOG "))
        self.assertTrue(g.last_play.startswith("Last goal: "))

    def test_espn_logo_names(self):
        self.assertIn("/sj.png", nhl._team({"abbrev": "SJS"}).logo_url)
        self.assertIn("/bos.png", nhl._team({"abbrev": "BOS"}).logo_url)


class FakeApi:
    """Routes ESPN core URLs to canned documents."""

    def __init__(self, status, situation, play):
        self.docs = {"/status": status, "/situation": situation}
        self.play = play
        self.saved = espn.fetch_json, espn.fetch_json_timed

    def fetch_json(self, url, timeout=10):
        for suffix, doc in self.docs.items():
            if url.endswith(suffix):
                return doc
        return self.play

    def __enter__(self):
        espn.fetch_json = self.fetch_json
        espn.fetch_json_timed = lambda url, timeout=10: (self.fetch_json(url), 0.0, 6)
        return self

    def __exit__(self, *exc):
        espn.fetch_json, espn.fetch_json_timed = self.saved


LAST_PLAY = {"lastPlay": {"$ref": "http://core/plays/1"}}


def live_status(period, secs):
    return {"clock": secs, "period": period, "type": {"name": "STATUS_IN_PROGRESS", "state": "in"}}


class EspnTest(unittest.TestCase):
    def setUp(self):
        self.nba, self.nfl = espn.NBA(), espn.NFL()

    def test_scoreboard_events(self):
        for league, name in ((self.nba, "nba_scoreboard.json"), (self.nfl, "nfl_scoreboard.json")):
            for e in fixture(name)["events"]:
                g = league.parse_event(e)
                self.assertIn(g.state, ("pre", "live", "final"))
                self.assertRegex(g.away.logo_url, r"img=/i/teamlogos/%s/500/[a-z]+\.png" % league.key)
                self.assertNotIn("/scoreboard/", g.home.logo_url)

    def nba_game(self):
        return self.nba.parse_event(fixture("nba_scoreboard.json")["events"][0])

    def test_nba_foul_stops_clock(self):
        play = fixture("nba_play.json")       # a Loose Ball Foul
        with FakeApi(fixture("nba_status.json"), fixture("nba_situation.json"), play):
            g, _, wait = self.nba.open(self.nba_game()).poll()
        self.assertEqual(g.state, "live")
        self.assertFalse(g.clock_running)
        self.assertEqual((g.away.score, g.home.score), (play["awayScore"], play["homeScore"]))
        self.assertTrue(any(s.startswith("TIMEOUTS") for s in g.home.stats))
        self.assertEqual(wait, 3)

    def test_nba_running_play_anchors_at_wallclock(self):
        play = copy.deepcopy(fixture("nba_play.json"))
        play["type"]["text"], play["text"] = "Jump Shot", "X misses 18-foot jumper"
        play["clock"]["value"] = 396.0
        with FakeApi(live_status(4, 396.0), dict(LAST_PLAY), play) as api:
            poller = self.nba.open(self.nba_game())
            g, _, _ = poller.poll()
            self.assertTrue(g.clock_running)
            self.assertEqual(g.clock_anchor, parse_utc(play["wallclock"]))
            self.assertEqual(g.clock_cap, self.nba.clock_cap)
            # Clock moves with no new play: still running, anchored at the new value.
            api.docs["/status"] = live_status(4, 390.0)
            g2, _, _ = poller.poll()
            self.assertTrue(g2.clock_running)
            self.assertEqual(g2.clock_secs, 390.0)

    def test_nba_late_made_basket_stops_clock(self):
        play = {"type": {"text": "Jump Shot"}, "text": "X makes jumper", "scoringPlay": True}
        self.assertTrue(self.nba.stops_clock(play, 90, 4))
        self.assertFalse(self.nba.stops_clock(play, 500, 4))
        steal = {"type": {"text": "Bad Pass Turnover"}, "text": "Y steals"}
        self.assertFalse(self.nba.stops_clock(steal, 500, 2))

    def test_nfl_rush_runs_clock_and_shows_situation(self):
        play = fixture("nfl_play_running.json")    # a Rush, SF ball
        event = fixture("nfl_scoreboard.json")["events"][0]
        g0 = self.nfl.parse_event(event)
        g0.away.id, g0.home.id = "25", "28"         # SF away, WSH home
        situation = dict(LAST_PLAY, homeTimeouts=2, awayTimeouts=3, isRedZone=True)
        with FakeApi(live_status(3, play["clock"]["value"]), situation, play):
            g, _, wait = self.nfl.open(g0).poll()
        self.assertTrue(g.clock_running)
        self.assertEqual(g.note, play["end"]["downDistanceText"])
        self.assertIn("● BALL", g.away.tags)
        self.assertIn("RED ZONE", g.away.tags)
        self.assertEqual(g.home.tags, [])
        self.assertEqual(g.home.stats, ["TIMEOUTS 2"])
        self.assertEqual(wait, 5)

    def test_nfl_clock_rules(self):
        inc = fixture("nfl_play_incomplete.json")
        self.assertTrue(self.nfl.stops_clock(inc, 600, 2))
        ob = {"type": {"text": "Rush"}, "text": "J.Doe left end pushed ob at KC 30 for 5 yards"}
        self.assertFalse(self.nfl.stops_clock(ob, 600, 4))
        self.assertTrue(self.nfl.stops_clock(ob, 250, 4))
        self.assertTrue(self.nfl.stops_clock(ob, 100, 2))
        self.assertEqual(self.nfl.score_alert(6), "TOUCHDOWN!")
        self.assertIsNone(self.nba.score_alert(2))

    def test_final_game(self):
        play = fixture("nfl_play_final.json")
        g0 = self.nfl.parse_event(fixture("nfl_scoreboard.json")["events"][0])
        with FakeApi(fixture("nfl_status.json"), fixture("nfl_situation.json"), play):
            g, _, _ = self.nfl.open(g0).poll()
        self.assertEqual(g.state, "final")
        self.assertEqual((g.away.score, g.home.score), (17, 23))
        self.assertEqual(g.note, "")


if __name__ == "__main__":
    unittest.main()
