"""Evolve item 19: /usage evolve section + settings whitelist."""

import unittest

from pa import db as dbmod, discordbot
from pa.evolve import changelog, items
from tests.helpers import FakeGateway, Fixture, TmpEnv
from tests.test_discordchat import OWNER


def _bot(env):
    return discordbot.DiscordBot(
        env.settings, env=env.env, notifier=False, engine=object())


class TestUsage(unittest.TestCase):
    def setUp(self):
        self.gw = FakeGateway()
        self.fx = Fixture()
        self.env = TmpEnv(
            DISCORD_BOT_TOKEN="fake-bot-token",
            DISCORD_OWNER_ID=OWNER,
            DISCORD_API_BASE=self.fx.web_base,
            DISCORD_GATEWAY_URL=self.gw.url,
            PA_EVOLVE="true", PA_ROUTER_ESCALATE="true")
        self.bot = _bot(self.env)

    def tearDown(self):
        self.bot.stop()
        self.gw.close()
        self.fx.close()

    def test_usage_shows_evolve_section(self):
        conn = self.env.conn()
        iid = items.open_item(conn, "lesson", "low", title="t")
        changelog.log_event(conn, self.env.settings, iid, "applied",
                            "lesson applied")
        dbmod.set_setting(conn, "router_escalate:2099-01-01", "x")
        import datetime
        day = datetime.datetime.now(datetime.timezone.utc) \
            .strftime("%Y-%m-%d")
        dbmod.set_setting(conn, f"router_escalate:{day}", "4")
        out = self.bot._usage_cmd(conn)
        conn.close()
        self.assertIn("evolve: on", out)
        self.assertIn("open items: 1", out)
        self.assertIn("last applied", out)
        self.assertIn("escalate today: 4/20", out)

    def test_usage_without_evolve_tables_is_clean(self):
        conn = self.env.conn()
        conn.execute("DROP TABLE evolve_items")
        conn.execute("DROP TABLE evolve_changelog")
        out = self.bot._usage_cmd(conn)
        conn.close()
        self.assertIn("requests", out)
        self.assertNotIn("evolve:", out)


class TestSettingsWhitelist(unittest.TestCase):
    def test_unknown_keys_rejected(self):
        from pa.webapp import App
        from tests.helpers import WSGIClient
        env = TmpEnv()
        env.add_owner()
        app, c = App(env.settings), WSGIClient(
            App(env.settings))
        c.login("owner", "correct horse 99")
        token = c.csrf(app)
        status, _, _ = c.post(
            "/settings",
            {"router_escalate:2099": "999", "evil_key": "1",
             "csrf": token},
            headers={"Origin": "http://testserver"})
        conn = env.conn()
        rows = conn.execute(
            "SELECT key FROM pa_settings WHERE key IN"
            " ('router_escalate:2099','evil_key')").fetchall()
        conn.close()
        self.assertTrue(status.startswith("303"))
        self.assertEqual(rows, [])


if __name__ == "__main__":
    unittest.main()
