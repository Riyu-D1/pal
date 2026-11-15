"""/as per-channel assistant switching."""

import unittest

from pa import chatagent
from tests.helpers import TmpEnv


class TestAsCommand(unittest.TestCase):
    def setUp(self):
        self.env = TmpEnv()
        self.env.add_assistant(name="pal", tools=[])
        self.env.add_assistant(name="coder", tools=[])
        self.conn = self.env.conn()
        from pa import discordbot
        self.bot = discordbot.DiscordBot(
            self.env.settings, env=self.env.env,
            notifier=False, engine=object())

    def test_set_show_clear(self):
        out = self.bot.execute(self.conn, ["as"], "c1")
        self.assertIn("pal", out)
        out = self.bot.execute(self.conn, ["as", "coder"], "c1")
        self.assertIn("coder", out)
        out = self.bot.execute(self.conn, ["as"], "c1")
        self.assertIn("coder", out)
        self.assertIn("override", out)
        out = self.bot.execute(self.conn, ["as", "default"], "c1")
        self.assertIn("global", out)
        row = self.conn.execute(
            "SELECT assistant_name FROM chat_threads"
            " WHERE channel_id='c1'").fetchone()
        self.assertEqual(row["assistant_name"], "")

    def test_unknown_assistant(self):
        out = self.bot.execute(self.conn, ["as", "ghost"], "c1")
        self.assertIn("No assistant", out)

    def test_engine_uses_override(self):
        self.conn.execute(
            "INSERT OR IGNORE INTO chat_threads(channel_id, summary,"
            " assistant_name, created_at, updated_at)"
            " VALUES('c9','','coder','x','x')")
        eng = chatagent.ChatEngine(
            self.env.settings, env=self.env.env,
            brain=lambda m: "```json\n{\"final\":\"hi\"}\n```")
        a = eng._assistant(self.conn, "c9")
        self.assertEqual(a["name"], "coder")
        a2 = eng._assistant(self.conn, "other-channel")
        self.assertEqual(a2["name"], "pal")


if __name__ == "__main__":
    unittest.main()
