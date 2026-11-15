"""Free-API router: tiered failover, cooldowns, caps, CLI sidecar.

All backends here are fakes (scripted callables); no network involved.
"""

import datetime
import unittest

from pa import chatagent, db as dbmod
from pa.provider import (ProviderAuthError, ProviderError,
                         ProviderNotConfigured, ProviderRateLimit,
                         ProviderUnavailable)
from pa.router import Backend, RouterProvider
from pa.util import utcnow
from tests.helpers import TmpEnv


def _backend(name, kind="api", requires=(), caps=None,
             models=None, key_env=None, url_setting=None):
    return Backend(name, kind, "https://example.invalid/v1", requires,
                   models or {"smart": "m-smart", "fast": "m-fast"},
                   caps or {}, key_env=key_env, url_setting=url_setting)


class FakeApiClient:
    """Scripted stand-in for a ChatProvider."""
    def __init__(self, script=None):
        self.script = list(script or [])
        self.calls = []
        self.last_headers = {}

    def chat(self, messages, tools=None, max_completion_tokens=1024,
             timeout=None, tier=None):
        self.calls.append({"messages": messages, "tools": tools,
                           "timeout": timeout})
        item = self.script.pop(0) if self.script else \
            ({"role": "assistant", "content": "ok", "tool_calls": []},
             {"prompt_tokens": 5, "completion_tokens": 3})
        if isinstance(item, Exception):
            raise item
        return item


class FakeSessionClient:
    """Scripted stand-in for a SidecarSessionClient."""
    def __init__(self, script=None):
        self.script = list(script or [])
        self.calls = []

    def run(self, prompt, deadline_seconds=None, check_cancel=None):
        self.calls.append(prompt)
        item = self.script.pop(0) if self.script else {"text": "ok"}
        if isinstance(item, Exception):
            raise item
        return item


def ok_msg(text="fine"):
    return ({"role": "assistant", "content": text, "tool_calls": []},
            {"prompt_tokens": 11, "completion_tokens": 7})


class RouterBase(unittest.TestCase):
    def setUp(self):
        self.env = TmpEnv(PA_ROUTER_ENABLED="true")
        self.settings = self.env.settings

    def router(self, backends, orders=None, clients=None, env=None):
        """RouterProvider over fake backends/clients (real presets in
        _build are discarded — the test supplies its own)."""
        rp = RouterProvider(
            self.settings, env=env if env is not None else self.env.env,
            db_path=self.env.db_path,
            client_factory=lambda be, model: clients[be.name])
        rp.backends = {b.name: b for b in backends}
        names = [b.name for b in backends]
        rp.orders = orders or {"smart": names,
                               "fast": list(reversed(names))}
        return rp

    def conn(self):
        return self.env.conn()


class TestTierSelection(RouterBase):
    def test_smart_uses_smart_order(self):
        a, b = _backend("a"), _backend("b")
        c_a, c_b = FakeApiClient(), FakeApiClient()
        rp = self.router([a, b], clients={"a": c_a, "b": c_b})
        msg, _ = rp.chat([{"role": "user", "content": "hi"}],
                         tier="smart")
        self.assertEqual(msg["content"], "ok")
        self.assertEqual(len(c_a.calls), 1)
        self.assertEqual(len(c_b.calls), 0)

    def test_fast_uses_fast_order(self):
        a, b = _backend("a"), _backend("b")
        c_a, c_b = FakeApiClient(), FakeApiClient()
        rp = self.router([a, b], clients={"a": c_a, "b": c_b})
        rp.chat([{"role": "user", "content": "hi"}], tier="fast")
        self.assertEqual(len(c_b.calls), 1)   # fast order is reversed
        self.assertEqual(len(c_a.calls), 0)

    def test_fallthrough_to_other_tier(self):
        a, b = _backend("a"), _backend("b")
        c_a = FakeApiClient([ProviderUnavailable("down")])
        c_b = FakeApiClient()
        rp = self.router([a, b],
                         orders={"smart": ["a"], "fast": ["b"]},
                         clients={"a": c_a, "b": c_b})
        msg, _ = rp.chat([{"role": "user", "content": "hi"}],
                         tier="smart")
        self.assertEqual(msg["content"], "ok")  # fell through to fast b

    def test_tools_present_skips_sidecars(self):
        api = _backend("api")
        side = _backend("side", kind="sidecar",
                        url_setting="gemini_cli_url",
                        requires=("PA_GEMINI_CLI_TOKEN",))
        c_api, c_side = FakeApiClient(), FakeSessionClient()
        env = dict(self.env.env, PA_GEMINI_CLI_TOKEN="t")
        rp = self.router([side, api],
                         orders={"smart": ["side", "api"],
                                 "fast": ["side", "api"]},
                         clients={"side": c_side, "api": c_api}, env=env)
        rp.chat([{"role": "user", "content": "hi"}],
                tools=[{"type": "function", "function": {"name": "f"}}])
        self.assertEqual(len(c_api.calls), 1)   # sidecar skipped: tools
        self.assertEqual(len(c_side.calls), 0)


class TestFailover(RouterBase):
    def test_429_cools_and_fails_over(self):
        a, b = _backend("a"), _backend("b")
        c_a = FakeApiClient([ProviderRateLimit("429", retry_after=5)])
        c_b = FakeApiClient()
        rp = self.router([a, b], clients={"a": c_a, "b": c_b})
        msg, _ = rp.chat([{"role": "user", "content": "hi"}], tier="smart")
        self.assertEqual(msg["content"], "ok")
        cd = rp._cooldown("a")
        self.assertIsNotNone(cd)
        until = datetime.datetime.fromisoformat(cd["until"])
        self.assertTrue(4 < (until - utcnow()).total_seconds() < 60)

    def test_5xx_then_timeout_then_success(self):
        a, b, c = (_backend("a"), _backend("b"), _backend("c"))
        c_a = FakeApiClient([ProviderUnavailable("HTTP 500")])
        c_b = FakeApiClient([ProviderUnavailable("timed out")])
        c_c = FakeApiClient()
        rp = self.router([a, b, c],
                         clients={"a": c_a, "b": c_b, "c": c_c})
        msg, _ = rp.chat([{"role": "user", "content": "hi"}], tier="smart")
        self.assertEqual(msg["content"], "ok")
        for cl in (c_a, c_b, c_c):
            self.assertEqual(len(cl.calls), 1)

    def test_all_fail_raises_unavailable(self):
        a, b = _backend("a"), _backend("b")
        rp = self.router(
            [a, b],
            clients={"a": FakeApiClient([ProviderUnavailable("x"),
                                         ProviderUnavailable("x")]),
                     "b": FakeApiClient([ProviderUnavailable("y"),
                                         ProviderUnavailable("y")])})
        with self.assertRaises(ProviderUnavailable):
            rp.chat([{"role": "user", "content": "hi"}], tier="smart",
                    timeout=3)   # budget too small to wait out cooldowns

    def test_short_cooldown_waited_out_within_budget(self):
        # sole backend 402s with a tiny Retry-After: the call waits out
        # the cooldown inside its own budget and succeeds on rewalk.
        a = _backend("a")
        c_a = FakeApiClient([ProviderRateLimit("402", retry_after=0.2)])
        rp = self.router([a], clients={"a": c_a})
        msg, _ = rp.chat([{"role": "user", "content": "hi"}],
                         tier="smart", timeout=10)
        self.assertEqual(msg["content"], "ok")
        self.assertEqual(len(c_a.calls), 2)

    def test_retry_after_respected(self):
        a, b = _backend("a"), _backend("b")
        c_a = FakeApiClient([ProviderRateLimit("429", retry_after=120)])
        rp = self.router([a, b], clients={
            "a": c_a, "b": FakeApiClient()})
        rp.chat([{"role": "user", "content": "hi"}], tier="smart")
        rp.chat([{"role": "user", "content": "hi"}], tier="smart")
        # second call skipped 'a' entirely: still only one attempt made
        self.assertEqual(len(c_a.calls), 1)

    def test_auth_error_long_cooldown(self):
        a = _backend("a")
        rp = self.router([a], clients={
            "a": FakeApiClient([ProviderAuthError("bad key")])})
        with self.assertRaises(ProviderUnavailable):
            rp.chat([{"role": "user", "content": "hi"}], tier="smart")
        cd = rp._cooldown("a")
        until = datetime.datetime.fromisoformat(cd["until"])
        self.assertGreater((until - utcnow()).total_seconds(), 3600)

    def test_402_maps_to_rate_limit(self):
        # Pollinations (and OpenRouter on negative balance) answer 402 on
        # a spent free allowance; _RouterChatProvider maps it like a 429.
        from pa.netclient import HTTPError
        from pa.router import _RouterChatProvider
        p = _RouterChatProvider("http://127.0.0.1:1/v1", "m")
        with self.assertRaises(ProviderRateLimit) as cm:
            p._raise_for(HTTPError(402, {"retry-after": "7"}, b"{}"))
        self.assertEqual(cm.exception.retry_after, 7)


class TestCapsAndSkip(RouterBase):
    def test_daily_cap_skips_before_call(self):
        a, b = _backend("a", caps={"rpd": 5}), _backend("b")
        c_a, c_b = FakeApiClient(), FakeApiClient()
        rp = self.router([a, b], clients={"a": c_a, "b": c_b})
        day = utcnow().strftime("%Y-%m-%d")
        conn = self.conn()
        conn.execute(
            "INSERT INTO router_usage_days(backend, day, requests)"
            " VALUES('a', ?, 5)", (day,))
        conn.close()
        rp.chat([{"role": "user", "content": "hi"}], tier="smart")
        self.assertEqual(len(c_a.calls), 0)
        self.assertEqual(len(c_b.calls), 1)

    def test_minute_cap_skips(self):
        a, b = _backend("a", caps={"rpm": 2}), _backend("b")
        c_a, c_b = FakeApiClient(), FakeApiClient()
        rp = self.router([a, b], clients={"a": c_a, "b": c_b})
        minute = utcnow().strftime("%Y-%m-%dT%H:%M")
        conn = self.conn()
        conn.execute(
            "INSERT INTO router_usage_minutes(backend, minute, requests)"
            " VALUES('a', ?, 2)", (minute,))
        conn.close()
        rp.chat([{"role": "user", "content": "hi"}], tier="smart")
        self.assertEqual(len(c_a.calls), 0)

    def test_unset_key_never_called(self):
        a = _backend("a", requires=("SOME_MISSING_KEY",))
        b = _backend("b")
        c_a, c_b = FakeApiClient(), FakeApiClient()
        rp = self.router([a, b], clients={"a": c_a, "b": c_b})
        msg, _ = rp.chat([{"role": "user", "content": "hi"}], tier="smart")
        self.assertEqual(len(c_a.calls), 0)
        self.assertEqual(msg["content"], "ok")

    def test_nothing_configured_raises_not_configured(self):
        a = _backend("a", requires=("MISSING",))
        rp = self.router([a], clients={"a": FakeApiClient()})
        with self.assertRaises(ProviderNotConfigured):
            rp.chat([{"role": "user", "content": "hi"}], tier="smart")

    def test_deadline_zero_attempts_nothing(self):
        a = _backend("a")
        c_a = FakeApiClient()
        rp = self.router([a], clients={"a": c_a})
        with self.assertRaises(ProviderError):
            rp.chat([{"role": "user", "content": "hi"}], tier="smart",
                    timeout=0)
        self.assertEqual(len(c_a.calls), 0)

    def test_usage_recorded(self):
        a = _backend("a")
        c_a = FakeApiClient()
        rp = self.router([a], clients={"a": c_a})
        rp.chat([{"role": "user", "content": "hi"}], tier="fast")
        day = utcnow().strftime("%Y-%m-%d")
        conn = self.conn()
        row = conn.execute(
            "SELECT requests, tokens_in, tokens_out FROM router_usage_days"
            " WHERE backend='a' AND day=?", (day,)).fetchone()
        conn.close()
        self.assertEqual((row["requests"], row["tokens_in"],
                          row["tokens_out"]), (1, 5, 3))


class TestMalformedSwitch(RouterBase):
    def test_note_malformed_cools_last_backend(self):
        a, b = _backend("a"), _backend("b")
        c_a = FakeApiClient([
            ({"role": "assistant", "content": "not json",
              "tool_calls": []},
             {"prompt_tokens": 1, "completion_tokens": 1}),
            ({"role": "assistant", "content": "still bad",
              "tool_calls": []},
             {"prompt_tokens": 1, "completion_tokens": 1})])
        c_b = FakeApiClient()
        rp = self.router([a, b], clients={"a": c_a, "b": c_b})
        brain = chatagent.ChatBrain(rp, 64)
        # first call -> 'not json' (a); nudge -> 'still bad' (a);
        # note_malformed cools a; retry -> b 'ok'
        msgs = [{"role": "user", "content": "x"}]
        self.assertEqual(brain.complete(msgs, 30, "smart"), "not json")
        self.assertEqual(brain.complete(msgs, 30, "smart"), "still bad")
        brain.note_malformed()
        self.assertIsNotNone(rp._cooldown("a"))
        self.assertEqual(brain.complete(msgs, 30, "smart"), "ok")
        self.assertEqual(len(c_b.calls), 1)

    def test_engine_switches_backend_on_double_malformed(self):
        a, b = _backend("a"), _backend("b")
        garbage = lambda: ({"role": "assistant",
                            "content": "??no json here??",
                            "tool_calls": []},
                           {"prompt_tokens": 1, "completion_tokens": 1})
        c_a = FakeApiClient([garbage(), garbage()])
        c_b = FakeApiClient(
            [ok_msg("```json\n{\"final\": \"recovered\"}\n```")])
        rp = self.router([a, b], clients={"a": c_a, "b": c_b})
        e = chatagent.ChatEngine(self.settings, self.env.db_path,
                                 env=self.env.env,
                                 brain=chatagent.ChatBrain(rp, 64))
        out = e.run_turn("c", "hi")
        self.assertEqual(out["text"], "recovered")
        self.assertEqual(len(c_a.calls), 2)
        self.assertEqual(len(c_b.calls), 1)


class TestNativeToolCallFolding(RouterBase):
    def test_empty_content_tool_call_becomes_fenced_json(self):
        # weak-but-tool-native models (pollinations' openai) answer the
        # fenced protocol with a native tool_call and no content; the
        # brain folds it back into text the parser understands.
        a = _backend("a")
        c_a = FakeApiClient([
            ({"role": "assistant", "content": None,
              "tool_calls": [{"id": "c1", "type": "function",
                              "function": {"name": "web_search",
                                           "arguments": '{"q": "x"}'}}]},
             {"prompt_tokens": 1, "completion_tokens": 1})])
        rp = self.router([a], clients={"a": c_a})
        brain = chatagent.ChatBrain(rp, 64)
        text = brain.complete([{"role": "user", "content": "x"}],
                              30, "smart")
        self.assertEqual(chatagent.parse_action(text),
                         ("tool", "web_search", {"q": "x"}))


class TestSessionBackend(RouterBase):
    def test_sidecar_call_wraps_text(self):
        side = _backend("side", kind="sidecar",
                        url_setting="gemini_cli_url",
                        requires=("PA_GEMINI_CLI_TOKEN",))
        c = FakeSessionClient([{"text": "session answer"}])
        env = dict(self.env.env, PA_GEMINI_CLI_TOKEN="t")
        rp = self.router([side], clients={"side": c}, env=env)
        msg, usage = rp.chat([{"role": "system", "content": "s"},
                              {"role": "user", "content": "hi"}],
                             tier="smart")
        self.assertEqual(msg["content"], "session answer")
        self.assertEqual(msg["tool_calls"], [])
        self.assertTrue(c.calls[0].endswith("You:"))
        self.assertGreater(usage["prompt_tokens"], 0)


class TestMigrationIdempotent(RouterBase):
    def test_migrate_twice(self):
        conn = self.conn()
        v1 = dbmod.migrate(conn)
        v2 = dbmod.migrate(conn)
        self.assertEqual(v1, v2)
        for t in ("router_usage_minutes", "router_usage_days",
                  "router_cooldowns"):
            conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()
        conn.close()

    def test_provider_for_wiring(self):
        self.env.add_assistant(name="pal", host="router",
                               model="default")
        from pa.provider import provider_for
        p = provider_for(self.settings, "router", "default")
        self.assertIsInstance(p, RouterProvider)

    def test_router_not_allowlisted_when_disabled(self):
        env = TmpEnv()  # PA_ROUTER_ENABLED unset
        pairs = env.settings.provider_allowlist()
        self.assertNotIn("router", [h for h, _u, _m, _k in pairs])


class _ReasoningClient(FakeApiClient):
    """gpt-oss-style fake: hidden reasoning burns the completion budget,
    so a tiny max_completion_tokens comes back empty with
    finish_reason=length; a real budget answers 'ok'."""
    def chat(self, messages, tools=None, max_completion_tokens=1024,
             timeout=None, tier=None):
        self.calls.append(
            {"max_completion_tokens": max_completion_tokens})
        if max_completion_tokens < 64:
            self.last_finish_reason = "length"
            return ({"role": "assistant", "content": "",
                     "tool_calls": []},
                    {"prompt_tokens": 5,
                     "completion_tokens": max_completion_tokens})
        self.last_finish_reason = "stop"
        return ok_msg("ok")


class TestCheckBackend(RouterBase):
    def test_probe_budget_lets_reasoning_model_answer(self):
        # regression: the probe sent max_tokens=16, which gpt-oss models
        # (groq, cloudflare) spend entirely on hidden reasoning — a
        # working key reported [FAIL] "empty reply".
        c = _ReasoningClient()
        rp = self.router([_backend("reasoner")],
                         clients={"reasoner": c})
        ok, _ms, detail = rp.check_backend("reasoner")
        self.assertTrue(ok)
        self.assertEqual(detail, "ok")
        self.assertGreaterEqual(
            c.calls[0]["max_completion_tokens"], 400)

    def test_empty_reply_finish_reason_length_explained(self):
        c = FakeApiClient([
            ({"role": "assistant", "content": "", "tool_calls": []},
             {"prompt_tokens": 5, "completion_tokens": 512})])
        c.last_finish_reason = "length"
        rp = self.router([_backend("reasoner")],
                         clients={"reasoner": c})
        ok, _ms, detail = rp.check_backend("reasoner")
        self.assertFalse(ok)
        self.assertIn("finish_reason=length", detail)
        self.assertNotEqual(detail, "empty reply")

    def test_plain_empty_reply_still_plain_detail(self):
        c = FakeApiClient([
            ({"role": "assistant", "content": "", "tool_calls": []},
             {"prompt_tokens": 5, "completion_tokens": 0})])
        c.last_finish_reason = "stop"
        rp = self.router([_backend("a")], clients={"a": c})
        ok, _ms, detail = rp.check_backend("a")
        self.assertFalse(ok)
        self.assertEqual(detail, "empty reply")


class TestOrderConfig(RouterBase):
    def test_env_order_and_model_overrides(self):
        env = dict(self.env.env,
                   PA_ROUTER_ORDER="groq,pollinations",
                   PA_ROUTER_SMART_MODELS="groq=custom/model",
                   PA_ROUTER_CAP_GROQ_RPD="42")
        from pa.config import load_settings
        settings = load_settings(env)
        rp = RouterProvider(settings, env=env, db_path=self.env.db_path,
                            client_factory=lambda be, m: FakeApiClient())
        self.assertEqual(rp.orders["smart"], ["groq", "pollinations"])
        self.assertEqual(rp.orders["fast"], ["groq", "pollinations"])
        self.assertEqual(rp.backends["groq"].models["smart"],
                         "custom/model")
        self.assertEqual(rp.backends["groq"].caps["rpd"], 42)
        self.assertEqual(rp.backends["groq"].models["fast"],
                         "openai/gpt-oss-20b")


if __name__ == "__main__":
    unittest.main()
