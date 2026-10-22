"""HTTP helpers.

Requests reuse keep-alive connections, pooled per scheme and host, so polls
skip the TCP and TLS handshakes, and ask for gzip. Thread-safe: a connection
serves one request at a time, and concurrent requests to a host each get
their own.
"""

import http.client
import json
import re
import select
import ssl
import threading
import time
import urllib.error
import urllib.request
import zlib
from urllib.parse import urljoin, urlsplit, urlunsplit

from .term import printable

UA ={"User-Agent": "scoreboard/1.0"}
HEADERS = {**UA, "Accept-Encoding": "gzip, deflate"}
MAX_REDIRECTS = 5
# Responses are read into memory, so cap them: the biggest real one (a
# whole NBA game's plays) is ~1 MB decoded, a tenth of that on the wire.
MAX_BODY = 8 << 20       # bytes received
MAX_DECODED = 32 << 20   # bytes after gzip or deflate
MAX_AGE = 3600           # cap on a Cache-Control max-age, in seconds
MAX_IDLE = 4      # idle connections kept per host
IDLE_SECS = 60    # older idle connections are dropped, not reused (wall time,
                  # so a laptop sleep counts: the monotonic clock stops in sleep)
# ESPN's $ref links are http://, but the same documents are served over
# https, so upgrade them and reuse the pooled TLS connection.
HTTPS_HOSTS = {"sports.core.api.espn.com"}
# How a reused connection fails when the server closed it while it was idle,
# or the network changed under it: it goes silent (TimeoutError), resets, or
# the route is gone (EHOSTUNREACH and the like are plain OSErrors). A fresh
# connection is tried once, so this can be broad.
STALE = (OSError, http.client.HTTPException)
# http.client can't use a proxy by itself; urllib can, so leave those to it.
PROXIED = any(k in ("http", "https") for k in urllib.request.getproxies())

_ssl = ssl.create_default_context()
_ssl.set_alpn_protocols(["http/1.1"])
_pool = {}        # (scheme, host) -> [(idle since, connection)], newest last
_lock = threading.Lock()


def _quiet(conn):
    """True if nothing has arrived on an idle connection. Anything that did
    (the server closing it, a 408, bytes past the last response) means it
    can't carry another request cleanly."""
    sock = conn.sock
    if sock is None or (isinstance(sock, ssl.SSLSocket) and sock.pending()):
        return False
    try:
        return not select.select([sock], [], [], 0)[0]
    except (OSError, ValueError):
        return False


def _checkout(key):
    """An idle connection to reuse, or None."""
    with _lock:
        idle = _pool.get(key) or []
        while idle:
            since, conn = idle.pop()
            if 0 <= time.time() - since < IDLE_SECS and _quiet(conn):
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
    now, expired = time.time(), []
    with _lock:
        # Expire every host's old connections, not just this one's, so a
        # host the app stopped using doesn't keep its sockets for good.
        for k, idle in list(_pool.items()):
            expired += [c for since, c in idle if not 0 <= now - since < IDLE_SECS]
            _pool[k] = [(since, c) for since, c in idle if 0 <= now - since < IDLE_SECS]
        idle = _pool.setdefault(key, [])
        if len(idle) < MAX_IDLE:
            idle.append((now, conn))
            conn = None
    for c in expired:
        c.close()
    if conn is not None:   # the pool is full
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
            body = _read(r)
        except Exception as e:
            conn.close()
            if reused and isinstance(e, STALE):
                _drop(key)
                conn = None   # went stale while idle: retry once on a new connection
                continue
            raise
        # http.client takes an interim response (103 Early Hints) for the
        # answer, leaving the real one unread: such a connection can't be reused.
        if r.will_close or r.status < 200:
            conn.close()
        else:
            _checkin(key, conn)
        return r, body


def _read(r):
    body = r.read(MAX_BODY + 1)
    if len(body) > MAX_BODY:
        raise ValueError("response too large")
    return body


def _inflate(body, wbits):
    d = zlib.decompressobj(wbits)
    out = d.decompress(body, MAX_DECODED) + (b"" if d.unconsumed_tail else d.flush())
    if d.unconsumed_tail or len(out) > MAX_DECODED:
        raise ValueError("response too large")
    if not d.eof:
        raise ValueError("truncated %s data" % ("gzip" if wbits > zlib.MAX_WBITS else "deflate"))
    return out


def _decode(body, encoding):
    encoding = (encoding or "").strip().lower()
    if encoding in ("gzip", "x-gzip"):
        return _inflate(body, 16 + zlib.MAX_WBITS)
    if encoding == "deflate":
        try:
            return _inflate(body, zlib.MAX_WBITS)
        except zlib.error:   # some servers send raw deflate, without the zlib wrapper
            return _inflate(body, -zlib.MAX_WBITS)
    return body


def _upgrade(url):
    """url with ESPN's http:// links made https://."""
    u = urlsplit(url)
    if u.scheme == "http" and u.netloc in HTTPS_HOSTS:
        return urlunsplit(u._replace(scheme="https"))
    return url


class _Redirects(urllib.request.HTTPRedirectHandler):
    """urllib's redirects (when a proxy is set) under the same rules as ours:
    ESPN's http links upgraded, only http(s), and never https to http."""
    max_redirections = MAX_REDIRECTS

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        newurl = _upgrade(newurl)
        scheme = urlsplit(newurl).scheme
        if scheme not in ("http", "https") or (req.type == "https" and scheme != "https"):
            raise urllib.error.HTTPError(newurl, code, "redirect to %s refused" % scheme, headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_Redirects)


def _open(url, timeout):
    """GET url, following redirects: (final response, decoded body).

    Raises urllib.error.HTTPError for non-2xx statuses, like urlopen.
    """
    url = _upgrade(url)   # first: through a proxy too, or ESPN's play links go out in the clear
    if PROXIED:
        req = urllib.request.Request(url, headers=HEADERS)
        with _opener.open(req, timeout=timeout) as r:
            return r, _decode(_read(r), r.headers.get("Content-Encoding"))
    for _ in range(MAX_REDIRECTS + 1):
        u = urlsplit(url)
        if u.scheme not in ("http", "https") or not u.netloc:
            raise ValueError("unsupported URL: %r" % url)
        r, body = _get(u.scheme, u.netloc, (u.path or "/") + ("?" + u.query if u.query else ""), timeout)
        location = r.headers.get("Location")
        if r.status in (301, 302, 303, 307, 308) and location:
            new = _upgrade(urljoin(url, location))
            if u.scheme == "https" and urlsplit(new).scheme != "https":
                raise urllib.error.HTTPError(url, r.status, "redirect from https refused", r.headers, None)
            url = new
            continue
        if not 200 <= r.status < 300:
            raise urllib.error.HTTPError(url, r.status, r.reason, r.headers, None)
        return r, _decode(body, r.headers.get("Content-Encoding"))
    raise urllib.error.HTTPError(url, r.status, "Too many redirects", r.headers, None)


def fetch(url, timeout=10):
    """GET url and return the body; raises on network errors and non-2xx statuses."""
    return _open(url, timeout)[1]


def _scrub(data):
    """Make every string in parsed JSON printable, in place: names, plays and
    labels all end up on screen, and none of them should move the cursor or
    retitle the window."""
    if isinstance(data, dict):
        items = data.items()
    elif isinstance(data, list):
        items = enumerate(data)
    else:
        return printable(data) if isinstance(data, str) else data
    for k, v in items:
        if isinstance(v, str):
            if not v.isprintable():
                data[k] = printable(v)
        elif isinstance(v, (dict, list)):
            _scrub(v)
    return data


def fetch_json(url, timeout=10):
    return _scrub(json.loads(fetch(url, timeout)))


def fetch_json_timed(url, timeout=10):
    """Fetch JSON plus how stale it is: (data, age, max_age) in seconds.

    The APIs sit behind CDN caches. The Age header says how long ago the
    cached copy was made, which is when its clock was read. age is None when
    the header is missing; what that means depends on the API.
    """
    r, body = _open(url, timeout)
    data = _scrub(json.loads(body))
    m = re.search(r"max-age=([0-9]+)", r.headers.get("Cache-Control") or "")
    max_age = min(int(m.group(1)), MAX_AGE) if m else 10
    # Age is whole seconds (RFC 9111); anything else (nan, -30, 1e400) is
    # ignored rather than allowed to date a snapshot in the future, or never.
    m = re.fullmatch(r"\s*([0-9]{1,9})\s*", r.headers.get("Age") or "")
    age = float(m.group(1)) if m else None
    return data, age, max_age
