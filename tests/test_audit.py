"""Item 13: audit-log writes + /audit command."""

import unittest

from pa import approvals, db as dbmod
from tests.helpers import TmpEnv


class TestAudit(unittest.TestCase):
    def setUp(self):
        self.env = TmpEnv()
        self.conn = self.env.conn()

    def test_decide_is_audited(self):
        aid = approvals.create_approval(
            self.conn, "chat_action",
            {"tool": "send_email", "args": {"to": "x@y.z"}})
        approvals.decide(self.conn, aid, True)
        row = self.conn.execute(
            "SELECT action, detail FROM audit_log"
            " WHERE action='approval_approved'").fetchone()
        self.assertIn("send_email", row["detail"])
        approvals.decide(
            self.conn,
            approvals.create_approval(self.conn, "chat_action",
                                      {"tool": "skill_save",
                                       "args": {}}), False)
        self.assertIsNotNone(self.conn.execute(
            "SELECT 1 FROM audit_log"
            " WHERE action='approval_denied'").fetchone())

    def test_audit_command_lists_and_filters(self):
        from pa import discordbot
        bot = discordbot.DiscordBot(self.env.settings, env=self.env.env,
                                    notifier=False, engine=object())
        dbmod.audit(self.conn, "owner", "memory_forget", "memory 3")
        dbmod.audit(self.conn, "webhook", "bad_signature", "gh")
        out = bot._audit(self.conn, [])
        self.assertIn("memory_forget", out)
        self.assertIn("bad_signature", out)
        out = bot._audit(self.conn, ["memory"])
        self.assertIn("memory_forget", out)
        self.assertNotIn("bad_signature", out)
        self.assertIn("audit", discordbot._COMMAND_NAMES)
        self.assertIn("audit", discordbot._EPHEMERAL_COMMANDS)


if __name__ == "__main__":
    unittest.main()
