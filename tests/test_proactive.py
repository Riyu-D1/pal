"""final-2 G2: proactive read-only tick — flags, quiet hours, due
goals, the read-only tool scope, drafts -> approval cards, budget
stop and the goal next_action update."""

import datetime
import unittest

from pa import activity, approvals, chattools, goals, proactive
from tests.helpers import TmpEnv

UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 3, 4, 12, 0, tzinfo=UTC)


class FakeEngine:
    """Duck-typed chatagent.ChatEngine — records the call, returns
    canned output (or whatever fn produces)."""

    def __init__(self, fn=None):
        self.calls = []
        self.fn = fn

    def run_turn(self, channel_id, text, unattended=False,
                 deadline_seconds=None, progress=None,
                 stop_check=None):
        self.calls.append({"channel": channel_id, "text": text,
                           "unattended": unattended,
                           "deadline": deadline_seconds})
        if self.fn:
            return self.fn(channel_id, text)
        return {"text": "looked around", "pending": [],
                "trace": []}


def _env(**flags):
    return TmpEnv(PA_GOALS="true", PA_PROACTIVE="true",
                  PA_ACTIVITY="true", **flags)


class TestGates(unittest.TestCase):
    def test_flags_off(self):
        env = TmpEnv()                     # PA_GOALS/PA_PROACTIVE off
        conn = env.conn()
        try:
            out = proactive.run(conn, env.settings, env.env,
                                engine=FakeEngine(), now=NOW)
        finally:
            conn.close()
        self.assertEqual(out["skipped"], "flags")

    def test_quiet_hours(self):
        env = _env()
        env.settings.quiet_hours = "00-23"   # covers noon UTC
        conn = env.conn()
        try:
            goals.add_goal(conn, "x")
            out = proactive.run(conn, env.settings, env.env,
                                engine=FakeEngine(), now=NOW)
        finally:
            conn.close()
        self.assertEqual(out["skipped"], "quiet_hours")

    def test_no_due_goals(self):
        env = _env()
        conn = env.conn()
        try:
            out = proactive.run(conn, env.settings, env.env,
                                engine=FakeEngine(), now=NOW)
        finally:
            conn.close()
        self.assertEqual(out["skipped"], "no_due_goals")


class TestTick(unittest.TestCase):
    def setUp(self):
        self.env = _env()
        self.conn = self.env.conn()
        self.gid = goals.add_goal(
            self.conn, "stay on top of recruiter mail",
            cadence="daily", sources="mail")

    def tearDown(self):
        self.conn.close()

    def test_unattended_readonly_turn(self):
        eng = FakeEngine()
        out = proactive.run(self.conn, self.env.settings,
                            self.env.env, engine=eng,
                            channel_id="dm", now=NOW)
        r = out["results"][0]
        self.assertEqual(r["goal_id"], self.gid)
        call = eng.calls[0]
        self.assertTrue(call["unattended"])
        self.assertEqual(call["deadline"], 90)
        self.assertIn("goal #1", call["text"]) or \
            self.assertIn("#1", call["text"])
        # the tick stamped the goal and logged a 'did' feed row
        self.assertIsNotNone(goals.get(self.conn, self.gid)
                             ["last_checked"])
        rows = activity.list_activity(self.conn, kind="did")
        self.assertEqual(len(rows), 1)
        self.assertIn("proactive tick", rows[0]["title"])

    def test_pending_become_feed_rows_and_cards(self):
        posted = []

        def fn(_c, _t):
            return {"text": "drafted a reply",
                    "pending": [{"id": "appr-1", "tool": "send_email",
                                 "args": {"to": "a@b.c"}}],
                    "trace": []}
        out = proactive.run(
            self.conn, self.env.settings, self.env.env,
            engine=FakeEngine(fn), channel_id="dm",
            post_pending=posted.append, now=NOW)
        self.assertEqual(out["results"][0]["pending"], 1)
        self.assertEqual(len(posted), 1)
        rows = activity.list_activity(
            self.conn, kind="needs_approval")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["ref"]["approval_id"], "appr-1")
        self.assertEqual(rows[0]["ref"]["goal_id"], self.gid)

    def test_budget_stop(self):
        self.conn.execute(
            "INSERT INTO budget_limits(scope, resource, cap)"
            " VALUES('background','llm_tokens:smart',10)")
        eng = FakeEngine()
        out = proactive.run(self.conn, self.env.settings,
                            self.env.env, engine=eng, now=NOW)
        self.assertEqual(out["results"][0]["skipped"], "budget")
        self.assertEqual(eng.calls, [])

    def test_engine_error_is_caught(self):
        def boom(_c, _t):
            raise RuntimeError("model exploded")
        out = proactive.run(
            self.conn, self.env.settings, self.env.env,
            engine=FakeEngine(boom), now=NOW)
        self.assertIn("exploded", out["results"][0]["error"])
        self.assertIsNotNone(goals.get(self.conn, self.gid)
                             ["last_checked"])


class TestToolScope(unittest.TestCase):
    def setUp(self):
        self.env = _env()
        self.conn = self.env.conn()

    def tearDown(self):
        self.conn.close()

    def test_allowed_tools_are_read_only(self):
        allow = proactive.allowed_tools(self.env.settings)
        self.assertIn("goal_report", allow)
        self.assertIn("goal_list", allow)
        for t in ("send_email", "goal_add", "memory_save"):
            self.assertNotIn(t, allow)

    def test_tool_allow_refuses_out_of_scope(self):
        ctx = chattools.ChatToolContext(
            self.conn, self.env.settings, None, "chan",
            env=self.env.env, tool_allow={"goal_list"})
        r = chattools.run(ctx, "goal_add", {"title": "x"})
        self.assertEqual(r["error"], "not_in_scope")
        r = chattools.run(ctx, "goal_list", {})
        self.assertNotIn("error", r)

    def test_goal_report_writes_feed_and_queues(self):
        from pa import goals as goalsmod
        gid = goalsmod.add_goal(self.conn, "x")
        ctx = chattools.ChatToolContext(
            self.conn, self.env.settings, None, "chan",
            env=self.env.env, unattended=True)
        r = chattools.run(ctx, "goal_report", {
            "goal_id": gid,
            "findings": ["3 recruiter mails unread"],
            "drafts": [
                {"summary": "reply to alice",
                 "draft": "hi alice — yes, friday works"},
                {"summary": "send recruiter reply",
                 "tool": "send_email",
                 "args": {"to": "r@x.y", "subject": "s",
                          "body": "b"}}],
            "next_action": "draft monday follow-up"})
        self.assertEqual(r["recorded"], 1)
        self.assertEqual(r["queued"], ["send_email"])
        # findings -> noticed; free draft -> draft; tool draft ->
        # needs_approval + a pending job_action row
        self.assertEqual(len(activity.list_activity(
            self.conn, kind="noticed")), 1)
        self.assertEqual(len(activity.list_activity(
            self.conn, kind="draft")), 1)
        nap = activity.list_activity(
            self.conn, kind="needs_approval")
        self.assertEqual(len(nap), 1)
        aid = nap[0]["ref"]["approval_id"]
        ap = approvals.get(self.conn, aid)
        self.assertEqual(ap["kind"], "job_action")
        self.assertEqual(ap["status"], "pending")
        # next_action landed on the goal
        self.assertEqual(goals.get(self.conn, gid)["next_action"],
                         "draft monday follow-up")
        # and the queued tool never ran — no mail sent, just held
        self.assertEqual(ctx.pending[0]["tool"], "send_email")

    def test_goal_report_flag_off(self):
        env = TmpEnv(PA_GOALS="true")        # PA_PROACTIVE unset
        conn = env.conn()
        try:
            ctx = chattools.ChatToolContext(
                conn, env.settings, None, "chan", env=env.env)
            r = chattools.run(ctx, "goal_report", {"goal_id": 1})
            self.assertEqual(r["error"], "disabled")
        finally:
            conn.close()


class TestSysjob(unittest.TestCase):
    def test_registered(self):
        from pa import sysjobs
        self.assertTrue(sysjobs.is_system("proactive-tick"))
        self.assertIs(sysjobs.handler_for("proactive-tick"),
                      proactive.job)


if __name__ == "__main__":
    unittest.main()
