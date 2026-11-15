"""final-2 G3: the local activity feed — record/list/render, the
/activity card, pending-approval buttons and the 'while you were
away' greeting."""

import datetime
import unittest

from pa import activity, db as dbmod, goals
from pa.util import now_iso
from tests.helpers import TmpEnv

UTC = datetime.timezone.utc


def _mk(env, settings_flags=(), **kw):
    for k, v in kw.items():
        env.env[k] = v
    conn = env.conn()
    return conn


class TestSchema(unittest.TestCase):
    def test_tables_and_seed(self):
        env = TmpEnv()
        conn = env.conn()
        try:
            for t in ("activity", "inbox_triage"):
                conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()
            row = conn.execute(
                "SELECT * FROM schedules WHERE name='cal-triage'"
                ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["enabled"], 0)
            self.assertEqual(row["kind"], "agent_job")
            self.assertEqual(row["day_of_month"], 5)
        finally:
            conn.close()


class TestActivityModule(unittest.TestCase):
    def setUp(self):
        self.env = TmpEnv()
        self.conn = self.env.conn()

    def tearDown(self):
        self.conn.close()

    def test_record_and_list(self):
        a1 = activity.record(self.conn, "noticed",
                             "3 recruiter mails seen",
                             source="proactive-tick")
        a2 = activity.record(self.conn, "draft",
                             "reply to Alice",
                             ref={"draft": "hi alice — yes!"})
        a3 = activity.record(self.conn, "needs_approval",
                             "send reply to Bob?",
                             ref={"approval_id": "appr-1"})
        rows = activity.list_activity(self.conn)
        self.assertEqual([r["id"] for r in rows], [a3, a2, a1])
        self.assertEqual(rows[1]["ref"]["draft"], "hi alice — yes!")
        # unknown kind folds to noticed
        a4 = activity.record(self.conn, "weird", "x")
        self.assertEqual(activity.get(self.conn, a4)["kind"], "noticed")

    def test_status_and_filters(self):
        a1 = activity.record(self.conn, "noticed", "a")
        a2 = activity.record(self.conn, "did", "b")
        self.assertTrue(activity.set_status(self.conn, a1, "done"))
        self.assertFalse(activity.set_status(self.conn, 99, "done"))
        rows = activity.list_activity(self.conn, status="done")
        self.assertEqual([r["id"] for r in rows], [a1])
        rows = activity.list_activity(self.conn, kind="did")
        self.assertEqual([r["id"] for r in rows], [a2])

    def test_since_hours_window(self):
        old = (datetime.datetime.now(UTC)
               - datetime.timedelta(hours=30)).isoformat()
        activity.record(self.conn, "noticed", "fresh")
        self.conn.execute("UPDATE activity SET created_at=?",
                          (old,))
        self.assertEqual(activity.list_activity(
            self.conn, since_hours=24), [])
        self.assertEqual(len(activity.list_activity(
            self.conn, since_hours=31)), 1)

    def test_card_lines(self):
        activity.record(self.conn, "needs_approval", "send x?",
                        ref={"approval_id": "appr-abc-123"})
        activity.record(self.conn, "draft", "reply to alice",
                        ref={"draft": "hi alice"})
        lines = activity.card_lines(self.conn)
        self.assertEqual(len(lines), 2)
        # newest first
        self.assertIn("reply to alice", lines[0])
        self.assertIn("draft: hi alice", lines[0])
        self.assertIn("send x?", lines[1])
        self.assertIn("appr-abc", lines[1])

    def test_while_away_empty_and_full(self):
        self.assertEqual(activity.while_away(self.conn), "")
        activity.record(self.conn, "noticed", "saw recruiter mail")
        activity.record(self.conn, "did", "saved a memory")
        out = activity.while_away(self.conn)
        self.assertIn("while you were away", out)
        self.assertIn("saw recruiter mail", out)

    def test_pending_approvals(self):
        from pa import approvals
        conn = self.conn
        activity.record(conn, "needs_approval", "send reply",
                        ref={"approval_id": "no-such"})
        aid = approvals.create_approval(
            conn, "job_action",
            {"tool": "email_send",
             "args": {"to": "a@b.c", "subject": "s", "body": "b"},
             "channel_id": "ch", "source": "job:t"})
        activity.record(conn, "needs_approval", "send reply 2",
                        ref={"approval_id": aid})
        rows = activity.pending_approvals(conn)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["tool"], "email_send")


class TestActivityCommand(unittest.TestCase):
    def setUp(self):
        self.env = TmpEnv(PA_ACTIVITY="true")
        from pa import discordbot
        self.bot = discordbot.DiscordBot(
            self.env.settings, env=self.env.env, notifier=False)

    def _cmd(self, *argv):
        conn = self.env.conn()
        try:
            return self.bot.execute(conn, list(argv), "chan-1")
        finally:
            conn.close()

    def test_flag_off_message(self):
        env = TmpEnv()
        from pa import discordbot
        bot = discordbot.DiscordBot(env.settings, env=env.env,
                                    notifier=False)
        conn = env.conn()
        try:
            out = bot.execute(conn, ["activity"], "chan-1")
        finally:
            conn.close()
        self.assertIn("PA_ACTIVITY", out)

    def test_empty_and_list(self):
        self.assertIn("Nothing", self._cmd("activity"))
        conn = self.env.conn()
        try:
            activity.record(conn, "noticed", "saw 3 mails",
                            source="inbox-watch")
        finally:
            conn.close()
        out = self._cmd("activity")
        self.assertIn("saw 3 mails", out)
        # week span reaches older rows
        conn = self.env.conn()
        try:
            conn.execute(
                "UPDATE activity SET created_at=?",
                ((datetime.datetime.now(UTC)
                  - datetime.timedelta(hours=30)).isoformat(),))
        finally:
            conn.close()
        self.assertNotIn("saw 3 mails", self._cmd("activity"))
        self.assertIn("saw 3 mails", self._cmd("activity", "week"))


class TestAwayGreeting(unittest.TestCase):
    def setUp(self):
        self.env = TmpEnv(PA_ACTIVITY="true",
                          PA_ACTIVITY_GREETING="true",
                          DISCORD_OWNER_ID="42")
        from pa import discordbot
        self.bot = discordbot.DiscordBot(
            self.env.settings, env=self.env.env, notifier=False)
        self.bot.api = type("API", (), {
            "open_dm": lambda s, uid: "chan-1"})()

    def _silence(self, hours):
        old = (datetime.datetime.now(UTC)
               - datetime.timedelta(hours=hours)).isoformat()
        conn = self.env.conn()
        try:
            conn.execute(
                "INSERT INTO chat_messages(channel_id, role, content,"
                " created_at) VALUES('chan-1','user','hi',?)",
                (old,))
        finally:
            conn.close()

    def test_no_silence_no_greeting(self):
        self._silence(1)
        self.assertEqual(self.bot._away_greeting("chan-1"), "")

    def test_after_4h_empty_feed_gives_empty(self):
        self._silence(6)
        self.assertEqual(self.bot._away_greeting("chan-1"), "")

    def test_after_4h_with_feed(self):
        self._silence(6)
        conn = self.env.conn()
        try:
            activity.record(conn, "noticed", "saw 3 recruiter mails")
        finally:
            conn.close()
        out = self.bot._away_greeting("chan-1")
        self.assertIn("while you were away", out)
        self.assertIn("saw 3 recruiter mails", out)

    def test_other_channel_gets_nothing(self):
        self._silence(6)
        self.assertEqual(self.bot._away_greeting("chan-2"), "")


if __name__ == "__main__":
    unittest.main()
