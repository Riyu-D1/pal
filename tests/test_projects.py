"""multi-day projects — start/status/stop tools, the
one-step-per-tick heartbeat, blocked -> ask card -> owner unblock,
owner-write steps -> Approve cards, and replan-on-input-change."""

import datetime
import unittest

from pa import activity, approvals, chattools, goals, projects, \
    proactive
from tests.helpers import TmpEnv


def _env(**flags):
    return TmpEnv(PA_GOALS="true", PA_PROACTIVE="true",
                  PA_ACTIVITY="true", PA_PROJECTS="true", **flags)


class TestProjects(unittest.TestCase):
    def setUp(self):
        self.env = _env()
        self.conn = self.env.conn()
        self.gid = goals.add_goal(self.conn, "job hunt")

    def tearDown(self):
        self.conn.close()

    def test_start_and_get(self):
        pid = projects.start(
            self.conn, self.gid, "polish CV",
            ["gather notes", "draft cv", {"text": "send", "tool": "x"}])
        p = projects.get(self.conn, pid)
        self.assertEqual(p["state"], "running")
        self.assertEqual(p["next_step"], "gather notes")
        self.assertEqual(len(p["plan"]["steps"]), 3)
        # goal event logged
        verbs = [e["verb"] for e in goals.events(self.conn, self.gid)]
        self.assertIn("project_start", verbs)
        # empty steps -> no project
        self.assertEqual(projects.start(self.conn, self.gid, "x", []),
                         0)

    def test_three_steps_across_ticks(self):
        pid = projects.start(
            self.conn, self.gid, "three-step",
            ["step one", "step two", "step three"])
        s = self.env.settings
        hb = lambda: projects.heartbeat(self.conn, s, self.env.env)
        self.assertEqual(hb(), [{"project_id": pid, "advanced": 0}])
        self.assertEqual(projects.get(self.conn, pid)["next_step"],
                         "step two")
        self.assertEqual(hb(), [{"project_id": pid, "advanced": 1}])
        out = hb()
        self.assertEqual(out, [{"project_id": pid, "advanced": 2}])
        # plan exhausted -> done on the NEXT tick
        self.assertEqual(hb(), [{"project_id": pid, "done": True}])
        self.assertEqual(projects.get(self.conn, pid)["state"], "done")
        rows = activity.list_activity(self.conn, kind="did")
        self.assertTrue(any("three-step" in r["title"] for r in rows))

    def test_ask_step_blocks_then_owner_unblocks(self):
        pid = projects.start(
            self.conn, self.gid, "needs input",
            [{"text": "pick target", "ask": "which company?"},
             "apply"])
        s = self.env.settings
        out = projects.heartbeat(self.conn, s, self.env.env)
        self.assertEqual(out, [{"project_id": pid, "blocked": True}])
        p = projects.get(self.conn, pid)
        self.assertEqual(p["state"], "blocked")
        self.assertTrue(p["ask_id"])
        ap = approvals.get(self.conn, p["ask_id"])
        self.assertEqual(ap["kind"], "ask")
        asks = activity.list_activity(self.conn, kind="ask")
        self.assertEqual(len(asks), 1)
        # owner answers -> next tick resumes
        approvals.decide(self.conn, p["ask_id"], True)
        out = projects.heartbeat(self.conn, s, self.env.env)
        self.assertIn({"project_id": pid, "unblocked": True}, out)
        self.assertEqual(projects.get(self.conn, pid)["state"],
                         "running")
        # the ask step itself then completes on a later tick
        projects.heartbeat(self.conn, s, self.env.env)
        self.assertEqual(projects.get(self.conn, pid)
                         ["plan"]["steps"][0]["state"], "done")

    def test_denied_ask_stops(self):
        pid = projects.start(
            self.conn, self.gid, "x",
            [{"text": "t", "ask": "ok?"}])
        s = self.env.settings
        projects.heartbeat(self.conn, s, self.env.env)
        p = projects.get(self.conn, pid)
        approvals.decide(self.conn, p["ask_id"], False)
        out = projects.heartbeat(self.conn, s, self.env.env)
        self.assertIn({"project_id": pid, "stopped": True}, out)
        self.assertEqual(projects.get(self.conn, pid)["state"],
                         "stopped")

    def test_owner_write_step_becomes_approve_card(self):
        pid = projects.start(
            self.conn, self.gid, "send cv",
            ["draft it",
             {"text": "send to recruiter", "tool": "send_email",
              "args": {"to": "r@x.y", "subject": "cv",
                       "body": "hi"}}])
        s = self.env.settings
        projects.heartbeat(self.conn, s, self.env.env)   # step 0
        out = projects.heartbeat(self.conn, s, self.env.env)
        self.assertTrue(out[0].get("held"))
        ap = approvals.get(self.conn, out[0]["held"])
        self.assertEqual(ap["kind"], "job_action")
        # step stays pending until the owner clicks
        self.assertEqual(projects.get(self.conn, pid)
                         ["plan"]["steps"][1]["state"], "pending")
        nap = activity.list_activity(
            self.conn, kind="needs_approval")
        self.assertTrue(any("send_email" in r["title"] for r in nap))
        # nothing was sent — it's a held approval, not an action

    def test_pal_account_step_runs(self):
        pid = projects.start(
            self.conn, self.gid, "note it",
            [{"text": "save a note", "tool": "todo_add",
              "args": {"text": "follow up friday"}}])
        s = self.env.settings
        out = projects.heartbeat(self.conn, s, self.env.env)
        self.assertEqual(out, [{"project_id": pid, "advanced": 0}])
        todo = self.conn.execute(
            "SELECT COUNT(*) c FROM chat_todos").fetchone()["c"]
        self.assertGreaterEqual(todo, 1)

    def test_replan_when_inputs_change(self):
        pid = projects.start(self.conn, self.gid, "watch inbox",
                             ["check", "check again"])
        s = self.env.settings
        probe_calls = {"n": 0}

        def probe(_pid, _plan):
            probe_calls["n"] += 1
            return {"unread": probe_calls["n"]}

        out = projects.heartbeat(self.conn, s, self.env.env,
                                 probe=probe)
        self.assertEqual(out[0]["replanned"], True)
        self.assertEqual(projects.get(self.conn, pid)
                         ["plan"]["inputs"], {"unread": 1})
        # next tick: inputs stable -> advances
        out = projects.heartbeat(self.conn, s, self.env.env,
                                 probe=lambda _p, _pl: {"unread": 1})
        self.assertEqual(out, [{"project_id": pid, "advanced": 0}])

    def test_stop(self):
        pid = projects.start(self.conn, self.gid, "x", ["a"])
        projects.set_state(self.conn, pid, "stopped")
        self.assertEqual(projects.get(self.conn, pid)["state"],
                         "stopped")
        out = projects.heartbeat(self.conn, self.env.settings,
                                 self.env.env)
        self.assertEqual(out, [])


class TestProjectTools(unittest.TestCase):
    def setUp(self):
        self.env = _env()
        self.conn = self.env.conn()
        self.gid = goals.add_goal(self.conn, "g")

    def tearDown(self):
        self.conn.close()

    def _ctx(self, **kw):
        return chattools.ChatToolContext(
            self.conn, self.env.settings, None, "chan",
            env=self.env.env, **kw)

    def test_flag_off(self):
        env = TmpEnv(PA_GOALS="true")     # PA_PROJECTS unset
        conn = env.conn()
        try:
            ctx = chattools.ChatToolContext(
                conn, env.settings, None, "chan", env=env.env)
            for t in ("project_start", "project_status",
                      "project_stop"):
                r = chattools.run(ctx, t, {})
                self.assertEqual(r["error"], "disabled")
        finally:
            conn.close()

    def test_start_is_owner_reviewed(self):
        ctx = self._ctx(request_approval=lambda *a: "approved")
        r = chattools.run(ctx, "project_start", {
            "goal_id": self.gid, "title": "p",
            "steps": ["one", "two"]})
        self.assertEqual(r["state"], "running")
        self.assertEqual(r["project_id"], 1)

    def test_status_and_stop(self):
        pid = projects.start(self.conn, self.gid, "p", ["a", "b"])
        ctx = self._ctx(request_approval=lambda *a: "approved")
        r = chattools.run(ctx, "project_status", {"id": pid})
        self.assertEqual(r["project"]["state"], "running")
        self.assertEqual(len(r["project"]["steps"]), 2)
        r = chattools.run(ctx, "project_status", {})
        self.assertEqual(r["count"], 1)
        r = chattools.run(ctx, "project_stop", {"id": pid})
        self.assertEqual(r["state"], "stopped")

    def test_denied_start_writes_nothing(self):
        ctx = self._ctx(request_approval=lambda *a: "denied")
        r = chattools.run(ctx, "project_start", {
            "goal_id": self.gid, "title": "p", "steps": ["a"]})
        self.assertEqual(r["error"], "not_approved")
        self.assertEqual(projects.list_projects(self.conn), [])


class TestHeartbeatInTick(unittest.TestCase):
    def test_projects_advance_without_due_goals(self):
        env = _env()
        conn = env.conn()
        try:
            gid = goals.add_goal(conn, "g")
            # goal just checked -> not due
            goals.mark_checked(conn, gid)
            pid = projects.start(conn, gid, "p", ["one", "two"])
            # fixed noon-UTC now -> never inside default quiet hours
            noon = datetime.datetime(
                2025, 1, 15, 12, 0, tzinfo=datetime.timezone.utc)
            out = proactive.run(conn, env.settings, env.env,
                                engine=None, now=noon)
            self.assertIn("projects", out)
            self.assertEqual(out["projects"],
                             [{"project_id": pid, "advanced": 0}])
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
