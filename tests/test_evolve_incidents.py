"""Evolve item 1: incident table, record(), and the capture hooks.

Hooks are exercised with fake brains/providers — no network. The
recorder itself must never change the turn's outcome.
"""

import json
import unittest
from unittest import mock

from pa import chatagent, worker as workmod
from pa.evolve import incidents
from pa.provider import ProviderNotConfigured, ProviderRateLimit
from pa.router import Backend, RouterProvider
from tests.helpers import TmpEnv
from tests.test_router import FakeApiClient


def fenced(obj):
    return "```json\n" + json.dumps(obj) + "\n```"


def _rows(conn):
    return conn.execute(
        "SELECT * FROM incidents ORDER BY id").fetchall()


class TestRecord(unittest.TestCase):
    def setUp(self):
        self.env = TmpEnv()

    def test_record_writes_one_row(self):
        conn = self.env.conn()
        iid = incidents.record(conn, "chat", "exception",
                               summary="boom", detail="trace: boom",
                               channel_id="c1", tool="x")
        conn.close()
        self.assertIsNotNone(iid)
        rows = _rows(self.env.conn())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source"], "chat")
        self.assertEqual(rows[0]["kind"], "exception")
        self.assertEqual(rows[0]["channel_id"], "c1")

    def test_no_table_is_a_clean_noop(self):
        conn = self.env.conn()
        conn.execute("DROP TABLE incidents")
        conn.commit()
        self.assertIsNone(incidents.record(
            conn, "chat", "exception", summary="boom"))
        conn.close()

    def test_fingerprints_cluster_same_failure(self):
        a = incidents.fingerprint(
            "tool_error", "weather: request 1205 failed")
        b = incidents.fingerprint(
            "tool_error", "weather: request 9999 failed")
        c = incidents.fingerprint("tool_error", "weather: auth failed")
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)

    def test_secret_in_detail_is_scrubbed(self):
        conn = self.env.conn()
        incidents.record(
            conn, "chat", "exception", summary="call failed",
            detail="key sk-abcdef0123456789\n"
                   "ghp_abcdef0123456789abcdef0123\n"
                   "xoxb-123456789012-abcdefghijkl\n"
                   "Bearer ya29.abcdef0123456789\n"
                   "-----BEGIN PRIVATE KEY-----\n"
                   "body")
        row = _rows(conn)[0]
        conn.close()
        self.assertNotIn("abcdef0123456789", row["detail"])
        self.assertNotIn("ghp_", row["detail"])
        self.assertNotIn("xoxb-", row["detail"])
        self.assertNotIn("ya29.", row["detail"])
        self.assertNotIn("PRIVATE KEY", row["detail"])
        self.assertIn("[redacted]", row["detail"])

    def test_never_raises_on_bad_input(self):
        conn = self.env.conn()
        self.assertIsNotNone(incidents.record(
            conn, None, None, summary=None, detail=None))
        conn.close()


class TestChatHooks(unittest.TestCase):
    def setUp(self):
        self.env = TmpEnv(PA_CHAT_MAX_STEPS="4")
        self.env.add_assistant(name="pal", tools=[])

    def _engine(self, script):
        def brain(messages):
            return script.pop(0) if script else fenced({"final": "d"})
        return chatagent.ChatEngine(self.env.settings, env=self.env.env,
                                    brain=brain)

    def test_tool_error_records_incident(self):
        eng = self._engine([
            fenced({"tool": "nonexistent_tool", "args": {}}),
            fenced({"final": "couldn't do that"}),
        ])
        out = eng.run_turn("c1", "try it")
        self.assertEqual(out["text"], "couldn't do that")
        rows = _rows(self.env.conn())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source"], "chat")
        self.assertEqual(rows[0]["kind"], "tool_error")
        self.assertEqual(rows[0]["tool"], "nonexistent_tool")
        self.assertEqual(rows[0]["channel_id"], "c1")

    def test_control_flow_results_are_not_incidents(self):
        """Approval-gated results (pending/not_approved/...) are normal
        turn flow, not failures."""
        eng = self._engine([
            fenced({"tool": "owner_fact",
                    "args": {"key": "likes", "value": "tea"}}),
            fenced({"final": "ok"}),
        ])
        eng.run_turn("c1", "remember I like tea")
        rows = _rows(self.env.conn())
        self.assertEqual(rows, [])

    def test_turn_exception_records_and_reply_unchanged(self):
        eng = chatagent.ChatEngine(self.env.settings, env=self.env.env,
                                   brain=lambda m: 1 / 0)
        out = eng.run_turn("c1", "hi")
        self.assertIn("internal error", out["text"])
        rows = _rows(self.env.conn())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "exception")

    def test_broken_recorder_never_changes_the_turn(self):
        eng = chatagent.ChatEngine(self.env.settings, env=self.env.env,
                                   brain=lambda m: 1 / 0)
        with mock.patch.object(incidents, "record",
                               side_effect=RuntimeError("db gone")):
            out = eng.run_turn("c1", "hi")
        self.assertIn("internal error", out["text"])

    def test_not_configured_tool_records_incident(self):
        # read_url needs the (disabled) computer service
        eng = self._engine([
            fenced({"tool": "read_url",
                    "args": {"url": "https://example.com/x"}}),
            fenced({"final": "no browser"}),
        ])
        eng.run_turn("c1", "read that page")
        rows = _rows(self.env.conn())
        kinds = [r["kind"] for r in rows]
        self.assertIn("not_configured", kinds)


class TestWorkerHook(unittest.TestCase):
    def test_failed_task_records_incident(self):
        env = TmpEnv()
        aid = env.add_assistant()
        conn = env.conn()
        tid = workmod.create_task(conn, aid, "t", "do it")

        def bad_factory(assistant):
            raise ProviderNotConfigured("no provider for pal")
        w = workmod.Worker(env.db_path, env.settings,
                           provider_factory=bad_factory)
        w.run_once()
        rows = _rows(conn)
        conn.close()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source"], "worker")
        self.assertEqual(rows[0]["kind"], "not_configured")
        self.assertEqual(rows[0]["task_id"], tid)


class TestRouterHook(unittest.TestCase):
    def test_all_backends_failed_records_incident(self):
        env = TmpEnv(PA_ROUTER_ENABLED="true")
        be = Backend("fake1", "api", "https://example.invalid/v1", (),
                     {"smart": "m", "fast": "m"}, {})
        rp = RouterProvider(
            env.settings, env=env.env, db_path=env.db_path,
            client_factory=lambda b, m: FakeApiClient(
                [ProviderRateLimit("429")] * 6))
        rp.backends = {"fake1": be}
        rp.orders = {"smart": ["fake1"], "fast": ["fake1"]}
        with self.assertRaises(Exception):
            rp.chat([{"role": "user", "content": "hi"}],
                    tier="smart", timeout=2.0)
        rows = _rows(env.conn())
        self.assertTrue(any(r["source"] == "router" for r in rows))


if __name__ == "__main__":
    unittest.main()
