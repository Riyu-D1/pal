"""Acceptance #10 secrets hygiene, #15 UI checks, #17 memory footprint."""

import io
import logging
import os
import resource
import unittest

from pa import db as dbmod, worker as workmod
from pa.logsetup import ScrubFilter
from pa.webapp import App
from tests.helpers import Fixture, TmpEnv, WSGIClient


class TestSecrets(unittest.TestCase):
    SECRET = "gsk_TESTSECRET_d0ntL3ak_0123456789"

    def test_scrub_filter_redacts(self):
        f = ScrubFilter()
        rec = logging.LogRecord("t", logging.INFO, "", 1,
                                f"key is {self.SECRET} ok", (), None)
        f.filter(rec)
        self.assertNotIn(self.SECRET, rec.getMessage())
        self.assertIn("[redacted]", rec.getMessage())

    def test_secret_nowhere(self):
        secret = self.SECRET
        os.environ["LLM_API_KEY"] = secret
        try:
            captured = io.StringIO()
            h = logging.StreamHandler(captured)
            h.addFilter(ScrubFilter())
            logging.getLogger().addHandler(h)
            fx = Fixture()
            env = TmpEnv(PA_OLLAMA_BASE_URL=fx.base_url)
            env.add_owner()
            app = App(env.settings)
            c = WSGIClient(app)
            c.login("owner", "correct horse 99")
            aid = env.add_assistant()
            conn = env.conn()
            # run a task whose provider call fails auth (key in play)
            fx.push_chat((401, {}, {"error": "bad key"}))
            workmod.create_task(conn, aid, "t", "body")
            workmod.Worker(env.db_path, env.settings).run_once()
            # scan: logs, db dump, every response
            log_text = captured.getvalue()
            self.assertNotIn(secret, log_text)
            dump = "\n".join(env.conn().iterdump())
            self.assertNotIn(secret, dump)
            for path in ("/", "/login", "/approvals", "/memories",
                         "/owner-facts", f"/tasks/1", "/api/tasks/1",
                         "/api/status", f"/assistants/{aid}"):
                token = c.csrf(app)
                _, _, body = c.get(path)
                self.assertNotIn(secret.encode(), body, path)
            # forced error pages too
            _, _, body = c.get("/nonexistent")
            self.assertNotIn(secret.encode(), body)
            fx.close()
        finally:
            del os.environ["LLM_API_KEY"]
            logging.getLogger().removeHandler(h)


class TestUI(unittest.TestCase):
    def setUp(self):
        self.env = TmpEnv()
        self.env.add_owner()
        self.app = App(self.env.settings)
        self.c = WSGIClient(self.app)
        self.c.login("owner", "correct horse 99")

    def test_labels_and_csrf(self):
        _, _, body = self.c.get("/assistants/new")
        for frag in (b'<label for="name">', b'<label for="instructions">',
                     b'<label for="provider">',
                     b'name="csrf"'):
            self.assertIn(frag, body)

    def test_viewport_and_focus_css(self):
        _, _, body = self.c.get("/")
        self.assertIn(b'name="viewport"', body)
        _, _, css = self.c.get("/static/style.css")
        self.assertIn(b"focus-visible", css)
        self.assertIn(b"@media (max-width: 480px)", css)
        self.assertIn(b"width: 100%", css)

    def test_error_banner_role_alert(self):
        conn = self.env.conn()
        aid = self.env.add_assistant()
        workmod.create_task(conn, aid, "t", "x")
        conn.execute("UPDATE tasks SET state='failed', error_kind='deadline'"
                     " WHERE id=1")
        _, _, body = self.c.get("/tasks/1")
        self.assertIn(b'role="alert"', body)
        self.assertIn(b"ran out of time", body)

    def test_plain_language_errors(self):
        # every defined error kind maps to plain words, not internals
        from pa.webapp import ERROR_TEXT
        for kind in ("quota_exceeded", "provider_auth", "search_disabled",
                     "fetch_blocked", "deadline", "cancelled"):
            self.assertIn(kind, ERROR_TEXT)

    def test_loading_empty_states(self):
        _, _, body = self.c.get("/")
        self.assertIn(b"No assistants yet", body)  # empty state
        self. env.add_assistant()
        _, _, body = self.c.get("/")
        self.assertNotIn(b"No assistants yet", body)


class TestMemoryFootprint(unittest.TestCase):
    def test_rss_under_workload(self):
        """Record RSS after a small workload; must sit well under 512MB."""
        fx = Fixture()
        env = TmpEnv(PA_OLLAMA_BASE_URL=fx.base_url)
        aid = env.add_assistant()
        conn = env.conn()
        big = "page text " * 3000  # ~30KB pages
        def fetch(url, settings=None, **kw):
            return {"text": big, "content_type": "text/plain",
                    "title": "t", "final_url": url, "status": 200}
        for i in range(5):
            workmod.create_task(conn, aid, f"t{i}", "work")
            fx.push_chat(fx.chat_tool("fetch_url",
                                      {"url": f"http://a.test/{i}"},
                                      call_id=f"f{i}"))
            fx.push_chat(fx.chat_text("done"))
        w = workmod.Worker(env.db_path, env.settings, fetch_impl=fetch)
        for _ in range(5):
            w.run_once()
        rss_mb = resource.getrusage(
            resource.RUSAGE_SELF).ru_maxrss / (1024 * 1024)
        # ru_maxrss on macOS is bytes; on Linux it's KB - handle both
        if rss_mb > 10_000:  # KB units
            rss_mb = rss_mb / 1024
        print(f"\n[memory] max RSS under workload: {rss_mb:.0f} MB "
              f"(target < 512)")
        self.assertLess(rss_mb, 512)
        fx.close()


if __name__ == "__main__":
    unittest.main()
