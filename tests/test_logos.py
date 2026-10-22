"""Logos load in the background: drawing never waits on the network."""

import os
import tempfile
import threading
import time
import unittest
import zlib
from unittest import mock

from tests.helpers import ROOT  # noqa: F401  (sets sys.path)
from tests.test_png import encode_rgba, raw_png
from scoreboard import logos
from scoreboard.model import Team

RED = (200, 20, 20)
RED_TINT = (208, 16, 16)   # what tint() makes of a RED logo (colors are bucketed)


def logo_png(rgb=RED):
    """An 8x8 PNG: a 6x6 square of rgb in a transparent border."""
    clear = (0, 0, 0, 0)
    rows = [[clear] * 8] + [[clear] + [rgb + (255,)] * 6 + [clear] for _ in range(6)] + [[clear] * 8]
    return encode_rgba(rows)


def wait_for(cond, timeout=10.0):   # generous: it returns as soon as cond holds, and CI can be slow
    end = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > end:
            return False
        time.sleep(0.005)
    return True


class LogoStoreTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.fetch = mock.Mock(side_effect=OSError("offline"))
        for patch in (mock.patch.object(logos, "CACHE_DIR", self.dir), mock.patch.object(logos, "fetch", self.fetch)):
            patch.start()
            self.addCleanup(patch.stop)
        self.store = logos.LogoStore()

    def on_disk(self, url, data=None):
        with open(logos.cache_path(url), "wb") as f:
            f.write(logo_png() if data is None else data)

    def team(self, url):
        return Team("X", "X Team", logo_url=url)

    def settled(self):
        """Wait for downloads in flight to finish."""
        self.assertTrue(wait_for(lambda: not self.store.loading))

    def leftovers(self):
        return [f for f in os.listdir(self.dir) if f.endswith(".part")]

    def test_logo_on_disk_shows_at_once(self):
        self.on_disk("u")
        self.assertEqual(self.store.grid("u", 3, 4), [[RED] * 3] * 3)   # 6x6 fit in 3x4
        self.assertEqual(self.store.tint(self.team("u")), RED_TINT)
        self.fetch.assert_not_called()

    def test_download_never_blocks_drawing(self):
        release = threading.Event()

        def slow_fetch(url):
            release.wait(5)
            return logo_png()
        self.fetch.side_effect = slow_fetch
        try:
            started = time.monotonic()
            for _ in range(20):   # redraws while the download is in flight
                self.assertIsNone(self.store.grid("u", 3, 4))
                self.assertEqual(self.store.tint(self.team("u")), logos.DEFAULT_TINT)
            self.assertLess(time.monotonic() - started, 0.5)
            self.assertEqual(self.fetch.call_count, 1)
        finally:
            release.set()
        self.assertTrue(wait_for(lambda: self.store.grid("u", 3, 4) is not None))
        self.assertEqual(self.store.grid("u", 3, 4), [[RED] * 3] * 3)
        self.assertEqual(self.store.tint(self.team("u")), RED_TINT)   # the gray wasn't kept
        self.assertEqual(self.fetch.call_count, 1)
        with open(logos.cache_path("u"), "rb") as f:
            self.assertEqual(f.read(), logo_png())
        self.assertEqual(self.leftovers(), [])

    def test_one_download_per_logo_across_threads(self):
        release = threading.Event()
        self.fetch.side_effect = lambda url: release.wait(5) and logo_png()

        def draw():
            for _ in range(50):
                self.store.grid("u", 3, 4)
                self.store.tint(self.team("u"))
        threads = [threading.Thread(target=draw) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(5)
        release.set()
        self.settled()
        self.assertEqual(self.fetch.call_count, 1)
        self.assertIsNotNone(self.store.grid("u", 3, 4))

    def test_failed_download_is_retried_after_a_while(self):
        self.fetch.side_effect = [OSError("offline"), logo_png()]
        self.assertIsNone(self.store.grid("u", 3, 4))
        self.settled()
        for _ in range(5):   # cooling down: no new attempt
            self.assertIsNone(self.store.grid("u", 3, 4))
            self.assertEqual(self.store.tint(self.team("u")), logos.DEFAULT_TINT)
        self.assertEqual(self.fetch.call_count, 1)
        self.store.failed["u"] -= logos.RETRY_AFTER
        self.assertTrue(wait_for(lambda: self.store.grid("u", 3, 4) is not None))
        self.assertEqual(self.fetch.call_count, 2)
        self.assertNotIn("u", self.store.failed)

    def test_bad_download_is_not_cached_on_disk(self):
        self.fetch.side_effect = None
        self.fetch.return_value = b"<html>not a logo</html>"
        self.assertIsNone(self.store.grid("u", 3, 4))
        self.settled()
        self.assertIn("u", self.store.failed)
        self.assertEqual(os.listdir(self.dir), [])

    def test_unreadable_file_on_disk_is_downloaded_again(self):
        self.on_disk("u", logo_png()[:60])   # cut short, e.g. by an old interrupted write
        self.fetch.side_effect = None
        self.fetch.return_value = logo_png()
        self.assertIsNone(self.store.grid("u", 3, 4))
        self.assertTrue(wait_for(lambda: self.store.grid("u", 3, 4) is not None))
        self.assertEqual(self.fetch.call_count, 1)
        with open(logos.cache_path("u"), "rb") as f:
            self.assertEqual(f.read(), logo_png())

    def test_empty_image_on_disk_is_downloaded_again(self):
        """It used to decode, stay cached, and crash every game of that team."""
        self.on_disk("u", raw_png(0, 4, zlib.compress(b"\0" * 4)))   # four empty rows: a valid PNG once
        self.fetch.side_effect = None
        self.fetch.return_value = logo_png()
        self.assertIsNone(self.store.grid("u", 3, 4))
        self.assertTrue(wait_for(lambda: self.store.grid("u", 3, 4) is not None))
        with open(logos.cache_path("u"), "rb") as f:
            self.assertEqual(f.read(), logo_png())

    def test_atomic_write(self):
        path = os.path.join(self.dir, "a.png")
        with mock.patch.object(logos.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                logos.write_atomic(path, b"data")
        self.assertEqual(os.listdir(self.dir), [])   # neither a partial file nor a temp file
        logos.write_atomic(path, b"old")
        with mock.patch.object(logos.os, "replace", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                logos.write_atomic(path, b"new")
        with open(path, "rb") as f:
            self.assertEqual(f.read(), b"old")   # untouched until the new one is complete
        logos.write_atomic(path, b"new")
        with open(path, "rb") as f:
            self.assertEqual(f.read(), b"new")
        self.assertEqual(self.leftovers(), [])

    def test_no_url(self):
        self.assertIsNone(self.store.grid("", 3, 4))
        self.assertEqual(self.store.tint(self.team("")), logos.DEFAULT_TINT)
        self.fetch.assert_not_called()

    def test_league_colors_come_first(self):
        team = Team("X", "X Team", logo_url="u", color=(10, 20, 120))
        self.assertEqual(self.store.tint(team), logos.brighten((10, 20, 120)))
        self.fetch.assert_not_called()

    def test_grids_are_reused_and_bounded(self):
        self.on_disk("u")
        grid = self.store.grid("u", 3, 4)
        for w in range(4, 4 + logos.MAX_GRIDS * 2):
            self.assertIs(self.store.grid("u", 3, 4), grid)   # in use: stays cached
            self.store.grid("u", w, 4)
        self.assertLessEqual(len(self.store.scaled), logos.MAX_GRIDS)
        self.fetch.assert_not_called()

    def test_decoded_logos_are_bounded_and_reload_from_disk(self):
        urls = ["u%d" % i for i in range(logos.MAX_IMAGES + 3)]
        for i, url in enumerate(urls):
            self.on_disk(url, logo_png((100 + i, 50, 50)))
            self.store.tint(self.team(url))
        self.assertLessEqual(len(self.store.images), logos.MAX_IMAGES)
        self.assertNotIn(urls[0], self.store.images)
        self.assertEqual(self.store.grid(urls[0], 1, 2), [[(100, 50, 50)]] * 2)
        self.fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
