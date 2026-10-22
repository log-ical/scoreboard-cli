import copy
import time
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
        texts = [t for _, t in g.plays]
        self.assertTrue(any(" GOAL " in t for t in texts) and any(" PENALTY " in t for t in texts))
        self.assertTrue(texts[0].startswith("OT ") and texts[-1].startswith("1st "))

    def test_records_from_standings(self):
        saved = nhl.fetch_json, dict(nhl.records), nhl.records_fetched
        try:
            nhl.fetch_json = lambda url, timeout=10: {"standings": [
                {"teamAbbrev": {"default": "SJS"}, "wins": 8, "losses": 2, "otLosses": 1}]}
            nhl.records_fetched = 0.0
            nhl.refresh_records().join(5)
            self.assertIsNone(nhl.refresh_records())   # not due again for a while
            self.assertEqual(nhl._team({"abbrev": "SJS"}).record, "8-2-1")
            self.assertEqual(nhl._team({"abbrev": "BOS"}).record, "")
        finally:
            nhl.fetch_json = saved[0]
            nhl.records.clear()
            nhl.records.update(saved[1])
            nhl.records_fetched = saved[2]

    def test_espn_logo_names(self):
        self.assertIn("/sj.png", nhl._team({"abbrev": "SJS"}).logo_url)
        self.assertIn("/bos.png", nhl._team({"abbrev": "BOS"}).logo_url)

    def test_odd_types_are_coerced_or_fail_in_the_parser(self):
        """Wrong JSON types must not reach the drawing (which runs on the
        screen's thread, where an error ends the app)."""
        self.assertEqual(nhl._team({"abbrev": "BOS", "score": "3"}).score, 3)
        data = fixture("nhl_landing.json")
        data["clock"]["secondsRemaining"] = "600"
        self.assertEqual(nhl.parse_game(data).clock_secs, 600.0)
        data["clock"]["secondsRemaining"] = "ten"
        with self.assertRaises(ValueError):
            nhl.parse_game(data)
        pbp = {"homeTeam": {"id": 1}, "plays": [{"typeDescKey": "goal", "details": {
            "xCoord": "50", "yCoord": 0, "eventOwnerTeamId": 1}}]}
        self.assertEqual(nhl.rink_events(pbp), ([], None))


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
                self.assertRegex(g.away.record, r"^\d+-\d+")

    def test_odd_types_become_text(self):
        e = copy.deepcopy(fixture("nba_scoreboard.json")["events"][0])
        team = e["competitions"][0]["competitors"][0]["team"]
        team["abbreviation"], team["displayName"] = None, 7
        e["competitions"][0]["venue"] = {"fullName": None}
        g = self.nba.parse_event(e)
        side = g.home if e["competitions"][0]["competitors"][0]["homeAway"] == "home" else g.away
        self.assertEqual((side.abbrev, side.name, g.venue), ("?", "7", ""))
        play = dict(fixture("nfl_play_running.json"), text=12)
        g = self.nfl.parse_event(fixture("nfl_scoreboard.json")["events"][0])
        self.nfl.decorate(g, {}, play)
        self.assertEqual(g.last_play, "Last play: 12")

    def test_snapshot_is_never_dated_before_its_play(self):
        """ESPN sends no Age, and its max-age counts down: a snapshot dated by
        a guess at the status copy's age could hold a play from after that
        date, and a delayed view would show it early."""
        play = dict(fixture("nba_play.json"), wallclock=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        with FakeApi(live_status(2, 300.0), LAST_PLAY, play) as api:
            espn.fetch_json_timed = lambda url, timeout=10: (api.fetch_json(url), None, 12)
            before = time.time()
            _, data_t, _ = self.nba.open(self.nba_game()).poll()
        self.assertGreaterEqual(data_t, before)

    def test_logo_path_cannot_add_query_parameters(self):
        url = espn._logo("https://a.espncdn.com/i/teamlogos/nba/500/bos.png&h=4000&w=4000")
        self.assertTrue(url.endswith("bos.png%26h%3D4000%26w%3D4000&h=200&w=200"), url)

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

    def test_status_copy_older_than_the_play_does_not_start_the_clock(self):
        """The status copy that comes with a stoppage play can be older than
        the play; the next, fresh one mustn't look like the clock moved."""
        play = copy.deepcopy(fixture("nba_play.json"))
        play["type"]["text"], play["clock"]["value"], play["period"]["number"] = "Full Timeout", 300.0, 3
        with FakeApi(live_status(3, 303.0), dict(LAST_PLAY), play) as api:
            poller = self.nba.open(self.nba_game())
            running = []
            for secs in (303.0, 300.0, 300.0, 295.0):   # stale copy, fresh ones, then play resumes
                api.docs["/status"] = live_status(3, secs)
                running.append(poller.poll()[0].clock_running)
        self.assertEqual(running, [False, False, False, True])

    def test_nfl_incompletion_with_a_stale_status_stays_stopped(self):
        """NFL plays carry the clock at the snap: an incompletion snapped at
        14:33 stops it at 14:29. A status copy from before the snap doesn't
        count; the stopped clock after it is the mark to move from."""
        play = copy.deepcopy(fixture("nfl_play_incomplete.json"))
        play["clock"]["value"], play["period"]["number"] = 873.0, 1
        g = self.nfl.parse_event(fixture("nfl_scoreboard.json")["events"][0])
        with FakeApi(live_status(1, 880.0), dict(LAST_PLAY), play) as api:
            poller = self.nfl.open(g)
            running = []
            for secs in (880.0, 869.0, 869.0, 864.0, 859.0):
                api.docs["/status"] = live_status(1, secs)
                running.append(poller.poll()[0].clock_running)
        self.assertEqual(running, [False, False, False, True, True])

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

    def test_play_list_fetched_only_while_open(self):
        def page(seqs, count):
            return {"count": count, "pageCount": -(-count // espn.PLAY_HISTORY),
                    "items": [{"sequenceNumber": str(n), "text": "play %d" % n,
                               "period": {"number": 2}, "clock": {"displayValue": "5:00"}} for n in seqs]}
        play = copy.deepcopy(fixture("nba_play.json"))
        with FakeApi(fixture("nba_status.json"), dict(LAST_PLAY), play) as api:
            urls = []
            plain = api.fetch_json

            def fetch(url, timeout=10):
                urls.append(url)
                if "/plays?" in url:
                    n = int(url.rsplit("=", 1)[1])
                    return page(range((n - 1) * 25 + 1, min(n * 25, 60) + 1), 60)
                return plain(url)
            espn.fetch_json = fetch
            poller = self.nba.open(self.nba_game())
            poller.poll()
            self.assertFalse(any("/plays?" in u for u in urls))
            self.assertEqual(poller.play_history(), ([(702, poller.play_entry(play)[1])], False))
            poller.want_plays = True
            poller.poll()
            # Only the newest two of the three pages, after a 1-play count probe.
            self.assertNotIn("page=1", " ".join(urls))
            plays, older = poller.play_history()
            seqs = [s for s, _ in plays]
            self.assertEqual(seqs[0], 702)
            self.assertEqual(seqs[1:], list(range(60, 25, -1)))
            self.assertTrue(older)
            self.assertEqual(dict(plays)[60], "Q2   5:00  play 60")
            poller.load_older()
            plays, older = poller.play_history()
            self.assertEqual(plays[-1][0], 1)
            self.assertFalse(older)
            # A refresh (new last play, list due) only needs the newest page.
            api.docs["/situation"] = {"lastPlay": {"$ref": "http://core/plays/2"}}
            poller.plays_fetched = 0.0
            del urls[:]
            poller.poll()
            self.assertEqual([u.rsplit("?", 1)[1] for u in urls if "/plays?" in u], ["limit=25&page=3"])
            self.assertEqual(len(poller.play_history()[0]), 61)

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
