"""The game list: the NHL's week of games, day headings, and scrolling."""

import datetime
import re
import threading
import time
import unittest

from tests.helpers import ROOT  # noqa: F401  (sets sys.path)
from tests.test_concurrency import FakeLeague, FakeMenu, FakeTerm, wait_for
from scoreboard import screens
from scoreboard.leagues import espn, nhl
from scoreboard.model import Game, Team
from scoreboard.settings import settings


plain = lambda s: re.sub(r"\x1b\[[0-9;]*m", "", s)


def nhl_game(gid, state="FUT", start="2026-10-21T23:30:00Z"):
    return {"id": gid, "gameState": state, "startTimeUTC": start, "venue": {"default": "Arena"},
            "awayTeam": {"abbrev": "AAA", "commonName": {"default": "As"}},
            "homeTeam": {"abbrev": "BBB", "commonName": {"default": "Bs"}}}


class NhlWeekTest(unittest.TestCase):
    def setUp(self):
        self.saved = nhl.fetch_json, nhl.refresh_records
        nhl.refresh_records = lambda: None

    def tearDown(self):
        nhl.fetch_json, nhl.refresh_records = self.saved

    def test_list_adds_the_days_after_the_current_one(self):
        week = {"gamesByDate": [
            {"date": "2026-10-19", "games": [nhl_game(1, "OFF")]},    # before: not added
            {"date": "2026-10-20", "games": [nhl_game(2, "OFF")]},    # the current day: from score/now
            {"date": "2026-10-21", "games": [nhl_game(3), nhl_game(4)]},
            {"date": "2026-10-26", "games": [nhl_game(5)]},           # the 7th day
            {"date": "2026-10-27", "games": [nhl_game(6)]},           # past the week
        ]}
        today = {"currentDate": "2026-10-20", "games": [nhl_game(2, "LIVE")]}
        nhl.fetch_json = lambda url, timeout=10: week if url.endswith("/scoreboard/now") else today
        games = nhl.NHL().list_games()
        self.assertEqual([g.id for g in games], ["2", "3", "4", "5"])
        self.assertEqual([g.state for g in games], ["live", "pre", "pre", "pre"])

    def test_week_failure_still_shows_the_current_day(self):
        def fetch(url, timeout=10):
            if url.endswith("/scoreboard/now"):
                raise OSError("down")
            return {"currentDate": "2026-10-20", "games": [nhl_game(2, "OFF")]}
        nhl.fetch_json = fetch
        self.assertEqual([g.id for g in nhl.NHL().list_games()], ["2"])


def espn_event(eid, date="2026-10-21T23:30Z"):
    side = lambda home, abbrev: {"homeAway": home, "team": {"abbreviation": abbrev}}
    return {"id": eid, "date": date, "status": {"type": {"state": "pre"}},
            "competitions": [{"competitors": [side("away", "AAA"), side("home", "BBB")]}]}


class EspnWeekTest(unittest.TestCase):
    def setUp(self):
        self.saved = espn.fetch_json
        self.urls, self.down = [], set()

        def fetch(url, timeout=10):
            self.urls.append(url)
            day = url.partition("?dates=")[2]
            if day in self.down:
                raise OSError("down")
            if not day:
                return {"day": {"date": "2026-10-21"}, "events": [espn_event("1"), espn_event("2")]}
            n = int(day[-2:])
            # Each day's last game is listed again on the next day, as late games are.
            return {"events": [espn_event("%d0" % n), espn_event("%d1" % n)] + ([espn_event("2")] if n == 22 else [])}
        espn.fetch_json = fetch

    def tearDown(self):
        espn.fetch_json = self.saved

    def test_nba_list_has_the_week_without_repeats(self):
        games = espn.NBA().list_games()
        self.assertEqual([g.id for g in games], ["1", "2"] + ["%d%d" % (n, k) for n in range(22, 28) for k in (0, 1)])
        self.assertEqual(len(self.urls), 7)
        self.assertTrue(all(u.endswith("?dates=202610%d" % n) for u, n in zip(self.urls[1:], range(22, 28))))

    def test_later_days_are_cached_and_failed_days_retried(self):
        nba = espn.NBA()
        self.down = {"20261024"}
        self.assertEqual(len(nba.list_games()), 2 + 5 * 2)   # one day missing
        self.urls.clear()
        self.down = set()
        self.assertEqual(len(nba.list_games()), 2 + 6 * 2)
        self.assertEqual(self.urls[1:], [self.urls[0] + "?dates=20261024"])   # only the day that failed
        self.urls.clear()
        nba.list_games()
        self.assertEqual(len(self.urls), 1)   # the later days are cached
        for d in nba.later:
            nba.later[d] = (0.0, nba.later[d][1])
        self.down = {"20261025"}
        self.urls.clear()
        self.assertEqual(len(nba.list_games()), 2 + 6 * 2)   # a failed refetch keeps the day
        self.assertEqual(len(self.urls), 7)

    def test_nfl_list_is_one_request(self):
        espn.NFL().list_games()
        self.assertEqual(len(self.urls), 1)


class HelpersTest(unittest.TestCase):
    def test_day_labels(self):
        today = datetime.date.today()
        self.assertEqual(screens.day_label(today), "Today")
        self.assertEqual(screens.day_label(today + datetime.timedelta(days=1)), "Tomorrow")
        self.assertEqual(screens.day_label(today - datetime.timedelta(days=1)), "Yesterday")
        later = today + datetime.timedelta(days=3)
        self.assertEqual(screens.day_label(later), "%s %d" % (later.strftime("%a %b"), later.day))

    def test_scroll_window_keeps_the_wanted_rows_in_view(self):
        body = ["row %d" % i for i in range(30)]
        rows, top = screens.scroll_window(body, (0, 0), 0, 10, 20)
        self.assertEqual(len(rows), 10)
        self.assertNotIn("more", rows[0])
        self.assertIn("▼ more", rows[-1])
        rows, top = screens.scroll_window(body, (24, 25), top, 10, 20)
        self.assertEqual((rows[-2], top), ("row 25", 18))
        self.assertIn("▲ more", rows[0])
        rows, top = screens.scroll_window(body, (20, 21), top, 10, 20)   # still in view: no jump
        self.assertEqual(top, 18)
        rows, top = screens.scroll_window(body, (29, 29), top, 10, 20)
        self.assertEqual((rows[-2], top), ("row 29", 22))
        self.assertNotIn("more", rows[-1])
        short = ["a", "b"]
        self.assertEqual(screens.scroll_window(short, (1, 1), 5, 10, 20), (short, 0))
        rows, _ = screens.scroll_window(short, (0, 0), 0, 10, 20, note="▲ earlier")
        self.assertEqual([r.strip() for r in rows], ["▲ earlier", "a", "b"])
        rows, top = screens.scroll_window(body, (0, 0), 0, 10, 20, note="▲ earlier")
        self.assertEqual((rows[0].strip(), rows[1], top), ("▲ earlier", "row 0", 0))
        rows, top = screens.scroll_window(body, (25, 25), 0, 10, 20, note="▲ earlier")
        self.assertIn("▲ more", rows[0])   # scrolled down: the usual marker


class ListScreenTest(unittest.TestCase):
    def setUp(self):
        self.saved = dict(settings), screens.LEAGUES, screens.save_settings
        screens.save_settings = lambda: None
        screens._earlier.clear()
        screens._picked = None

    def tearDown(self):
        settings.clear()
        settings.update(self.saved[0])
        screens.LEAGUES, screens.save_settings = self.saved[1], self.saved[2]
        screens._earlier.clear()
        screens._picked = None

    def run_list(self, league):
        screens.LEAGUES = [league]
        settings.update(league=league.key, delay=0)
        term, out = FakeTerm(), {}
        t = threading.Thread(target=lambda: out.update(r=screens.game_list_screen(term, FakeMenu())), daemon=True)
        t.start()
        return term, t, out

    def test_up_at_the_top_loads_earlier_game_days(self):
        def day_games(day, n, state="final"):
            ts = time.mktime(tuple(int(x) for x in day.split("-")) + (19, 0, 0, 0, 0, -1))
            return [Game("tst", "%s-%d" % (day, i), state, Team("E%d" % i, "e"), Team("H%d" % i, "h"),
                         start_utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))) for i in range(n)]
        today = datetime.date.today()
        days = [(today - datetime.timedelta(days=n)).isoformat() for n in (0, 1, 3)]
        asked = []

        class Past(FakeLeague):
            first_day = days[0]

            def list_games(self):
                return day_games(days[0], 2, "pre")

            def earlier(self, day):
                asked.append(day)
                if day == days[0]:
                    return days[1], day_games(days[1], 3)
                if day == days[1]:
                    return days[2], day_games(days[2], 1)
                return None, []
        term, t, out = self.run_list(Past(None, "tst"))
        try:
            self.assertTrue(wait_for(lambda: term.shown("E0   @ H0")))
            self.assertIn("▲ earlier", term.draws[-1][1])
            term.keys.put("up")         # from the first game: the day before
            self.assertTrue(wait_for(lambda: term.shown("Yesterday")))
            self.assertEqual(asked, [days[0]])
            time.sleep(0.1)
            last = plain(term.draws[-1][1])
            self.assertLess(last.index("Yesterday"), last.index("Today"))
            # The selection stepped up into the new day: its last game.
            selected = [l for l in term.draws[-1][1].split("\n") if "\x1b[7m" in l and "@" in l]
            self.assertIn("E2   @ H2", plain(selected[0]))
            for _ in range(3):
                term.keys.put("up")     # to the top of that day, then once more: the day before
            self.assertTrue(wait_for(lambda: len(asked) == 2))
            self.assertEqual(asked[1], days[1])     # skipped the empty day in between
            self.assertTrue(wait_for(lambda: plain(term.draws[-1][1]).count("@") == 6))
            for _ in range(2):
                term.keys.put("up")
            self.assertTrue(wait_for(lambda: term.shown("no games in the two weeks before")))
            term.keys.put("up")         # nothing left to ask for
            time.sleep(0.1)
            self.assertEqual(len(asked), 3)
            term.keys.put("enter")      # open the earliest game...
            t.join(1)
            self.assertEqual(out["r"][1].id, days[2] + "-0")
            term, t, out = self.run_list(Past(None, "tst"))   # ...and come back to it
            self.assertTrue(wait_for(lambda: term.shown("E0   @ H0")))
            time.sleep(0.1)
            selected = [l for l in term.draws[-1][1].split("\n") if "\x1b[7m" in l and "@" in l]
            self.assertIn("E0   @ H0", plain(selected[0]))
            self.assertIn("FINAL", plain(selected[0]))   # the earliest day's game, not today's E0
            self.assertEqual(plain(term.draws[-1][1]).count("@"), 6)   # the earlier days are still there
        finally:
            term.keys.put("q")
            t.join(1)

    def test_up_on_live_games_switches_to_all_games(self):
        class Live(FakeLeague):
            first_day = datetime.date.today().isoformat()

            def list_games(self):
                return [Game("tst", "1", "live", Team("LIV", "l"), Team("ONE", "o")),
                        Game("tst", "2", "pre", Team("PRE", "p"), Team("TWO", "t"))]

            def earlier(self, day):
                raise AssertionError("not from the live list")
        term, t, out = self.run_list(Live(None, "tst"))
        try:
            self.assertTrue(wait_for(lambda: term.shown("LIV  @ ONE")))
            self.assertNotIn("PRE", term.draws[-1][1])
            self.assertIn("▲ all games", term.draws[-1][1])
            term.keys.put("up")
            self.assertTrue(wait_for(lambda: "PRE" in term.draws[-1][1]))
            self.assertIn("▲ earlier", term.draws[-1][1])
        finally:
            term.keys.put("q")
            t.join(1)

    def test_week_list_has_day_headings_and_scrolls_to_the_selection(self):
        noon = time.localtime()[:3] + (12, 0, 0, 0, 0, -1)
        start = time.mktime(noon)   # noon today, so the first heading is always "Today"
        games = [Game("tst", str(i), "pre", Team("A%02d" % i, "a"), Team("B%02d" % i, "b"),
                      start_utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(start + (i // 8) * 86400)))
                 for i in range(48)]

        class Week(FakeLeague):
            def list_games(self):
                return games
        screens.LEAGUES = [Week(None, "tst")]
        settings.update(league="tst", delay=0)
        term, out = FakeTerm(), {}
        t = threading.Thread(target=lambda: out.update(r=screens.game_list_screen(term, FakeMenu())), daemon=True)
        t.start()
        try:
            self.assertTrue(wait_for(lambda: term.shown("A00  @ B00")))
            first = term.draws[-1][1]
            self.assertIn("Today", first)
            self.assertIn("Tomorrow", first)
            self.assertLessEqual(len(first.split("\n")), 40)
            for _ in range(45):
                term.keys.put("down")
            self.assertTrue(wait_for(lambda: term.shown("A45  @ B45")))
            time.sleep(0.1)
            last = re.sub(r"\x1b\[[0-9;]*m", "", term.draws[-1][1])
            self.assertIn("A45  @ B45", last)     # the selected game is in view
            self.assertIn("▲ more", last)
            self.assertNotIn("A00  @ B00", last)
            term.keys.put("q")
            t.join(1)
            self.assertIsNone(out["r"])
        finally:
            term.keys.put("q")


if __name__ == "__main__":
    unittest.main()
