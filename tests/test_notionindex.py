"""Notion -> recall FTS + KG indexing (read-only, cursor'd)."""

import unittest
from unittest.mock import patch

from pa import connectflows, notionindex, recall, sysjobs
from tests.helpers import TmpEnv


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


def _connect_notion(conn):
    connectflows.upsert(
        conn, "notion", "mcp", "owner", "", "active",
        "https://mcp.notion.com/mcp", "", "",
        {"toolkit": "notion"})


PAGE1 = {"id": "p1", "title": "Trip ideas",
         "last_edited_time": "2026-11-01T10:00:00Z"}
PAGE2 = {"id": "p2", "properties": {
         "Name": {"type": "title", "title": [
             {"plain_text": "Reading list"}]}},
         "last_edited_time": "2026-11-02T10:00:00Z"}


def _search_out(pages, next_cursor=""):
    return {"result": {"results": pages,
                       "next_cursor": next_cursor}}


def _page_out(text):
    return {"result": {"content": text}}


class Base(unittest.TestCase):
    def setUp(self):
        self.env = TmpEnv(PA_NOTION_INDEX="true")
        self.conn = self.env.conn()
        self.posts = []
        self.bot = _Bot(self.env, self.posts)


class TestIndex(Base):
    def test_flag_off(self):
        self.env.settings.notion_index = False
        self.assertEqual(notionindex.run(self.bot, {}),
                         "notion-index off")

    def test_not_connected(self):
        self.assertEqual(notionindex.run(self.bot, {}),
                         "notion not connected")

    def test_seeded_disabled(self):
        row = self.conn.execute(
            "SELECT enabled, freq, day_of_month FROM schedules"
            " WHERE name='notion-index'").fetchone()
        self.assertEqual(row["enabled"], 0)
        self.assertEqual(row["freq"], "interval")
        self.assertTrue(sysjobs.is_system("notion-index"))

    def test_pages_indexed_into_recall_and_kg(self):
        _connect_notion(self.conn)
        calls = []
        def fake_call(conn, s, env, row, slug, args):
            calls.append(slug)
            if slug == "search":
                return _search_out([PAGE1, PAGE2])
            return _page_out(f"body of {args['page_id']}")
        with patch("pa.connectflows.call_tool", fake_call):
            out = notionindex.run(self.bot, {})
        self.assertIn("2 indexed", out)
        # recall FTS rows carry source=notion
        rows = self.conn.execute(
            "SELECT ref_id, title, body FROM recall_fts"
            " WHERE source='notion' ORDER BY ref_id").fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["title"], "Trip ideas")
        self.assertIn("body of p1", rows[0]["body"])
        self.assertEqual(rows[1]["title"], "Reading list")
        # KG meta saw the notion:// notes
        kg = self.conn.execute(
            "SELECT path FROM kg_meta WHERE path LIKE 'notion://%'"
        ).fetchall()
        self.assertEqual(len(kg), 2)

    def test_unchanged_second_tick_skips(self):
        _connect_notion(self.conn)
        def fake_call(conn, s, env, row, slug, args):
            if slug == "search":
                return _search_out([PAGE1])
            return _page_out("body of p1")
        with patch("pa.connectflows.call_tool", fake_call):
            notionindex.run(self.bot, {})
            out = notionindex.run(self.bot, {})
        self.assertIn("0 indexed", out)
        self.assertIn("1 unchanged", out)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) c FROM recall_fts"
            " WHERE source='notion'").fetchone()["c"], 1)

    def test_reindex_keeps_notion_rows(self):
        _connect_notion(self.conn)
        def fake_call(conn, s, env, row, slug, args):
            if slug == "search":
                return _search_out([PAGE1])
            return _page_out("body of p1")
        with patch("pa.connectflows.call_tool", fake_call):
            notionindex.run(self.bot, {})
        recall.reindex(self.conn, self.env.settings)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) c FROM recall_fts"
            " WHERE source='notion'").fetchone()["c"], 1)

    def test_reconnect_card_once(self):
        _connect_notion(self.conn)
        def boom(conn, s, env, row, slug, args):
            raise connectflows.ConnectError("reconnect")
        with patch("pa.connectflows.call_tool", boom):
            out1 = notionindex.run(self.bot, {})
            out2 = notionindex.run(self.bot, {})
        self.assertIn("reconnect", out1)
        self.assertEqual(len(self.posts), 1)   # deduped
        self.assertIn("/connect notion", self.posts[0][1])

    def test_cursor_persisted(self):
        _connect_notion(self.conn)
        def fake_call(conn, s, env, row, slug, args):
            if slug == "search":
                return _search_out([PAGE1], next_cursor="cur-7")
            return _page_out("x")
        with patch("pa.connectflows.call_tool", fake_call):
            notionindex.run(self.bot, {})
        from pa import db as dbmod
        self.assertEqual(dbmod.get_setting(
            self.conn, "notion_cursor"), "cur-7")

    def test_only_read_slugs_called(self):
        _connect_notion(self.conn)
        seen = []
        def fake_call(conn, s, env, row, slug, args):
            seen.append(slug)
            if slug == "search":
                return _search_out([PAGE1])
            return _page_out("x")
        with patch("pa.connectflows.call_tool", fake_call):
            notionindex.run(self.bot, {})
        writes = [s for s in seen if any(
            v in s.upper() for v in
            ("CREATE", "UPDATE", "DELETE", "APPEND", "PATCH"))]
        self.assertEqual(writes, [])


if __name__ == "__main__":
    unittest.main()
