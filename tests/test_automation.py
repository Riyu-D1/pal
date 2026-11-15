"""'when X happens, do Y' -> one artefact proposal
phrasing -> artefact type, the single approval card, Approve creates
it, deny creates nothing."""

import unittest

from pa import automation, chattools, monitors, webhooks
from tests.helpers import TmpEnv


class TestParse(unittest.TestCase):
    def test_five_phrasings(self):
        cases = [
            ("when the price on https://shop.example/x changes, "
             "tell me", "monitor", "web"),
            ("when an email from boss@work.com arrives, draft a "
             "reply", "monitor", "gmail"),
            ("when a meeting is about to start, remind me to join",
             "monitor", "calendar"),
            ("every morning at 7, brief me on the news",
             "schedule", None),
            ("when a github push lands on main, draft me a note",
             "webhook", None),
        ]
        for sentence, artefact, kind in cases:
            p = automation.propose(sentence)
            self.assertIsNotNone(p, sentence)
            self.assertEqual(p["artefact"], artefact, sentence)
            self.assertEqual(p["tool"], "automation_apply")
            if kind:
                self.assertEqual(p["args"]["kind"], kind, sentence)

    def test_unparseable(self):
        for s in ("", "do the thing", "hello",
                  "when"):
            self.assertIsNone(automation.propose(s))

    def test_when_parsing(self):
        self.assertEqual(automation._when("every morning"),
                         "daily 09:00")
        self.assertEqual(automation._when("at 6pm"),
                         "daily 18:00")
        self.assertEqual(automation._when("every friday"),
                         "weekly fri 09:00")
        self.assertEqual(automation._when("hourly"), "hourly :05")


class TestProposeTool(unittest.TestCase):
    def setUp(self):
        self.env = TmpEnv(PA_AUTOMATION="true", PA_ACTIVITY="true")
        self.conn = self.env.conn()

    def tearDown(self):
        self.conn.close()

    def _ctx(self, **kw):
        return chattools.ChatToolContext(
            self.conn, self.env.settings, None, "chan",
            env=self.env.env, **kw)

    def test_flag_off(self):
        env = TmpEnv()
        conn = env.conn()
        try:
            ctx = chattools.ChatToolContext(
                conn, env.settings, None, "c", env=env.env)
            for t in ("automation_propose", "automation_apply"):
                self.assertEqual(chattools.run(ctx, t, {})["error"],
                                 "disabled")
        finally:
            conn.close()

    def test_propose_queues_card_then_approve_creates(self):
        calls = []

        def approve(name, args, aid):
            calls.append((name, aid))
            return "approved"
        ctx = self._ctx(request_approval=approve)
        r = chattools.run(ctx, "automation_propose", {
            "sentence": "when https://example.com/price changes, "
                        "tell me"})
        self.assertEqual(r["artefact"], "monitor")
        self.assertEqual(calls[0][0], "automation_apply")
        # Approve replays the automation_apply call
        chattools.execute_approved(
            ctx, "automation_apply",
            {"artefact": "monitor", "kind": "web",
             "query": "https://example.com/price",
             "every_minutes": 15})
        rows = monitors.list_monitors(self.conn)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "web")

    def test_denied_creates_nothing(self):
        ctx = self._ctx(request_approval=lambda *a: "denied")
        r = chattools.run(ctx, "automation_propose", {
            "sentence": "every morning, check my mail"})
        self.assertTrue(r["pending"])
        self.assertEqual(monitors.list_monitors(self.conn), [])
        rows = self.conn.execute(
            "SELECT COUNT(*) c FROM schedules WHERE name LIKE 'auto%'"
            ).fetchone()["c"]
        self.assertEqual(rows, 0)

    def test_schedule_and_webhook_apply(self):
        ctx = self._ctx()
        r = chattools.execute_approved(ctx, "automation_apply", {
            "artefact": "schedule", "name": "auto-t1",
            "when": "daily 08:00", "prompt": "check mail"})
        self.assertEqual(r["created"], "schedule")
        row = self.conn.execute(
            "SELECT freq, time_of_day, enabled FROM schedules"
            " WHERE name='auto-t1'").fetchone()
        self.assertEqual((row["freq"], row["time_of_day"],
                          row["enabled"]), ("daily", "08:00", 1))
        r = chattools.execute_approved(ctx, "automation_apply", {
            "artefact": "webhook", "name": "auto-hook",
            "prompt": "on fire, draft a note"})
        self.assertEqual(r["created"], "webhook")
        self.assertEqual(
            [e["name"] for e in webhooks.list_endpoints(self.conn)],
            ["auto-hook"])

    def test_apply_is_gated_without_replay(self):
        calls = []
        ctx = self._ctx(request_approval=lambda n, a, i:
                        calls.append(n) or "denied")
        r = chattools.run(ctx, "automation_apply", {
            "artefact": "webhook", "name": "x"})
        self.assertEqual(r["error"], "not_approved")
        self.assertEqual(webhooks.list_endpoints(self.conn), [])


if __name__ == "__main__":
    unittest.main()
