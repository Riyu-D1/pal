"""final-2 G1: standing goals — schema, CRUD, the goal_* tools
(owner-reviewed, allow-rule-able), the /goals card and the
disabled-by-default proactive-tick seed."""

import datetime
import unittest

from pa import approvals, chattools, db as dbmod, goals, policy
from tests.helpers import TmpEnv

UTC = datetime.timezone.utc


def _ctx(env, assistant=None, **kw):
    conn = env.conn()
    return chattools.ChatToolContext(
        conn, env.settings, assistant, "chan-1", env=env.env, **kw)


class TestSchema(unittest.TestCase):
    def test_tables_and_seed(self):
        env = TmpEnv()
        conn = env.conn()
        try:
            for t in ("goals", "goal_events", "projects"):
                conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()
            self.assertEqual(dbmod.current_version(conn),
                             dbmod.SCHEMA_VERSION)
            row = conn.execute(
                "SELECT * FROM schedules WHERE name='proactive-tick'"
                ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["enabled"], 0)
            self.assertEqual(row["kind"], "agent_job")
            self.assertEqual(row["freq"], "interval")
            self.assertEqual(row["day_of_month"], 60)
        finally:
            conn.close()


class TestGoalsModule(unittest.TestCase):
    def setUp(self):
        self.env = TmpEnv()
        self.conn = self.env.conn()

    def tearDown(self):
        self.conn.close()

    def test_crud(self):
        gid = goals.add_goal(
            self.conn, "stay on top of recruiter mail",
            why="job hunt", criteria="no unread recruiter mail >24h",
            cadence="daily", sources="mail,calendar")
        g = goals.get(self.conn, gid)
        self.assertEqual(g["status"], "active")
        self.assertEqual(g["cadence"], "daily")
        self.assertEqual(g["sources"], "mail,calendar")
        self.assertEqual(goals.events(self.conn, gid)[0]["verb"],
                         "created")
        g = goals.update_goal(
            self.conn, gid, next_action="triage today's mail",
            progress="1 draft")
        self.assertEqual(g["next_action"], "triage today's mail")
        g = goals.set_status(self.conn, gid, "paused")
        self.assertEqual(g["status"], "paused")
        g = goals.set_status(self.conn, gid, "done", note="hired")
        self.assertEqual(g["status"], "done")
        verbs = [e["verb"] for e in goals.events(self.conn, gid)]
        self.assertEqual(verbs, ["done", "paused", "update", "created"])

    def test_add_rejects_empty_and_bad_cadence(self):
        self.assertEqual(goals.add_goal(self.conn, "  "), 0)
        gid = goals.add_goal(self.conn, "x", cadence="hourly")
        self.assertEqual(goals.get(self.conn, gid)["cadence"], "daily")

    def test_sources_whitelist(self):
        gid = goals.add_goal(self.conn, "x", sources="mail,evil,vault")
        self.assertEqual(goals.get(self.conn, gid)["sources"],
                         "mail,vault")

    def test_due_goals_cadence(self):
        now = datetime.datetime(2026, 3, 4, 12, 0, tzinfo=UTC)
        gid = goals.add_goal(self.conn, "daily", cadence="daily")
        goals.add_goal(self.conn, "event", cadence="on_event")
        goals.add_goal(self.conn, "paused", cadence="daily")
        goals.set_status(self.conn, 3, "paused")
        # never checked -> the daily is due; on_event/paused never are
        due = goals.due_goals(self.conn, now)
        self.assertEqual([g["id"] for g in due], [gid])
        self.conn.execute(
            "UPDATE goals SET last_checked=? WHERE id=?",
            (now.isoformat(), gid))
        self.assertEqual(goals.due_goals(self.conn, now), [])
        # 21 h later -> due again
        later = now + datetime.timedelta(hours=21)
        self.assertEqual([g["id"] for g in
                          goals.due_goals(self.conn, later)], [gid])
        # weekly goal needs 6 days
        wid = goals.add_goal(self.conn, "weekly", cadence="weekly")
        self.conn.execute(
            "UPDATE goals SET last_checked=? WHERE id=?",
            (now.isoformat(), wid))
        self.assertNotIn(wid, [g["id"] for g in
                               goals.due_goals(self.conn, later)])

    def test_survives_restart(self):
        gid = goals.add_goal(self.conn, "persist me")
        self.conn.close()
        conn2 = self.env.conn()
        try:
            self.assertEqual(goals.get(conn2, gid)["title"],
                             "persist me")
        finally:
            conn2.close()
            self.conn = self.env.conn()


class TestGoalTools(unittest.TestCase):
    def setUp(self):
        self.env = TmpEnv(PA_GOALS="true")
        self.aid = self.env.add_assistant(name="pal", tools=[])
        conn = self.env.conn()
        self.assistant = conn.execute(
            "SELECT * FROM assistants WHERE id=?", (self.aid,)).fetchone()
        conn.close()

    def _approve_all(self, calls):
        def approve(name, args, aid):
            calls.append(name)
            conn = self.env.conn()
            try:
                approvals.decide(conn, aid, True)
            finally:
                conn.close()
            return "approved"
        return approve

    def test_flag_off_hides_and_refuses(self):
        env = TmpEnv()                       # PA_GOALS unset
        ctx = _ctx(env, self.assistant)
        r = chattools.run(ctx, "goal_add", {"title": "x"})
        self.assertEqual(r["error"], "disabled")
        self.assertNotIn("goal_add",
                         chattools.prompt_lines(env.settings))

    def test_flag_on_lists_tools(self):
        lines = chattools.prompt_lines(self.env.settings)
        for t in ("goal_add", "goal_list", "goal_update", "goal_done"):
            self.assertIn(t, lines)

    def test_add_is_owner_reviewed(self):
        calls = []
        ctx = _ctx(self.env, self.assistant,
                   request_approval=self._approve_all(calls))
        r = chattools.run(ctx, "goal_add", {
            "title": "keep on top of recruiter mail",
            "cadence": "daily"})
        self.assertEqual(calls, ["goal_add"])
        self.assertEqual(r["status"], "active")
        self.assertEqual(r["goal_id"], 1)

    def test_denied_writes_nothing(self):
        ctx = _ctx(self.env, self.assistant,
                   request_approval=lambda n, a, i: "denied")
        r = chattools.run(ctx, "goal_add", {"title": "x"})
        self.assertEqual(r["error"], "not_approved")
        self.assertEqual(goals.list_goals(ctx.conn), [])

    def test_unattended_queues_job_action(self):
        ctx = _ctx(self.env, self.assistant, unattended=True)
        r = chattools.run(ctx, "goal_add", {"title": "x"})
        self.assertTrue(r["queued"])
        self.assertEqual(len(ctx.pending), 1)
        self.assertEqual(ctx.pending[0]["tool"], "goal_add")
        # nothing landed yet — the owner's click creates it
        self.assertEqual(goals.list_goals(ctx.conn), [])
        aid = ctx.pending[0]["id"]
        approvals.decide(ctx.conn, aid, True)
        out = chattools.execute_approved(
            ctx, "goal_add", ctx.pending[0]["args"])
        self.assertEqual(out["status"], "active")
        self.assertEqual(len(goals.list_goals(ctx.conn)), 1)

    def test_list_update_done(self):
        ctx = _ctx(self.env, self.assistant,
                   request_approval=lambda n, a, i: "approved")
        gid = goals.add_goal(ctx.conn, "g1")
        goals.add_goal(ctx.conn, "g2")
        r = chattools.run(ctx, "goal_list", {})
        self.assertEqual(r["count"], 2)
        r = chattools.run(ctx, "goal_update",
                        {"id": gid, "status": "paused"})
        self.assertEqual(r["status"], "paused")
        r = chattools.run(ctx, "goal_done", {"id": gid})
        self.assertEqual(r["status"], "done")
        r = chattools.run(ctx, "goal_list", {"status": "active"})
        self.assertEqual(r["count"], 1)

    def test_policy_classifies_goals(self):
        conn = self.env.conn()
        try:
            self.assertEqual(policy.classify("goal_list"), "read")
            for t in ("goal_add", "goal_update", "goal_done"):
                d = policy.decide(conn, t, {}, unattended=True)
                # pal's own store -> allow within budgets (the tool's
                # approval flag is what queues it for the owner)
                self.assertEqual(d.account_owner, "pal")
                self.assertEqual(d.verdict, "allow")
        finally:
            conn.close()


class TestGoalsCommand(unittest.TestCase):
    def setUp(self):
        self.env = TmpEnv(PA_GOALS="true")
        from pa import discordbot
        self.bot = discordbot.DiscordBot(
            self.env.settings, env=self.env.env, notifier=False)

    def _cmd(self, *argv):
        conn = self.env.conn()
        try:
            return self.bot.execute(conn, list(argv), "chan-1")
        finally:
            conn.close()

    def test_flag_off_message(self):
        env = TmpEnv()
        from pa import discordbot
        bot = discordbot.DiscordBot(env.settings, env=env.env,
                                    notifier=False)
        conn = env.conn()
        try:
            out = bot.execute(conn, ["goals"], "chan-1")
        finally:
            conn.close()
        self.assertIn("PA_GOALS", out)

    def test_add_list_inspect_status(self):
        out = self._cmd("goals", "add", "keep on top of mail")
        self.assertIn("#1", out)
        out = self._cmd("goals")
        self.assertIn("keep on top of mail", out)
        out = self._cmd("goals", "1")
        self.assertIn("cadence: daily", out)
        out = self._cmd("goals", "pause", "1")
        self.assertIn("paused", out)
        out = self._cmd("goals", "done", "1")
        self.assertIn("done", out)
        out = self._cmd("goals", "99")
        self.assertIn("no goal", out)


if __name__ == "__main__":
    unittest.main()
