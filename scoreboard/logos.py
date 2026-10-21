"""Team logos (downloaded once, cached on disk) and team tint colors.

Downloads run on background threads, so drawing never waits on the network:
until a logo is in, grid() gives None (the screens draw a badge instead) and
tint() a neutral gray, and the next redraw picks the logo up.
"""

import hashlib
import os
import tempfile
import threading
import time

from .net import fetch
from .png import crop_transparent, decode_png, downscale, primary_color

CACHE_DIR = os.path.join(os.path.expanduser("~"), ".cache", "scoreboard", "logos")
MAX_IMAGES = 12      # decoded logos kept in memory (a few MB each); others reload from CACHE_DIR
MAX_GRIDS = 32       # scaled grids kept (every window size makes new ones)
RETRY_AFTER = 60.0   # seconds before a logo that failed to download is tried again
DEFAULT_TINT = (220, 220, 220)


def _remember(cache, key, value, limit):
    """Store key as the most recently used entry, dropping the oldest past limit."""
    cache.pop(key, None)
    cache[key] = value
    if len(cache) > limit:
        for k in list(cache)[:len(cache) - limit]:
            cache.pop(k, None)
    return value


def cache_path(url):
    return os.path.join(CACHE_DIR, hashlib.md5(url.encode()).hexdigest()[:16] + ".png")


def write_atomic(path, data):
    """Write via a temporary file in the same folder, so the file at path is
    always either absent or complete, even if the write is interrupted."""
    folder = os.path.dirname(path)
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".", suffix=".part")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def brighten(rgb):
    """Lift dark colors (e.g. navy) so text in that color stays readable."""
    peak = max(rgb)
    if peak >= 170:
        return tuple(rgb)
    if peak == 0:
        return (200, 200, 200)
    return tuple(min(255, int(c * 170 / peak) + 40) for c in rgb)


class LogoStore:
    """Called from the screen's thread; downloads report back from their own."""

    def __init__(self):
        self.images = {}      # url -> decoded, cropped logo; least recently used first
        self.scaled = {}      # (url, w, h, aspect) -> pixel grid; least recently used first
        self.tints = {}       # url -> rgb, for teams tinted from their logo
        self.loading = set()  # urls being downloaded
        self.failed = {}      # url -> time.monotonic() of its last failed download
        self.lock = threading.Lock()   # guards images, loading and failed

    def _image(self, url):
        """The logo for url if it's ready, else None (starting a download if
        none is in flight and it hasn't failed in the last RETRY_AFTER s)."""
        if not url:
            return None
        with self.lock:
            img = self.images.get(url)
            if img is not None:
                return _remember(self.images, url, img, MAX_IMAGES)
            failed_at = self.failed.get(url)
            if url in self.loading or (failed_at is not None and time.monotonic() - failed_at < RETRY_AFTER):
                return None
            self.loading.add(url)
        # Already on disk: decoding takes a few ms, so do it right here and
        # the logo is there on the first frame instead of a badge.
        path = cache_path(url)
        if os.path.exists(path):
            try:
                with open(path, "rb") as f:
                    img = crop_transparent(decode_png(f.read()))
            except Exception:
                img = None
                try:
                    os.remove(path)   # unreadable: download it again
                except OSError:
                    pass
            if img is not None:
                return self._finish(url, img)
        try:
            threading.Thread(target=self._download, args=(url, path), daemon=True,
                             name="logo " + url[-40:]).start()
        except RuntimeError:   # can't start a thread
            self._finish(url, None)
        return None

    def _download(self, url, path):
        try:
            data = fetch(url)
            img = crop_transparent(decode_png(data))
        except Exception:
            img = None
        else:
            try:
                write_atomic(path, data)
            except OSError:
                pass   # still shown, just not cached
        self._finish(url, img)

    def _finish(self, url, img):
        with self.lock:
            self.loading.discard(url)
            if img is None:
                self.failed[url] = time.monotonic()
                return None
            self.failed.pop(url, None)
            return _remember(self.images, url, img, MAX_IMAGES)

    def grid(self, url, box_w, box_h, aspect=1.0):
        """Logo scaled to fit inside box_w x box_h pixels, keeping its aspect
        on screen, where each pixel is `aspect` times as wide as it is tall.
        None while the logo isn't loaded (or can't be).

        The same grid object comes back for the same arguments while it is
        cached, which lets render.logo_lines reuse its rendering."""
        key = (url, box_w, box_h, aspect)
        try:
            grid = self.scaled[key]
        except KeyError:
            img = self._image(url)
            if not img:
                return None   # not cached: the logo may turn up later
            scale = min(box_w * aspect / img[0], box_h / img[1])
            tw = max(1, round(img[0] * scale / aspect))
            th = max(2, round(img[1] * scale))
            grid = downscale(img, tw, th)
        return _remember(self.scaled, key, grid, MAX_GRIDS)

    def tint(self, team):
        """Team color for names and scores: league data first, else the logo."""
        for rgb in (team.color, team.alt_color):
            if rgb and max(rgb) >= 60 and not min(rgb) > 200:
                return brighten(rgb)
        url = team.logo_url
        if url not in self.tints:
            img = self._image(url)
            if img is None:
                return DEFAULT_TINT   # not cached: the logo may turn up later
            self.tints[url] = brighten(primary_color(img))
        return self.tints[url]
