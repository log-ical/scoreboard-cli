"""Team logos (downloaded once, cached on disk) and team tint colors."""

import hashlib
import os

from .net import fetch
from .png import crop_transparent, decode_png, downscale, primary_color

CACHE_DIR = os.path.join(os.path.expanduser("~"), ".cache", "scoreboard", "logos")


def brighten(rgb):
    """Lift dark colors (e.g. navy) so text in that color stays readable."""
    peak = max(rgb)
    if peak >= 170:
        return tuple(rgb)
    if peak == 0:
        return (200, 200, 200)
    return tuple(min(255, int(c * 170 / peak) + 40) for c in rgb)


class LogoStore:
    def __init__(self):
        self.images = {}   # url -> decoded image or None
        self.scaled = {}   # (url, w, h) -> pixel grid
        self.tints = {}    # url -> rgb

    def _load(self, url):
        if url in self.images:
            return self.images[url]
        img = None
        if url:
            path = os.path.join(CACHE_DIR, hashlib.md5(url.encode()).hexdigest()[:16] + ".png")
            try:
                if not os.path.exists(path):
                    os.makedirs(CACHE_DIR, exist_ok=True)
                    data = fetch(url)
                    with open(path, "wb") as f:
                        f.write(data)
                with open(path, "rb") as f:
                    img = crop_transparent(decode_png(f.read()))
            except Exception:
                img = None
        self.images[url] = img
        return img

    def grid(self, url, box_w, box_h, aspect=1.0):
        """Logo scaled to fit inside box_w x box_h pixels, keeping its aspect
        on screen, where each pixel is `aspect` times as wide as it is tall."""
        key = (url, box_w, box_h, aspect)
        if key not in self.scaled:
            img = self._load(url)
            grid = None
            if img:
                scale = min(box_w * aspect / img[0], box_h / img[1])
                tw = max(1, round(img[0] * scale / aspect))
                th = max(2, round(img[1] * scale))
                grid = downscale(img, tw, th)
            self.scaled[key] = grid
        return self.scaled[key]

    def tint(self, team):
        """Team color for names and scores: league data first, else the logo."""
        for rgb in (team.color, team.alt_color):
            if rgb and max(rgb) >= 60 and not min(rgb) > 200:
                return brighten(rgb)
        if team.logo_url not in self.tints:
            img = self._load(team.logo_url)
            self.tints[team.logo_url] = brighten(primary_color(img)) if img else (220, 220, 220)
        return self.tints[team.logo_url]
