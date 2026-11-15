"""Evolve item 16: owner-approved provider setup."""

import inspect
import unittest

from pa import approvals
from pa.evolve import provision
from tests.helpers import TmpEnv


class _Bot:
    def __init__(self):
        self.posts = []
        self.api = self

    def _owner_dm_id(self):
        return "DM1"

    def post_message(self, channel, content, **kw):
        self.posts.append((channel, content, kw))


class TestProvision(unittest.TestCase):
    def test_card_has_url_and_promise(self):
        env = TmpEnv()
        conn = env.conn()
        bot = _Bot()
        out = provision.propose(
            conn, env.settings, bot, "Groq API key",
            "fast tier is exhausted", "https://console.groq.com/keys")
        row = approvals.get(conn, out["approval_id"])
        conn.close()
        body = bot.posts[0][1]
        self.assertIn("https://console.groq.com/keys", body)
        self.assertIn("never types passwords", body)
        self.assertEqual(row["status"], "pending")
        self.assertIn("evprov:", str(bot.posts[0][2]))

    def test_http_url_refused(self):
        env = TmpEnv()
        conn = env.conn()
        out = provision.propose(
            conn, env.settings, _Bot(), "x", "y", "http://evil.example")
        conn.close()
        self.assertIn("error", out)

    def test_approve_with_computer_browses(self):
        env = TmpEnv(PA_COMPUTER_ENABLED="true")
        conn = env.conn()
        visited = []
        out = provision.run_approved(
            conn, env.settings, _Bot(), {"url": "https://x.example"},
            browse=visited.append)
        conn.close()
        self.assertEqual(out, "opened")
        self.assertEqual(visited, ["https://x.example"])

    def test_approve_computer_off_dms_url(self):
        env = TmpEnv(PA_COMPUTER_ENABLED="false")
        conn = env.conn()
        bot = _Bot()
        out = provision.run_approved(
            conn, env.settings, bot, {"url": "https://x.example"})
        conn.close()
        self.assertEqual(out, "dm_only")
        self.assertIn("https://x.example", bot.posts[-1][1])

    def test_no_consumer_chat_automation(self):
        import pa.evolve.provision as prov
        import pa.evolve
        import pkgutil
        src = inspect.getsource(prov)
        for m in pkgutil.iter_modules(pa.evolve.__path__):
            mod = __import__(f"pa.evolve.{m.name}", fromlist=["x"])
            src += inspect.getsource(mod)
        for host in ("chatgpt.com", "claude.ai", "gemini.google.com",
                     "chat.openai.com"):
            self.assertNotIn(host, src)


if __name__ == "__main__":
    unittest.main()
