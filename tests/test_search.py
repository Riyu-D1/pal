"""Acceptance #16: search privacy rules."""

import os
import unittest

from pa import approvals, memory as memmod, search as searchmod
from pa import worker as workmod
from tests.helpers import Fixture, TmpEnv, wait_for


class SearchSvc:
    """Records queries; returns canned results."""
    def __init__(self):
        self.queries = []

    def search(self, q, max_results=5):
        self.queries.append(q)
        return [{"url": "http://a.test/1", "title": "r1",
                 "content": "snippet"}]


class TestDisabled(unittest.TestCase):
    def test_search_disabled_when_not_opted_in(self):
        env = TmpEnv(SEARCH_ENABLED="false")
        self.assertRaises(searchmod.SearchDisabled,
                          searchmod.search_service, env.settings,
                          {"TAVILY_API_KEY": "tvly-x"})
        # flag on but no key -> still disabled
        self.assertRaises(searchmod.SearchDisabled,
                          searchmod.search_service,
                          TmpEnv(SEARCH_ENABLED="true").settings, {})

    def test_tool_returns_disabled_no_http(self):
        fx = Fixture()
        env = TmpEnv(PA_OLLAMA_BASE_URL=fx.base_url,
                     SEARCH_ENABLED="false")
        aid = env.add_assistant()
        conn = env.conn()
        tid = workmod.create_task(conn, aid, "t", "search for x")
        fx.push_chat(fx.chat_tool("web_search", {"query": "x"},
                                  call_id="s1"))
        fx.push_chat(fx.chat_text("no search available"))
        workmod.Worker(env.db_path, env.settings).run_once()
        t = conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        self.assertEqual(t["state"], "succeeded")
        # the fixture's /search endpoint was never hit
        self.assertTrue(all("messages" in c or "query" not in c
                            for c in fx.calls))
        r = conn.execute(
            "SELECT result_json FROM steps WHERE task_id=? AND"
            " tool_name='web_search'", (tid,)).fetchone()
        self.assertIn("search_disabled", r["result_json"])
        fx.close()


class TestQueryPolicy(unittest.TestCase):
    def test_sensitive_patterns_flagged(self):
        for q in ("email me at a@b.com", "key: gsk_abc123def456",
                  "my ssn 123-45-6789", "card 4242 4242 4242 4242"):
            self.assertEqual(searchmod.check_query_policy(q, False),
                             "preview", q)

    def test_private_task_always_preview(self):
        self.assertEqual(
            searchmod.check_query_policy("weather london", True),
            "preview")

    def test_clean_public_query_ok(self):
        self.assertEqual(
            searchmod.check_query_policy("python 3.12 release notes",
                                         False), "ok")

    def test_forbidden_text_detection(self):
        self.assertTrue(searchmod.contains_forbidden_text(
            "when is our wedding anniversary 2024",
            ["our wedding anniversary 2024"]))
        self.assertFalse(searchmod.contains_forbidden_text(
            "python release", ["secret note content"]))


class TestPrivateFlow(unittest.TestCase):
    """Private task -> query preview approval -> approve -> runs."""

    def test_private_task_preview_then_approved(self):
        fx = Fixture()
        env = TmpEnv(PA_OLLAMA_BASE_URL=fx.base_url)
        aid = env.add_assistant()
        conn = env.conn()
        svc = SearchSvc()
        tid = workmod.create_task(conn, aid, "t", "private research",
                                  private=True)
        fx.push_chat(fx.chat_tool("web_search", {"query": "best kettle"},
                                  call_id="s1"))
        fx.push_chat(fx.chat_tool(
            "finish", {"claims": [{"text": "kettles exist",
                                   "source_ids": []}]}, call_id="f1"))
        w = workmod.Worker(env.db_path, env.settings, search_impl=svc)
        w.run_once()
        t = conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        self.assertEqual(t["state"], "waiting_approval")
        self.assertEqual(svc.queries, [])  # never reached Tavily
        ap = approvals.list_pending(conn)
        self.assertEqual(len(ap), 1)
        self.assertEqual(ap[0]["kind"], "search_query")
        # owner approves -> task re-queued -> second attempt runs search
        approvals.decide(conn, ap[0]["id"], True)
        workmod.resume_waiting(conn, ap[0])
        w2 = workmod.Worker(env.db_path, env.settings, search_impl=svc)
        w2.run_once()
        t = conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        self.assertEqual(t["state"], "succeeded")
        self.assertEqual(svc.queries, ["best kettle"])
        fx.close()

    def test_query_never_contains_private_content(self):
        """Model tries to search for stored memory text verbatim -> it is
        sent to preview, NOT to Tavily."""
        fx = Fixture()
        env = TmpEnv(PA_OLLAMA_BASE_URL=fx.base_url)
        aid = env.add_assistant()
        conn = env.conn()
        memmod.insert_memory(conn, aid, "our wedding anniversary 2024")
        svc = SearchSvc()
        tid = workmod.create_task(conn, aid, "t", "look up my note")
        fx.push_chat(fx.chat_tool(
            "web_search",
            {"query": "our wedding anniversary 2024"}, call_id="s1"))
        fx.push_chat(fx.chat_text("ok"))
        w = workmod.Worker(env.db_path, env.settings, search_impl=svc)
        w.run_once()
        self.assertEqual(svc.queries, [])
        self.assertEqual(conn.execute(
            "SELECT state FROM tasks WHERE id=?", (tid,)).fetchone()["state"],
            "waiting_approval")
        fx.close()


if __name__ == "__main__":
    unittest.main()
