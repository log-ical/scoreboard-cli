import json
import os
import tempfile
import unittest

from tests.helpers import ROOT  # noqa: F401
from scoreboard import settings as cfg


class SettingsTest(unittest.TestCase):
    def test_delay_steps(self):
        v, up = 0, []
        for _ in range(16):
            v = cfg.step_delay(v, 1)
            up.append(v)
        self.assertEqual(up[:13], [5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55, 60, 75])
        down = []
        for _ in range(16):
            v = cfg.step_delay(v, -1)
            down.append(v)
        self.assertEqual(down[-1], 0)
        self.assertEqual(cfg.step_delay(cfg.MAX_DELAY, 1), cfg.MAX_DELAY)

    def test_migrates_old_nhl_live_settings(self):
        with tempfile.TemporaryDirectory() as d:
            old = os.path.join(d, "old.json")
            with open(old, "w") as f:
                json.dump({"delay": 45, "show_sog": False, "goal_alert": False, "refresh": 5}, f)
            saved = cfg.CONFIG_PATH, cfg.OLD_CONFIG_PATH, dict(cfg.settings)
            try:
                cfg.CONFIG_PATH, cfg.OLD_CONFIG_PATH = os.path.join(d, "new.json"), old
                cfg.settings.clear()
                cfg.settings.update(cfg.DEFAULTS)
                cfg.load_settings()
                self.assertEqual(cfg.settings["delay"], 45)
                self.assertFalse(cfg.settings["show_stats"])
                self.assertFalse(cfg.settings["score_alert"])
                self.assertNotIn("refresh", cfg.settings)
            finally:
                cfg.CONFIG_PATH, cfg.OLD_CONFIG_PATH = saved[0], saved[1]
                cfg.settings.clear()
                cfg.settings.update(saved[2])


class FileTest(unittest.TestCase):
    """settings.json as people edit it, and as full disks and crashes leave it."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.saved = cfg.CONFIG_PATH, cfg.OLD_CONFIG_PATH, dict(cfg.settings)
        cfg.CONFIG_PATH = os.path.join(self.dir.name, "settings.json")
        cfg.OLD_CONFIG_PATH = os.path.join(self.dir.name, "old.json")
        cfg.settings.clear()
        cfg.settings.update(cfg.DEFAULTS)

    def tearDown(self):
        cfg.CONFIG_PATH, cfg.OLD_CONFIG_PATH = self.saved[0], self.saved[1]
        cfg.settings.clear()
        cfg.settings.update(self.saved[2])
        self.dir.cleanup()

    def write(self, text, path=None):
        with open(path or cfg.CONFIG_PATH, "w", encoding="utf-8") as f:
            f.write(text)

    def test_a_typo_keeps_the_file_aside_instead_of_saving_over_it(self):
        text = '{"hide_list_scores": true, "delay": 90,}'
        self.write(text)
        self.assertTrue(cfg.load_settings())          # a problem to warn about
        self.assertTrue(cfg.save_settings())
        with open(cfg.CONFIG_PATH + ".bad") as f:
            self.assertEqual(f.read(), text)          # what the user wrote is still there
        with open(cfg.CONFIG_PATH) as f:
            self.assertEqual(json.load(f), cfg.DEFAULTS)

    def test_values_are_checked_one_by_one(self):
        self.write(json.dumps({"delay": 3600, "logo_size": "Large", "hide_list_scores": "true", "bell": True,
                               "layout": "horizontal"}))
        problems = cfg.load_settings()
        self.assertEqual(len(problems), 2, problems)  # logo_size and hide_list_scores
        self.assertEqual(cfg.settings["delay"], cfg.MAX_DELAY)   # brought into range, not dropped
        self.assertEqual((cfg.settings["logo_size"], cfg.settings["hide_list_scores"]), ("large", False))
        self.assertEqual((cfg.settings["bell"], cfg.settings["layout"]), (True, "horizontal"))

    def test_other_files_that_cant_be_used(self):
        for text in ("[]", "null", '"nba"', "42", "", "[" * 100000):
            cfg.settings.update(cfg.DEFAULTS)
            self.write(text)
            self.assertTrue(cfg.load_settings(), text[:10])
            self.assertEqual(cfg.settings, cfg.DEFAULTS)
        self.write('﻿{"delay": 45}')     # a BOM, as some editors save
        self.assertEqual(cfg.load_settings(), [])
        self.assertEqual(cfg.settings["delay"], 45)

    def test_broken_file_does_not_bring_back_nhl_live_settings(self):
        self.write('{"delay": 600}', cfg.OLD_CONFIG_PATH)
        self.write('{"delay": 90,')
        cfg.load_settings()
        self.assertEqual(cfg.settings["delay"], 0)

    def test_failed_save_leaves_the_old_file(self):
        self.write('{"delay": 90}')
        cfg.settings["delay"] = 30

        def full(*args):
            raise OSError(28, "No space left on device")
        saved = os.replace
        os.replace = full
        try:
            self.assertFalse(cfg.save_settings())
        finally:
            os.replace = saved
        with open(cfg.CONFIG_PATH) as f:
            self.assertEqual(json.load(f), {"delay": 90})
        self.assertEqual(sorted(os.listdir(self.dir.name)), ["settings.json"])   # no temp file left


class PlayListTest(unittest.TestCase):
    def test_delayed_view_hides_later_plays(self):
        from scoreboard.model import Game, Team
        from scoreboard.screens import visible_plays
        loaded = [(12, "c"), (11, "b"), (10, "a")]
        delayed = Game("nba", "1", "live", Team("A", "A"), Team("B", "B"), play_seq=11)
        self.assertEqual(visible_plays(delayed, loaded), ["b", "a"])
        pregame = Game("nba", "1", "pre", Team("A", "A"), Team("B", "B"))
        self.assertEqual(visible_plays(pregame, loaded), [])
        nhl = Game("nhl", "1", "live", Team("A", "A"), Team("B", "B"), plays=[(None, "goal")])
        self.assertEqual(visible_plays(nhl), ["goal"])


if __name__ == "__main__":
    unittest.main()
