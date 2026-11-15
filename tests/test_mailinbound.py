"""mail-inbound sysjob (pal's own Gmail -> owner turns, allowlist
only) + PA_NOTIFY_PHONE phone dispatch for high/urgent notifications.

All external surfaces are faked — no Gmail, node or Discord traffic.
"""

import json
import unittest

from pa import db as dbmod, mailinbound, notify, sysjobs
from tests.helpers import TmpEnv


class _Bot:
    """Minimal bot surface the handlers touch: settings, env,
    db_path, api, _voice."""

    def __init__(self, env, posts=None):
        self.s = env.settings
        self.env = env.env
        self.db_path = env.db_path
        self.api = _FakeAPI(posts if posts is not None else [])
        self._voice = None

    def _owner_dm_id(self):
        return "dm-owner"


class _FakeAPI:
    def __init__(self, posts):
        self.posts = posts
        self.deleted = []

    def post_message(self, channel, text, **kw):
        self.posts.append((channel, text))
        return True

    def open_dm(self, uid):
        return "dm-owner"

    def reply(self, channel, text, **kw):
        return self.post_message(channel, text)


class _FakeGmail:
    """Duck-typed GmailClient: canned list + body fetch."""
    def __init__(self, msgs, bodies=None):
        self._msgs = msgs
        self._bodies = bodies or {}
        self.sent = []
        self.listed = []

    def list_messages(self, query="", max_results=10):
        self.listed.append(query)
        return list(self._msgs)

    def get_message(self, mid, meta_only=False):
        return self._bodies.get(mid, {"body": "", "id": mid})

    def send_message(self, to, subject, body):
        self.sent.append((to, subject, body))
        return {"id": "sent1"}


def _msg(mid, frm, subject="hi", snippet="hello there"):
    return {"id": mid, "from": frm, "subject": subject,
            "snippet": snippet, "internal_date": "1"}


class Base(unittest.TestCase):
    def setUp(self):
        self.env = TmpEnv(
            PA_MAIL_INBOUND="true",
            PA_OWNER_EMAIL="me@owner.com",
            PA_MAIL_INBOUND_SENDERS="alt@owner.com",
        )
        self.conn = self.env.conn()
        self.bot = _Bot(self.env, posts=[])

    def _run(self, msgs, bodies=None):
        acct = ("native", _FakeGmail(msgs, bodies))
        # bypass the connection lookup — the fake is the mailbox
        orig = mailinbound._pal_account
        mailinbound._pal_account = lambda b, c: acct
        try:
            return mailinbound._run(self.bot, self.conn)
        finally:
            mailinbound._pal_account = orig
        return None

    def _inbox(self):
        return self.conn.execute(
            "SELECT channel_id, prompt, source, status FROM chat_inbox"
            " ORDER BY id").fetchall()


class TestMailInbound(Base):
    def test_flag_off_no_poll(self):
        self.env.settings.mail_inbound = False
        out = mailinbound.run(self.bot, {})
        self.assertEqual(out, "mail-inbound off")

    def test_no_allowlist(self):
        self.env.settings.owner_email = ""
        self.env.settings.mail_inbound_senders = ""
        out = self._run([_msg("m1", "me@owner.com")])
        self.assertIn("allowlist", out)
        self.assertEqual(self._inbox(), [])

    def test_allowlisted_sender_queued_as_owner_turn(self):
        out = self._run([_msg("m1", "Owner <me@owner.com>",
                              subject="dinner?",
                              snippet="are we eating at 7")])
        self.assertIn("1 accepted", out)
        rows = self._inbox()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source"], "mail")
        self.assertEqual(rows[0]["status"], "queued")
        self.assertIn("dinner?", rows[0]["prompt"])
        self.assertIn("are we eating at 7", rows[0]["prompt"])
        # marked accepted in the shared dedupe table
        seen = self.conn.execute(
            "SELECT classification FROM inbox_seen WHERE message_id='m1'"
        ).fetchone()
        self.assertEqual(seen["classification"], "mail-inbound:accepted")

    def test_extra_allowlist_sender(self):
        out = self._run([_msg("m9", "alt@owner.com")])
        self.assertIn("1 accepted", out)

    def test_non_allowlisted_dropped_no_turn(self):
        out = self._run([
            _msg("m2", "stranger@evil.com"),
            _msg("m3", "me@owner.com")])
        self.assertIn("1 accepted", out)
        self.assertIn("1 dropped", out)
        rows = self._inbox()
        self.assertEqual(len(rows), 1)
        acc = self.conn.execute(
            "SELECT message_id FROM inbox_seen WHERE"
            " classification='mail-inbound:accepted'").fetchall()
        self.assertEqual([r["message_id"] for r in acc], ["m3"])
        drop = self.conn.execute(
            "SELECT classification FROM inbox_seen WHERE message_id='m2'"
        ).fetchone()
        self.assertEqual(drop["classification"], "mail-inbound:dropped")

    def test_seen_messages_not_requeued(self):
        self._run([_msg("m4", "me@owner.com")])
        self._run([_msg("m4", "me@owner.com")])
        self.assertEqual(len(self._inbox()), 1)

    def test_no_pal_gmail_account(self):
        env2 = TmpEnv(PA_MAIL_INBOUND="true",
                      PA_OWNER_EMAIL="me@owner.com")
        bot2 = _Bot(env2, [])
        conn2 = env2.conn()
        # no gmail env creds, no connections -> clean 'no pal gmail'
        out = mailinbound._run(bot2, conn2)
        self.assertIn("no pal gmail", out)

    def test_ack_only_to_allowlisted(self):
        fake = _FakeGmail([_msg("m5", "me@owner.com"),
                           _msg("m6", "x@other.com")])
        orig = mailinbound._pal_account
        mailinbound._pal_account = lambda b, c: ("native", fake)
        try:
            mailinbound._run(self.bot, self.conn)
        finally:
            mailinbound._pal_account = orig
        # exactly one ack, to the allowlisted owner only
        self.assertEqual(len(fake.sent), 1)
        self.assertEqual(fake.sent[0][0], "me@owner.com")


class TestPhoneNotify(Base):
    def _spy(self):
        """Patch nodes.submit_job; return the call list."""
        from unittest.mock import patch
        calls = []
        def _fake(conn, s, cap, args=None, **kw):
            calls.append((cap, args or {}, kw))
            return {"job_id": 7, "status": "queued"}
        p = patch("pa.nodes.submit_job", _fake)
        return p, calls

    def test_phone_off_no_dispatch(self):
        self.env.settings.notify_phone = False
        p, calls = self._spy()
        with p:
            out = notify.notify(_Bot(self.env, []), self.conn,
                                "test", "high", "hello")
        self.assertEqual(out, "sent")
        self.assertEqual(calls, [])

    def test_phone_on_high_dispatches_notify_cap(self):
        self.env.settings.notify_phone = True
        p, calls = self._spy()
        with p:
            out = notify.notify(_Bot(self.env, []), self.conn,
                                "test", "high", "lunch is ready")
        self.assertEqual(out, "sent")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "notify")
        self.assertIn("lunch is ready", calls[0][1]["text"])
        self.assertTrue(calls[0][2]["unattended"])

    def test_phone_on_urgent_dispatches_notify_and_vibrate(self):
        self.env.settings.notify_phone = True
        p, calls = self._spy()
        with p:
            out = notify.notify(_Bot(self.env, []), self.conn,
                                "test", "urgent", "fire alarm")
        self.assertEqual(out, "sent")
        caps = [c[0] for c in calls]
        self.assertEqual(caps, ["notify", "vibrate"])

    def test_vibrate_skipped_when_notify_fails(self):
        self.env.settings.notify_phone = True
        calls = []
        def _fail(conn, s, cap, args=None, **kw):
            calls.append(cap)
            return {"error": "policy_denied", "reason": "no"}
        from unittest.mock import patch
        with patch("pa.nodes.submit_job", _fail):
            out = notify.notify(_Bot(self.env, []), self.conn,
                                "test", "urgent", "ping")
        self.assertEqual(out, "sent")       # DM still went out
        self.assertEqual(calls, ["notify"])  # no vibrate on a dead cap

    def test_normal_priority_no_phone(self):
        self.env.settings.notify_phone = True
        p, calls = self._spy()
        with p:
            notify.notify(_Bot(self.env, []), self.conn,
                          "test", "normal", "quiet note")
        self.assertEqual(calls, [])

    def test_dm_failure_does_not_block_phone(self):
        self.env.settings.notify_phone = True
        bot = _Bot(self.env, [])
        bot._owner_dm_id = lambda: ""        # Discord closed
        p, calls = self._spy()
        with p:
            out = notify.notify(bot, self.conn, "test", "high", "ping")
        self.assertEqual(out, "no_dm")
        self.assertEqual(len(calls), 1)

    def test_queued_notify_row_when_no_node(self):
        """Unpatched: no healthy node -> 'notify' job rows queued (the
        cap itself passed policy — pal -> owner channel)."""
        self.env.settings.notify_phone = True
        out = notify.notify(_Bot(self.env, []), self.conn,
                            "test", "high", "queued path")
        self.assertEqual(out, "sent")
        row = self.conn.execute(
            "SELECT cap, status FROM node_jobs").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["cap"], "notify")
        self.assertEqual(row["status"], "queued")


class TestScheduleSeed(unittest.TestCase):
    def test_mail_inbound_seeded_disabled_interval(self):
        env = TmpEnv()
        conn = env.conn()
        row = conn.execute(
            "SELECT * FROM schedules WHERE name='mail-inbound'").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["enabled"], 0)
        self.assertEqual(row["freq"], "interval")
        self.assertEqual(row["day_of_month"], 5)
        self.assertEqual(row["kind"], "agent_job")
        self.assertTrue(sysjobs.is_system("mail-inbound"))
        self.assertIs(sysjobs.handler_for("mail-inbound"),
                      mailinbound.run)


if __name__ == "__main__":
    unittest.main()
