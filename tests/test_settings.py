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


if __name__ == "__main__":
    unittest.main()
