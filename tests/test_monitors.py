"""standing monitors (watch_*) + calendar-soon alerts."""

import datetime
import unittest

from pa import chattools, monitors, sysjobs
from tests.helpers import TmpEnv


class _API:
    def __init__(self):
        self.posts = []

    def post_message(self, channel, content, **kw):
        self.posts.append((channel, content))
        return {"id": "1"}


class _FakeBot:
    def __init__(self, env):
        self.env, self.s = env.env, env.settings
        self.api = _API()
        self._env = env

    def _db(self):
        return self._env.conn()

    def _owner_dm_id(self):
        return "DM123"


class Base(unittest.TestCase):
    def setUp(self):
        self.env = TmpEnv()
        self.conn = self.env.conn()


class TestCrud(Base):
    def test_add_validates_kind_and_query(self):
        self.assertIn("error", monitors.add_monitor(
            self.conn, "carrier-pigeon", "x"))
        self.assertIn("error", monitors.add_monitor(
            self.conn, "web", "not-a-url"))
        self.assertIn("error", monitors.add_monitor(
            self.conn, "gmail", ""))
        out = monitors.add_monitor(self.conn, "web",
                                   "https://example.com", 1)
        self.assertEqual(out["every_minutes"], 5)  # min clamp

    def test_list_set_remove(self):
        out = monitors.add_monitor(self.conn, "gmail", "is:unread", 15)
        mid = out["id"]
        self.assertEqual(len(monitors.list_monitors(self.conn)), 1)
        monitors.set_monitor(self.conn, mid, False)
        self.assertEqual(
            monitors.list_monitors(self.conn)[0]["enabled"], 0)
        self.assertTrue(monitors.remove_monitor(self.conn, mid)
                        ["removed"])

    def test_tick_job_seeded_disabled(self):
        row = self.conn.execute(
            "SELECT enabled FROM schedules WHERE name='monitor-tick'"
        ).fetchone()
        self.assertEqual(row["enabled"], 0)
        self.assertTrue(sysjobs.is_system("monitor-tick"))


class TestTick(Base):
    def test_web_change_alerts_once(self):
        monitors.add_monitor(self.conn, "web", "https://x.test/", 5)
        pages = ["v1", "v2"]

        def fake_fetch(url, **kw):
            return {"text": pages[0], "title": "t", "url": url}

        import pa.egress as egressmod
        orig = egressmod.guarded_fetch
        egressmod.guarded_fetch = fake_fetch
        try:
            bot = _FakeBot(self.env)
            self.assertEqual(monitors.run(bot, {}),
                             "checked 1, alerted 0")  # baseline
            pages[0] = "v2"
            self.conn.execute(
                "UPDATE monitors SET last_run_at=NULL")
            self.assertEqual(monitors.run(bot, {}),
                             "checked 1, alerted 1")
            self.assertIn("changed", bot.api.posts[0][1])
            # same content again -> no second alert
            bot.api.posts.clear()
            self.conn.execute(
                "UPDATE monitors SET last_run_at=NULL")
            self.assertEqual(monitors.run(bot, {}),
                             "checked 1, alerted 0")
        finally:
            egressmod.guarded_fetch = orig

    def test_calendar_soon_alerts_once_per_event(self):
        soon = (datetime.datetime.now(datetime.timezone.utc)
                + datetime.timedelta(minutes=10))
        ev = {"id": "e1", "summary": "Dentist",
              "start": soon.isoformat(), "end": soon.isoformat()}

        class FakeCal:
            def __init__(self, *a, **k):
                pass

            def list_events(self, **kw):
                return [ev]

        import pa.gcal as gcalmod
        orig = gcalmod.GCalClient
        gcalmod.GCalClient = FakeCal
        try:
            monitors.add_monitor(self.conn, "calendar", "15", 5)
            bot = _FakeBot(self.env)
            self.assertEqual(monitors.run(bot, {}),
                             "checked 1, alerted 1")
            self.assertIn("Dentist", bot.api.posts[0][1])
            self.assertIn("m (", bot.api.posts[0][1])
            bot.api.posts.clear()
            self.conn.execute(
                "UPDATE monitors SET last_run_at=NULL")
            self.assertEqual(monitors.run(bot, {}),
                             "checked 1, alerted 0")  # deduped
        finally:
            gcalmod.GCalClient = orig

    def test_paused_monitor_skipped(self):
        out = monitors.add_monitor(self.conn, "web",
                                   "https://x.test/", 5)
        monitors.set_monitor(self.conn, out["id"], False)
        bot = _FakeBot(self.env)
        self.assertEqual(monitors.run(bot, {}), "checked 0, alerted 0")


class TestTools(unittest.TestCase):
    def setUp(self):
        self.env = TmpEnv()
        aid = self.env.add_assistant(name="pal", tools=[])
        conn = self.env.conn()
        self.ast = conn.execute(
            "SELECT * FROM assistants WHERE id=?", (aid,)).fetchone()
        self.ctx = chattools.ChatToolContext(
            conn, self.env.settings, self.ast, "c", env=self.env.env)

    def test_add_is_approval_gated(self):
        aids = []
        self.ctx.request_approval = lambda n, a, i: (
            aids.append(i), "approved")[1]
        out = chattools.run(self.ctx, "watch_add",
                            {"kind": "web",
                             "query": "https://example.com"})
        self.assertIn("id", out)
        self.assertTrue(aids)

    def test_list_and_set(self):
        self.ctx.request_approval = lambda n, a, i: "approved"
        mid = chattools.run(self.ctx, "watch_add",
                            {"kind": "gmail", "query": "x"})["id"]
        out = chattools.run(self.ctx, "watch_list", {})
        self.assertEqual(out["monitors"][0]["id"], mid)
        out = chattools.run(self.ctx, "watch_set",
                            {"id": mid, "enabled": False})
        self.assertFalse(out["enabled"])


class TestCommand(unittest.TestCase):
    def test_monitor_command_list_and_toggle(self):
        env = TmpEnv()
        conn = env.conn()
        monitors.add_monitor(conn, "gmail", "is:unread", 15)
        bot = discordbot_stub(env)
        out = bot._monitor(conn, [])
        self.assertIn("#1", out)
        self.assertIn("gmail", out)
        out = bot._monitor(conn, ["1", "off"])
        self.assertIn("paused", out)
        out = bot._monitor(conn, ["1", "del"])
        self.assertIn("removed", out)
        conn.close()

    def test_monitor_in_commands(self):
        from pa import discordbot
        self.assertIn("monitor", discordbot._COMMAND_NAMES)
        self.assertIn("monitor", discordbot._EPHEMERAL_COMMANDS)


def discordbot_stub(env):
    from pa import discordbot
    return discordbot.DiscordBot(env.settings, env=env.env,
                                 notifier=False, engine=object())


if __name__ == "__main__":
    unittest.main()
