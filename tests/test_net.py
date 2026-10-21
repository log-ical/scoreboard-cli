import gzip
import http.client
import threading
import time
import unittest
import urllib.error
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from tests.helpers import ROOT  # noqa: F401  (sets sys.path)
from scoreboard import net

BODY = b'{"a": 1}'


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"   # keep-alive
    disable_nagle_algorithm = True  # headers and body go out in separate writes

    def setup(self):
        super().setup()
        with self.server.lock:
            self.server.connections += 1

    def log_message(self, *args):
        pass

    def send(self, status=200, body=BODY, **headers):
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k.replace("_", "-"), v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path, _, query = self.path.partition("?")
        if path == "/plain":
            self.send()
        elif path == "/gzip":
            self.send(body=gzip.compress(BODY), Content_Encoding="gzip")
        elif path == "/deflate":
            self.send(body=zlib.compress(BODY), Content_Encoding="deflate")
        elif path == "/rawdeflate":
            c = zlib.compressobj(wbits=-zlib.MAX_WBITS)
            self.send(body=c.compress(BODY) + c.flush(), Content_Encoding="deflate")
        elif path == "/png":
            self.send(body=bytes(range(256)), Content_Type="image/png")
        elif path == "/chunked":
            self.send_response(200)
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for part in (BODY[:3], BODY[3:]):
                self.wfile.write(b"%x\r\n%s\r\n" % (len(part), part))
            self.wfile.write(b"0\r\n\r\n")
        elif path == "/timed":
            self.send(Age="7", Cache_Control="public, max-age=19")
        elif path == "/headers":
            self.send(body=("%s|%s" % (self.headers["User-Agent"], self.headers["Accept-Encoding"])).encode())
        elif path == "/redirect":
            self.send(307, b"", Location="http://%s:%d/gzip" % self.server.server_address)
        elif path == "/relative":
            self.send(302, b"moved", Location="plain?x=1")
        elif path == "/loop":
            self.send(302, b"", Location="/loop")
        elif path == "/missing":
            self.send(404, b"not here")
        elif path == "/close":
            self.send()
            self.close_connection = True   # without saying so: the client will pool it
        elif path == "/drop":
            self.close_connection = True   # hang up without a response
        elif path == "/silent":
            if getattr(self, "answered", False):
                time.sleep(0.5)       # gone quiet on this connection, as after a network change
                self.close_connection = True
            else:
                self.answered = True
                time.sleep(0.05)      # so concurrent first requests overlap
                self.send()
        elif path == "/slow":
            time.sleep(0.5)
            self.send()
        elif path == "/echo":
            time.sleep(0.01)
            self.send(body=query.encode())
        else:
            self.send(500, b"?")


class Server(ThreadingHTTPServer):
    block_on_close = False
    request_queue_size = 32   # the default 5 drops some of test_threads' connects (1 s SYN retry)

    def handle_error(self, request, client_address):
        pass   # e.g. writing to a client that timed out


class FetchTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = Server(("127.0.0.1", 0), Handler)
        cls.server.lock, cls.server.connections = threading.Lock(), 0
        threading.Thread(target=cls.server.serve_forever, args=(0.05,), daemon=True).start()
        cls.base = "http://127.0.0.1:%d" % cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.saved = net.IDLE_SECS, net.PROXIED

    def tearDown(self):
        net.IDLE_SECS, net.PROXIED = self.saved
        with net._lock:
            for idle in net._pool.values():
                for _, conn in idle:
                    conn.close()
            net._pool.clear()

    def connections(self):
        with self.server.lock:
            return self.server.connections

    def idle(self):
        return len(net._pool.get(("http", self.base[7:]), []))

    def test_bodies_and_encodings(self):
        for path in ("/plain", "/gzip", "/deflate", "/rawdeflate", "/chunked"):
            self.assertEqual(net.fetch(self.base + path), BODY, path)
        self.assertEqual(net.fetch(self.base + "/png"), bytes(range(256)))
        self.assertEqual(net.fetch_json(self.base + "/gzip"), {"a": 1})
        self.assertEqual(net.fetch(self.base + "/headers"), b"scoreboard/1.0|gzip, deflate")

    def test_reuses_connection(self):
        before = self.connections()
        for path in ("/plain", "/gzip", "/chunked", "/plain"):
            net.fetch(self.base + path)
        self.assertEqual(self.connections() - before, 1)
        self.assertEqual(self.idle(), 1)

    def test_idle_too_long_not_reused(self):
        net.IDLE_SECS = 0
        before = self.connections()
        net.fetch(self.base + "/plain")
        net.fetch(self.base + "/plain")
        self.assertEqual(self.connections() - before, 2)

    def test_timed(self):
        self.assertEqual(net.fetch_json_timed(self.base + "/timed"), ({"a": 1}, 7.0, 19))
        self.assertEqual(net.fetch_json_timed(self.base + "/plain"), ({"a": 1}, None, 10))

    def test_redirects(self):
        self.assertEqual(net.fetch_json(self.base + "/redirect"), {"a": 1})
        self.assertEqual(net.fetch(self.base + "/relative"), BODY)
        with self.assertRaises(urllib.error.HTTPError) as cm:
            net.fetch(self.base + "/loop")
        cm.exception.close()

    def test_error_status_raises_and_keeps_connection(self):
        before = self.connections()
        with self.assertRaises(urllib.error.HTTPError) as cm:
            net.fetch_json(self.base + "/missing")
        cm.exception.close()
        self.assertEqual(cm.exception.code, 404)
        self.assertEqual(net.fetch(self.base + "/plain"), BODY)
        self.assertEqual(self.connections() - before, 1)

    def test_stale_connection_retried_once(self):
        net.fetch(self.base + "/close")   # pooled, but the server hangs up
        self.assertEqual(self.idle(), 1)
        time.sleep(0.05)
        before = self.connections()
        self.assertEqual(net.fetch(self.base + "/plain"), BODY)
        self.assertEqual(self.connections() - before, 1)
        # A server that never answers fails on the fresh connection, too.
        with self.assertRaises(ConnectionError):
            net.fetch(self.base + "/drop")
        with self.assertRaises(ConnectionError):
            net.fetch(self.base + "/drop")

    def test_silent_reused_connection_retried_on_a_fresh_one(self):
        start = threading.Barrier(2)

        def first():
            start.wait()
            net.fetch(self.base + "/silent")
        threads = [threading.Thread(target=first) for _ in range(2)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        self.assertEqual(self.idle(), 2)
        # Both pooled connections have gone quiet: one timeout, then a fresh
        # connection, not a timeout for every dead connection in the pool.
        t = time.monotonic()
        self.assertEqual(net.fetch(self.base + "/silent", timeout=0.2), BODY)
        self.assertLess(time.monotonic() - t, 0.4)
        self.assertEqual(self.idle(), 1)

    def test_timeout(self):
        t = time.monotonic()
        with self.assertRaises(TimeoutError):
            net.fetch(self.base + "/slow", timeout=0.1)
        self.assertLess(time.monotonic() - t, 0.4)
        self.assertEqual(self.idle(), 0)   # a broken connection isn't pooled
        self.assertEqual(net.fetch(self.base + "/plain"), BODY)

    def test_threads(self):
        """Concurrent requests each get their own connection and response."""
        results, errors = {}, []
        start = threading.Barrier(8)

        def run(i):
            start.wait()
            try:
                for j in range(10):
                    results[i, j] = net.fetch(self.base + "/echo?%d-%d" % (i, j))
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=run, args=(i,)) for i in range(8)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        self.assertEqual(errors, [])
        self.assertEqual(results, {(i, j): b"%d-%d" % (i, j) for i in range(8) for j in range(10)})
        self.assertLessEqual(self.idle(), net.MAX_IDLE)

    def test_proxied_uses_urllib(self):
        net.PROXIED = True
        self.assertEqual(net.fetch(self.base + "/gzip"), BODY)
        self.assertEqual(net.fetch_json_timed(self.base + "/timed"), ({"a": 1}, 7.0, 19))
        with self.assertRaises(urllib.error.HTTPError) as cm:
            net.fetch(self.base + "/missing")
        cm.exception.close()
        self.assertEqual(self.idle(), 0)


class UpgradeTest(unittest.TestCase):
    def test_espn_core_upgraded_to_https(self):
        seen = []

        class Response:
            status, reason, headers = 200, "OK", http.client.HTTPMessage()

        def get(scheme, host, path, timeout):
            seen.append((scheme, host, path))
            return Response(), b"{}"

        saved, net._get = net._get, get
        try:
            net.fetch_json("http://sports.core.api.espn.com/v2/plays/1?lang=en&region=us")
            net.fetch_json("http://example.com/x")
        finally:
            net._get = saved
        self.assertEqual(seen, [("https", "sports.core.api.espn.com", "/v2/plays/1?lang=en&region=us"),
                                ("http", "example.com", "/x")])


if __name__ == "__main__":
    unittest.main()
