"""final-2 T2: calendar triage — prep cards within 30m, quiet-hours
suppression of DMs, conflict proposals that never touch the calendar
(asserted with a recording fake client)."""

import datetime
import unittest
from unittest import mock

from pa import activity, caltriage
from tests.helpers import TmpEnv


class FakeCal:
    def __init__(self, events):
        self.events = events
        self.writes = []

    def list_events(self, days_ahead=7, max_results=10,
                    time_min=None):
        return self.events

    def create_event(self, *a, **k):
        self.writes.append("create")
        return {}

    def __getattr__(self, name):
        if name.startswith(("create", "update", "delete", "patch",
                            "insert", "move")):
            return lambda *a, **k: self.writes.append(name)
        raise AttributeError(name)


def _ev(summary, in_minutes, dur=60):
    s = datetime.datetime.now(datetime.timezone.utc) \
        + datetime.timedelta(minutes=in_minutes)
    return {"id": summary[:4] + str(in_minutes), "summary": summary,
            "start": s.isoformat(),
            "end": (s + datetime.timedelta(minutes=dur)).isoformat(),
            "location": ""}


class Bot:
    def __init__(self, env, s):
        self.env, self.s = env, s
        self.dms = []

    def _db(self):
        return self.env.conn()


class TestCalTriage(unittest.TestCase):
    def setUp(self):
        self.env = TmpEnv(PA_CAL_TRIAGE="true")
        self.conn = self.env.conn()
        self.env.settings.quiet_hours = ""
        self.bot = Bot(self.env, self.env.settings)
        self.cal = FakeCal([])

    def tearDown(self):
        self.conn.close()

    def _run(self, quiet=False):
        import pa.call as callmod
        with mock.patch.object(
                caltriage.gcal if hasattr(caltriage, "gcal")
                else __import__("pa.gcal", fromlist=["GCalClient"]),
                "GCalClient", return_value=self.cal), \
             mock.patch.object(callmod, "in_quiet_hours",
                               return_value=quiet), \
             mock.patch.object(
                 caltriage.sysjobs, "dm_and_voice",
                 lambda b, t: self.bot.dms.append(t)):
            return caltriage.run(self.bot, {})

    def test_prep_card_within_30m(self):
        self.cal.events = [_ev("Standup with Alice", 20),
                           _ev("Dentist", 200)]
        self.assertIn("1 prep", self._run())
        self.assertTrue(self.bot.dms)
        self.assertIn("Standup", self.bot.dms[0])
        rows = activity.list_activity(self.conn, kind="noticed")
        self.assertEqual(len(rows), 1)
        # second run doesn't re-post
        self._run()
        self.assertEqual(len(self.bot.dms), 1)

    def test_quiet_hours_local_only(self):
        self.env.settings.quiet_hours = "0-23"
        self.cal.events = [_ev("Standup", 10)]
        self._run(quiet=True)
        self.assertEqual(self.bot.dms, [])
        self.assertEqual(
            len(activity.list_activity(self.conn, kind="noticed")),
            1)

    def test_conflict_proposal_never_writes(self):
        self.cal.events = [_ev("A call", 20), _ev("B call", 40)]
        self._run()
        drafts = activity.list_activity(self.conn, kind="draft")
        self.assertTrue(drafts)
        self.assertIn("clash", drafts[0]["body"])
        self.assertEqual(self.cal.writes, [])

    def test_flag_off_noop(self):
        self.env.settings.cal_triage = False
        self.assertEqual(caltriage.run(self.bot, {}), "")


class _API:
    def __init__(self):
        self.posts = []

    def post_message(self, channel, text):
        self.posts.append((channel, text))
        return {"id": "m1"}


class SoonBot:
    """Bot with just enough surface for notify() to post a DM."""

    def __init__(self, env):
        self.env, self.s = env, env.settings
        self.api = _API()
        self._voice = None
        self.db_path = env.db_path

    def _db(self):
        return self.env.conn()

    def _owner_dm_id(self):
        return "dm-1"


class TestCalSoon(unittest.TestCase):
    """final-5 F2: PA_CAL_SOON meeting-soon pings on the same tick."""

    def setUp(self):
        self.env = TmpEnv(PA_CAL_SOON="true")
        self.conn = self.env.conn()
        self.bot = SoonBot(self.env)
        self.cal = FakeCal([])

    def tearDown(self):
        self.conn.close()

    def _run(self, quiet=False):
        import pa.call as callmod
        import pa.gcal as gcalmod
        with mock.patch.object(gcalmod, "GCalClient",
                               return_value=self.cal), \
             mock.patch.object(callmod, "in_quiet_hours",
                               return_value=quiet):
            return caltriage.run(self.bot, {})

    def test_event_in_14m_fires_15m_once(self):
        self.cal.events = [_ev("Standup", 14)]
        out = self._run()
        self.assertIn("1 soon alert", out)
        self.assertEqual(len(self.bot.api.posts), 1)
        self.assertIn("Standup", self.bot.api.posts[0][1])
        # second tick: same lead deduped, no second DM
        self.assertEqual(self._run(), "")
        self.assertEqual(len(self.bot.api.posts), 1)
        rows = activity.list_activity(self.conn, kind="noticed")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source"], "cal-soon")

    def test_event_in_40m_fires_nothing(self):
        self.cal.events = [_ev("Later", 40)]
        self.assertEqual(self._run(), "")
        self.assertEqual(self.bot.api.posts, [])

    def test_event_in_4m_fires_5m_lead(self):
        self.cal.events = [_ev("Now-ish", 4)]
        self.assertIn("1 soon alert", self._run())
        self.assertEqual(len(self.bot.api.posts), 1)

    def test_quiet_hours_holds_alert(self):
        self.cal.events = [_ev("Early", 10)]
        out = self._run(quiet=True)
        self.assertIn("1 soon alert", out)
        self.assertEqual(self.bot.api.posts, [])   # held, not sent
        rows = activity.list_activity(self.conn, kind="noticed")
        self.assertEqual(len(rows), 1)

    def test_no_calendar_writes(self):
        self.cal.events = [_ev("Standup", 14)]
        self._run()
        self.assertEqual(self.cal.writes, [])

    def test_runs_when_triage_off(self):
        # PA_CAL_TRIAGE is off in this env — soon path still fires
        self.assertFalse(self.env.settings.cal_triage)
        self.cal.events = [_ev("Standup", 14)]
        self.assertIn("1 soon alert", self._run())

    def test_both_flags_off_noop(self):
        self.env.settings.cal_soon = False
        self.assertEqual(caltriage.run(self.bot, {}), "")

    def test_leads_configurable(self):
        env = TmpEnv(PA_CAL_SOON="true", PA_CAL_SOON_LEADS="30")
        self.assertEqual(caltriage.soon_leads(env.settings), [30])


if __name__ == "__main__":
    unittest.main()
