"""Acceptance #6: token budget and pacing."""

import unittest

from pa.pacer import Pacer, QuotaExhausted
from tests.helpers import Fixture, TmpEnv


class FakeClock:
    def __init__(self):
        self.t = 0.0
        self.slept = []

    def now(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


class TestMinuteBucket(unittest.TestCase):
    def setUp(self):
        self.env = TmpEnv(PA_TPM_LIMIT="1000", PA_RPM_LIMIT="2",
                          PA_MAX_COMPLETION_TOKENS="300")
        self.conn = self.env.conn()
        self.clock = FakeClock()
        self.pacer = Pacer(self.env.settings, self.conn,
                           now=self.clock.now, sleep=self.clock.sleep)

    def test_back_to_back_calls_delayed_when_over_minute_total(self):
        # each call reserves ~est+300 tokens / 1 request
        self.pacer.acquire(600, 300)
        # second call would take the minute total to 1200+ > 1000 tpm
        # and rpm 2 already hit -> it must wait, not send
        self.clock.t = 5.0
        self.pacer.acquire(600, 300)
        self.assertTrue(self.clock.slept, "second call was not delayed")
        self.assertGreaterEqual(sum(self.clock.slept), 50.0)

    def test_headers_tighten_bucket(self):
        # headers say remaining-tokens is almost gone
        self.pacer.observe_headers({
            "x-ratelimit-remaining-tokens": "10",
            "x-ratelimit-remaining-requests": "1"})
        self.clock.t = 5.0
        self.pacer.acquire(100, 100)
        self.assertTrue(self.clock.slept)


class TestDailyCeiling(unittest.TestCase):
    def test_stops_before_daily_request_ceiling(self):
        env = TmpEnv(PA_DAILY_REQUEST_LIMIT="2")
        conn = env.conn()
        p = Pacer(env.settings, conn)
        p.acquire(10, 10)
        p.record_usage({"prompt_tokens": 5, "completion_tokens": 5})
        p.acquire(10, 10)
        p.record_usage({"prompt_tokens": 5, "completion_tokens": 5})
        with self.assertRaises(QuotaExhausted) as cm:
            p.acquire(10, 10)
        self.assertIn("T", cm.exception.reset_at)

    def test_daily_token_ceiling(self):
        env = TmpEnv(PA_DAILY_TOKEN_LIMIT="1000")
        conn = env.conn()
        p = Pacer(env.settings, conn)
        p.record_usage({"prompt_tokens": 400, "completion_tokens": 400})
        with self.assertRaises(QuotaExhausted):
            p.acquire(300, 300)


class TestOverBudgetPrompt(unittest.TestCase):
    def test_over_budget_task_is_rejected(self):
        fx = Fixture()
        env = TmpEnv(PA_OLLAMA_BASE_URL=fx.base_url,
                     PA_MAX_INPUT_TOKENS="60")
        aid = env.add_assistant(instructions="x" * 500)
        conn = env.conn()
        from pa import worker as workmod
        tid = workmod.create_task(conn, aid, "t", "do something sizeable")
        workmod.Worker(env.db_path, env.settings).run_once()
        t = conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        self.assertEqual(t["state"], "failed")
        self.assertIn(t["error_kind"], ("over_budget",))
        self.assertEqual(len(fx.calls), 0)
        fx.close()


if __name__ == "__main__":
    unittest.main()
