"""Evolve item 5: changelog ledger, /evolve command, weekly digest."""

import os
import unittest

from pa import discordbot, scheduler as sch, sysjobs
from pa.evolve import changelog, digest, items
from tests.helpers import FakeGateway, Fixture, TmpEnv
from tests.test_discordchat import OWNER


class _API:
    def __init__(self):
        self.posts = []

    def post_message(self, channel, content, **kw):
        self.posts.append((channel, content))
        return {"id": "1"}


class _FakeBot:
    def __init__(self, env):
        self.env, self.s = env.env, env.settings
        self.api = _API()
        self._env = env

    def _db(self):
        return self._env.conn()

    def _owner_dm_id(self):
        return "DM123"


class TestChangelog(unittest.TestCase):
    def setUp(self):
        self.env = TmpEnv()

    def test_log_event_appends_row_and_note(self):
        conn = self.env.conn()
        changelog.log_event(conn, self.env.settings, 7, "applied",
                            "skill brew-tea v2")
        rows = changelog.recent(conn)
        conn.close()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["verb"], "applied")
        self.assertEqual(rows[0]["item_id"], 7)
        # no vault configured -> <data>/evolve/YYYY-MM.md
        base = os.path.join(os.path.dirname(self.env.db_path),
                            "evolve")
        files = [f for f in os.listdir(base) if f.endswith(".md")]
        self.assertEqual(len(files), 1)
        with open(os.path.join(base, files[0])) as f:
            body = f.read()
        self.assertIn("skill brew-tea v2", body)
        self.assertIn("item #7", body)

    def test_seeded_disabled_and_system(self):
        conn = self.env.conn()
        self.assertTrue(sysjobs.is_system("evolve-digest"))
        self.assertIsNotNone(sysjobs.handler_for("evolve-digest"))
        row = sch.get_schedule(conn, "evolve-digest")
        conn.close()
        self.assertEqual(row["enabled"], 0)
        self.assertEqual(row["freq"], "weekly")


class TestEvolveCommand(unittest.TestCase):
    def setUp(self):
        self.gw = FakeGateway()
        self.fx = Fixture()
        self.env = TmpEnv(
            DISCORD_BOT_TOKEN="fake-bot-token",
            DISCORD_OWNER_ID=OWNER,
            DISCORD_API_BASE=self.fx.web_base,
            DISCORD_GATEWAY_URL=self.gw.url)
        self.bot = discordbot.DiscordBot(
            self.env.settings, env=self.env.env, notifier=False,
            engine=object())

    def tearDown(self):
        self.bot.stop()
        self.gw.close()
        self.fx.close()

    def test_evolve_lists_items_and_events(self):
        conn = self.env.conn()
        iid = items.open_item(conn, "fix", "high",
                              title="tool_error: weather dead",
                              dedupe_key="fp:abc")
        changelog.log_event(conn, self.env.settings, iid, "applied",
                            "fixed the thing")
        out = self.bot.execute(conn, ["evolve"], "dm1")
        conn.close()
        self.assertIn("evolve: off", out)
        self.assertIn(f"#{iid}", out)
        self.assertIn("weather dead", out)
        self.assertIn("applied", out)
        self.assertNotIn("token", out.lower())
        self.assertNotIn("secret", out.lower())

    def test_evolve_id_shows_plan(self):
        conn = self.env.conn()
        iid = items.open_item(conn, "skill", "low",
                              title="learn brew-tea",
                              plan="1. write skill\n2. eval\n3. apply")
        out = self.bot.execute(conn, ["evolve", str(iid)], "dm1")
        conn.close()
        self.assertIn("skill/low", out)
        self.assertIn("write skill", out)
        out = self.bot.execute(conn, ["evolve", "999"], "dm1")
        self.assertIn("no evolve item", out)


class TestDigest(unittest.TestCase):
    def test_quiet_when_empty(self):
        env = TmpEnv()
        bot = _FakeBot(env)
        self.assertEqual(digest.run(bot, {}), "")
        self.assertEqual(bot.api.posts, [])

    def test_sends_report_when_events_exist(self):
        env = TmpEnv()
        bot = _FakeBot(env)
        conn = env.conn()
        iid = items.open_item(conn, "skill", "low",
                              title="learn brew-tea")
        changelog.log_event(conn, env.settings, iid, "applied",
                            "skill brew-tea v1")
        items.open_item(conn, "connector", "low",
                        title="not_configured: browse_goto")
        out = digest.run(bot, {})
        conn.close()
        self.assertEqual(out, "digest sent")
        self.assertEqual(len(bot.api.posts), 1)
        channel, body = bot.api.posts[0]
        self.assertEqual(channel, "DM123")
        self.assertIn("brew-tea", body)
        self.assertIn("open item", body)


if __name__ == "__main__":
    unittest.main()
