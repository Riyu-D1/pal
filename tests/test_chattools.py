"""Chat tools: every registry tool against fakes, approval pause/resume,
the DDG fallback parser, sandboxed shell and the mac SSH runner."""

import json
import os
import threading
import time
import unittest

from pa import approvals, chattools, db as dbmod, sandbox, scheduler as sch
from pa.gcal import GCalError
from pa.gmail import GmailError
from pa.search import DuckDuckGoSearch, SearchError
from tests.helpers import TmpEnv


def ctx_for(env, assistant, **kw):
    conn = env.conn()
    return chattools.ChatToolContext(
        conn, env.settings, assistant, "chan-1", env=env.env, **kw)


class Base(unittest.TestCase):
    def setUp(self):
        self.env = TmpEnv()
        self.aid = self.env.add_assistant(name="pal", tools=[])
        conn = self.env.conn()
        self.assistant = conn.execute(
            "SELECT * FROM assistants WHERE id=?", (self.aid,)).fetchone()
        conn.close()


class TestMemoryTools(Base):
    def test_save_and_search(self):
        ctx = ctx_for(self.env, self.assistant)
        r = chattools.run(ctx, "memory_save",
                          {"content": "owner likes green tea"})
        self.assertTrue(r["saved"])
        r = chattools.run(ctx, "memory_search", {"query": "tea"})
        self.assertEqual(r["count"], 1)
        self.assertIn("green tea", r["results"][0]["content"])


class TestSearchFetch(Base):
    def test_web_search_uses_injected_service(self):
        class FakeSearch:
            def search(self, q):
                return [{"url": "https://x.example", "title": "t",
                         "content": "c"}]
        ctx = ctx_for(self.env, self.assistant,
                      search_impl=FakeSearch())
        r = chattools.run(ctx, "web_search", {"query": "london weather"})
        self.assertEqual(r["count"], 1)

    def test_web_search_blocks_private_leak(self):
        ctx = ctx_for(self.env, self.assistant)
        chattools.run(ctx, "memory_save",
                      {"content": "my secret diary password is tuna"})
        r = chattools.run(ctx, "web_search",
                          {"query": "my secret diary password is tuna"})
        self.assertEqual(r["error"], "sensitive_query")

    def test_fetch_url(self):
        def fake_fetch(url, settings):
            return {"final_url": url, "title": "t",
                    "content_type": "text/html",
                    "text": "<html><body><p>hello world</p></body></html>"}
        ctx = ctx_for(self.env, self.assistant, fetch_impl=fake_fetch)
        r = chattools.run(ctx, "fetch_url",
                          {"url": "https://x.example"})
        self.assertIn("hello world", r["excerpt"])

    def test_ddg_parser(self):
        page = """
        <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fpage&rut=x">Example Title</a>
        <a class="result__snippet">first snippet</a>
        <a class="result__a" href="https://direct.example/x">Direct</a>
        """
        ddg = DuckDuckGoSearch("https://html.duckduckgo.com",
                               fetch_impl=lambda u: {"text": page})
        out = ddg.search("q")
        self.assertEqual(out[0]["url"], "https://example.com/page")
        self.assertEqual(out[0]["title"], "Example Title")
        self.assertEqual(out[0]["content"], "first snippet")
        self.assertEqual(out[1]["url"], "https://direct.example/x")

    def test_ddg_empty_raises(self):
        ddg = DuckDuckGoSearch("https://x",
                               fetch_impl=lambda u: {"text": "captcha"})
        with self.assertRaises(SearchError):
            ddg.search("q")


class FakeGmail:
    def __init__(self):
        self.sent = []
        self.drafts = []

    def list_messages(self, query="", max_results=10):
        return [{"id": "m1", "subject": "hi", "from": "a@b.c",
                 "date": "d", "snippet": "s"}]

    def get_message(self, mid):
        return {"id": mid, "subject": "hi", "body": "wrapped"}

    def create_draft(self, to, subject, body):
        self.drafts.append((to, subject, body))
        return {"draft_id": "d1", "to": to, "subject": subject}

    def send_message(self, to, subject, body):
        self.sent.append((to, subject, body))
        return {"id": "sent-1", "to": to}


class FakeGcal:
    def __init__(self):
        self.created = []

    def list_events(self, days_ahead=7, max_results=10):
        return [{"id": "e1", "summary": "standup",
                 "start": "2026-11-10T09:00", "end": "2026-11-10T09:30"}]

    def create_event(self, summary, start, end, timezone=None,
                     description="", location=""):
        self.created.append((summary, start, end, timezone))
        return {"id": "new-1", "summary": summary}


class TestMailCalendar(Base):
    def test_email_list_read_draft(self):
        gmail = FakeGmail()
        ctx = ctx_for(self.env, self.assistant,
                      gmail_factory=lambda s, e: gmail)
        self.assertEqual(chattools.run(ctx, "email_list", {})["count"], 1)
        self.assertEqual(
            chattools.run(ctx, "email_read", {"id": "m1"})["subject"],
            "hi")
        r = chattools.run(ctx, "draft_email",
                          {"to": "x@y.z", "subject": "s", "body": "b"})
        self.assertEqual(r["draft_id"], "d1")
        self.assertEqual(len(gmail.drafts), 1)   # draft needs no approval

    def test_send_email_needs_and_uses_approval(self):
        gmail = FakeGmail()
        calls = []

        def approve(name, args, aid):
            calls.append((name, aid))
            conn = self.env.conn()
            try:
                self.assertEqual(
                    approvals.get(conn, aid)["status"], "pending")
                approvals.decide(conn, aid, True)
            finally:
                conn.close()
            return "approved"

        ctx = ctx_for(self.env, self.assistant,
                      request_approval=approve,
                      gmail_factory=lambda s, e: gmail)
        r = chattools.run(ctx, "send_email",
                          {"to": "x@y.z", "subject": "s", "body": "b"})
        self.assertEqual(r["id"], "sent-1")
        self.assertEqual(gmail.sent, [("x@y.z", "s", "b")])
        self.assertEqual(calls[0][0], "send_email")

    def test_send_email_denied_never_sends(self):
        gmail = FakeGmail()
        ctx = ctx_for(self.env, self.assistant,
                      request_approval=lambda n, a, i: "denied",
                      gmail_factory=lambda s, e: gmail)
        r = chattools.run(ctx, "send_email",
                          {"to": "x@y.z", "subject": "s", "body": "b"})
        self.assertEqual(r["error"], "not_approved")
        self.assertEqual(r["decision"], "denied")
        self.assertEqual(gmail.sent, [])

    def test_no_approval_handler_means_unavailable(self):
        gmail = FakeGmail()
        ctx = ctx_for(self.env, self.assistant,
                      gmail_factory=lambda s, e: gmail)
        r = chattools.run(ctx, "send_email",
                          {"to": "x@y.z", "subject": "s", "body": "b"})
        self.assertEqual(r["decision"], "unavailable")
        self.assertEqual(gmail.sent, [])

    def test_calendar_list_and_create(self):
        gcal = FakeGcal()
        ctx = ctx_for(self.env, self.assistant,
                      request_approval=lambda n, a, i: "approved",
                      gcal_factory=lambda s, e: gcal)
        self.assertEqual(
            chattools.run(ctx, "calendar_list", {})["count"], 1)
        r = chattools.run(ctx, "calendar_create",
                          {"summary": "dentist", "start": "2026-11-12T10:00",
                           "end": "2026-11-12T10:30"})
        self.assertEqual(r["id"], "new-1")
        self.assertEqual(gcal.created[0][3], self.env.settings.timezone)


class TestVaultTools(Base):
    def setUp(self):
        super().setUp()
        import tempfile
        self.vault = tempfile.mkdtemp(prefix="pa-vault-")
        self.env.settings.vault_path = self.vault
        with open(os.path.join(self.vault, "note.md"), "w") as f:
            f.write("# Trip\npack the charger")

    def test_search_read_write_append(self):
        ctx = ctx_for(self.env, self.assistant,
                      request_approval=lambda n, a, i: "approved")
        hits = chattools.run(ctx, "vault_search", {"query": "charger"})
        self.assertEqual(hits["count"], 1)
        note = chattools.run(ctx, "vault_read", {"path": "note.md"})
        self.assertIn("pack the charger", note["content"])
        r = chattools.run(ctx, "vault_append",
                        {"path": "note.md", "text": "and passport"})
        self.assertTrue(r["appended"])
        r = chattools.run(ctx, "vault_write",
                        {"path": "new.md", "content": "# New\nfresh"})
        self.assertFalse(r["existed"])
        with open(os.path.join(self.vault, "note.md")) as f:
            self.assertIn("passport", f.read())

    def test_write_denied_leaves_file_absent(self):
        ctx = ctx_for(self.env, self.assistant,
                      request_approval=lambda n, a, i: "denied")
        r = chattools.run(ctx, "vault_write",
                          {"path": "no.md", "content": "x"})
        self.assertEqual(r["error"], "not_approved")
        self.assertFalse(os.path.exists(os.path.join(self.vault, "no.md")))

    def test_escape_refused(self):
        ctx = ctx_for(self.env, self.assistant,
                      request_approval=lambda n, a, i: "approved")
        r = chattools.run(ctx, "vault_write",
                          {"path": "../escape.md", "content": "x"})
        self.assertIn("error", r)


class TestShellMacRemind(Base):
    def test_readonly_shell_no_approval(self):
        ctx = ctx_for(self.env, self.assistant)
        r = chattools.run(ctx, "shell", {"command": "echo hi"})
        self.assertEqual(r["exit"], 0)
        self.assertEqual(r["stdout"].strip(), "hi")

    def test_writing_shell_uses_approval(self):
        ctx = ctx_for(self.env, self.assistant,
                      request_approval=lambda n, a, i: "approved")
        target = os.path.join(self.env.settings.chat_sandbox, "made.txt")
        r = chattools.run(ctx, "shell", {"command": f"touch {target}"})
        self.assertTrue(r.get("ok"), r)
        self.assertTrue(os.path.exists(target))

    def test_shell_denied(self):
        ctx = ctx_for(self.env, self.assistant,
                      request_approval=lambda n, a, i: "denied")
        r = chattools.run(ctx, "shell", {"command": "rm -rf /"})
        self.assertEqual(r["error"], "not_approved")

    def test_readonly_detection(self):
        self.assertTrue(sandbox.is_readonly("ls -la"))
        self.assertTrue(sandbox.is_readonly("cat note.txt"))
        self.assertFalse(sandbox.is_readonly("ls; rm x"))
        self.assertFalse(sandbox.is_readonly("echo hi > f"))
        self.assertFalse(sandbox.is_readonly("find . -delete"))
        self.assertFalse(sandbox.is_readonly("rm x"))
        self.assertFalse(sandbox.is_readonly("sudo ls"))

    def test_mac_offline_and_ok(self):
        env = TmpEnv(MAC_SSH_HOST="mac.tail", MAC_SSH_USER="me")
        ctx = ctx_for(env, self.assistant,
                      request_approval=lambda n, a, i: "approved",
                      mac_runner=lambda *a, **k: type(
                          "P", (), {"returncode": 255, "stdout": "",
                                    "stderr": "ssh: connect to host: "
                                              "Connection refused"})())
        r = chattools.run(ctx, "mac", {"command": "say hi"})
        self.assertTrue(r["offline"])
        ctx2 = ctx_for(env, self.assistant,
                       request_approval=lambda n, a, i: "approved",
                       mac_runner=lambda *a, **k: type(
                           "P", (), {"returncode": 0, "stdout": "done",
                                     "stderr": ""})())
        r2 = chattools.run(ctx2, "mac", {"command": "say hi"})
        self.assertTrue(r2["ok"])

    def test_mac_always_approved_gate(self):
        ctx = ctx_for(self.env, self.assistant,
                      request_approval=lambda n, a, i: "denied")
        r = chattools.run(ctx, "mac", {"command": "ls"})
        self.assertEqual(r["error"], "not_approved")

    def test_remind_once_and_daily(self):
        ctx = ctx_for(self.env, self.assistant)
        r = chattools.run(ctx, "remind_me",
                        {"text": "call mum", "when": "in 30m"})
        self.assertEqual(r["freq"], "once")
        r = chattools.run(ctx, "remind_me",
                        {"text": "stretch", "when": "daily 07:30"})
        self.assertEqual(r["freq"], "daily")
        mine = [s for s in sch.list_schedules(ctx.conn)
                if s["name"].startswith("remind-")]
        self.assertEqual(len(mine), 2)
        self.assertTrue(all(x["enabled"] and x["kind"] == "chat_prompt"
                            for x in mine))
        r = chattools.run(ctx, "remind_me",
                        {"text": "x", "when": "someday"})
        self.assertEqual(r["error"], "bad_arguments")

    def test_once_fires_chat_inbox(self):
        ctx = ctx_for(self.env, self.assistant)
        out = chattools.run(ctx, "remind_me",
                            {"text": "ping", "when": "in 1m"})
        sched = ctx.conn.execute(
            "SELECT * FROM schedules WHERE name=?",
            (out["scheduled"],)).fetchone()
        fired, _tid, detail = sch._fire(
            ctx.conn, sched, sched["time_of_day"], "fired",
            __import__("datetime").datetime.now(
                __import__("datetime").timezone.utc))
        self.assertTrue(fired)
        self.assertEqual(detail, "chat prompt queued")
        row = ctx.conn.execute(
            "SELECT * FROM chat_inbox").fetchone()
        self.assertEqual(row["channel_id"], "chan-1")
        self.assertIn("ping", row["prompt"])


class TestWaitDecision(Base):
    def test_approved(self):
        conn = self.env.conn()
        aid = approvals.create_approval(conn, "chat_action", {"tool": "x"})
        conn.close()

        def decider():
            time.sleep(0.2)
            c = self.env.conn()
            approvals.decide(c, aid, True)
            c.close()

        threading.Thread(target=decider, daemon=True).start()
        out = chattools.wait_decision(self.env.db_path, aid,
                                      time.monotonic() + 5, poll=0.05)
        self.assertEqual(out, "approved")

    def test_expired_24h(self):
        conn = self.env.conn()
        aid = approvals.create_approval(conn, "chat_action", {"tool": "x"})
        conn.execute("UPDATE approvals SET created_at=? WHERE id=?",
                     ("2020-01-01T00:00:00+00:00", aid))
        conn.close()
        out = chattools.wait_decision(self.env.db_path, aid,
                                      time.monotonic() + 5, poll=0.05)
        self.assertEqual(out, "expired")
        # decide() agrees
        conn = self.env.conn()
        self.assertEqual(approvals.decide(conn, aid, True), "expired")
        conn.close()

    def test_timeout(self):
        conn = self.env.conn()
        aid = approvals.create_approval(conn, "chat_action", {"tool": "x"})
        conn.close()
        out = chattools.wait_decision(self.env.db_path, aid,
                                      time.monotonic() + 0.2, poll=0.05)
        self.assertEqual(out, "timeout")


if __name__ == "__main__":
    unittest.main()
