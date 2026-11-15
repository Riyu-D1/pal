"""morning-briefing: gather (calendar, overnight mail, vault todos,
weather), compose (model + fallback spoken), run, /briefing now."""

import datetime
import os
import tempfile
import unittest
from unittest import mock

from pa import briefing, db as dbmod, discordbot, chattools
from tests.helpers import TmpEnv


class FakeAPI:
    def __init__(self):
        self.posts = []

    def open_dm(self, user_id):
        return "dm-1"

    def post_message(self, channel_id, content, **kw):
        self.posts.append((channel_id, content))
        return {"id": "m1"}


class FakeVoice:
    def __init__(self):
        self.notes = []

    def send_voice_note(self, channel_id, text, **kw):
        self.notes.append((channel_id, text))
        return {"sent": True}


CAL = {"events": [
    {"summary": "Dentist", "start": "2026-03-04T09:30:00Z"},
    {"summary": "Standup", "start": "2026-03-04T11:00:00Z"}]}


def _fake_run(name):
    def run(ctx, tool, args):
        if tool == "calendar_list":
            return CAL
        if tool == "email_list":
            return {"messages": []}
        return {"error": "nope"}
    return run


class Base(unittest.TestCase):
    def setUp(self):
        self.vault = tempfile.mkdtemp(prefix="vault-")
        self.env = TmpEnv(DISCORD_OWNER_ID="o1",
                          VAULT_PATH=self.vault,
                          PA_VOICE_NOTES="true")
        self.env.conn().close()
        self.bot = discordbot.DiscordBot(
            self.env.settings, env=self.env.env, notifier=False)
        self.bot.api = FakeAPI()
        self.bot._calls = None
        self.bot._voice = FakeVoice()
        p = mock.patch.object(chattools, "run", _fake_run("x"))
        p.start()
        self.addCleanup(p.stop)
        p2 = mock.patch.object(
            briefing, "_weather", return_value="clear in London, now 8°C")
        p2.start()
        self.addCleanup(p2.stop)

    def conn(self):
        return dbmod.connect(self.env.db_path)


class TestGather(Base):
    def test_calendar_lines(self):
        conn = self.conn()
        parts = briefing.gather(self.bot, conn)
        conn.close()
        self.assertTrue(any("Dentist" in l for l in parts["events"]))
        self.assertTrue(any("Standup" in l for l in parts["events"]))

    def test_overnight_mail_from_watcher_rows(self):
        conn = self.conn()
        conn.execute(
            "INSERT INTO inbox_seen(message_id, classification,"
            " internal_date, subject, frm, alerted, created_at)"
            " VALUES('m1','important','1','School trip form',"
            " 'Mrs J <j@sch.uk>',1,?)",
            (datetime.datetime.now(datetime.timezone.utc).isoformat(),))
        conn.execute(
            "INSERT INTO inbox_seen(message_id, classification,"
            " internal_date, subject, frm, alerted, created_at)"
            " VALUES('m2','ignore','1','sale','promo@x',0,?)",
            (datetime.datetime.now(datetime.timezone.utc).isoformat(),))
        parts = briefing.gather(self.bot, conn)
        conn.close()
        self.assertTrue(any("School trip" in l for l in parts["emails"]))
        self.assertFalse(any("sale" in l for l in parts["emails"]))

    def test_overnight_mail_fallback_to_unread(self):
        def run(ctx, tool, args):
            if tool == "calendar_list":
                return {"events": []}
            if tool == "email_list":
                return {"messages": [
                    {"subject": "visa approved", "from": "UKVI"}]}
            return {}
        with mock.patch.object(chattools, "run", run):
            conn = self.conn()
            parts = briefing.gather(self.bot, conn)
            conn.close()
        self.assertTrue(any("visa approved" in l for l in parts["emails"]))

    def test_vault_due_finds_dated_todos(self):
        today = datetime.date.today().isoformat()
        with open(os.path.join(self.vault, "todo.md"), "w") as f:
            f.write(f"# todos\n- [ ] pay council tax due {today}\n"
                    "- [ ] someday learn piano\n"
                    "- [x] done thing 2020-01-01\n"
                    "- [ ] far future 2999-01-01\n")
        conn = self.conn()
        parts = briefing.gather(self.bot, conn)
        conn.close()
        self.assertTrue(any("council tax" in l for l in parts["due"]))
        self.assertFalse(any("piano" in l for l in parts["due"]))
        self.assertFalse(any("far future" in l for l in parts["due"]))


class TestWeather(unittest.TestCase):
    def test_weather_line(self):
        env = TmpEnv()
        self.addCleanup(lambda: None)
        payload = {
            "current": {"temperature_2m": 8.4, "weather_code": 61,
                        "wind_speed_10m": 12},
            "daily": {"temperature_2m_max": [11.0],
                      "temperature_2m_min": [4.0],
                      "precipitation_probability_max": [80]}}
        class FakeCli:
            def __init__(self, *a, **kw):
                pass
            def get_json(self, path):
                return {}, payload
        with mock.patch.object(briefing.netclient, "APIClient", FakeCli):
            line = briefing._weather(env.settings)
        self.assertIn("light rain", line)
        self.assertIn("8.4", line)
        self.assertIn("80%", line)

    def test_weather_failure_returns_empty(self):
        env = TmpEnv()
        with mock.patch.object(briefing.netclient, "APIClient",
                               side_effect=RuntimeError("down")):
            self.assertEqual(briefing._weather(env.settings), "")


class TestCompose(Base):
    def test_model_spoken(self):
        conn = self.conn()
        parts = {"events": ["• 09:30 — Dentist"], "emails": [],
                 "due": [], "weather": "clear", "date": "2026-03-04"}
        with mock.patch.object(briefing.sysjobs, "model_text",
                               return_value="Morning! Dentist at half "
                                            "nine, otherwise clear."):
            text, spoken = briefing.compose(self.bot, conn, parts)
        conn.close()
        self.assertIn("half nine", spoken)
        self.assertIn("Morning briefing", text)
        self.assertIn("Dentist", text)

    def test_fallback_spoken_when_model_down(self):
        conn = self.conn()
        parts = {"events": ["• x"], "emails": ["• a"], "due": ["• b"],
                 "weather": "rain", "date": "2026-03-04"}
        with mock.patch.object(briefing.sysjobs, "model_text",
                               return_value=""):
            _text, spoken = briefing.compose(self.bot, conn, parts)
        conn.close()
        self.assertIn("Good morning", spoken)
        self.assertIn("rain", spoken)


class TestRun(Base):
    def test_posts_text_and_voice_to_dm(self):
        with mock.patch.object(briefing.sysjobs, "model_text",
                               return_value="spoken version"):
            out = briefing.run(self.bot, {"channel_id": ""})
        self.assertIn("briefing posted", out)
        self.assertTrue(any(ch == "dm-1" and "Morning briefing" in c
                            for ch, c in self.bot.api.posts))
        self.assertEqual(self.bot._voice.notes[0][1], "spoken version")

    def test_briefing_now_queues_the_job(self):
        conn = self.conn()
        out = self.bot.execute(conn, ["briefing", "now"])
        self.assertIn("queued", out)
        row = conn.execute(
            "SELECT source, status FROM chat_inbox"
            " WHERE source='job:morning-briefing'").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], "queued")
        conn.close()

    def test_briefing_usage_error(self):
        conn = self.conn()
        self.assertIn("usage",
                      self.bot.execute(conn, ["briefing", "later"]))
        conn.close()


if __name__ == "__main__":
    unittest.main()
