"""inbox-watch: fetch+cursor, dedupe, rules, classification, quiet
hours, daily call cap, hourly important batch, /watch command."""

import time
import unittest
from unittest import mock

from pa import db as dbmod, discordbot, gmail as gmailmod, inboxwatch, \
    sysjobs
from tests.helpers import TmpEnv


class FakeAPI:
    def __init__(self):
        self.posts = []

    def open_dm(self, user_id):
        return "dm-1"

    def post_message(self, channel_id, content, **kw):
        self.posts.append((channel_id, content))
        return {"id": "m1"}


class FakeCalls:
    def __init__(self, result=None):
        self.calls = []
        self.result = result or {"calling": True}

    def call(self, message, urgent=False, source="chat"):
        self.calls.append({"message": message, "urgent": urgent,
                           "source": source})
        return dict(self.result)


class FakeGmail:
    """Stands in for gmailmod.GmailClient."""
    queue = []

    def __init__(self, *a, **kw):
        pass

    def list_messages(self, query="", max_results=10):
        return FakeGmail.queue.pop(0) if FakeGmail.queue else []


def _msg(mid, frm="Alice <alice@x.com>", subj="hello",
         internal="1700000000000", snippet="hi"):
    return {"id": mid, "from": frm, "subject": subj,
            "snippet": snippet, "internal_date": internal,
            "labels": ["INBOX"]}


class Base(unittest.TestCase):
    def setUp(self):
        self.env = TmpEnv(DISCORD_OWNER_ID="owner-1")
        self.env.conn().close()
        self.bot = discordbot.DiscordBot(
            self.env.settings, env=self.env.env, notifier=False)
        self.bot.api = FakeAPI()
        self.bot._calls = FakeCalls()
        self.bot._voice = None
        FakeGmail.queue = []
        self._p = mock.patch.object(inboxwatch.gmailmod, "GmailClient",
                                    FakeGmail)
        self._p.start()
        self.addCleanup(self._p.stop)

    def conn(self):
        return dbmod.connect(self.env.db_path)

    def run_job(self):
        return inboxwatch.run(self.bot, {})


class TestFetchAndCursor(Base):
    def test_first_run_primes_cursor_and_alerts_nothing(self):
        FakeGmail.queue = [[_msg("a", internal="1700000005000")]]
        out = self.run_job()
        self.assertIn("primed", out)
        conn = self.conn()
        self.assertEqual(
            dbmod.get_setting(conn, "inbox_watch_cursor"),
            "1700000005000")
        self.assertEqual(self.bot.api.posts, [])
        self.assertEqual(self.bot._calls.calls, [])
        conn.close()

    def test_second_run_fetches_after_cursor(self):
        conn = self.conn()
        dbmod.set_setting(conn, "inbox_watch_cursor", "1700000000000")
        conn.close()
        FakeGmail.queue = [[_msg("b", internal="1700000001000")]]
        seen_q = []

        orig = FakeGmail.list_messages
        def spy(self, query="", max_results=10):
            seen_q.append(query)
            return orig(self, query, max_results)
        FakeGmail.list_messages = spy
        try:
            with mock.patch.object(inboxwatch.sysjobs, "model_text",
                                   return_value="IGNORE"):
                self.run_job()
        finally:
            FakeGmail.list_messages = orig
        self.assertIn("after:1700000000", seen_q[0])
        # cursor moved to the newest message
        conn = self.conn()
        self.assertEqual(dbmod.get_setting(
            conn, "inbox_watch_cursor"), "1700000001000")
        conn.close()

    def test_no_gmail_raises_watcherror(self):
        with mock.patch.object(inboxwatch.gmailmod, "GmailClient",
                               side_effect=gmailmod.GmailNotConfigured(
                                   "no creds")):
            with self.assertRaises(inboxwatch.WatchError):
                self.run_job()


class TestRules(unittest.TestCase):
    def _rule(self, conn, rules, frm):
        for pattern, action in rules:
            conn.execute(
                "INSERT OR REPLACE INTO watch_rules(pattern, action,"
                " created_at) VALUES(?,?,?)", (pattern, action, "now"))
        return inboxwatch._rule_for(conn, frm)

    def test_matching(self):
        env = TmpEnv()
        conn = env.conn()
        try:
            def check(rules, frm, want):
                conn.execute("DELETE FROM watch_rules")
                self.assertEqual(self._rule(conn, rules, frm), want)
            check([("boss@corp.com", "vip")], "Boss <boss@corp.com>",
                  "vip")
            check([("@corp.com", "vip")], "Anyone <x@corp.com>", "vip")
            check([("corp.com", "mute")], "N <n@corp.com>", "mute")
            # mute wins over vip on the same domain
            check([("corp.com", "vip"), ("corp.com", "mute")],
                  "N <n@corp.com>", "mute")
            check([("corp.com", "vip")], "N <n@other.org>", None)
        finally:
            conn.close()


class TestClassify(Base):
    def test_vip_bypasses_model(self):
        conn = self.conn()
        conn.execute(
            "INSERT INTO watch_rules(pattern, action, created_at)"
            " VALUES('mum@fam.uk','vip','now')")
        conn.close()
        with mock.patch.object(inboxwatch.sysjobs, "model_text",
                               side_effect=AssertionError(
                                   "model must not be called")):
            self.assertEqual(inboxwatch.classify(
                self.bot, self.conn(), _msg("m", "Mum <mum@fam.uk>")),
                "urgent")

    def test_model_word_parse(self):
        for text, want in (("URGENT", "urgent"), ("urgent.", "urgent"),
                           ("Important", "important"),
                           ("IGNORE", "ignore"), ("CRITICAL", "critical"),
                           ("I think urgent", "important"),
                           ("", "important")):
            with mock.patch.object(inboxwatch.sysjobs, "model_text",
                                   return_value=text):
                cls = inboxwatch.classify(
                    self.bot, self.conn(), _msg("m1"))
                self.assertEqual(cls, want, text)

    def test_newsletter_heuristic_when_model_down(self):
        with mock.patch.object(inboxwatch.sysjobs, "model_text",
                               return_value=""):
            self.assertEqual(inboxwatch.classify(
                self.bot, self.conn(),
                _msg("n", "Weekly <noreply@news.x>", "newsletter",
                     snippet="unsubscribe here")), "ignore")
            self.assertEqual(inboxwatch.classify(
                self.bot, self.conn(), _msg("p", "Bob <bob@x.com>")),
                "important")


class TestActing(Base):
    def _prime(self):
        conn = self.conn()
        dbmod.set_setting(conn, "inbox_watch_cursor", "1699999999999")
        conn.close()

    def test_urgent_calls_owner_outside_quiet_hours(self):
        self._prime()
        FakeGmail.queue = [[_msg("u1", subj="dentist moved to 9am",
                                 internal="1700000001000")]]
        with mock.patch.object(inboxwatch.sysjobs, "model_text",
                               return_value="URGENT"), \
             mock.patch.object(inboxwatch.callmod, "in_quiet_hours",
                               return_value=False):
            out = self.run_job()
        self.assertEqual(len(self.bot._calls.calls), 1)
        self.assertIn("dentist", self.bot._calls.calls[0]["message"])
        self.assertFalse(self.bot._calls.calls[0]["urgent"])
        conn = self.conn()
        row = conn.execute(
            "SELECT classification, alerted FROM inbox_seen"
            " WHERE message_id='u1'").fetchone()
        self.assertEqual((row["classification"], row["alerted"]),
                         ("urgent", 1))
        conn.close()

    def test_urgent_in_quiet_hours_dms_instead(self):
        self._prime()
        FakeGmail.queue = [[_msg("u2", subj="asap", internal="1")]]
        with mock.patch.object(inboxwatch.sysjobs, "model_text",
                               return_value="URGENT"), \
             mock.patch.object(inboxwatch.callmod, "in_quiet_hours",
                               return_value=True):
            self.run_job()
        self.assertEqual(self.bot._calls.calls, [])
        self.assertTrue(any("URGENT" in c for _, c in self.bot.api.posts))
        self.assertTrue(any("quiet hours" in c
                            for _, c in self.bot.api.posts))

    def test_critical_rings_even_in_quiet_hours(self):
        self._prime()
        FakeGmail.queue = [[_msg("c1", subj="fraud alert",
                                 internal="1")]]
        with mock.patch.object(inboxwatch.sysjobs, "model_text",
                               return_value="CRITICAL"), \
             mock.patch.object(inboxwatch.callmod, "in_quiet_hours",
                               return_value=True):
            self.run_job()
        self.assertEqual(len(self.bot._calls.calls), 1)
        self.assertTrue(self.bot._calls.calls[0]["urgent"])

    def test_failed_call_falls_back_to_dm(self):
        self._prime()
        self.bot._calls.result = {"error": "busy"}
        FakeGmail.queue = [[_msg("u3", subj="hi", internal="1")]]
        with mock.patch.object(inboxwatch.sysjobs, "model_text",
                               return_value="URGENT"), \
             mock.patch.object(inboxwatch.callmod, "in_quiet_hours",
                               return_value=False):
            self.run_job()
        self.assertTrue(any("URGENT" in c for _, c in self.bot.api.posts))

    def test_daily_call_cap(self):
        self._prime()
        day = time.strftime("%Y-%m-%d", time.gmtime())
        conn = self.conn()
        dbmod.set_setting(conn, f"inbox_watch_calls:{day}", "6")
        conn.close()
        FakeGmail.queue = [[_msg("u4", subj="hi", internal="1")]]
        with mock.patch.object(inboxwatch.sysjobs, "model_text",
                               return_value="URGENT"), \
             mock.patch.object(inboxwatch.callmod, "in_quiet_hours",
                               return_value=False):
            self.run_job()
        self.assertEqual(self.bot._calls.calls, [])
        self.assertTrue(any("cap" in c for _, c in self.bot.api.posts))

    def test_dedupe_never_alerts_twice(self):
        self._prime()
        FakeGmail.queue = [[_msg("d1", subj="hi", internal="1")],
                           [_msg("d1", subj="hi", internal="1")]]
        with mock.patch.object(inboxwatch.sysjobs, "model_text",
                               return_value="URGENT"), \
             mock.patch.object(inboxwatch.callmod, "in_quiet_hours",
                               return_value=False):
            self.run_job()
            out = self.run_job()
        self.assertEqual(len(self.bot._calls.calls), 1)
        self.assertIn("0 classified", out)

    def test_important_batches_hourly(self):
        self._prime()
        FakeGmail.queue = [[_msg("i1", subj="minutes attached",
                                 internal="1")]]
        with mock.patch.object(inboxwatch.sysjobs, "model_text",
                               return_value="IMPORTANT"), \
             mock.patch.object(inboxwatch.callmod, "in_quiet_hours",
                               return_value=False):
            self.run_job()               # queues + first flush
        self.assertTrue(any("important" in c.lower()
                            for _, c in self.bot.api.posts))
        posts = list(self.bot.api.posts)
        self.bot.api.posts.clear()
        FakeGmail.queue = [[_msg("i2", subj="second", internal="2")]]
        with mock.patch.object(inboxwatch.sysjobs, "model_text",
                               return_value="IMPORTANT"), \
             mock.patch.object(inboxwatch.callmod, "in_quiet_hours",
                               return_value=False):
            self.run_job()               # queued, not yet due
        self.assertEqual(self.bot.api.posts, [])
        # pretend an hour passed -> next run flushes the pending mail
        conn = self.conn()
        dbmod.set_setting(conn, "inbox_watch_last_batch", "0")
        conn.close()
        FakeGmail.queue = [[]]
        self.run_job()
        self.assertTrue(any("second" in c for _, c in self.bot.api.posts))
        self.assertEqual(self.bot._calls.calls, [])
        self.assertTrue(posts)          # sanity: first batch did post

    def test_ignore_does_nothing(self):
        self._prime()
        FakeGmail.queue = [[_msg("g1", subj="sale", internal="1")]]
        with mock.patch.object(inboxwatch.sysjobs, "model_text",
                               return_value="IGNORE"):
            self.run_job()
        self.assertEqual(self.bot.api.posts, [])
        self.assertEqual(self.bot._calls.calls, [])


class TestWatchCommand(Base):
    def test_add_list_remove(self):
        conn = self.conn()
        out = self.bot.execute(conn, ["watch"])
        self.assertIn("No watch rules", out)
        out = self.bot.execute(conn, ["watch", "vip", "Mum@Fam.UK"])
        self.assertIn("vip", out)
        out = self.bot.execute(conn, ["watch", "mute", "@spam.com"])
        self.assertIn("mute", out)
        out = self.bot.execute(conn, ["watch"])
        self.assertIn("mum@fam.uk", out)
        self.assertIn("@spam.com", out)
        # switch action
        out = self.bot.execute(conn, ["watch", "mute", "mum@fam.uk"])
        self.assertIn("mute", out)
        rows = conn.execute(
            "SELECT action FROM watch_rules WHERE pattern='mum@fam.uk'"
        ).fetchall()
        self.assertEqual(rows[0]["action"], "mute")
        out = self.bot.execute(conn, ["watch", "remove", "mum@fam.uk"])
        self.assertIn("removed", out)
        self.assertIsNone(conn.execute(
            "SELECT 1 FROM watch_rules WHERE pattern='mum@fam.uk'"
        ).fetchone())
        conn.close()


class TestDispatch(Base):
    def test_job_worker_runs_handler_not_react(self):
        conn = self.conn()
        conn.execute(
            "INSERT INTO chat_inbox(channel_id, prompt, source, status,"
            " created_at) VALUES('','p','job:inbox-watch','queued','t')")
        iid = conn.execute("SELECT MAX(id) i FROM chat_inbox").fetchone()
        iid = iid["i"]
        conn.close()
        FakeGmail.queue = [[_msg("x", internal="1")]]
        # engine that would explode if a ReAct turn ran
        self.bot._engine_obj = None
        self.bot._sysjob_worker("inbox-watch", inboxwatch.run, iid)
        conn = self.conn()
        row = conn.execute(
            "SELECT status, reply FROM chat_inbox WHERE id=?",
            (iid,)).fetchone()
        self.assertEqual(row["status"], "done")
        self.assertIn("primed", row["reply"])
        conn.close()

    def test_handler_exception_marks_failed_and_dms(self):
        conn = self.conn()
        conn.execute(
            "INSERT INTO chat_inbox(channel_id, prompt, source, status,"
            " created_at) VALUES('','p','job:inbox-watch','queued','t')")
        iid = conn.execute(
            "SELECT MAX(id) i FROM chat_inbox").fetchone()["i"]
        conn.close()

        def boom(bot, payload):
            raise RuntimeError("gmail dead")
        self.bot._sysjob_worker("inbox-watch", boom, iid)
        conn = self.conn()
        row = conn.execute(
            "SELECT status FROM chat_inbox WHERE id=?", (iid,)).fetchone()
        self.assertEqual(row["status"], "failed")
        conn.close()
        self.assertTrue(any("failed" in c for _, c in self.bot.api.posts))


if __name__ == "__main__":
    unittest.main()
