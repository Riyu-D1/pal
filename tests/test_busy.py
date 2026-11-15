"""voice_notes 'auto' — voice notes only while the owner
is busy (pa.busy triggers + the voice_should_send gate)."""

import datetime
import time
import unittest
from unittest import mock

from pa import busy as busymod, calsource, db as dbmod, notify, \
    sysjobs
from tests.helpers import TmpEnv

UTC = datetime.timezone.utc


class _API:
    def __init__(self):
        self.posts = []

    def post_message(self, channel, content, **kw):
        self.posts.append((channel, content))
        return {"id": "m1"}


class _Voice:
    def __init__(self):
        self.notes = []

    def send_voice_note(self, channel_id, text, **kw):
        self.notes.append((channel_id, text))
        return {"sent": True}


class Bot:
    """Just enough bot surface for busy/voice gating + notify()."""

    def __init__(self, env):
        self._env, self.env, self.s = env, env.env, env.settings
        self.db_path = env.db_path
        self.api = _API()
        self._voice = _Voice()
        self.s.quiet_hours = ""

    def _db(self):
        return self._env.conn()

    def _owner_dm_id(self):
        return "dm-1"


class Base(unittest.TestCase):
    def setUp(self):
        self.env = TmpEnv()
        self.conn = self.env.conn()
        self.bot = Bot(self.env)
        self.s = self.env.settings

    def tearDown(self):
        self.conn.close()


class TestMode(Base):
    def test_unset_defaults_to_auto(self):
        self.assertEqual(busymod.mode(self.bot), "auto")
        self.assertFalse(busymod.voice_should_send(self.bot))

    def test_env_true_means_on(self):
        env = TmpEnv(PA_VOICE_NOTES="true")
        self.addCleanup(lambda: None)
        bot = Bot(env)
        self.assertEqual(busymod.mode(bot), "on")

    def test_explicit_on_off_auto_win(self):
        for v in ("on", "off", "auto"):
            dbmod.set_setting(self.conn, "voice_notes", v)
            self.assertEqual(busymod.mode(self.bot), v)

    def test_on_always_sends(self):
        dbmod.set_setting(self.conn, "voice_notes", "on")
        self.assertTrue(busymod.voice_should_send(self.bot))

    def test_off_never_sends_even_when_busy(self):
        dbmod.set_setting(self.conn, "voice_notes", "off")
        busymod.note_voice(self.conn)
        busymod.note_text(self.conn, "i'm busy")
        self.assertFalse(busymod.voice_should_send(self.bot))


class TestTextTrigger(Base):
    """Trigger 2: 'i'm busy' sets busy_until; 'not busy' clears it."""

    def test_im_busy_marks_busy(self):
        self.assertFalse(busymod.is_busy(self.bot))
        busymod.note_text(self.conn, "i'm busy")
        self.assertTrue(busymod.voice_should_send(self.bot))

    def test_busy_phrases(self):
        for t in ("driving home", "can't read this",
                  "in a meeting", "busy for a bit"):
            conn = self.env.conn()
            busymod.note_text(conn, "not busy")
            busymod.note_text(conn, t)
            self.assertTrue(busymod.is_busy(self.bot), t)
            conn.close()

    def test_duration_parsed(self):
        busymod.note_text(self.conn, "busy for 30 minutes")
        until = float(dbmod.get_setting(self.conn, "busy_until"))
        self.assertAlmostEqual(until - time.time(), 30 * 60, delta=5)
        busymod.note_text(self.conn, "busy for an hour")
        until = float(dbmod.get_setting(self.conn, "busy_until"))
        self.assertAlmostEqual(until - time.time(), 60 * 60, delta=5)

    def test_default_duration_is_an_hour(self):
        busymod.note_text(self.conn, "i'm busy")
        until = float(dbmod.get_setting(self.conn, "busy_until"))
        self.assertAlmostEqual(
            until - time.time(), 60 * 60, delta=5)

    def test_free_phrases_clear(self):
        for t in ("not busy", "i'm free", "i'm back", "back"):
            busymod.note_text(self.conn, "i'm busy")
            self.assertTrue(busymod.is_busy(self.bot))
            busymod.note_text(self.conn, t)
            self.assertFalse(busymod.is_busy(self.bot), t)

    def test_free_also_clears_voice_trigger(self):
        busymod.note_voice(self.conn)
        self.assertTrue(busymod.is_busy(self.bot))
        busymod.note_text(self.conn, "i'm free")
        self.assertFalse(busymod.is_busy(self.bot))

    def test_unrelated_text_changes_nothing(self):
        busymod.note_text(self.conn, "what's the weather like?")
        self.assertFalse(busymod.is_busy(self.bot))


class TestVoiceTrigger(Base):
    """Trigger 1: a voice message -> busy for ~10 minutes."""

    def test_voice_message_marks_busy(self):
        busymod.note_voice(self.conn)
        self.assertTrue(busymod.voice_should_send(self.bot))

    def test_old_voice_message_expires(self):
        dbmod.set_setting(self.conn, "busy_voice_at",
                          f"{time.time() - 20 * 60:.0f}")
        self.assertFalse(busymod.is_busy(self.bot))


class TestCalendarTrigger(Base):
    """Trigger 3: an opaque, non-all-day event running right now."""

    def _ev(self, start_min, end_min, **kw):
        s = datetime.datetime.now(UTC) \
            + datetime.timedelta(minutes=start_min)
        e = datetime.datetime.now(UTC) \
            + datetime.timedelta(minutes=end_min)
        ev = {"id": "e1", "summary": "Meeting",
              "start": s.isoformat(), "end": e.isoformat(),
              "all_day": False, "transparent": False,
              "location": ""}
        ev.update(kw)
        return ev

    def _events(self, evs):
        return mock.patch.object(
            calsource, "list_events", return_value=evs)

    def test_running_event_is_busy(self):
        with self._events([self._ev(-30, 60)]):
            self.assertTrue(busymod.is_busy(self.bot))
        # cached until event end — still busy, no second read
        self.assertTrue(busymod.is_busy(self.bot))

    def test_future_event_not_busy(self):
        with self._events([self._ev(30, 90)]):
            self.assertFalse(busymod.is_busy(self.bot))

    def test_ended_event_not_busy(self):
        with self._events([self._ev(-90, -10)]):
            self.assertFalse(busymod.is_busy(self.bot))

    def test_all_day_event_not_busy(self):
        with self._events([self._ev(-30, 60, all_day=True)]):
            self.assertFalse(busymod.is_busy(self.bot))

    def test_transparent_event_not_busy(self):
        with self._events([self._ev(-30, 60, transparent=True)]):
            self.assertFalse(busymod.is_busy(self.bot))

    def test_no_calendar_source_not_busy(self):
        with self._events(None):
            self.assertFalse(busymod.is_busy(self.bot))


class TestProactiveGate(Base):
    """Proactive alerts follow the same mode rule (notify path)."""

    def test_dm_and_voice_off_sends_no_voice(self):
        dbmod.set_setting(self.conn, "voice_notes", "off")
        sysjobs.dm_and_voice(self.bot, "hello")
        self.assertEqual(self.bot._voice.notes, [])
        self.assertTrue(self.bot.api.posts)        # text still lands

    def test_dm_and_voice_on_sends_voice(self):
        dbmod.set_setting(self.conn, "voice_notes", "on")
        sysjobs.dm_and_voice(self.bot, "hello")
        self.assertEqual(len(self.bot._voice.notes), 1)

    def test_dm_and_voice_auto_busy_sends_voice(self):
        dbmod.set_setting(self.conn, "voice_notes", "auto")
        busymod.note_text(self.conn, "i'm busy")
        sysjobs.dm_and_voice(self.bot, "hello")
        self.assertEqual(len(self.bot._voice.notes), 1)

    def test_dm_and_voice_auto_idle_stays_text(self):
        dbmod.set_setting(self.conn, "voice_notes", "auto")
        sysjobs.dm_and_voice(self.bot, "hello")
        self.assertEqual(self.bot._voice.notes, [])

    def test_notify_urgent_follows_the_same_rule(self):
        dbmod.set_setting(self.conn, "voice_notes", "auto")
        out = notify.notify(self.bot, self.conn, "urgent-mail",
                            "urgent", "visa approved")
        self.assertEqual(out, "sent")
        self.assertEqual(self.bot._voice.notes, [])
        busymod.note_text(self.conn, "i'm busy")
        notify.notify(self.bot, self.conn, "urgent-mail-2",
                      "urgent", "second")
        self.assertEqual(len(self.bot._voice.notes), 1)


class TestMessageRouteFeedsBusy(unittest.TestCase):
    """_handle_event: owner text feeds note_text, voice feeds
    note_voice — through the real route machinery."""

    def setUp(self):
        from pa import discordbot
        self.env = TmpEnv(DISCORD_OWNER_ID="o1")
        self.env.conn().close()
        self.bot = discordbot.DiscordBot(
            self.env.settings, env=self.env.env, notifier=False)
        self.bot.api = mock.Mock()
        # the workers spawn threads + network — stub them; we only
        # care that the route fed the busy markers
        self.bot._start_chat = mock.Mock()
        self.bot._start_voice = mock.Mock()

    def _event(self, content="", attachments=None):
        return {"id": f"m{time.time_ns()}", "channel_id": "dm1",
                "channel_type": 1, "author": {"id": "o1"},
                "content": content,
                "attachments": attachments or []}

    def test_busy_text_marks_owner_busy(self):
        self.bot._handle_event("MESSAGE_CREATE",
                               self._event("i'm busy"))
        conn = self.env.conn()
        self.assertTrue(busymod.is_busy(self.bot))
        conn.close()

    def test_voice_attachment_marks_owner_busy(self):
        att = {"id": "a1", "filename": "voice-message.ogg",
               "content_type": "audio/ogg",
               "url": "https://cdn.discordapp.com/att/1/v.ogg"}
        self.bot._handle_event("MESSAGE_CREATE",
                               self._event(attachments=[att]))
        conn = self.env.conn()
        self.assertTrue(busymod.is_busy(self.bot))
        conn.close()


if __name__ == "__main__":
    unittest.main()
