"""A3: coursework layer — runtime toolkit detection, deadline watch,
write gating, PDF -> vault notes."""

import datetime
import os
import tempfile
import unittest
from unittest.mock import patch

from pa import connectflows, coursework, db as dbmod, sysjobs
from tests.helpers import TmpEnv


UTC = datetime.timezone.utc


class _API:
    def __init__(self, posts):
        self.posts = posts

    def post_message(self, ch, text, **kw):
        self.posts.append((ch, text))
        return True


class _Bot:
    def __init__(self, env, posts=None):
        self.s = env.settings
        self.env = env.env
        self.db_path = env.db_path
        self.api = _API(posts if posts is not None else [])
        self._voice = None

    def _owner_dm_id(self):
        return "dm-owner"


def _connect_classroom(conn):
    connectflows.upsert(
        conn, "classroom", "composio", "owner", "", "active",
        "", "blob", "",
        {"toolkit": "classroom", "connected_account_id": "ca-1"})


class Base(unittest.TestCase):
    def setUp(self):
        self.env = TmpEnv(PA_COURSEWORK="true", COMPOSIO_API_KEY="k")
        self.conn = self.env.conn()
        self.posts = []
        self.bot = _Bot(self.env, self.posts)


class TestToolkit(Base):
    def test_absent_degrades_cleanly(self):
        out = coursework.list_classes(self.bot, self.conn)
        self.assertEqual(out["error"], "not_connected")
        self.assertIn("connect", out["message"])
        out2 = coursework.list_assignments(self.bot, self.conn)
        self.assertEqual(out2["error"], "not_connected")

    def test_find_toolkit_runtime(self):
        _connect_classroom(self.conn)
        row, tk = coursework.find_toolkit(self.conn)
        self.assertIsNotNone(row)
        self.assertIn("classroom", tk)

    def test_wrong_kind_ignored(self):
        connectflows.upsert(conn=self.conn, provider="classroom",
                            kind="oauth", account_label="", scopes="",
                            status="active", server_url="",
                            token_blob="x", expires_at="", meta={})
        row, _ = coursework.find_toolkit(self.conn)
        self.assertIsNone(row)          # oauth row is not a toolkit


class TestLists(Base):
    def setUp(self):
        super().setUp()
        _connect_classroom(self.conn)

    def _fake_tools(self, slugs):
        return [{"slug": s, "description": "", "schema": {},
                 "read_only": True} for s in slugs]

    def test_list_assignments_normalized(self):
        due = {"year": 2026, "month": 11, "day": 20,
               "dueTime": {"hours": 17, "minutes": 0}}
        calls = []
        def fake_call(conn, s, env, row, slug, args):
            calls.append(slug)
            return {"result": {"courseWork": [
                {"id": "w1", "title": "essay", "dueDate": due,
                 "courseId": "c9"}]}}
        with patch("pa.coursework._pick_tool",
                   return_value="CLASSROOM_LIST_COURSEWORK"), \
             patch("pa.connectflows.call_tool", fake_call):
            items = coursework.list_assignments(self.bot, self.conn)
        self.assertEqual(calls, ["CLASSROOM_LIST_COURSEWORK"])
        self.assertEqual(items[0]["title"], "essay")
        self.assertTrue(items[0]["due"].startswith("2026-11-20"))
        self.assertEqual(items[0]["course"], "c9")

    def test_pick_tool_by_verb(self):
        tools = self._fake_tools(
            ["CLASSROOM_CREATE_COURSE", "CLASSROOM_LIST_COURSES"])
        with patch("pa.composio.ComposioClient") as C:
            C.return_value.tools.return_value = tools
            slug = coursework._pick_tool(
                self.conn, self.bot.s, self.env.env, "classroom",
                coursework._CLASS_VERBS)
        self.assertEqual(slug, "CLASSROOM_LIST_COURSES")

    def test_missing_verb_toolkit_missing(self):
        _connect_classroom(self.conn)
        with patch("pa.coursework._pick_tool", return_value=""):
            out = coursework.list_classes(self.bot, self.conn)
        self.assertEqual(out["error"], "toolkit_missing")


class TestWrite(Base):
    def test_unattended_write_needs_approval_no_execute(self):
        _connect_classroom(self.conn)
        with patch("pa.connectflows.call_tool") as ct:
            out = coursework.write(
                self.conn, self.bot.s, "CLASSROOM_SUBMIT_WORK",
                {"id": "w1"}, unattended=True)
        self.assertEqual(out["error"], "approval_required")
        ct.assert_not_called()


class TestWatchJob(Base):
    def test_seeded_disabled_and_flag_off(self):
        env2 = TmpEnv()                  # flag off
        conn2 = env2.conn()
        row = conn2.execute(
            "SELECT enabled, freq, day_of_month FROM schedules"
            " WHERE name='coursework-watch'").fetchone()
        self.assertEqual(row["enabled"], 0)
        self.assertEqual(row["freq"], "interval")
        self.assertEqual(row["day_of_month"], 60)
        self.assertTrue(sysjobs.is_system("coursework-watch"))
        b = _Bot(env2, [])
        self.assertEqual(coursework.run(b, {}), "coursework off")

    def test_24h_reminder_once(self):
        _connect_classroom(self.conn)
        due = (datetime.datetime.now(UTC)
               + datetime.timedelta(hours=20)).isoformat()
        items = [{"id": "w9", "title": "lab report",
                  "due": due, "course": "sci"}]
        with patch("pa.coursework.list_assignments",
                   return_value=items):
            out = coursework.run(self.bot, {})
            self.assertIn("1 deadline", out)
            self.assertEqual(len(self.posts), 1)
            self.assertIn("lab report", self.posts[0][1])
            out2 = coursework.run(self.bot, {})
        self.assertIn("0 deadline", out2)
        self.assertEqual(len(self.posts), 1)   # deduped

    def test_2h_lead(self):
        _connect_classroom(self.conn)
        due = (datetime.datetime.now(UTC)
               + datetime.timedelta(minutes=90)).isoformat()
        items = [{"id": "w10", "title": "quiz", "due": due,
                  "course": ""}]
        with patch("pa.coursework.list_assignments",
                   return_value=items):
            out = coursework.run(self.bot, {})
        self.assertIn("1 deadline", out)
        self.assertIn("today", self.posts[0][1])


class TestPdfNotes(unittest.TestCase):
    def test_extract_to_vault_note(self):
        with tempfile.TemporaryDirectory() as vault_dir, \
                tempfile.TemporaryDirectory() as d:
            env = TmpEnv(VAULT_PATH=vault_dir)
            conn = env.conn()
            pdf = os.path.join(d, "paper.pdf")
            with open(pdf, "wb") as f:
                f.write(b"%PDF-1.4 fake")
            with patch("pa.coursework.extract_pdf_text",
                       return_value="chapter one text"):
                out = coursework.pdf_to_note(
                    conn, env.settings, pdf,
                    title="Paper Review", cls="bio")
            self.assertTrue(out["extracted"])
            self.assertTrue(out["path"].startswith("coursework/bio/"))
            note = os.path.join(vault_dir, out["path"])
            self.assertTrue(os.path.exists(note))
            text = open(note).read()
            self.assertIn("chapter one text", text)
            self.assertIn("paper.pdf", text)

    def test_missing_extractor_writes_stub(self):
        with tempfile.TemporaryDirectory() as vault_dir:
            env = TmpEnv(VAULT_PATH=vault_dir)
            conn = env.conn()
            with patch("pa.coursework.extract_pdf_text",
                       return_value=""):
                out = coursework.pdf_to_note(
                    conn, env.settings, "/tmp/x.pdf",
                    title="Slides", cls="inbox")
            self.assertFalse(out["extracted"])
            note = os.path.join(vault_dir, out["path"])
            self.assertIn("no PDF extractor", open(note).read())

    def test_no_vault_clean_error(self):
        env = TmpEnv()                     # no VAULT_PATH
        conn = env.conn()
        with patch("pa.coursework.extract_pdf_text",
                   return_value="x"):
            out = coursework.pdf_to_note(conn, env.settings, "/t.pdf")
        self.assertIn("error", out)


if __name__ == "__main__":
    unittest.main()
