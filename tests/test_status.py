"""/status health card + deduplicated failure alerts (job 2x fail,
expired connection, all TTS down)."""

import time
import unittest
from unittest import mock

from pa import db as dbmod, discordbot, status as statusmod, \
    voice as voicemod
from tests.helpers import TmpEnv


class FakeAPI:
    def __init__(self):
        self.posts = []

    def open_dm(self, user_id):
        return "dm-1"

    def post_message(self, channel_id, content, **kw):
        self.posts.append((channel_id, content))
        return {"id": "m1"}


class TestBuild(unittest.TestCase):
    def test_card_sections(self):
        env = TmpEnv(GEMINI_API_KEY="k", GROQ_API_KEY="k2",
                     COMPOSIO_API_KEY="ck")
        conn = env.conn()
        try:
            conn.execute(
                "INSERT INTO connections(provider, kind, account,"
                " account_label, status, server_url, created_at,"
                " updated_at) VALUES('notion','mcp','','lbl','active',"
                " 'http://mcp','t','t')")
            conn.execute(
                "INSERT INTO voice_usage(name, day, n) VALUES"
                " ('tts:gemini', ?, 3)",
                (time.strftime("%Y-%m-%d", time.gmtime()),))
            out = statusmod.build(
                conn, env.settings, env.env,
                probe=lambda *a: "probe ok")
            self.assertIn(f"schema v{dbmod.SCHEMA_VERSION}", out)
            self.assertIn("notion", out)
            self.assertIn("probe ok", out)
            self.assertIn("tts gemini", out)
            self.assertIn("3 today", out)
            self.assertIn("inbox-watch", out)
            self.assertIn("morning-briefing", out)
            self.assertIn("composio calls", out)
        finally:
            conn.close()

    def test_probe_never_raises(self):
        env = TmpEnv(COMPOSIO_API_KEY="ck")
        conn = env.conn()
        try:
            conn.execute(
                "INSERT INTO connections(provider, kind, account,"
                " account_label, status, server_url, meta_json,"
                " created_at, updated_at) VALUES('gmail','composio',"
                " '','','active','','{}','t','t')")
            txt = statusmod.probe_connection(conn, env.settings,
                                             env.env, dict(
                                                 provider="gmail",
                                                 kind="composio",
                                                 status="active",
                                                 meta_json="{}"))
            self.assertIn("no connected account id", txt)
        finally:
            conn.close()


class TestHealthAlerts(unittest.TestCase):
    def _conn(self, env):
        return env.conn()

    def test_job_failed_twice_alerts_once_then_clears(self):
        env = TmpEnv()
        conn = env.conn()
        try:
            conn.execute(
                "INSERT INTO schedules(name, kind, freq, time_of_day,"
                " timezone, enabled, payload_json, created_at,"
                " updated_at) VALUES('j1','agent_job','daily','07:00',"
                " 'Europe/London',1,'{}','t','t')")
            for st in ("failed", "failed"):
                conn.execute(
                    "INSERT INTO chat_inbox(channel_id, prompt, source,"
                    " status, created_at) VALUES('','','job:j1',?,'t')",
                    (st,))
            alerts, clears = statusmod.health_alerts(
                conn, env.settings, env.env)
            self.assertIn(("jobfail:j1", mock.ANY), alerts)
            self.assertTrue(any(k == "jobfail:j1" for k, _ in alerts))
            # one more inbox row 'done' -> clears
            conn.execute(
                "INSERT INTO chat_inbox(channel_id, prompt, source,"
                " status, created_at) VALUES('','','job:j1','done','t')")
            alerts, clears = statusmod.health_alerts(
                conn, env.settings, env.env)
            self.assertIn("jobfail:j1", clears)
        finally:
            conn.close()

    def test_expired_connection_alerts(self):
        env = TmpEnv()
        conn = env.conn()
        try:
            conn.execute(
                "INSERT INTO connections(provider, kind, account,"
                " account_label, status, created_at, updated_at)"
                " VALUES('gmail','composio','','','expired','t','t')")
            alerts, clears = statusmod.health_alerts(
                conn, env.settings, env.env)
            self.assertTrue(any(k == "conn:gmail" for k, _ in alerts))
            conn.execute(
                "UPDATE connections SET status='active'")
            alerts, clears = statusmod.health_alerts(
                conn, env.settings, env.env)
            self.assertIn("conn:gmail", clears)
        finally:
            conn.close()

    def test_all_tts_down_alerts(self):
        env = TmpEnv(GEMINI_API_KEY="k", FISH_API_KEY="k2")
        conn = env.conn()
        try:
            voicemod._COOLDOWNS["gemini"] = time.monotonic() + 300
            voicemod._COOLDOWNS["fish"] = time.monotonic() + 300
            alerts, clears = statusmod.health_alerts(
                conn, env.settings, env.env)
            self.assertTrue(any(k.startswith("tts-down:")
                                for k, _ in alerts))
            voicemod._COOLDOWNS.clear()
            alerts, clears = statusmod.health_alerts(
                conn, env.settings, env.env)
            self.assertTrue(any(k.startswith("tts-down:")
                                for k in clears))
        finally:
            conn.close()
            voicemod._COOLDOWNS.clear()


class TestTickHealth(unittest.TestCase):
    def setUp(self):
        self.env = TmpEnv(DISCORD_OWNER_ID="o1")
        self.env.conn().close()
        self.bot = discordbot.DiscordBot(
            self.env.settings, env=self.env.env, notifier=False)
        self.bot.api = FakeAPI()

    def _fail_job(self):
        conn = dbmod.connect(self.env.db_path)
        conn.execute(
            "INSERT INTO schedules(name, kind, freq, time_of_day,"
            " timezone, enabled, payload_json, created_at, updated_at)"
            " VALUES('j1','agent_job','daily','07:00','Europe/London',"
            " 1,'{}','t','t')")
        for _ in range(2):
            conn.execute(
                "INSERT INTO chat_inbox(channel_id, prompt, source,"
                " status, created_at) VALUES('','','job:j1','failed',"
                " 't')")
        conn.close()

    def test_dm_once_deduped_then_again_after_clear(self):
        self._fail_job()
        self.bot.tick_health()
        self.assertEqual(len(self.bot.api.posts), 1)
        self.assertIn("failed twice", self.bot.api.posts[0][1])
        self.bot.tick_health()
        self.assertEqual(len(self.bot.api.posts), 1)   # deduped
        # job succeeds -> key cleared -> next failure alerts again
        conn = dbmod.connect(self.env.db_path)
        conn.execute(
            "INSERT INTO chat_inbox(channel_id, prompt, source, status,"
            " created_at) VALUES('','','job:j1','done','t')")
        conn.close()
        self.bot.tick_health()
        self.assertEqual(len(self.bot.api.posts), 1)
        for _ in range(2):
            conn = dbmod.connect(self.env.db_path)
            conn.execute(
                "INSERT INTO chat_inbox(channel_id, prompt, source,"
                " status, created_at) VALUES('','','job:j1','failed',"
                " 't')")
            conn.close()
        self.bot.tick_health()
        self.assertEqual(len(self.bot.api.posts), 2)


class TestStatusCommand(unittest.TestCase):
    def test_bare_status_is_system_card(self):
        env = TmpEnv()
        bot = discordbot.DiscordBot(env.settings, env=env.env,
                                    notifier=False)
        bot.api = FakeAPI()
        conn = env.conn()
        try:
            out = bot.execute(conn, ["status"])
            self.assertIn("schema v", out)
            self.assertIn("Jobs", out)
        finally:
            conn.close()

    def test_status_with_id_shows_task(self):
        env = TmpEnv()
        bot = discordbot.DiscordBot(env.settings, env=env.env,
                                    notifier=False)
        conn = env.conn()
        try:
            out = bot.execute(conn, ["status", "99"])
            self.assertIn("no task", out)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
