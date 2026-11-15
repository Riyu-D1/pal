"""inbox triage — pal-DB buckets, local reply drafts
and the hard guarantee that the unattended path never calls a Gmail
write (asserted against a fake client that records every write)."""

import unittest

from pa import activity, approvals, triage
from tests.helpers import TmpEnv


class FakeGmail:
    """Any write-shaped call gets recorded — the triage path must
    never make one."""
    def __init__(self):
        self.writes = []

    def __getattr__(self, name):
        if name.startswith(("send", "modify", "trash", "delete",
                            "create", "update", "label")):
            return lambda *a, **k: self.writes.append(name)
        return lambda *a, **k: None


class TestTriage(unittest.TestCase):
    def setUp(self):
        self.env = TmpEnv(PA_TRIAGE="true")
        self.conn = self.env.conn()

    def tearDown(self):
        self.conn.close()

    def test_bucket_fixtures(self):
        cases = [
            ({"from": "news@digest.substack.com",
              "subject": "Weekly roundup", "snippet": "unsubscribe"},
             "newsletter"),
            ({"from": "noreply@amazon.com",
              "subject": "Your order receipt", "snippet": "invoice"},
             "receipt"),
            ({"from": "prize@lottery.example",
              "subject": "Congratulations winner!", "snippet": ""},
             "spam-ish"),
            ({"from": "alerts@github.com",
              "subject": "Dependabot alert", "snippet": ""}, "fyi"),
            ({"from": "Sam <sam@friend.example>",
              "subject": "dinner Friday?",
              "snippet": "Can you make it?"}, "reply-needed"),
            ({"from": "bank@bank.example",
              "subject": "Statement ready", "snippet": ""}, "fyi"),
        ]
        for msg, want in cases:
            self.assertEqual(triage.bucket(msg), want, msg)

    def test_reply_needed_writes_draft_and_card(self):
        class B:
            s = self.env.settings
        bot = B()
        b = triage.on_new_mail(bot, self.conn, {
            "from": "Sam <sam@friend.example>",
            "subject": "dinner Friday?",
            "snippet": "Can you make it?"}, "m1")
        self.assertEqual(b, "reply-needed")
        row = self.conn.execute(
            "SELECT * FROM inbox_triage WHERE message_id='m1'"
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertTrue(row["draft_approval_id"])
        drafts = activity.list_activity(self.conn, kind="draft")
        self.assertTrue(drafts)
        nap = activity.list_activity(self.conn, kind="needs_approval")
        self.assertTrue(nap)
        ap = approvals.get(self.conn, row["draft_approval_id"])
        self.assertIn("send_email", ap["payload_json"])
        self.assertIn("dinner Friday", ap["payload_json"])

    def test_no_gmail_write_in_unattended_path(self):
        """The whole triage path must stay local — run it against a
        gmail double that records every write-shaped call."""
        fake = FakeGmail()
        import pa.triage as tmod
        old = getattr(tmod, "gmail", None)
        try:
            tmod.gmail = fake            # would record if used
            class B:
                s = self.env.settings
            bot = B()
            triage.on_new_mail(bot, self.conn, {
                "from": "Sam <sam@friend.example>",
                "subject": "quick question",
                "snippet": "can you?"}, "m2")
        finally:
            if old is not None:
                tmod.gmail = old
        self.assertEqual(fake.writes, [])

    def test_today_summary(self):
        class B:
            s = self.env.settings
        bot = B()
        triage.on_new_mail(bot, self.conn, {
            "from": "noreply@x.example", "subject": "alert",
            "snippet": ""}, "a")
        triage.on_new_mail(bot, self.conn, {
            "from": "Sam <s@x.example>", "subject": "call me?",
            "snippet": "can you talk"}, "b")
        out = triage.today(self.conn)
        self.assertEqual(out["buckets"]["fyi"], 1)
        self.assertEqual(out["buckets"]["reply-needed"], 1)
        self.assertEqual(out["reply_needed"][0]["subject"],
                         "call me?")


if __name__ == "__main__":
    unittest.main()
