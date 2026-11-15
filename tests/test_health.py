"""deep /healthz — real ok, 503 + short codes on
failure, nothing sensitive in the body."""

import datetime
import json
import unittest

from pa import discordbot, health
from pa.util import now_iso
from pa.webapp import App
from tests.helpers import TmpEnv, WSGIClient


def _app(**over):
    env = TmpEnv(**over)
    env.add_owner()
    return App(env.settings, env=env.env), env


class TestHealthz(unittest.TestCase):
    def test_healthy_fixture_200(self):
        app, _env = _app()
        status, _, body = WSGIClient(app).get("/healthz")
        self.assertTrue(status.startswith("200"))
        data = json.loads(body)
        self.assertTrue(data["ok"])
        self.assertEqual(data["checks"]["db"], "ok")
        self.assertIn("uptime_s", data)
        self.assertIn("rss_mb", data["checks"])
        self.assertIn("sha", data)

    def test_broken_db_returns_503(self):
        app, env = _app()
        conn = env.conn()
        conn.execute("DROP TABLE pa_settings")
        conn.close()
        status, _, body = WSGIClient(app).get("/healthz")
        self.assertTrue(status.startswith("503"))
        data = json.loads(body)
        self.assertFalse(data["ok"])
        self.assertIn("db", data["failed"])

    def test_stale_worker_beat_returns_503(self):
        app, env = _app(PA_HEALTH_EXPECT_WORKER="true")
        conn = env.conn()
        old = (datetime.datetime.now(datetime.timezone.utc)
               - datetime.timedelta(minutes=10)).isoformat(
                   timespec="seconds")
        conn.execute("INSERT OR REPLACE INTO pa_settings(key, value,"
                     " updated_at) VALUES('worker_beat', ?, ?)",
                     (old, now_iso()))
        conn.commit()
        conn.close()
        status, _, body = WSGIClient(app).get("/healthz")
        self.assertTrue(status.startswith("503"))
        data = json.loads(body)
        self.assertIn("worker", data["failed"])

    def test_fresh_worker_beat_is_ok(self):
        app, env = _app(PA_HEALTH_EXPECT_WORKER="true")
        health.beat(env.conn())
        status, _, body = WSGIClient(app).get("/healthz")
        self.assertTrue(status.startswith("200"))
        self.assertTrue(json.loads(body)["ok"])

    def test_worker_check_off_by_default(self):
        """No PA_HEALTH_EXPECT_WORKER -> never-written beat, no fail."""
        app, _env = _app()
        status, _, body = WSGIClient(app).get("/healthz")
        self.assertTrue(status.startswith("200"))
        self.assertNotIn("worker_s", json.loads(body)["checks"])

    def test_rss_cap_failure(self):
        app, _env = _app(PA_HEALTH_RSS_MB="1")
        status, _, body = WSGIClient(app).get("/healthz")
        self.assertTrue(status.startswith("503"))
        data = json.loads(body)
        self.assertIn("rss", data["failed"])

    def test_discord_check_when_flagged(self):
        discordbot._GATEWAY["last_frame"] = None
        app, _env = _app(PA_HEALTH_DISCORD="true",
                         DISCORD_BOT_TOKEN="tok")
        c = WSGIClient(app)
        status, _, body = c.get("/healthz")
        self.assertTrue(status.startswith("503"))
        self.assertIn("discord", json.loads(body)["failed"])
        # a live gateway frame clears it
        discordbot._GATEWAY["last_frame"] = discordbot.time.time()
        status, _, body = c.get("/healthz")
        self.assertTrue(status.startswith("200"))
        discordbot._GATEWAY["last_frame"] = None

    def test_body_has_no_secret_or_datadir(self):
        env = TmpEnv(PA_HEALTH_EXPECT_WORKER="true")
        env.add_owner()
        app = App(env.settings, env=env.env)
        status, _, body = WSGIClient(app).get("/healthz")
        self.assertTrue(status.startswith("503"))   # no worker beat
        low = body.lower()
        self.assertNotIn(env.db_path.encode().lower(), low)
        self.assertNotIn(b"token", low)
        self.assertNotIn(b"secret", low)
        self.assertNotIn(b"/users/", low)


if __name__ == "__main__":
    unittest.main()
