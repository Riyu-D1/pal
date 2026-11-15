"""code_task — queue a coding task on the freebuff agent."""

import unittest

from pa import chattools
from pa.freebuff_session import FREEBUFF_HOST
from tests.helpers import TmpEnv


class TestCodeTask(unittest.TestCase):
    def setUp(self):
        self.env = TmpEnv()
        self.conn = self.env.conn()

    def ctx(self, channel="c1"):
        ast = self.conn.execute(
            "SELECT * FROM assistants WHERE name='pal'").fetchone()
        return chattools.ChatToolContext(
            self.conn, self.env.settings, ast, channel,
            env=self.env.env,
            request_approval=lambda *a: "approved")

    def test_queues_on_freebuff_assistant(self):
        self.env.add_assistant(name="pal", tools=[])
        self.env.add_assistant(name="coder", tools=[],
                               host=FREEBUFF_HOST, model="fb")
        out = chattools.run(self.ctx(), "code_task",
                            {"prompt": "fix the flaky test"})
        self.assertTrue(out["queued"])
        row = self.conn.execute(
            "SELECT state, body, assistant_id FROM tasks").fetchone()
        self.assertEqual(row["state"], "queued")
        self.assertIn("flaky test", row["body"])
        self.assertIsNotNone(self.conn.execute(
            "SELECT 1 FROM audit_log WHERE action='code_task'"
        ).fetchone())

    def test_no_coding_assistant(self):
        self.env.add_assistant(name="pal", tools=[])
        out = chattools.run(self.ctx(), "code_task",
                            {"prompt": "fix it"})
        self.assertEqual(out["error"], "not_configured")

    def test_named_assistant(self):
        self.env.add_assistant(name="pal", tools=[])
        self.env.add_assistant(name="other", tools=[])
        out = chattools.run(self.ctx(), "code_task",
                            {"prompt": "fix", "assistant": "other"})
        self.assertTrue(out["queued"])
        self.assertEqual(out["assistant"], "other")

    def test_gated(self):
        self.assertTrue(
            chattools.CHAT_TOOLS["code_task"]["approval"])


if __name__ == "__main__":
    unittest.main()
