"""Provider policy tests.

- a second provider's key never enables it
- 401 fails as auth error, no fallback
- 429 fails with the reset time
- host or model outside the allowlist is rejected
- generic LLM_* endpoint config; key only ever reaches the pinned host
- streaming + tools toggles
"""

import os
import unittest

from pa import worker as workmod
from pa.config import ConfigError, load_settings
from pa.provider import (ChatProvider, ProviderAuthError,
                         ProviderRateLimit, ProviderUnavailable,
                         provider_for)
from tests.helpers import Fixture, TmpEnv


class TestAllowlist(unittest.TestCase):
    def test_second_provider_key_never_enables(self):
        env = {
            "PA_ENV": "production",
            "OPENROUTER_API_KEY": "sk-or-fake-key-00000000",
            "GEMINI_API_KEY": "AIzaFAKEKEY000000000000",
            "GROQ_API_KEY": "gsk_fake",   # legacy key name ignored too
        }
        s = load_settings(env)
        self.assertEqual(s.provider_allowlist(), [])
        for host in ("openrouter.ai",
                     "generativelanguage.googleapis.com",
                     "api.groq.com"):
            with self.assertRaises(ConfigError):
                s.resolve_provider(host, "any-model")

    def test_llm_endpoint_is_the_only_allowlisted(self):
        s = load_settings({
            "PA_ENV": "production",
            "LLM_BASE_URL": "https://llm.example.com/v1",
            "LLM_MODEL": "my-model"})
        self.assertEqual(
            [(h, m, k) for h, _b, m, k in s.provider_allowlist()],
            [("llm.example.com", ["my-model"], "LLM_API_KEY")])
        with self.assertRaises(ConfigError):
            s.resolve_provider("evil.example.com", "my-model")
        with self.assertRaises(ConfigError):
            s.resolve_provider("llm.example.com", "some-other-model")

    def test_host_or_model_off_allowlist_rejected(self):
        s = load_settings({"PA_ENV": "production"})
        with self.assertRaises(ConfigError):
            s.resolve_provider("evil.example.com", "any-model")

    def test_dev_ollama_blocked_in_production(self):
        s = load_settings({"PA_ENV": "production",
                           "PA_OLLAMA_BASE_URL": "http://localhost:11434/v1"})
        with self.assertRaises(ConfigError):
            s.resolve_provider("localhost", "qwen2.5:1.5b")

    def test_http_to_public_host_refused(self):
        for url in ("http://llm.example.com/v1", "http://8.8.8.8/v1"):
            with self.assertRaises(ConfigError):
                load_settings({"PA_ENV": "production",
                               "LLM_BASE_URL": url, "LLM_MODEL": "m"})
        # but https to the same host is fine
        s = load_settings({"PA_ENV": "production",
                           "LLM_BASE_URL": "https://llm.example.com/v1",
                           "LLM_MODEL": "m"})
        self.assertTrue(s.llm_configured)
        # and plain http to loopback/private is intentional and allowed
        s = load_settings({"PA_ENV": "production",
                           "LLM_BASE_URL": "http://192.168.1.20:8080/v1",
                           "LLM_MODEL": "m"})
        self.assertTrue(s.llm_configured)

    def test_base_url_rejects_credentials_and_bad_scheme(self):
        for bad in ("https://user:pw@llm.example.com/v1",
                    "ftp://llm.example.com/v1", "not-a-url"):
            with self.assertRaises(ConfigError):
                load_settings({"LLM_BASE_URL": bad})
        with self.assertRaises(ConfigError):
            load_settings({"LLM_BASE_URL": "https://llm.example.com/v1"})
        # model missing when url set

    def test_dev_ollama_allowed_outside_production(self):
        s = load_settings({"PA_ENV": "test"})
        base, key_env = s.resolve_provider("localhost", "qwen2.5:1.5b")
        self.assertEqual(base, "http://localhost:11434/v1")
        self.assertIsNone(key_env)


class TestErrors(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()
        self.env = TmpEnv(PA_OLLAMA_BASE_URL=self.fx.base_url,
                          PA_RATE_MAX_RETRIES="2")
        self.aid = self.env.add_assistant()

    def tearDown(self):
        self.fx.close()

    def _task(self):
        conn = self.env.conn()
        return workmod.create_task(conn, self.aid, "t", "do x")

    def test_401_fails_auth_no_fallback(self):
        self.fx.push_chat((401, {"retry-after": "1"}, {"error": "bad key"}))
        conn = self.env.conn()
        tid = self._task()
        w = workmod.Worker(self.env.db_path, self.env.settings)
        w.run_once()
        t = conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        self.assertEqual(t["state"], "failed")
        self.assertEqual(t["error_kind"], "provider_auth")
        # exactly one attempt; nothing retried on a different provider
        self.assertEqual(len(self.fx.calls), 1)

    def test_403_also_auth(self):
        self.fx.push_chat(403)
        conn = self.env.conn()
        tid = self._task()
        workmod.Worker(self.env.db_path, self.env.settings).run_once()
        t = conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        self.assertEqual(t["error_kind"], "provider_auth")

    def test_429_bounded_retry_then_reset_time(self):
        for _ in range(5):
            self.fx.push_chat((429, {
                "retry-after": "0.01",
                "x-ratelimit-reset-tokens": "0.05s",
            }, {"error": "rate limited"}))
        conn = self.env.conn()
        tid = self._task()
        workmod.Worker(self.env.db_path, self.env.settings).run_once()
        t = conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        self.assertEqual(t["state"], "failed")
        self.assertEqual(t["error_kind"], "quota_exceeded")
        self.assertIsNotNone(t["reset_at"])
        self.assertIn("T", t["reset_at"])
        # bounded: initial + 2 retries
        self.assertEqual(len(self.fx.calls), 3)

    def test_429_then_success(self):
        self.fx.push_chat((429, {"retry-after": "0.01"},
                          {"error": "slow down"}))
        self.fx.push_chat(self.fx.chat_text("recovered"))
        conn = self.env.conn()
        tid = self._task()
        workmod.Worker(self.env.db_path, self.env.settings).run_once()
        t = conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        self.assertEqual(t["state"], "succeeded")
        self.assertEqual(len(self.fx.calls), 2)


class TestClientBehaviour(unittest.TestCase):
    def test_auth_error_mapping(self):
        fx = Fixture()
        fx.push_chat((401, {}, {"error": "nope"}))
        c = ChatProvider(fx.base_url, "m", api_key="k")
        with self.assertRaises(ProviderAuthError):
            c.chat([{"role": "user", "content": "hi"}])
        fx.close()

    def test_rate_limit_carries_reset(self):
        fx = Fixture()
        fx.push_chat((429, {"retry-after": "12",
                            "x-ratelimit-reset-requests": "30s"},
                      {"error": "rl"}))
        c = ChatProvider(fx.base_url, "m", api_key="k")
        with self.assertRaises(ProviderRateLimit) as cm:
            c.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(cm.exception.retry_after, 12.0)
        self.assertIsNotNone(cm.exception.reset_at)
        fx.close()

    def test_trickle_body_bounds_total_request_time(self):
        # A server that feeds the body a dribble at a time to keep the
        # socket alive (observed: a queueing free endpoint held a call
        # ~47 min) must still die at the request deadline — `timeout`
        # bounds the whole request, not each recv.
        import time
        from tests.helpers import SlowBody

        def trickle(handler):
            handler.send_response(200)
            handler.send_header("Content-Type", "application/json")
            handler.end_headers()
            SlowBody(delay=0.4, count=50).write(handler.wfile)

        fx = Fixture()
        fx.push_chat(trickle)
        c = ChatProvider(fx.base_url, "m", api_key="k")
        started = time.monotonic()
        with self.assertRaises(ProviderUnavailable):
            c.chat([{"role": "user", "content": "hi"}], timeout=1.5)
        self.assertLess(time.monotonic() - started, 10)
        fx.close()

    def test_completions_path_override(self):
        """Endpoints with a nonstandard route (e.g. Pollinations' /openai)
        are reachable via LLM_COMPLETIONS_PATH; empty posts to the base
        URL's own path verbatim."""
        fx = Fixture()
        c = ChatProvider(fx.web_base + "/openai", "openai",
                         completions_path="")
        msg, _usage = c.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(msg["content"], "done")
        self.assertEqual(fx.calls[0]["path"], "/openai")
        fx.close()

    def test_completions_path_config_validation(self):
        with self.assertRaises(ConfigError):
            load_settings({"LLM_BASE_URL": "https://x.example.com",
                           "LLM_MODEL": "m",
                           "LLM_COMPLETIONS_PATH": "no-slash"})
        s = load_settings({"LLM_BASE_URL": "https://x.example.com",
                           "LLM_MODEL": "m",
                           "LLM_COMPLETIONS_PATH": ""})
        self.assertEqual(s.llm_completions_path, "")

    def test_provider_for_uses_allowlist(self):
        env = TmpEnv(LLM_BASE_URL="https://llm.example.com/v1",
                     LLM_MODEL="m1")
        os.environ["LLM_API_KEY"] = "llm-secret-000000000000"
        try:
            p = provider_for(env.settings, "llm.example.com", "m1")
            self.assertEqual(p.base_url, "https://llm.example.com/v1")
            self.assertEqual(p.api_key, "llm-secret-000000000000")
        finally:
            del os.environ["LLM_API_KEY"]

    def test_key_only_attached_to_pinned_host(self):
        """The Authorization header is added only to requests against the
        configured base_url client — there is no code path that could
        send the key elsewhere (no redirects are followed)."""
        fx = Fixture()
        fx.push_chat(fx.chat_text("hi"))
        c = ChatProvider(fx.base_url, "m", api_key="sk-testkey-000000")
        c.chat([{"role": "user", "content": "hi"}])
        # the one recorded request carried the key to the pinned endpoint
        self.assertEqual(len(fx.auths), 1)
        self.assertEqual(fx.auths[0], "Bearer sk-testkey-000000")
        # a keyless client (anonymous endpoint) sends no auth header
        fx.auths.clear()
        fx.push_chat(fx.chat_text("hi"))
        c2 = ChatProvider(fx.base_url, "m")
        c2.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(fx.auths, [None])
        fx.close()

    def test_streaming_folds_sse(self):
        fx = Fixture()
        sse = (
            'data: {"choices":[{"delta":{"content":"Hel"},"index":0}]}\n\n'
            'data: {"choices":[{"delta":{"content":"lo"},"index":0}]}\n\n'
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,'
            '"id":"c1","function":{"name":"finish","arguments":'
            '"{\\"claims\\":[]}"}}]},"index":0}]}\n\n'
            'data: {"choices":[],"usage":{"prompt_tokens":3,'
            '"completion_tokens":2}}\n\n'
            "data: [DONE]\n\n")
        fx.push_chat((200, {"Content-Type": "text/event-stream"},
                      sse.encode()))
        c = ChatProvider(fx.base_url, "m", api_key="k", streaming=True)
        msg, usage = c.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(msg["content"], "Hello")
        self.assertEqual(msg["tool_calls"][0]["function"]["name"], "finish")
        self.assertEqual(usage["prompt_tokens"], 3)
        self.assertEqual(fx.calls[-1]["stream"], True)
        fx.close()

    def test_tools_disabled_never_sends_schemas(self):
        fx = Fixture()
        fx.push_chat(fx.chat_text("plain answer"))
        c = ChatProvider(fx.base_url, "m", api_key="k",
                         tools_enabled=False)
        msg, _u = c.chat([{"role": "user", "content": "hi"}],
                         tools=[{"type": "function", "function":
                                 {"name": "finish", "parameters": {}}}])
        body = fx.calls[-1]
        self.assertNotIn("tools", body)
        self.assertNotIn("tool_choice", body)
        self.assertEqual(msg["content"], "plain answer")
        fx.close()


if __name__ == "__main__":
    unittest.main()
