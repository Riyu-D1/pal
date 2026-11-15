"""Item 20: delegate + result wake-up into the chat thread."""

import unittest

from pa import chattools, sysjobs
from tests.helpers import TmpEnv


class TestDelegate(unittest.TestCase):
    def setUp(self):
        self.env = TmpEnv()
        aid = self.env.add_assistant(name="pal", tools=[])
        self.conn = self.env.conn()
        ast = self.conn.execute(
            "SELECT * FROM assistants WHERE id=?", (aid,)).fetchone()
        self.ctx = chattools.ChatToolContext(
            self.conn, self.env.settings, ast, "chan-99",
            env=self.env.env)

    def test_enqueues_into_same_channel(self):
        out = chattools.run(self.ctx, "delegate",
                            {"prompt": "summarise the news"})
        self.assertTrue(out["delegated"])
        row = self.conn.execute(
            "SELECT channel_id, source, prompt, status"
            " FROM chat_inbox").fetchone()
        self.assertEqual(row["channel_id"], "chan-99")
        self.assertEqual(row["source"], "job:delegate")
        self.assertEqual(row["status"], "queued")
        self.assertIn("summarise the news", row["prompt"])
        # audited + not a system job (runs the generic unattended path)
        self.assertIsNotNone(self.conn.execute(
            "SELECT 1 FROM audit_log WHERE action='delegate'"
        ).fetchone())
        self.assertFalse(sysjobs.is_system("delegate"))

    def test_empty_prompt(self):
        self.assertEqual(
            chattools.run(self.ctx, "delegate", {"prompt": " "})
            ["error"], "bad_arguments")


if __name__ == "__main__":
    unittest.main()
