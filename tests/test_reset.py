"""/reset backup+clear, and the 'tools changed' self-trust note that
goes into the system prompt after connections/deploy changes."""

import json
import unittest
from unittest import mock

from pa import chatagent, connectflows, db as dbmod, discordbot, version
from tests.helpers import TmpEnv


class FakeAPI:
    def post_message(self, channel_id, content, **kw):
        return {"id": "m1"}


def _seed_chat(conn, channel_id="c1"):
    conn.execute(
        "INSERT INTO chat_threads(channel_id, summary, created_at,"
        " updated_at) VALUES(?,?,?,?)",
        (channel_id, "old summary", "t0", "t0"))
    conn.execute(
        "INSERT INTO chat_messages(channel_id, role, content,"
        " created_at) VALUES(?,?,?,?)",
        (channel_id, "user", "hi", "2020-01-01T00:00:00Z"))
    conn.execute(
        "INSERT INTO chat_messages(channel_id, role, content,"
        " created_at) VALUES(?,?,?,?)",
        (channel_id, "assistant", "i can't do that",
         "2020-01-01T00:01:00Z"))


class TestReset(unittest.TestCase):
    def test_backup_and_clear(self):
        env = TmpEnv()
        conn = env.conn()
        try:
            _seed_chat(conn)
            n = dbmod.reset_channel_history(conn, "c1")
            self.assertEqual(n, 2)
            left = conn.execute(
                "SELECT COUNT(*) n FROM chat_messages"
                " WHERE channel_id='c1'").fetchone()["n"]
            self.assertEqual(left, 0)
            th = conn.execute(
                "SELECT summary FROM chat_threads"
                " WHERE channel_id='c1'").fetchone()
            self.assertEqual(th["summary"], "")
            bak = conn.execute(
                "SELECT messages_json FROM chat_history_backups"
                " WHERE channel_id='c1'").fetchone()
            msgs = json.loads(bak["messages_json"])
            self.assertEqual(len(msgs), 2)
            self.assertEqual(msgs[1]["content"], "i can't do that")
        finally:
            conn.close()

    def test_reset_command(self):
        env = TmpEnv()
        bot = discordbot.DiscordBot(env.settings, env=env.env,
                                    notifier=False)
        conn = env.conn()
        try:
            _seed_chat(conn)
            out = bot.execute(conn, ["reset"], channel_id="c1")
            self.assertIn("cleared 2", out)
            out = bot.execute(conn, ["reset"], channel_id="c1")
            self.assertIn("cleared 0", out)
        finally:
            conn.close()


class TestCapsNote(unittest.TestCase):
    def setUp(self):
        self.env = TmpEnv()
        self.conn = self.env.conn()
        self.env.add_assistant(name="chat")

    def tearDown(self):
        self.conn.close()

    def _engine(self):
        return chatagent.ChatEngine(
            self.env.settings, db_path=self.env.db_path,
            env=self.env.env, brain=lambda m: "ok")

    def _note(self, channel_id="c1"):
        eng = self._engine()
        return eng._caps_note(self.conn, channel_id)

    def test_no_stamp_no_note(self):
        self.assertEqual(self._note(), "")

    def test_stamp_newer_than_last_reply_injects(self):
        _seed_chat(self.conn)
        dbmod.set_setting(self.conn, "caps_updated_at",
                          "2020-01-02T00:00:00Z")
        self.assertIn("tools or connections changed",
                      self._note())

    def test_stamp_older_than_last_reply_silent(self):
        _seed_chat(self.conn)
        dbmod.set_setting(self.conn, "caps_updated_at",
                          "2019-01-01T00:00:00Z")
        # no deploy change pending — otherwise it re-stamps caps
        dbmod.set_setting(self.conn, "deploy_sha", version.git_sha())
        self.assertEqual(self._note(), "")

    def test_empty_channel_silent(self):
        dbmod.set_setting(self.conn, "caps_updated_at",
                          "2020-01-02T00:00:00Z")
        self.assertEqual(self._note(channel_id="empty"), "")

    def test_deploy_sha_change_stamps_caps(self):
        _seed_chat(self.conn)
        dbmod.set_setting(self.conn, "caps_updated_at",
                          "2019-01-01T00:00:00Z")
        with mock.patch.object(version, "git_sha",
                               return_value="deadbeef123"):
            note = self._note()
        self.assertIn("tools or connections changed", note)
        self.assertEqual(dbmod.get_setting(self.conn, "deploy_sha"),
                         "deadbeef123")

    def test_note_inside_system_prompt(self):
        _seed_chat(self.conn)
        dbmod.set_setting(self.conn, "caps_updated_at",
                          "2020-01-02T00:00:00Z")
        eng = self._engine()
        assistant = self.conn.execute(
            "SELECT * FROM assistants WHERE name='chat'").fetchone()
        thread = self.conn.execute(
            "SELECT * FROM chat_threads WHERE channel_id='c1'"
        ).fetchone()
        sys_p = eng._system(self.conn, assistant, thread)
        self.assertIn("tools or connections changed", sys_p)


class TestCapsBumps(unittest.TestCase):
    def test_upsert_and_disconnect_bump(self):
        env = TmpEnv(PA_SECRETS_KEY="test-key-1234",
                     PA_PUBLIC_URL="http://localhost:9000")
        conn = env.conn()
        try:
            self.assertEqual(
                dbmod.get_setting(conn, "caps_updated_at"), "")
            connectflows.upsert(
                conn, "testapp", "mcp", "t", "", "active",
                "http://x", "blob", "", {})
            stamp = dbmod.get_setting(conn, "caps_updated_at")
            self.assertTrue(stamp)
            connectflows.disconnect(conn, env.env, "testapp")
            self.assertGreaterEqual(
                dbmod.get_setting(conn, "caps_updated_at"), stamp)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
