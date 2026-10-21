"""The screens never wait on the network: polls and list loads run on threads."""

import copy
import queue
import threading
import time
import unittest

from tests.helpers import ROOT, fixture  # noqa: F401
from tests.test_leagues import LAST_PLAY, FakeApi
from scoreboard import screens
from scoreboard.leagues import espn, nhl
from scoreboard.model import Game, Team
from scoreboard.settings import settings
from scoreboard.term import Colors


def wait_for(cond, timeout=2.0):
    end = time.time() + timeout
    while not cond():
        if time.time() > end:
            return False
        time.sleep(0.005)
    return True


def game(venue="Arena", play_seq=None):
    return Game("nba", "1", "live", Team("AAA", "Away Team", 1), Team("BBB", "Home Team", 2),
                period=1, period_label="1st QUARTER", short_period="Q1", clock_secs=600.0,
                venue=venue, play_seq=play_seq)


class FakeTerm:
    """Records what is drawn and when; the test types keys into a queue."""

    def __init__(self):
        self.keys = queue.Queue()
        self.draws = []          # [(time, text)]

    def size(self):
        return 100, 40

    def pixel_aspect(self):
        return 1.0

    def draw(self, lines):
        self.draws.append((time.time(), "\n".join(lines)))

    def read_key(self, timeout):
        try:
            return self.keys.get(timeout=min(timeout, 0.02))   # a short frame keeps the tests fast
        except queue.Empty:
            return None

    def shown(self, text):
        return any(text in d for _, d in list(self.draws))


class FakeLogos:
    def grid(self, *args):
        return None

    def tint(self, team):
        return (200, 200, 200)


class FakeMenu:
    open = False

    def toggle(self):
        pass


class FakeLeague:
    def __init__(self, poller, key="tst"):
        self.key, self.name, self.poller = key, key.upper(), poller

    def open(self, listed):
        return self.poller

    def score_alert(self, points):
        return None


class SlowPoller:
    """poll() hangs until released, like a request to a slow server."""

    def __init__(self, venue="Arena"):
        self.venue = venue
        self.release, self.returned = threading.Event(), threading.Event()
        self.threads = []

    def poll(self):
        self.threads.append(threading.current_thread())
        self.release.wait(2)
        self.returned.set()
        return game(self.venue), time.time(), 3600


class PlaysPoller:
    """An ESPN-like poller with its own play list; records the threads it runs on."""

    def __init__(self):
        self.want_plays, self.polls, self.older_calls, self.threads = False, [], 0, set()
        self.plays = {98: "Q1  9:00  play 98", 99: "Q1  8:00  play 99", 100: "Q1  7:00  play 100"}

    def poll(self):
        self.threads.add(threading.current_thread())
        self.polls.append(self.want_plays)
        return game(play_seq=100), time.time(), 3600

    def load_older(self):
        self.threads.add(threading.current_thread())
        self.older_calls += 1
        self.plays.update({n: "Q1 10:00  play %d" % n for n in range(90, 98)})

    def play_history(self):
        self.threads.add(threading.current_thread())
        return sorted(self.plays.items(), reverse=True), self.older_calls == 0


class ScreenTest(unittest.TestCase):
    def setUp(self):
        self.saved = dict(settings)
        settings.update(delay=0, layout="vertical", show_last_play=True, bell=False)
        self.save_settings, screens.save_settings = screens.save_settings, lambda: None

    def tearDown(self):
        settings.clear()
        settings.update(self.saved)
        screens.save_settings = self.save_settings

    def start_game(self, poller, term):
        out = {}

        def run():
            out["result"] = screens.game_screen(term, FakeLeague(poller), None, FakeLogos(),
                                                Colors("truecolor"), FakeMenu())
        t = threading.Thread(target=run, daemon=True)
        t.start()
        return t, out

    def test_draws_and_leaves_while_poll_hangs(self):
        poller, term = SlowPoller(), FakeTerm()
        try:
            started = time.time()
            t, out = self.start_game(poller, term)
            time.sleep(0.4)
            times = [d for d, _ in list(term.draws)]
            self.assertGreater(len(times), 10)
            self.assertLess(times[0] - started, 0.2)
            self.assertLess(max(b - a for a, b in zip(times, times[1:])), 0.2)
            self.assertTrue(term.shown("Loading game…"))
            # Leaving doesn't wait for the poll either.
            term.keys.put("b")
            t.join(0.3)
            self.assertFalse(t.is_alive())
            self.assertTrue(out["result"])
            self.assertFalse(poller.returned.is_set())
        finally:
            poller.release.set()

    def test_play_requests_are_served_by_the_worker(self):
        poller, term = PlaysPoller(), FakeTerm()
        t, _ = self.start_game(poller, term)
        self.assertTrue(wait_for(lambda: term.shown("Arena")))
        self.assertEqual(poller.polls, [False])
        term.keys.put("p")      # opening the list polls right away, with the list wanted
        self.assertTrue(wait_for(lambda: poller.polls == [False, True]))
        self.assertTrue(wait_for(lambda: term.shown("play 98")))
        term.keys.put("]")      # past the loaded plays: the worker fetches older ones
        self.assertTrue(wait_for(lambda: term.shown("play 90")))
        self.assertEqual(poller.older_calls, 1)
        n = len(term.draws)
        term.keys.put("]")      # nothing older left: nothing to ask for
        self.assertTrue(wait_for(lambda: len(term.draws) > n + 3))
        self.assertEqual(poller.older_calls, 1)
        self.assertFalse(any("loading" in d.lower() for _, d in term.draws[n:]))
        term.keys.put("q")
        t.join(1)
        self.assertFalse(t.is_alive())
        # The poller was only ever used on the worker thread.
        self.assertEqual(len(poller.threads), 1)
        self.assertNotIn(t, poller.threads)
        self.assertNotIn(threading.current_thread(), poller.threads)

    def test_older_requests_made_mid_load_are_not_lost(self):
        poller, release = PlaysPoller(), threading.Event()
        load = poller.load_older

        def slow_load():
            release.wait(2)
            load()
        poller.load_older = slow_load
        feed = screens.GameFeed(poller)
        try:
            self.assertTrue(wait_for(lambda: poller.polls))
            feed.show_plays(True)
            feed.load_older()
            self.assertTrue(wait_for(lambda: feed.plays_pending()))
            time.sleep(0.05)    # the first load is in flight now
            feed.load_older()   # a second request, made while it loads
            release.set()
            self.assertTrue(wait_for(lambda: poller.older_calls == 2))
            self.assertTrue(wait_for(lambda: not feed.plays_pending()))
        finally:
            release.set()
            feed.stop()

    def test_no_poll_after_stop_during_an_older_load(self):
        poller, release, polls_at = PlaysPoller(), threading.Event(), []
        poll, load = poller.poll, poller.load_older

        def quick_poll():
            polls_at.append(time.time())
            return poll()[0], time.time(), 0.01   # a poll is always due
        poller.poll = quick_poll
        poller.load_older = lambda: (release.wait(2), load())
        feed = screens.GameFeed(poller)
        try:
            self.assertTrue(wait_for(lambda: polls_at))
            feed.load_older()
            self.assertTrue(wait_for(lambda: feed.plays_pending() or poller.older_calls == 0))
            time.sleep(0.05)
            stopped = time.time()
            feed.stop()
            release.set()
            feed.thread.join(1)
            self.assertFalse(feed.thread.is_alive())
            self.assertEqual([t for t in polls_at if t > stopped], [])
        finally:
            release.set()
            feed.stop()

    def test_closed_screen_cannot_feed_the_next_one(self):
        old, term1 = SlowPoller("OLD GAME"), FakeTerm()
        t1, _ = self.start_game(old, term1)
        self.assertTrue(wait_for(lambda: old.threads))
        term1.keys.put("b")
        t1.join(1)
        self.assertFalse(t1.is_alive())

        new, term2 = PlaysPoller(), FakeTerm()
        t2, _ = self.start_game(new, term2)
        self.assertTrue(wait_for(lambda: term2.shown("Arena")))
        old.release.set()       # the old game's poll finally answers
        self.assertTrue(old.returned.wait(1))
        self.assertTrue(wait_for(lambda: not old.threads[0].is_alive(), 1))
        n = len(term2.draws)
        self.assertTrue(wait_for(lambda: len(term2.draws) > n + 3))
        self.assertFalse(term2.shown("OLD GAME"))
        self.assertEqual(len(old.threads), 1)   # and it never polled again
        term2.keys.put("q")
        t2.join(1)

    def test_game_list_loads_in_background(self):
        release = threading.Event()

        class SlowList(FakeLeague):
            def list_games(self):
                release.wait(2)
                return [game()]

        class Broken(FakeLeague):
            def list_games(self):
                raise OSError("boom")
        saved = screens.LEAGUES
        screens.LEAGUES = [SlowList(None, "aaa"), Broken(None, "bbb")]
        settings["league"] = "aaa"
        term, out = FakeTerm(), {}
        try:
            t = threading.Thread(target=lambda: out.update(r=screens.game_list_screen(term, FakeMenu())),
                                 daemon=True)
            t.start()
            self.assertTrue(wait_for(lambda: term.shown("Loading AAA games…"), 0.3))
            term.keys.put("2")     # switching leagues doesn't wait for the first one
            self.assertTrue(wait_for(lambda: term.shown("Could not reach BBB data: boom"), 0.3))
            term.keys.put("1")
            release.set()
            self.assertTrue(wait_for(lambda: term.shown("AAA  @ BBB")))
            term.keys.put("r")     # refreshing keeps the list up
            n = len(term.draws)
            self.assertTrue(wait_for(lambda: len(term.draws) > n + 3))
            self.assertIn("AAA  @ BBB", term.draws[-1][1])
            term.keys.put("q")
            t.join(1)
            self.assertFalse(t.is_alive())
            self.assertIsNone(out["r"])
        finally:
            release.set()
            screens.LEAGUES = saved


class WorkerTest(unittest.TestCase):
    def test_play_history_safe_while_poller_changes_it(self):
        """The screen reads copies made on the worker while it keeps
        replacing and adding to the poller's play dict."""
        n, lock = [10], threading.Lock()

        def plays_page(page):
            last = n[0]
            return {"count": last, "pageCount": -(-last // espn.PLAY_HISTORY),
                    "items": [{"sequenceNumber": str(s), "text": "play %d" % s}
                              for s in range((page - 1) * espn.PLAY_HISTORY + 1,
                                             min(page * espn.PLAY_HISTORY, last) + 1)]}

        def fetch(url, timeout=10):
            if url.endswith("/situation"):
                with lock:
                    n[0] += 1        # a new play on every poll
                    return {"lastPlay": {"$ref": "http://core/plays/%d" % n[0]}}
            if "/plays?limit=1" in url:
                return {"count": n[0]}
            if "/plays?" in url:
                return plays_page(int(url.rsplit("=", 1)[1]))
            if "/plays/" in url:
                s = int(url.rsplit("/", 1)[1])
                return {"sequenceNumber": str(s), "text": "play %d" % s}
            return fixture("nba_status.json")

        class Fast(espn.NBA):
            poll_every = 0

        saved = espn.fetch_json, espn.fetch_json_timed, espn.PLAY_REFRESH
        espn.fetch_json, espn.PLAY_REFRESH = fetch, 0    # refetch the list on every poll
        espn.fetch_json_timed = lambda url, timeout=10: (fetch(url), 0.0, 6)
        league = Fast()
        poller = league.open(league.parse_event(fixture("nba_scoreboard.json")["events"][0]))
        feed = screens.GameFeed(poller)
        try:
            feed.show_plays(True)
            lists, end = 0, time.time() + 0.3
            while time.time() < end:
                feed.load_older()
                for kind, *msg in feed.updates():
                    self.assertNotEqual(kind, "error", msg)
                    if kind == "plays":
                        seqs = [s for s, _ in msg[0]]
                        self.assertEqual(seqs, sorted(set(seqs), reverse=True))
                        self.assertIsNot(msg[0], poller.plays)
                        lists += 1
                time.sleep(0.001)
            self.assertGreater(lists, 10)
            self.assertGreater(n[0], 20)
        finally:
            feed.stop()
            feed.thread.join(2)   # it must not reach the real fetch functions
            espn.fetch_json, espn.fetch_json_timed, espn.PLAY_REFRESH = saved


class FetchTest(unittest.TestCase):
    def test_espn_status_and_situation_fetched_at_once(self):
        play = copy.deepcopy(fixture("nba_play.json"))
        nba = espn.NBA()
        situation_started, overlapped = threading.Event(), []
        with FakeApi(fixture("nba_status.json"), dict(LAST_PLAY), play) as api:
            def fetch(url, timeout=10):
                if url.endswith("/situation"):
                    situation_started.set()
                if url.endswith("/status"):
                    overlapped.append(situation_started.wait(1))
                return api.fetch_json(url)
            espn.fetch_json = fetch
            espn.fetch_json_timed = lambda url, timeout=10: (fetch(url), 0.0, 6)
            g, _, _ = nba.open(nba.parse_event(fixture("nba_scoreboard.json")["events"][0])).poll()
        self.assertEqual(overlapped, [True])
        self.assertEqual(g.away.score, play["awayScore"])

    def test_nhl_list_does_not_wait_for_standings(self):
        release = threading.Event()

        def fetch(url, timeout=10):
            if "/standings/" in url:
                release.wait(2)
                return {"standings": [{"teamAbbrev": {"default": "SJS"}, "wins": 8, "losses": 2, "otLosses": 1}]}
            return fixture("nhl_score.json")
        saved = nhl.fetch_json, dict(nhl.records), nhl.records_fetched
        nhl.fetch_json, nhl.records_fetched = fetch, 0.0
        nhl.records.clear()
        try:
            t0 = time.time()
            games = nhl.NHL().list_games()
            self.assertLess(time.time() - t0, 0.5)
            self.assertTrue(games)
            self.assertEqual(nhl.records, {})
            release.set()
            self.assertTrue(wait_for(lambda: nhl.records.get("SJS") == "8-2-1"))
        finally:
            release.set()
            nhl.fetch_json = saved[0]
            nhl.records.clear()
            nhl.records.update(saved[1])
            nhl.records_fetched = saved[2]


if __name__ == "__main__":
    unittest.main()
