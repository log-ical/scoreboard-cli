"""Symbol sets for terminals with fewer glyphs (the Linux console, ASCII)."""

import os
import pathlib
import re
import unittest
from unittest import mock

from tests.helpers import ROOT
from tests.test_render import FakeTerminal
from scoreboard import live, settings as cfg, term
from scoreboard.model import Game, Team
from scoreboard.term import RESET, Colors, visible_len

plain = lambda s: re.sub(r"\x1b\[[0-9;]*m", "", s)


def drawn():
    """Every non-ASCII character in the app's string literals, so a new one
    can't slip in without a substitute (docstrings and comments aside)."""
    chars = set()
    for path in pathlib.Path(ROOT, "scoreboard").rglob("*.py"):
        for line in path.read_text().splitlines():
            code = line.split("#", 1)[0]
            for lit in re.findall(r'"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'', code):
                chars |= {ch for ch in lit if ord(ch) > 127}
    return "".join(sorted(chars))


DRAWN = drawn()


class SymbolsTest(unittest.TestCase):
    def setUp(self):
        self.saved = term.symbols

    def tearDown(self):
        term.symbols = self.saved

    def test_detect(self):
        utf8 = mock.Mock(encoding="utf-8")
        with mock.patch("sys.stdout", utf8):
            for env, want in (({"TERM": "linux"}, "console"), ({"TERM": "xterm-256color"}, "full"),
                              ({"TERM": "dumb"}, "ascii"), ({"TERM": "vt220"}, "console")):
                with mock.patch.dict(os.environ, env):
                    self.assertEqual(term.detect_symbols(), want, env)
        with mock.patch("sys.stdout", mock.Mock(encoding="ascii")), mock.patch.dict(os.environ, {"TERM": "xterm"}):
            self.assertEqual(term.detect_symbols(), "ascii")
        term.set_symbols("nonsense")
        self.assertEqual(term.symbols, "full")

    def test_substitutes_are_one_column(self):
        for mode in ("console", "ascii"):
            term.set_symbols(mode)
            for ch in DRAWN:
                self.assertEqual(visible_len(term.translate(ch)), 1, (mode, ch))
        term.set_symbols("ascii")
        self.assertTrue(plain(term.translate(DRAWN + " Montréal Slafkovský")).isascii())
        self.assertIn("Montreal Slafkovsky", term.translate("Montréal Slafkovský"))

    def test_console_keeps_what_its_fonts_have(self):
        term.set_symbols("console")
        self.assertEqual(term.translate("┌─┬─┐│█▀▄▌▐·"), "┌─┬─┐│█▀▄▌▐·")
        self.assertEqual(term.translate("● LIVE ╭━┃╮ ▶◀ ↑/↓ ‹ on › Loading…"), "* LIVE ┌─│┐ >< ^/v < on > Loading.")
        term.set_symbols("full")
        self.assertEqual(term.translate("● ◦"), "● ◦")

    def test_markers_tell_shots_from_goals(self):
        g = Game("nhl", "1", "live", Team("ANA", "a"), Team("NYI", "b"),
                 rink=[(80, 0, "away", False), (-80, 0, "home", True)], rink_last=("NYI GOAL", -80, "home"))
        tints = ((0, 80, 200), (0, 120, 60))
        for mode, shot, goal in (("full", "◦", "●"), ("console", "+", "@"), ("ascii", "+", "@")):
            term.set_symbols(mode)
            text = "\n".join(plain(l) for l in live.view("nhl", "rink", g, [], 60, Colors("16"), tints))
            self.assertIn("%s shot %s goal" % (shot, goal), text)
        term.set_symbols("console")
        self.assertEqual((live.marks()["made"], live.marks()["missed"]), ("O", "x"))

    def test_draw_uses_the_set_and_options_switch_it(self):
        term.set_symbols("console")
        t = FakeTerminal(20, 2)
        self.assertIn("* LIVE", t.draw_captured(["● LIVE"]))
        saved = dict(cfg.settings)
        try:
            menu = cfg.OptionsMenu(Colors("16"))
            menu.sel = [k for k, _, _ in cfg.OPTIONS].index("symbols")
            cfg.settings["symbols"] = "console"
            menu.handle_key("right")
            self.assertEqual((cfg.settings["symbols"], term.symbols), ("ascii", "ascii"))
        finally:
            cfg.settings.clear()
            cfg.settings.update(saved)


if __name__ == "__main__":
    unittest.main()
