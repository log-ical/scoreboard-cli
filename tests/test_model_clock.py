import unittest

from tests.helpers import ROOT  # noqa: F401
from scoreboard.clock import ClockSmoother, clock_target
from scoreboard.model import Game, Team, is_stale, list_status, parse_utc


def game(period=3, secs=600.0, state="live", away=1, home=1, brk="", running=True, anchor=0.0, cap=None):
    return Game(league="t", id="1", state=state, away=Team("A", "Away", away), home=Team("H", "Home", home),
                period=period, short_period="P%d" % period, break_label=brk, clock_secs=secs,
                clock_running=running, clock_anchor=anchor, clock_cap=cap, final_label="FINAL/OT")


class StaleTest(unittest.TestCase):
    def test_cases(self):
        cases = [
            ("stale 18 s copy", game(secs=1103), game(secs=1085), True),
            ("review bumps clock 1 s", game(secs=601), game(secs=600), False),
            ("stale copy with a new goal", game(secs=700, home=2), game(secs=600), False),
            ("older period", game(period=2, secs=100), game(period=3, secs=1000), True),
            ("break after period", game(secs=1000, brk="INT"), game(secs=0), False),
            ("new period", game(period=4, secs=300), game(secs=0, brk="INT"), False),
            ("normal update", game(secs=580), game(secs=600), False),
            ("stopped clock", game(secs=600), game(secs=600), False),
            ("final after live", game(secs=0, state="final"), game(secs=3), False),
            ("live after final", game(secs=3), game(secs=0, state="final"), True),
        ]
        for name, new, last, want in cases:
            with self.subTest(name):
                self.assertEqual(is_stale(new, last), want)

    def test_countdowns(self):
        from scoreboard.model import fmt_countdown, fmt_countdown_short, until_start
        self.assertEqual(fmt_countdown(299), "04:59")
        self.assertEqual(fmt_countdown(3899), "1:04:59")
        self.assertEqual(fmt_countdown(2 * 86400 + 3 * 3600 + 15 * 60 + 9), "2D 03:15")
        self.assertEqual(fmt_countdown_short(30), "in <1m")
        self.assertEqual(fmt_countdown_short(45 * 60), "in 45m")
        self.assertEqual(fmt_countdown_short(125 * 60), "in 2h 05m")
        g = game()
        g.start_utc = "2026-10-21T23:30:00Z"
        start = parse_utc(g.start_utc)
        self.assertEqual(until_start(g, start - 90), 90)
        self.assertEqual(until_start(g, start + 600), 0)
        g.start_utc = ""
        self.assertIsNone(until_start(g))

    def test_list_status_hides_overtime(self):
        g = game(state="final")
        self.assertEqual(list_status(g), "FINAL/OT")
        self.assertEqual(list_status(g, hide=True), "FINAL")
        self.assertEqual(list_status(game(secs=65)), "P3  01:05")


class ClockTest(unittest.TestCase):
    def test_target_runs_from_anchor_and_respects_cap(self):
        self.assertEqual(clock_target(game(secs=600, anchor=100), 110), 590)
        self.assertEqual(clock_target(game(secs=600, anchor=100, cap=26), 200), 574)
        self.assertEqual(clock_target(game(secs=600, anchor=100, running=False), 200), 600)

    def test_smoother_catches_up_pauses_and_snaps(self):
        s = ClockSmoother()
        g = game()
        self.assertEqual(s.update(g, 600, 0), 600)
        self.assertEqual(s.update(g, 597, 1), 598)     # 2 s behind: run at 2x
        self.assertEqual(s.update(g, 599, 2), 598)     # 2 s ahead: pause
        self.assertEqual(s.update(g, 597, 3), 597)     # on target: normal tick
        self.assertEqual(s.update(g, 580, 4), 580)     # far off: snap


if __name__ == "__main__":
    unittest.main()
