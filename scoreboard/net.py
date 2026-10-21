"""HTTP helpers.

Requests reuse keep-alive connections, pooled per scheme and host, so polls
skip the TCP and TLS handshakes, and ask for gzip. Thread-safe: a connection
serves one request at a time, and concurrent requests to a host each get
their own.
"""

import gzip
import http.client
import json
import re
import ssl
import threading
import time
import urllib.error
import urllib.request
import zlib
from urllib.parse import urljoin, urlsplit

UA = {"User-Agent": "scoreboard/1.0"}
HEADERS = {**UA, "Accept-Encoding": "gzip, deflate"}
MAX_REDIRECTS = 5
MAX_IDLE = 4      # idle connections kept per host
IDLE_SECS = 60    # older idle connections are dropped, not reused (wall time,
                  # so a laptop sleep counts: the monotonic clock stops in sleep)
# ESPN's $ref links are http://, but the same documents are served over
# https, so upgrade them and reuse the pooled TLS connection.
HTTPS_HOSTS = {"sports.core.api.espn.com"}
# How a reused connection fails when the server closed it while it was idle,
# or the network changed under it (then it just goes silent: TimeoutError).
STALE = (ConnectionError, http.client.BadStatusLine, http.client.IncompleteRead, ssl.SSLError,
         TimeoutError)
# http.client can't use a proxy by itself; urllib can, so leave those to it.
PROXIED = any(k in ("http", "https") for k in urllib.request.getproxies())

_ssl = ssl.create_default_context()
_ssl.set_alpn_protocols(["http/1.1"])
_pool = {}        # (scheme, host) -> [(idle since, connection)], newest last
_lock = threading.Lock()


def _checkout(key):
    """An idle connection to reuse, or None."""
    with _lock:
        idle = _pool.get(key) or []
        while idle:
            since, conn = idle.pop()
            if 0 <= time.time() - since < IDLE_SECS:
                return conn
            conn.close()
    return None


def _drop(key):
    """Close a host's idle connections: when one has gone bad, so have they."""
    with _lock:
        idle = _pool.pop(key, [])
    for _, conn in idle:
        conn.close()


def _checkin(key, conn):
    with _lock:
        idle = _pool.setdefault(key, [])
        if len(idle) < MAX_IDLE:
            idle.append((time.time(), conn))
            return
    conn.close()


def _get(scheme, host, path, timeout):
    """One GET with the body read in full: (response, raw body)."""
    key = (scheme, host)
    conn = _checkout(key)
    while True:
        reused = conn is not None
        if reused:
            conn.timeout = timeout
            conn.sock.settimeout(timeout)
        elif scheme == "https":
            conn = http.client.HTTPSConnection(host, timeout=timeout, context=_ssl)
        else:
            conn = http.client.HTTPConnection(host, timeout=timeout)
        try:
            conn.request("GET", path, headers=HEADERS)
            r = conn.getresponse()
            body = r.read()
        except Exception as e:
            conn.close()
            if reused and isinstance(e, STALE):
                _drop(key)
                conn = None   # went stale while idle: retry once on a new connection
                continue
            raise
        if r.will_close:
            conn.close()
        else:
            _checkin(key, conn)
        return r, body


def _decode(body, encoding):
    encoding = (encoding or "").strip().lower()
    if encoding in ("gzip", "x-gzip"):
        return gzip.decompress(body)
    if encoding == "deflate":
        try:
            return zlib.decompress(body)
        except zlib.error:   # some servers send raw deflate, without the zlib wrapper
            return zlib.decompress(body, -zlib.MAX_WBITS)
    return body


def _open(url, timeout):
    """GET url, following redirects: (final response, decoded body).

    Raises urllib.error.HTTPError for non-2xx statuses, like urlopen.
    """
    if PROXIED:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r, _decode(r.read(), r.headers.get("Content-Encoding"))
    for _ in range(MAX_REDIRECTS + 1):
        u = urlsplit(url)
        scheme = "https" if u.scheme == "http" and u.netloc in HTTPS_HOSTS else u.scheme
        if scheme not in ("http", "https") or not u.netloc:
            raise ValueError("unsupported URL: %r" % url)
        r, body = _get(scheme, u.netloc, (u.path or "/") + ("?" + u.query if u.query else ""), timeout)
        location = r.headers.get("Location")
        if r.status in (301, 302, 303, 307, 308) and location:
            url = urljoin(url, location)
            continue
        if not 200 <= r.status < 300:
            raise urllib.error.HTTPError(url, r.status, r.reason, r.headers, None)
        return r, _decode(body, r.headers.get("Content-Encoding"))
    raise urllib.error.HTTPError(url, r.status, "Too many redirects", r.headers, None)


def fetch(url, timeout=10):
    """GET url and return the body; raises on network errors and non-2xx statuses."""
    return _open(url, timeout)[1]


def fetch_json(url, timeout=10):
    return json.loads(fetch(url, timeout))


def fetch_json_timed(url, timeout=10):
    """Fetch JSON plus how stale it is: (data, age, max_age) in seconds.

    The APIs sit behind CDN caches. The Age header says how long ago the
    cached copy was made, which is when its clock was read. age is None when
    the header is missing; what that means depends on the API.
    """
    r, body = _open(url, timeout)
    data = json.loads(body)
    m = re.search(r"max-age=(\d+)", r.headers.get("Cache-Control") or "")
    max_age = int(m.group(1)) if m else 10
    try:
        age = float(r.headers["Age"])
    except (KeyError, TypeError, ValueError):
        age = None
    return data, age, max_age
