"""acceptance: research(question, depth) -> cited workspace report.

The child is an ordinary researcher sub-agent; its fenced JSON
{claims, sources} ledger is shaped into a Markdown report by
research.build_report/finish. Claims without a numbered source are
quarantined; publish=True queues page_publish as a held action."""

import json
import unittest

from pa import approvals, chattools, research, workspace
from tests.helpers import TmpEnv


class ResEnv(TmpEnv):
    def __init__(self, **kw):
        kw.setdefault("PA_SUBAGENTS", "1")
        kw.setdefault("PA_RESEARCH", "1")
        kw.setdefault("PA_WORKSPACE", "1")
        super().__init__(**kw)


def _ctx(env, channel="chan-1"):
    conn = env.conn()
    ast = conn.execute(
        "SELECT * FROM assistants WHERE name='pal'").fetchone()
    return chattools.ChatToolContext(conn, env.settings, ast, channel,
                                     env=env.env)


class _Bot:
    """tick_notifications calls finish(bot, conn, task) — only .s is
    used (workspace project)."""
    def __init__(self, settings):
        self.s = settings


RESULT_JSON = """Findings below.

Solar output rose sharply.

```json
{"claims": [
   {"text": "EU solar hit 300 TWh in 2024",
    "sources": [1, 2]},
   {"text": "Wind stayed flat",
    "sources": [2]},
   {"text": "Nuclear grew 40%",
    "sources": [9]},
   {"text": "Coal is gone",
    "sources": []}
 ],
 "sources": [
   {"title": "Ember report", "url": "https://ember.example/r1"},
   {"title": "IEA stats", "url": "https://iea.example/s2"}
 ]}
```"""


class TestFlagOff(unittest.TestCase):
    def test_research_off(self):
        env = TmpEnv()
        env.add_assistant(name="pal", tools=[])
        out = chattools.run(_ctx(env), "research",
                            {"question": "q"})
        self.assertEqual(out["error"], "not_configured")

    def test_tool_hidden_when_off(self):
        env = TmpEnv()
        self.assertNotIn("- research ",
                         chattools.prompt_lines(env.settings))
        env2 = ResEnv()
        self.assertIn("- research ", chattools.prompt_lines(env2.settings))


class TestQueue(unittest.TestCase):
    def setUp(self):
        self.env = ResEnv()
        self.env.add_assistant(name="pal", tools=[])
        self.ctx = _ctx(self.env)

    def test_queues_task_and_row(self):
        out = chattools.run(self.ctx, "research",
                            {"question": "cheapest SBC for a node?",
                             "depth": "quick"})
        self.assertTrue(out["queued"], out)
        self.assertEqual(out["depth"], "quick")
        conn = self.env.conn()
        row = conn.execute(
            "SELECT * FROM research_jobs WHERE task_id=?",
            (out["task_id"],)).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["depth"], "quick")
        self.assertEqual(row["question"],
                         "cheapest SBC for a node?")
        sa = conn.execute(
            "SELECT budget_json FROM subagents WHERE task_id=?",
            (out["task_id"],)).fetchone()
        budget = json.loads(sa["budget_json"])
        self.assertEqual(budget["tool_calls"],
                         research.DEPTHS["quick"]["tool_calls"])
        self.assertEqual(budget["minutes"],
                         research.DEPTHS["quick"]["minutes"])
        # child carries the citation-ledger instruction
        t = conn.execute("SELECT body FROM tasks WHERE id=?",
                         (out["task_id"],)).fetchone()
        self.assertIn("```json", t["body"])

    def test_bad_depth(self):
        out = chattools.run(self.ctx, "research",
                            {"question": "q", "depth": "infinite"})
        self.assertEqual(out["error"], "bad_arguments")

    def test_needs_subagents(self):
        env = TmpEnv(PA_RESEARCH="1", PA_WORKSPACE="1")
        env.add_assistant(name="pal", tools=[])
        out = chattools.run(_ctx(env), "research",
                            {"question": "q"})
        self.assertEqual(out["error"], "not_configured")


class TestReport(unittest.TestCase):
    def test_numbered_citations_and_quarantine(self):
        md = research.build_report("EU power?", "standard",
                                   RESULT_JSON, 7)
        self.assertIn("# Research: EU power?", md)
        self.assertIn("- EU solar hit 300 TWh in 2024 [1][2]", md)
        self.assertIn("- Wind stayed flat [2]", md)
        # source index 9 doesn't exist -> quarantined with the empty
        self.assertIn("## Unverified", md)
        self.assertIn("- Nuclear grew 40%", md)
        self.assertIn("- Coal is gone", md)
        self.assertNotIn("[9]", md)
        self.assertIn("[1] Ember report — https://ember.example/r1", md)
        self.assertIn("[2] IEA stats — https://iea.example/s2", md)
        # every line in Findings carries a citation
        findings = md.split("## Findings")[1].split("## ")[0]
        for line in findings.splitlines():
            if line.startswith("- "):
                self.assertIn("[", line, line)

    def test_fallback_without_json_block(self):
        text = "Big summary. See https://a.example/x and " \
               "https://b.example/y for detail."
        md = research.build_report("q", "quick", text, 3)
        self.assertIn("## Summary", md)
        self.assertIn("[1] https://a.example/x", md)
        self.assertIn("[2] https://b.example/y", md)


class TestFinish(unittest.TestCase):
    def setUp(self):
        self.env = ResEnv()
        self.env.add_assistant(name="pal", tools=[])
        self.ctx = _ctx(self.env)

    def _task(self, publish=False):
        out = chattools.run(self.ctx, "research",
                            {"question": "EU power?", "publish": publish})
        conn = self.env.conn()
        conn.execute(
            "UPDATE tasks SET state='succeeded', result_json=?"
            " WHERE id=?",
            (json.dumps({"summary": RESULT_JSON}), out["task_id"]))
        conn.commit()
        return conn.execute("SELECT * FROM tasks WHERE id=?",
                            (out["task_id"],)).fetchone()

    def test_writes_report_to_workspace(self):
        t = self._task()
        conn = self.env.conn()
        note = research.finish(_Bot(self.env.settings), conn, t)
        self.assertIn("report:", note)
        self.assertIn("reports/eu-power-t", note)
        got = workspace.read(conn, self.env.settings, "main",
                             f"reports/eu-power-t{t['id']}.md")
        self.assertIn("## Findings", got["content"])
        self.assertIn("[1] Ember report", got["content"])

    def test_publish_queues_approval(self):
        t = self._task(publish=True)
        conn = self.env.conn()
        note = research.finish(_Bot(self.env.settings), conn, t)
        self.assertIn("publish queued", note)
        pend = approvals.list_pending(conn)
        self.assertEqual(len(pend), 1)
        self.assertEqual(pend[0]["kind"], "job_action")
        payload = json.loads(pend[0]["payload_json"])
        self.assertEqual(payload["tool"], "page_publish")
        self.assertIn("# Research: EU power?",
                      payload["args"]["markdown"])

    def test_no_row_no_note(self):
        env = ResEnv()
        aid = env.add_assistant(name="pal", tools=[])
        conn = env.conn()
        from pa import worker as workmod
        tid = workmod.create_task(conn, aid, "t", "b")
        conn.execute("UPDATE tasks SET state='succeeded' WHERE id=?",
                     (tid,))
        conn.commit()
        t = conn.execute("SELECT * FROM tasks WHERE id=?",
                         (tid,)).fetchone()
        self.assertEqual(
            research.finish(_Bot(env.settings), conn, t), "")


if __name__ == "__main__":
    unittest.main()
