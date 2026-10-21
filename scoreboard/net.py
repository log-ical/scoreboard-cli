"""HTTP helpers."""

import json
import re
import urllib.request

UA = {"User-Agent": "scoreboard/1.0"}


def fetch(url, timeout=10):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def fetch_json(url, timeout=10):
    return json.loads(fetch(url, timeout))


def fetch_json_timed(url, timeout=10):
    """Fetch JSON plus how stale it is: (data, age, max_age) in seconds.

    The APIs sit behind CDN caches. The Age header says how long ago the
    cached copy was made, which is when its clock was read. age is None when
    the header is missing; what that means depends on the API.
    """
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read())
        m = re.search(r"max-age=(\d+)", r.headers.get("Cache-Control") or "")
        max_age = int(m.group(1)) if m else 10
        try:
            age = float(r.headers["Age"])
        except (KeyError, TypeError, ValueError):
            age = None
    return data, age, max_age
