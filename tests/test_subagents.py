"""S1 acceptance: budgeted sub-agents via delegate().

Children are ordinary worker tasks bound to sub:<role> assistants
(tools allowlist enforced by ToolContext), per-child budgets in the
subagents table (tool_calls/tokens enforced in AgentRunner, minutes
= task deadline), approvals preserved (propose_memory still lands in
the owner queue), completion through normal task results."""

import json
import unittest

from pa import chattools, subagents, tools as toolsmod, \
    worker as workmod
from tests.helpers import Fixture, TmpEnv


def fake_fetch(url, settings=None, **kw):
    return {"text": f"content for {url}", "content_type": "text/plain",
            "title": "t", "final_url": url, "status": 200}


class SubEnv(TmpEnv):
    def __init__(self, **kw):
        kw.setdefault("PA_SUBAGENTS", "1")
        super().__init__(**kw)


def _ctx(env, channel="chan-1"):
    conn = env.conn()
    ast = conn.execute(
        "SELECT * FROM assistants WHERE name='pal'").fetchone()
    return chattools.ChatToolContext(conn, env.settings, ast, channel,
                                     env=env.env)


class TestDelegateShape(unittest.TestCase):
    def setUp(self):
        self.env = SubEnv()
        self.env.add_assistant(name="pal", tools=[])
        self.ctx = _ctx(self.env)

    def test_creates_task_role_and_budget(self):
        out = chattools.run(self.ctx, "delegate", {
            "role": "researcher",
            "prompt": "compare two cheap VPS providers",
            "budget": {"tool_calls": 5, "minutes": 10, "tokens": 9000}})
        self.assertTrue(out["queued"], out)
        conn = self.env.conn()
        t = conn.execute("SELECT * FROM tasks WHERE id=?",
                         (out["task_id"],)).fetchone()
        self.assertEqual(t["state"], "queued")
        a = conn.execute("SELECT * FROM assistants WHERE id=?",
                         (t["assistant_id"],)).fetchone()
        self.assertEqual(a["name"], "sub:researcher")
        self.assertIn("web_search", json.loads(a["tools"]))
        self.assertNotIn("run_python", json.loads(a["tools"]))
        sa = conn.execute("SELECT * FROM subagents WHERE task_id=?",
                          (t["id"],)).fetchone()
        self.assertEqual(sa["role"], "researcher")
        self.assertEqual(sa["parent_channel"], "chan-1")
        b = json.loads(sa["budget_json"])
        self.assertEqual(b["tool_calls"], 5)
        self.assertEqual(b["minutes"], 10)
        # new path: nothing goes into chat_inbox
        self.assertIsNone(conn.execute(
            "SELECT 1 FROM chat_inbox WHERE source='job:delegate'"
        ).fetchone())

    def test_reuses_role_assistant(self):
        chattools.run(self.ctx, "delegate",
                      {"role": "researcher", "prompt": "one"})
        chattools.run(self.ctx, "delegate",
                      {"role": "researcher", "prompt": "two"})
        n = self.env.conn().execute(
            "SELECT COUNT(*) c FROM assistants WHERE name="
            "'sub:researcher'").fetchone()["c"]
        self.assertEqual(n, 1)

    def test_bad_role(self):
        out = chattools.run(self.ctx, "delegate",
                            {"role": "spy", "prompt": "x"})
        self.assertEqual(out["error"], "bad_arguments")

    def test_empty_prompt(self):
        out = chattools.run(self.ctx, "delegate",
                            {"role": "analyst", "prompt": "  "})
        self.assertEqual(out["error"], "bad_arguments")

    def test_flag_off_keeps_inbox_behaviour(self):
        env = TmpEnv()
        env.add_assistant(name="pal", tools=[])
        ctx = _ctx(env)
        out = chattools.run(ctx, "delegate", {"prompt": "old path"})
        self.assertTrue(out["delegated"])
        self.assertIsNotNone(env.conn().execute(
            "SELECT 1 FROM chat_inbox WHERE source='job:delegate'"
        ).fetchone())

    def test_capacity_cap(self):
        chattools.run(self.ctx, "delegate",
                      {"role": "researcher", "prompt": "a"})
        chattools.run(self.ctx, "delegate",
                      {"role": "analyst", "prompt": "b"})
        out = chattools.run(self.ctx, "delegate",
                            {"role": "researcher", "prompt": "c"})
        self.assertEqual(out["error"], "capacity")

    def test_tool_subset_narrows(self):
        chattools.run(self.ctx, "delegate", {
            "role": "researcher", "prompt": "x",
            "tools": ["web_search", "run_python"]})
        a = self.env.conn().execute(
            "SELECT tools FROM assistants WHERE name='sub:researcher'"
        ).fetchone()
        tools = json.loads(a["tools"])
        self.assertEqual(tools, ["web_search"])   # run_python dropped

    def test_coder_needs_freebuff(self):
        out = chattools.run(self.ctx, "delegate",
                            {"role": "coder", "prompt": "fix it"})
        self.assertEqual(out["error"], "not_configured")


class TestWorkerRun(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()
        self.env = SubEnv(PA_OLLAMA_BASE_URL=self.fx.base_url)
        self.env.add_assistant(name="pal", tools=[], host="localhost",
                               model=self.env.settings.dev_model)
        self.ctx = _ctx(self.env)
        self.conn = self.env.conn()

    def tearDown(self):
        self.fx.close()

    def _delegate(self, **kw):
        args = {"role": "researcher", "prompt": "go"}
        args.update(kw)
        out = chattools.run(self.ctx, "delegate", args)
        self.assertTrue(out.get("queued"), out)
        return out["task_id"]

    def _run(self):
        w = workmod.Worker(self.env.db_path, self.env.settings,
                           fetch_impl=fake_fetch)
        w.run_once()

    def test_completion_posts_summary(self):
        tid = self._delegate()
        self.fx.push_chat(self.fx.chat_text("the answer is 42"))
        self._run()
        t = self.conn.execute("SELECT * FROM tasks WHERE id=?",
                              (tid,)).fetchone()
        self.assertEqual(t["state"], "succeeded")
        self.assertIn("42", t["result_json"])

    def test_tool_calls_budget_stops(self):
        tid = self._delegate(budget={"tool_calls": 1})
        for i in range(3):
            self.fx.push_chat(self.fx.chat_tool(
                "memory_search", {"query": "x"}, call_id=f"c{i}"))
        self._run()
        t = self.conn.execute("SELECT * FROM tasks WHERE id=?",
                              (tid,)).fetchone()
        self.assertEqual(t["state"], "failed")
        self.assertEqual(t["error_kind"], "over_budget")
        self.assertIn("tool_calls", t["error_detail"])
        # only the budgeted call ran
        n = self.conn.execute(
            "SELECT COUNT(*) c FROM steps WHERE task_id=? AND"
            " kind='tool_call'", (tid,)).fetchone()["c"]
        self.assertEqual(n, 1)

    def test_tool_subset_enforced(self):
        """researcher can't reach owner-email read or run_python."""
        tid = self._delegate()
        self.fx.push_chat(self.fx.chat_tool(
            "email_list", {}, call_id="e1"))
        self.fx.push_chat(self.fx.chat_text("couldn't read mail"))
        self._run()
        step = self.conn.execute(
            "SELECT result_json FROM steps WHERE task_id=? AND"
            " kind='tool_call'", (tid,)).fetchone()
        self.assertIn("tool_not_allowed", step["result_json"])

    def test_child_write_still_needs_owner(self):
        """propose_memory lands in the approval queue — nothing is
        written to memory directly."""
        tid = self._delegate()
        self.fx.push_chat(self.fx.chat_tool(
            "propose_memory", {"kind": "memory", "content": "fact"},
            call_id="m1"))
        self.fx.push_chat(self.fx.chat_text("done"))
        self._run()
        ap = self.conn.execute(
            "SELECT * FROM approvals WHERE task_id=? AND"
            " kind='memory_write'", (tid,)).fetchone()
        self.assertIsNotNone(ap)
        self.assertEqual(ap["status"], "pending")
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM memories").fetchone())

    def test_cancel_frees_slot(self):
        tid = self._delegate()
        self.assertEqual(subagents.live_count(self.conn), 1)
        workmod.request_cancel(self.conn, tid)
        self.assertEqual(subagents.live_count(self.conn), 0)
        # slot freed: another delegate fits under PA_SUBAGENT_MAX=2
        out = chattools.run(self.ctx, "delegate",
                            {"role": "analyst", "prompt": "b"})
        self.assertTrue(out.get("queued"), out)


class TestNodeWorkerTools(unittest.TestCase):
    """node_submit/node_poll as worker tools (browser role's heavy
    lifting lives on the node, not the 1 GB pal VM)."""

    def setUp(self):
        self.env = SubEnv()
        aid = self.env.add_assistant(
            name="sub:browser", tools=["node_submit", "node_poll"])
        self.conn = self.env.conn()
        self.tid = workmod.create_task(self.conn, aid, "b", "browse")
        task = self.conn.execute("SELECT * FROM tasks WHERE id=?",
                                 (self.tid,)).fetchone()
        ast = self.conn.execute("SELECT * FROM assistants WHERE id=?",
                                (aid,)).fetchone()
        self.tc = toolsmod.ToolContext(self.conn, self.env.settings,
                                       task, ast)

    def test_node_submit_queues_without_nodes(self):
        out = self.tc.run("node_submit",
                          json.dumps({"cap": "browse",
                                      "args": {"url": "https://x.test"}}))
        self.assertIn("job_id", out)
        self.assertEqual(out["status"], "queued")
        row = self.conn.execute(
            "SELECT * FROM node_jobs WHERE id=?",
            (out["job_id"],)).fetchone()
        self.assertEqual(row["scope"], "background")

    def test_node_poll_returns_row(self):
        out = self.tc.run("node_submit",
                          json.dumps({"cap": "browse"}))
        polled = self.tc.run("node_poll",
                             json.dumps({"job_id": out["job_id"]}))
        self.assertEqual(polled["status"], "queued")

    def test_gated_cap_not_executed(self):
        """an owner-channel cap surfaces approval_required, never runs."""
        self.env.add_assistant(name="sub:gated", tools=["node_submit"])
        out = self.tc.run("node_submit",
                          json.dumps({"cap": "sms_send",
                                      "args": {"to": "+1", "body": "x"}}))
        # no phone node -> policy deny/approve surfaces as an error,
        # and no node job ever leaves 'denied'/'queued-for-approval'
        self.assertTrue(out.get("error"), out)
        self.assertNotEqual(out.get("status"), "submitted")

    def test_run_python_worker_tool(self):
        aid = self.env.add_assistant(name="sub:analyst",
                                     tools=["run_python"])
        task = self.conn.execute(
            "SELECT * FROM tasks WHERE id=?", (self.tid,)).fetchone()
        ast = self.conn.execute(
            "SELECT * FROM assistants WHERE id=?", (aid,)).fetchone()
        tc = toolsmod.ToolContext(self.conn, self.env.settings, task,
                                  ast)
        out = tc.run("run_python", json.dumps({"code": "print(2+2)"}))
        self.assertIn("4", json.dumps(out))


if __name__ == "__main__":
    unittest.main()
