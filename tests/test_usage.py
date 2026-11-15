"""/usage + /settings page."""

import unittest

from pa import db as dbmod, discordbot
from tests.helpers import TmpEnv


class TestUsageCmd(unittest.TestCase):
    def setUp(self):
        self.env = TmpEnv()
        self.env.add_assistant(name="pal", tools=[])
        self.conn = self.env.conn()
        self.bot = discordbot.DiscordBot(
            self.env.settings, env=self.env.env,
            notifier=False, engine=object())

    def test_usage_text(self):
        day = __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc).strftime("%Y-%m-%d")
        self.conn.execute(
            "INSERT INTO usage_days(day, requests, tokens_in,"
            " tokens_out) VALUES(?,7,100,50)", (day,))
        out = self.bot.execute(self.conn, ["usage"], "c1")
        self.assertIn("**7**/", out)
        self.assertIn("150", out)
        self.assertIn("pending approvals", out)

    def test_settings_keys_whitelist(self):
        from pa import webapp
        self.assertIn("voice_notes", webapp.App._SETTINGS_KEYS
                      if hasattr(webapp, "App")
                      else discordbot.DiscordBot.__dict__)


class TestSettingsSave(unittest.TestCase):
    def test_save_and_read_back(self):
        env = TmpEnv()
        conn = env.conn()
        # emulate the POST handler's write path
        for key, v in (("voice_notes", "on"),
                       ("chat_progress", "off")):
            dbmod.set_setting(conn, key, v)
        self.assertEqual(
            dbmod.get_setting(conn, "voice_notes"), "on")
        self.assertEqual(
            dbmod.get_setting(conn, "chat_progress"), "off")

    def test_progress_pref_reads_setting(self):
        env = TmpEnv()
        conn = env.conn()
        bot = discordbot.DiscordBot(
            env.settings, env=env.env, notifier=False,
            engine=object())
        self.assertTrue(bot._chat_progress_on())   # env default
        dbmod.set_setting(conn, "chat_progress", "off")
        conn.close()
        self.assertFalse(bot._chat_progress_on())


if __name__ == "__main__":
    unittest.main()
