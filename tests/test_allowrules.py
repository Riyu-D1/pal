"""Item 10: standing allow rules — auto-approve safe tools only."""

import unittest

from pa import allowrules, chattools
from tests.helpers import TmpEnv


class TestRules(unittest.TestCase):
    def setUp(self):
        self.env = TmpEnv()
        self.conn = self.env.conn()

    def test_add_whitelists_tools(self):
        self.assertIn("error", allowrules.add(self.conn, "send_email"))
        self.assertIn("error", allowrules.add(self.conn, "shell"))
        self.assertIn("error", allowrules.add(self.conn, "mac"))
        self.assertIn("error",
                      allowrules.add(self.conn, "browse_fill_secret"))
        self.assertIn("error",
                      allowrules.add(self.conn, "code_task"))
        self.assertTrue(
            allowrules.add(self.conn, "watch_add")["saved"])

    def test_match_and_auto_approve(self):
        allowrules.add(self.conn, "watch_add", "gmail")
        self.assertTrue(allowrules.auto_approve(
            self.conn, "watch_add",
            {"kind": "gmail", "query": "is:unread"}))
        self.assertFalse(allowrules.auto_approve(
            self.conn, "watch_add", {"kind": "web", "query": "u"}))
        # audited
        self.assertIsNotNone(self.conn.execute(
            "SELECT 1 FROM audit_log WHERE action='auto_allow'"
        ).fetchone())

    def test_empty_pattern_matches_all(self):
        allowrules.add(self.conn, "skill_save")
        self.assertTrue(allowrules.auto_approve(
            self.conn, "skill_save", {"name": "x"}))

    def test_remove(self):
        allowrules.add(self.conn, "watch_add")
        rid = allowrules.list_rules(self.conn)[0]["id"]
        self.assertTrue(allowrules.remove(self.conn, rid))
        self.assertFalse(allowrules.auto_approve(
            self.conn, "watch_add", {}))


class TestCtxGate(unittest.TestCase):
    def test_approve_short_circuits_with_rule(self):
        env = TmpEnv()
        aid = env.add_assistant(name="pal", tools=[])
        conn = env.conn()
        ast = conn.execute(
            "SELECT * FROM assistants WHERE id=?", (aid,)).fetchone()
        ctx = chattools.ChatToolContext(
            conn, env.settings, ast, "c", env=env.env)
        allowrules.add(conn, "watch_add")
        calls = []
        ctx.request_approval = lambda n, a, i: calls.append(n)
        self.assertEqual(ctx.approve("watch_add",
                                     {"kind": "web", "query": "u"}),
                         "approved")
        self.assertEqual(calls, [])          # never asked
        self.assertIsNone(conn.execute(
            "SELECT 1 FROM approvals").fetchone())  # no row either

    def test_unattended_rule_runs_not_queues(self):
        env = TmpEnv()
        aid = env.add_assistant(name="pal", tools=[])
        conn = env.conn()
        ast = conn.execute(
            "SELECT * FROM assistants WHERE id=?", (aid,)).fetchone()
        ctx = chattools.ChatToolContext(
            conn, env.settings, ast, "c", env=env.env, unattended=True)
        allowrules.add(conn, "watch_add")
        self.assertEqual(ctx.approve("watch_add", {"kind": "gmail",
                                                   "query": "x"}),
                         "approved")
        self.assertEqual(ctx.pending, [])


class TestCommand(unittest.TestCase):
    def test_allow_command(self):
        env = TmpEnv()
        conn = env.conn()
        from pa import discordbot
        bot = discordbot.DiscordBot(env.settings, env=env.env,
                                    notifier=False, engine=object())
        self.assertIn("No standing allow rules",
                      bot._allow(conn, []))
        self.assertIn("rule saved",
                      bot._allow(conn, ["add", "watch_add"]))
        self.assertIn("must be approved",
                      bot._allow(conn, ["add", "send_email"]))
        self.assertIn("#1", bot._allow(conn, []))
        self.assertIn("removed", bot._allow(conn, ["del", "1"]))
        self.assertIn("allow", discordbot._COMMAND_NAMES)


if __name__ == "__main__":
    unittest.main()
