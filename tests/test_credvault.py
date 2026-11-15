"""credential vault + browse_fill_secret."""

import unittest

from pa import chattools, credvault
from tests.helpers import TmpEnv

KEY = "ab" * 32  # PA_SECRETS_KEY hex


class Base(unittest.TestCase):
    def setUp(self):
        self.env = TmpEnv(PA_SECRETS_KEY=KEY)
        self.conn = self.env.conn()


class TestVault(Base):
    def test_add_list_never_shows_secret(self):
        credvault.add(self.conn, self.env.env, "example.com",
                      "me@x.com", "hunter2", "work")
        rows = credvault.list_credentials(self.conn)
        self.assertEqual(rows[0]["domain"], "example.com")
        self.assertEqual(rows[0]["label"], "work")
        self.assertNotIn("hunter2", str(rows))
        raw = self.conn.execute(
            "SELECT blob FROM credentials").fetchone()["blob"]
        self.assertNotIn("hunter2", raw)
        self.assertNotIn("me@x.com", raw)

    def test_domain_suffix_matching(self):
        credvault.add(self.conn, self.env.env, "example.com",
                      "u", "p")
        self.assertIsNotNone(
            credvault._lookup(self.conn, self.env.env,
                              "app.example.com"))
        self.assertIsNone(
            credvault._lookup(self.conn, self.env.env,
                              "evil-example.com"))
        self.assertIsNone(
            credvault._lookup(self.conn, self.env.env,
                              "example.com.evil.net"))

    def test_audit_has_no_secret(self):
        credvault.add(self.conn, self.env.env, "x.com", "u", "sekret")
        rows = self.conn.execute(
            "SELECT detail FROM audit_log").fetchall()
        self.assertNotIn("sekret",
                         " ".join(r["detail"] or "" for r in rows))


class _FakeSvc:
    def __init__(self, nodes, url):
        self.calls = []
        self._nodes, self._url = nodes, url

    def call(self, action, args=None):
        self.calls.append((action, args))
        if action == "snapshot":
            return {"url": self._url, "nodes": self._nodes,
                    "text": ""}
        return {"ok": True}

    def typed(self):
        return [a for act, a in self.calls if act == "type"]


class TestFill(Base):
    def _ctx(self):
        aid = self.env.add_assistant(name="pal", tools=[])
        ast = self.conn.execute(
            "SELECT * FROM assistants WHERE id=?", (aid,)).fetchone()
        return chattools.ChatToolContext(
            self.conn, self.env.settings, ast, "c", env=self.env.env)

    NODES = ["@e1 input type=text \"Email\"", "@e2 a \"link\"",
             "@e3 input type=password \"Password\""]

    def test_fill_types_secret_never_returns_it(self):
        credvault.add(self.conn, self.env.env, "example.com",
                      "me@x.com", "hunter2")
        ctx = self._ctx()
        ctx.computer = _FakeSvc(
            self.NODES, "https://app.example.com/login")
        out = credvault.fill_secret(ctx, "")
        self.assertTrue(out["filled"])
        typed = ctx.computer.typed()
        self.assertEqual(typed[0]["target"], "@e1")
        self.assertEqual(typed[0]["text"], "me@x.com")
        self.assertEqual(typed[1]["target"], "@e3")
        self.assertEqual(typed[1]["text"], "hunter2")
        self.assertNotIn("hunter2", str(out))  # never echoed

    def test_phishing_domain_refused(self):
        credvault.add(self.conn, self.env.env, "example.com",
                      "me@x.com", "hunter2")
        ctx = self._ctx()
        ctx.computer = _FakeSvc(
            self.NODES, "https://evil-example.com/login")
        out = credvault.fill_secret(ctx, "")
        self.assertEqual(out["error"], "no_credential")
        self.assertEqual(ctx.computer.typed(), [])

    def test_no_password_field(self):
        credvault.add(self.conn, self.env.env, "example.com",
                      "u", "p")
        ctx = self._ctx()
        ctx.computer = _FakeSvc(
            ["@e1 input type=text \"Search\""],
            "https://example.com/")
        out = credvault.fill_secret(ctx, "")
        self.assertEqual(out["error"], "no_password_field")

    def test_tool_is_approval_gated(self):
        spec = chattools.CHAT_TOOLS["browse_fill_secret"]
        self.assertTrue(spec["approval"])
        from pa import allowrules
        self.assertNotIn("browse_fill_secret",
                         allowrules.ALWAYS_CAPABLE)


class TestCommand(Base):
    def test_credentials_command(self):
        # with secure capture on (default) the args path seals
        # only while a capture is pending — see test_secretcapture.py
        # for the full matrix (modal, form, paste-redaction).
        from pa import discordbot, secretform
        bot = discordbot.DiscordBot(self.env.settings,
                                    env=self.env.env,
                                    notifier=False, engine=object())
        refused = bot._credentials(
            self.conn, ["add", "example.com", "me@x.com", "hunter2"])
        self.assertIn("don't put secrets", refused)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) c FROM credentials").fetchone()["c"], 0)
        secretform.mark_pending(self.conn, "password")
        out = bot._credentials(
            self.conn, ["add", "example.com", "me@x.com", "hunter2"])
        self.assertIn("saved", out)
        listed = bot._credentials(self.conn, [])
        self.assertIn("example.com", listed)
        self.assertNotIn("hunter2", listed)
        self.assertIn("removed",
                      bot._credentials(self.conn, ["del", "example.com"]))
        self.assertIn("credentials", discordbot._COMMAND_NAMES)
        self.assertIn("credentials", discordbot._EPHEMERAL_COMMANDS)

    def test_credentials_command_flag_off(self):
        # PA_SECRET_CAPTURE=false keeps the base-branch text path
        env2 = TmpEnv(PA_SECRETS_KEY=KEY, PA_SECRET_CAPTURE="false")
        conn2 = env2.conn()
        from pa import discordbot
        bot = discordbot.DiscordBot(env2.settings, env=env2.env,
                                    notifier=False, engine=object())
        out = bot._credentials(
            conn2, ["add", "example.com", "me@x.com", "hunter2"])
        self.assertIn("saved", out)


if __name__ == "__main__":
    unittest.main()
