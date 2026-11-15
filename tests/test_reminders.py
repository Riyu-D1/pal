"""Reminders: natural-language parsing, reminder payload delivery
(DM + voice note, urgent -> call), /reminders list + cancel."""

import datetime
import unittest
from unittest import mock

from pa import chattools, db as dbmod, discordbot, scheduler as sch
from tests.helpers import TmpEnv


class FakeAPI:
    def __init__(self):
        self.posts = []

    def open_dm(self, user_id):
        return "dm-1"

    def post_message(self, channel_id, content, **kw):
        self.posts.append((channel_id, content))
        return {"id": "m1"}


class FakeCalls:
    def __init__(self):
        self.calls = []

    def call(self, message, urgent=False, source="chat"):
        self.calls.append({"message": message, "urgent": urgent,
                           "source": source})
        return {"calling": True}


class FakeVoice:
    def __init__(self):
        self.notes = []

    def send_voice_note(self, channel_id, text, **kw):
        self.notes.append((channel_id, text))
        return {"sent": True}


def _ctx(env, conn):
    return chattools.ChatToolContext(
        conn, env.settings, None, "chan-1", env=env.env)


def _remind(env, conn, text="call mum", when="in 20 minutes",
            urgent=False):
    return chattools.run(_ctx(env, conn), "remind_me",
                         {"text": text, "when": when, "urgent": urgent})


class TestParsing(unittest.TestCase):
    def setUp(self):
        self.env = TmpEnv()
        self.conn = self.env.conn()

    def tearDown(self):
        self.conn.close()

    def _sched(self, out):
        name = out["scheduled"]
        row = sch.get_schedule(self.conn, name)
        return row

    def test_in_minutes_full_words(self):
        for when in ("in 20 minutes", "in 2 hours", "in 1 hour",
                     "in 3 days", "in 45s"):
            out = _remind(self.env, self.conn, when=when)
            self.assertIn("scheduled", out, when)
            self.assertEqual(self._sched(out)["freq"], "once")

    def test_at_explicit(self):
        out = _remind(self.env, self.conn, when="at 5pm")
        self.assertIn("scheduled", out)
        self.assertTrue(self._sched(out)["time_of_day"].endswith("T17:00"))
        out = _remind(self.env, self.conn, when="at 17:30")
        self.assertTrue(self._sched(out)["time_of_day"].endswith("T17:30"))

    def test_at_ambiguous_picks_future(self):
        out = _remind(self.env, self.conn, when="at 5")
        self.assertIn("scheduled", out)
        tod = self._sched(out)["time_of_day"]
        hh, mm = int(tod[11:13]), int(tod[14:16])
        # ambiguous 'at 5' can only mean 05:00 or 17:00
        self.assertIn((hh, mm), [(5, 0), (17, 0)])
        # and it must be in the future in Europe/London
        from zoneinfo import ZoneInfo
        inst = datetime.datetime.strptime(tod, "%Y-%m-%dT%H:%M") \
            .replace(tzinfo=ZoneInfo("Europe/London"))
        self.assertGreater(
            inst, datetime.datetime.now(ZoneInfo("Europe/London")))

    def test_tomorrow_and_today(self):
        out = _remind(self.env, self.conn, when="tomorrow 9am")
        self.assertIn("scheduled", out)
        self.assertTrue(self._sched(out)["time_of_day"].endswith("T09:00"))
        out = _remind(self.env, self.conn, when="tomorrow at 18:30")
        self.assertTrue(self._sched(out)["time_of_day"].endswith("T18:30"))

    def test_every_forms(self):
        out = _remind(self.env, self.conn, when="every monday 9am")
        row = self._sched(out)
        self.assertEqual(row["freq"], "weekly")
        self.assertEqual(row["day_of_week"], 0)
        self.assertEqual(row["time_of_day"], "09:00")

        out = _remind(self.env, self.conn, when="every day 08:30")
        row = self._sched(out)
        self.assertEqual(row["freq"], "daily")

        out = _remind(self.env, self.conn, when="every 1st 9am")
        row = self._sched(out)
        self.assertEqual(row["freq"], "monthly")
        self.assertEqual(row["day_of_month"], 1)

        out = _remind(self.env, self.conn, when="every 30 minutes")
        row = self._sched(out)
        self.assertEqual(row["freq"], "interval")
        self.assertEqual(row["day_of_month"], 30)

    def test_bad_when_rejected(self):
        out = _remind(self.env, self.conn, when="whenever mate")
        self.assertIn("error", out)

    def test_reminder_payload_marks_reminder(self):
        out = _remind(self.env, self.conn, text="call mum",
                      when="in 5m", urgent=True)
        row = self._sched(out)
        from pa.util import json_loads_bounded
        p = json_loads_bounded(row["payload_json"])
        self.assertTrue(p["reminder"])
        self.assertEqual(p["text"], "call mum")
        self.assertTrue(p["urgent"])
        self.assertEqual(p["channel_id"], "chan-1")


class TestDelivery(unittest.TestCase):
    def setUp(self):
        # PA_VOICE_NOTES -> mode 'on': the reminder voice note must land
        # ('auto' would stay text-only while not busy)
        self.env = TmpEnv(DISCORD_OWNER_ID="o1", PA_VOICE_NOTES="true")
        self.env.conn().close()
        self.bot = discordbot.DiscordBot(
            self.env.settings, env=self.env.env, notifier=False)
        self.bot.api = FakeAPI()
        self.bot._calls = FakeCalls()
        self.bot._voice = FakeVoice()

    def _fire(self, text="call mum", urgent=False, channel="chan-1"):
        conn = dbmod.connect(self.env.db_path)
        conn.execute(
            "INSERT INTO schedules(name, kind, freq, time_of_day,"
            " timezone, enabled, payload_json, created_at, updated_at)"
            " VALUES('remind-t1','chat_prompt','once',?,"
            " 'Europe/London',1,?,?,?)",
            (datetime.datetime.now(datetime.timezone.utc)
             .strftime("%Y-%m-%dT%H:%M"),
             __import__("json").dumps(
                 {"channel_id": channel, "prompt": f"[scheduled "
                                                   f"reminder] {text}",
                  "reminder": True, "text": text, "urgent": urgent}),
             "t", "t"))
        conn.execute(
            "INSERT INTO chat_inbox(channel_id, prompt, source, status,"
            " created_at) VALUES(?,?,'schedule:remind-t1','queued','t')",
            (channel, f"[scheduled reminder] {text}"))
        conn.close()
        self.bot.tick_chat_inbox()
        import time
        time.sleep(0.3)               # delivery runs on a daemon thread

    def test_delivers_dm_plus_voice(self):
        self._fire()
        self.assertTrue(any("⏰" in c and "call mum" in c
                            for _, c in self.bot.api.posts))
        self.assertTrue(any("call mum" in t
                            for _, t in self.bot._voice.notes))
        self.assertEqual(self.bot._calls.calls, [])

    def test_urgent_also_calls(self):
        self._fire(urgent=True)
        self.assertEqual(len(self.bot._calls.calls), 1)
        self.assertTrue(self.bot._calls.calls[0]["urgent"])
        self.assertEqual(self.bot._calls.calls[0]["source"], "reminder")

    def test_empty_channel_falls_back_to_dm(self):
        self._fire(channel="")
        self.assertTrue(any(ch == "dm-1" for ch, _ in self.bot.api.posts))

    def test_non_reminder_schedule_still_chats(self):
        # a plain schedule:* row (no reminder flag) goes to the engine
        engine = mock.Mock()
        engine.run_turn.return_value = {"text": "ok", "pending": []}
        self.bot._engine_obj = engine
        conn = dbmod.connect(self.env.db_path)
        conn.execute(
            "INSERT INTO chat_inbox(channel_id, prompt, source, status,"
            " created_at) VALUES('c','p','schedule:plain','queued','t')")
        conn.close()
        self.bot.tick_chat_inbox()
        import time
        time.sleep(0.4)
        engine.run_turn.assert_called_once()


class TestRemindersCommand(unittest.TestCase):
    def setUp(self):
        self.env = TmpEnv()
        self.bot = discordbot.DiscordBot(
            self.env.settings, env=self.env.env, notifier=False)
        self.bot.api = FakeAPI()

    def test_list_and_cancel(self):
        conn = self.env.conn()
        out = _remind(self.env, conn, when="in 1 day")
        name = out["scheduled"]
        out = self.bot.execute(conn, ["reminders"])
        self.assertIn(name, out)
        self.assertIn("call mum", out)
        # cancel needs a reminder name, not an agent job
        out = self.bot.execute(conn, ["reminders", "cancel", name])
        self.assertIn("cancelled", out)
        self.assertIsNone(sch.get_schedule(conn, name))
        out = self.bot.execute(conn, ["reminders", "cancel", name])
        self.assertIn("no reminder", out)
        out = self.bot.execute(conn, ["reminders"])
        self.assertIn("No reminders", out)
        conn.close()

    def test_cancel_purges_queued_copy(self):
        conn = self.env.conn()
        out = _remind(self.env, conn, when="in 1 day")
        name = out["scheduled"]
        conn.execute(
            "INSERT INTO chat_inbox(channel_id, prompt, source, status,"
            " created_at) VALUES('c','p',?,'queued','t')",
            (f"schedule:{name}",))
        self.bot.execute(conn, ["reminders", "cancel", name])
        left = conn.execute(
            "SELECT COUNT(*) n FROM chat_inbox WHERE source=?",
            (f"schedule:{name}",)).fetchone()["n"]
        self.assertEqual(left, 0)
        conn.close()


if __name__ == "__main__":
    unittest.main()
