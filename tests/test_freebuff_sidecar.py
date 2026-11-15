"""Freebuff sidecar + remote provider tests.

Deterministic: a real in-thread sidecar on 127.0.0.1 with an injected
stub provider — no CLI, no tmux, no network beyond loopback. Fake
success is NOT live success.
"""

import json
import threading
import unittest
import urllib.request

from pa.config import ConfigError, load_settings
from pa.freebuff_sidecar import (FreebuffRemoteProvider, _Server,
                                 make_handler)
from pa.freebuff_session import freebuff_provider_for
from pa.provider import (ProviderAuthError, ProviderNotConfigured,
                         ProviderRateLimit, ProviderUnavailable)
from tests.helpers import TmpEnv

TOKEN = "test-token-123"


def _sidecar(run, token=TOKEN):
    """Start a real sidecar on an ephemeral port; returns (url, srv)."""
    srv = _Server(("127.0.0.1", 0),
                  make_handler(run, token, lambda: "0.2.36"))
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return f"http://127.0.0.1:{srv.server_address[1]}", srv


def _close(srv):
    srv.shutdown()
    srv.server_close()


def _ok_run(prompt, deadline_seconds=None, check_cancel=None):
    return {"text": "sidecar answer", "reasoning": None,
            "meta": {"run_id": "r1"}}


class TestSidecarServer(unittest.TestCase):
    def setUp(self):
        self.url, self.srv = _sidecar(_ok_run)
        self.addCleanup(_close, self.srv)

    def _post(self, path, body, token=TOKEN):
        req = urllib.request.Request(
            self.url + path, data=json.dumps(body).encode(),
            headers={"x-pa-freebuff-token": token},
            method="POST")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_healthz_requires_token(self):
        req = urllib.request.Request(self.url + "/healthz")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(ctx.exception.code, 401)

    def test_healthz_ok(self):
        req = urllib.request.Request(
            self.url + "/healthz",
            headers={"x-pa-freebuff-token": TOKEN})
        with urllib.request.urlopen(req, timeout=5) as r:
            body = json.loads(r.read())
        self.assertTrue(body["ok"])
        self.assertEqual(body["cli"], "0.2.36")

    def test_run_roundtrip(self):
        status, body = self._post("/run", {"prompt": "hello"})
        self.assertEqual(status, 200)
        self.assertEqual(body["text"], "sidecar answer")
        self.assertEqual(body["meta"]["run_id"], "r1")

    def test_run_bad_token(self):
        status, _ = self._post("/run", {"prompt": "x"}, token="nope")
        self.assertEqual(status, 401)

    def test_run_malformed(self):
        status, body = self._post("/run", {"prompt": ""})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["kind"], "bad_request")

    def test_provider_error_serialized(self):
        def _raise(p, d, c):
            raise ProviderRateLimit("limit hit", reset_at="2099-01-01",
                                    retry_after=42)
        url, srv = _sidecar(_raise)
        self.addCleanup(_close, srv)
        status, body = self._post_at(url, {"prompt": "x"})
        self.assertEqual(status, 429)
        self.assertEqual(body["error"]["kind"], "quota_exceeded")
        self.assertEqual(body["error"]["retry_after"], 42)

    def _post_at(self, url, body, token=TOKEN):
        req = urllib.request.Request(
            url + "/run", data=json.dumps(body).encode(),
            headers={"x-pa-freebuff-token": token}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())


class TestRemoteProvider(unittest.TestCase):
    def _remote(self, run, token=TOKEN):
        url, srv = _sidecar(run)
        self.addCleanup(_close, srv)
        return FreebuffRemoteProvider(url, token)

    def test_happy_path(self):
        p = self._remote(_ok_run)
        out = p.run("do a thing")
        self.assertEqual(out["text"], "sidecar answer")
        self.assertEqual(out["meta"]["via"], "sidecar")
        self.assertEqual(p.kind, "agent_session")
        self.assertFalse(p.tools_enabled)

    def test_bad_token_maps_to_auth(self):
        p = self._remote(_ok_run, token="wrong")
        with self.assertRaises(ProviderAuthError):
            p.run("x")

    def test_rate_limit_maps(self):
        def _raise(prompt, d, c):
            raise ProviderRateLimit("quota gone", reset_at="2099-01-01",
                                    retry_after=60)
        p = self._remote(_raise)
        with self.assertRaises(ProviderRateLimit) as ctx:
            p.run("x")
        self.assertEqual(ctx.exception.retry_after, 60)

    def test_not_configured_maps(self):
        def _raise(prompt, d, c):
            raise ProviderNotConfigured("not signed in on sidecar host")
        p = self._remote(_raise)
        with self.assertRaises(ProviderNotConfigured):
            p.run("x")

    def test_unreachable_maps_to_unavailable(self):
        p = FreebuffRemoteProvider("http://127.0.0.1:1", TOKEN)
        with self.assertRaises(ProviderUnavailable):
            p.run("x")

    def test_healthz_check(self):
        p = self._remote(_ok_run)
        checks = p.check_installation()
        self.assertTrue(checks[0][1])


class TestRemoteConfig(unittest.TestCase):
    def test_remote_url_selects_remote_provider(self):
        env = TmpEnv(PA_FREEBUFF_ENABLED="true",
                     PA_FREEBUFF_REMOTE_URL="http://127.0.0.1:8777",
                     PA_FREEBUFF_REMOTE_TOKEN="t")
        p = freebuff_provider_for(env.settings)
        self.assertIsInstance(p, FreebuffRemoteProvider)

    def test_remote_url_requires_token(self):
        with self.assertRaises(ConfigError):
            TmpEnv(PA_FREEBUFF_ENABLED="true",
                   PA_FREEBUFF_REMOTE_URL="http://127.0.0.1:8777")

    def test_remote_http_public_rejected(self):
        with self.assertRaises(ConfigError):
            TmpEnv(PA_FREEBUFF_ENABLED="true",
                   PA_FREEBUFF_REMOTE_URL="http://8.8.8.8:8777",
                   PA_FREEBUFF_REMOTE_TOKEN="t")

    def test_remote_https_public_ok(self):
        env = TmpEnv(PA_FREEBUFF_ENABLED="true",
                     PA_FREEBUFF_REMOTE_URL="https://fb.example.net",
                     PA_FREEBUFF_REMOTE_TOKEN="t")
        p = freebuff_provider_for(env.settings)
        self.assertIsInstance(p, FreebuffRemoteProvider)

    def test_no_remote_url_gives_local(self):
        env = TmpEnv(PA_FREEBUFF_ENABLED="true")
        p = freebuff_provider_for(env.settings)
        self.assertNotIsInstance(p, FreebuffRemoteProvider)


if __name__ == "__main__":
    unittest.main()
