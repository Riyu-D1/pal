"""Acceptance #7: egress blocks non-public targets and pins the vetted IP."""

import gzip
import io
import socket
import unittest

from pa.egress import (FetchBlocked, FetchFailed, guarded_fetch,
                       is_allowed_ip)
from tests.helpers import FakeDialer, FakeResolver, Fixture

PUBLIC_IP = "93.184.216.34"


class TestAddressPolicy(unittest.TestCase):
    def test_blocked_literals(self):
        for ip in ("127.0.0.1", "10.0.0.5", "169.254.169.254",
                   "172.16.4.4", "192.168.1.1", "100.64.1.1", "0.0.0.0",
                   "224.0.0.1", "240.1.2.3", "198.51.100.7",
                   "192.0.2.10"):
            self.assertFalse(is_allowed_ip(ip), ip)
        for ip in ("::1", "::ffff:127.0.0.1", "fd00::1", "fe80::1",
                   "ff02::1", "64:ff9b::8.8.8.8", "::ffff:10.1.2.3"):
            self.assertFalse(is_allowed_ip(ip), ip)

    def test_public_ok(self):
        for ip in ("93.184.216.34", "8.8.8.8", "1.1.1.1",
                   "2606:2800:220:1:248:1893:25c8:1946"):
            self.assertTrue(is_allowed_ip(ip), ip)


class FetchCase(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()
        self.dialer = FakeDialer("127.0.0.1", self.fx.port)
        self.resolver = FakeResolver({"public.test": [PUBLIC_IP]})

    def tearDown(self):
        self.fx.close()

    def fetch(self, url, **kw):
        kw.setdefault("resolver", self.resolver)
        kw.setdefault("dialer", self.dialer)
        kw.setdefault("timeout", 5.0)
        return guarded_fetch(url, **kw)

    def test_url_schemes_and_ports(self):
        for url in ("file:///etc/passwd", "gopher://x/", "ftp://x/"):
            with self.assertRaises(FetchBlocked, msg=url):
                self.fetch(url)
        with self.assertRaises(FetchBlocked):
            self.fetch("http://public.test:22/ssh")
        with self.assertRaises(FetchBlocked):
            self.fetch("https://public.test:8443/")
        with self.assertRaises(FetchBlocked):
            self.fetch("http://user:pass@public.test/")

    def test_private_ip_literal_blocked(self):
        for url in ("http://127.0.0.1/", "http://[::1]/",
                    "http://169.254.169.254/latest/meta-data",
                    "http://10.1.2.3/"):
            with self.assertRaises((FetchBlocked, FetchFailed), msg=url):
                self.fetch(url)

    def test_hostname_resolving_private_blocked(self):
        self.resolver.mapping["evil.test"] = ["10.0.0.5"]
        with self.assertRaises(FetchBlocked):
            self.fetch("http://evil.test/")
        # any bad answer in the set fails the whole request
        self.resolver.mapping["mixed.test"] = [PUBLIC_IP, "10.0.0.6"]
        with self.assertRaises(FetchBlocked):
            self.fetch("http://mixed.test/")

    def test_dns_rebinding_pinned_ip(self):
        # first answer public, second private - but we only resolve once
        calls = []

        def resolver(host, port):
            calls.append(host)
            ips = [PUBLIC_IP] if len(calls) == 1 else ["10.9.9.9"]
            return [(socket.AF_INET, socket.SOCK_STREAM,
                     socket.IPPROTO_TCP, "", (ip, port)) for ip in ips]
        self.fx.page("/p", b"<title>x</title><p>ok body</p>")
        res = self.fetch("http://public.test/p", resolver=resolver)
        self.assertEqual(res["status"], 200)
        self.assertEqual(calls, ["public.test"])  # exactly one resolution
        self.assertEqual(self.dialer.dialed, [(PUBLIC_IP, 80)])

    def test_connection_uses_vetted_ip(self):
        self.fx.page("/p", b"<title>x</title><p>data</p>")
        self.fetch("http://public.test/p")
        self.assertEqual(self.dialer.dialed[0][0], PUBLIC_IP)

    def test_sni_uses_hostname(self):
        seen = {}

        class FakeTLS:
            def wrap_socket(self, sock, server_hostname=None):
                seen["sni"] = server_hostname
                return sock  # fixture speaks plain http; we only verify SNI
        self.fx.page("/s", b"<title>s</title><p>x</p>")
        res = guarded_fetch("https://public.test/s", resolver=self.resolver,
                            dialer=self.dialer, tls_context=FakeTLS(),
                            timeout=5.0)
        self.assertEqual(seen["sni"], "public.test")
        self.assertEqual(res["status"], 200)

    def test_redirect_chain_vetted(self):
        self.fx.page("/r", b"", extra={"Location": "/next"},
                     code=302)
        self.fx.page("/next", b"<title>n</title><p>final</p>")
        res = self.fetch("http://public.test/r")
        self.assertEqual(res["final_url"], "http://public.test/next")
        self.assertIn("final", res["text"])

    def test_redirect_to_private_blocked(self):
        self.resolver.mapping["127.0.0.1"] = ["127.0.0.1"]
        self.fx.page("/r", b"", code=302,
                     extra={"Location": f"http://127.0.0.1:{self.fx.port}/"})
        with self.assertRaises(FetchBlocked):
            self.fetch("http://public.test/r")

    def test_too_many_redirects(self):
        for i in range(6):
            self.fx.page(f"/r{i}", b"", code=302,
                         extra={"Location": f"/r{i+1}"})
        with self.assertRaises(FetchBlocked):
            self.fetch("http://public.test/r0", max_redirects=3)

    def test_no_cookies_or_credentials_sent(self):
        seen = {}

        def capture(handler):
            seen["headers"] = dict(handler.headers)
            body = b"<title>t</title><p>ok</p>"
            handler.send_response(200)
            handler.send_header("Content-Type", "text/html")
            handler.send_header("Content-Length", str(len(body)))
            handler.end_headers()
            handler.wfile.write(body)
        self.fx.pages["/c"] = capture
        self.fetch("http://public.test/c")
        self.assertNotIn("cookie", {k.lower() for k in seen["headers"]})
        self.assertNotIn("authorization",
                         {k.lower() for k in seen["headers"]})

    def test_content_type_restrictions(self):
        self.fx.page("/img", b"\x89PNG", content_type="image/png")
        with self.assertRaises(FetchBlocked):
            self.fetch("http://public.test/img")

    def test_slow_loris_times_out(self):
        from tests.helpers import SlowBody
        self.fx.pages["/slow"] = (200, {"Content-Type": "text/plain"},
                                  SlowBody(delay=0.4, count=50))
        with self.assertRaises(FetchFailed):
            self.fetch("http://public.test/slow", timeout=1.5)

    def test_oversized_body_bounded(self):
        self.fx.page("/big", b"x" * (2 * 1024 * 1024),
                     content_type="text/plain")
        with self.assertRaises(FetchFailed):
            self.fetch("http://public.test/big", max_bytes=500_000)

    def test_gzip_bomb_bounded(self):
        payload = gzip.compress(b"a" * (10 * 1024 * 1024))
        self.fx.page("/bomb", payload, content_type="text/plain",
                     extra={"Content-Encoding": "gzip"})
        with self.assertRaises(FetchFailed):
            self.fetch("http://public.test/bomb", max_bytes=100_000)

    def test_normal_gzip_ok(self):
        payload = gzip.compress(b"decompressed content here")
        self.fx.page("/gz", payload, content_type="text/plain",
                     extra={"Content-Encoding": "gzip"})
        res = self.fetch("http://public.test/gz")
        self.assertEqual(res["text"], "decompressed content here")

    def test_chunked_body_decoded(self):
        """HTTP/1.1 servers without Content-Length send chunked bodies;
        the raw parser must strip chunk-size prefixes (wttr.in case)."""
        def chunked(handler):
            body = b"<title>t</title><p>chunked hello</p>"
            handler.send_response(200)
            handler.send_header("Content-Type", "text/html")
            handler.send_header("Transfer-Encoding", "chunked")
            handler.end_headers()
            # deliberately awkward chunk boundaries + an extension
            handler.wfile.write(b"4;ext=1\r\n<titl\r\n")
            handler.wfile.write(b"%x\r\n%s\r\n" % (len(body) - 4,
                                                   body[4:]))
            handler.wfile.write(b"0\r\n\r\n")
        self.fx.pages["/ch"] = chunked
        res = self.fetch("http://public.test/ch")
        self.assertIn("chunked hello", res["text"])
        self.assertEqual(res["title"], "t")
        self.assertNotIn("\r\n", res["text"][:10])

    def test_truncated_chunked_fails(self):
        def chunked(handler):
            handler.send_response(200)
            handler.send_header("Content-Type", "text/html")
            handler.send_header("Transfer-Encoding", "chunked")
            handler.end_headers()
            handler.wfile.write(b"ff\r\nshort\r\n")  # then close early
        self.fx.pages["/tr"] = chunked
        with self.assertRaises(FetchFailed):
            self.fetch("http://public.test/tr")


if __name__ == "__main__":
    unittest.main()
