"""final-6 F1: pa.calsource — one calendar read path.

Composio 'googlecalendar' connection first (read-only
GOOGLECALENDAR_FIND_EVENT), the legacy GCalClient second, None +
one 'calendar source unavailable' log line when neither exists.
cal-triage / leave-now / calendar monitors all ride it now.
"""

import datetime
import unittest
from unittest import mock

from pa import activity, caltriage, calsource, composio as \
    composiomod, connectflows, gcal, leavenow
from tests.helpers import TmpEnv


def _ev(summary, in_minutes, dur=60, **kw):
    """A normalised-shape event (what the legacy client emits)."""
    s = datetime.datetime.now(datetime.timezone.utc) \
        + datetime.timedelta(minutes=in_minutes)
    ev = {"id": summary[:4] + str(in_minutes), "summary": summary,
          "start": s.isoformat(),
          "end": (s + datetime.timedelta(minutes=dur)).isoformat(),
          "location": ""}
    ev.update(kw)
    return ev


def _composio_ev(summary, in_minutes, dur=60, **kw):
    """A raw Google-API-shaped event like Composio returns — start/end
    are {'dateTime','timeZone'} dicts, not plain strings."""
    s = datetime.datetime.now(datetime.timezone.utc) \
        + datetime.timedelta(minutes=in_minutes)
    ev = {"id": summary[:4] + str(in_minutes), "summary": summary,
          "start": {"dateTime": s.isoformat(), "timeZone": "UTC"},
          "end": {"dateTime": (s + datetime.timedelta(
              minutes=dur)).isoformat(), "timeZone": "UTC"},
          "location": "", "status": "confirmed"}
    ev.update(kw)
    return ev


def _connect(conn, provider="googlecalendar"):
    """An active Composio googlecalendar connection row."""
    connectflows.activate_composio(
        conn, provider,
        {"connected_account_id": "ca-1", "toolkit": "googlecalendar"})


def _fake_composio(events):
    """A ComposioClient whose .tools/.execute return a list-events
    tool + canned event payload (also lets the real call_tool wrap
    it: out['result'] = our 'data')."""
    cli = mock.Mock()
    cli.tools.return_value = [
        {"slug": "GOOGLECALENDAR_FIND_EVENT", "description": "",
         "schema": {}, "read_only": True}]
    cli.execute.return_value = {"data": {"items": events},
                                "successful": True}
    return cli


class TestNormalise(unittest.TestCase):
    def setUp(self):
        self.env = TmpEnv()
        self.conn = self.env.conn()

    def tearDown(self):
        self.conn.close()
        calsource._warned = False

    def test_dict_times_become_tz_aware_iso(self):
        ev = _composio_ev("Standup", 20)
        out = calsource._normalise([ev], self.env.settings)
        self.assertEqual(len(out), 1)
        st = out[0]["start"]
        parsed = datetime.datetime.fromisoformat(st)
        self.assertIsNotNone(parsed.tzinfo)
        self.assertFalse(out[0]["all_day"])
        self.assertFalse(out[0]["transparent"])

    def test_date_only_marks_all_day(self):
        ev = _composio_ev("Offsite", 60)
        ev["start"] = {"date": "2026-11-20"}
        ev["end"] = {"date": "2026-11-21"}
        ev["transparency"] = "transparent"
        out = calsource._normalise([ev], self.env.settings)
        self.assertEqual(out[0]["start"], "2026-11-20")
        self.assertTrue(out[0]["all_day"])
        self.assertTrue(out[0]["transparent"])

    def test_naive_string_gets_owner_tz(self):
        out = calsource._normalise(
            [{"id": "e1", "summary": "x",
              "start": "2026-11-14T10:00:00",
              "end": "2026-11-14T11:00:00"}], self.env.settings)
        parsed = datetime.datetime.fromisoformat(out[0]["start"])
        self.assertIsNotNone(parsed.tzinfo)


class TestSourceOrder(unittest.TestCase):
    def setUp(self):
        self.env = TmpEnv(COMPOSIO_API_KEY="test-key")
        self.conn = self.env.conn()
        calsource._warned = False

    def tearDown(self):
        self.conn.close()
        calsource._warned = False

    def test_composio_preferred_over_legacy(self):
        _connect(self.conn)
        cli = _fake_composio([_composio_ev("Standup", 20)])
        with mock.patch.object(composiomod, "ComposioClient",
                               return_value=cli), \
             mock.patch.object(
                 gcal, "GCalClient",
                 side_effect=AssertionError("legacy was used")):
            evs = calsource.list_events(
                self.env.settings, self.env.env, self.conn,
                days_ahead=1, max_results=10)
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0]["summary"], "Standup")
        slug, _args, acc = cli.execute.call_args[0]
        self.assertEqual(slug, "GOOGLECALENDAR_FIND_EVENT")
        self.assertEqual(_args["calendar_id"], "primary")
        self.assertIn("timeMin", _args)
        self.assertEqual(acc, "ca-1")

    def test_legacy_used_when_no_composio(self):
        class FakeCal:
            def __init__(self, *a, **k):
                pass

            def list_events(self, **kw):
                return [_ev("Dentist", 30)]
        with mock.patch.object(gcal, "GCalClient", FakeCal):
            evs = calsource.list_events(
                self.env.settings, self.env.env, self.conn)
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0]["summary"], "Dentist")
        self.assertFalse(evs[0]["all_day"])

    def test_no_source_returns_none_and_logs_once(self):
        env = TmpEnv()          # no COMPOSIO_API_KEY, no GMAIL_* envs
        conn = env.conn()
        calsource._warned = False
        with self.assertLogs("pa.calsource", level="INFO") as cm:
            self.assertIsNone(calsource.list_events(
                env.settings, env.env, conn))
            self.assertIsNone(calsource.list_events(
                env.settings, env.env, conn))
        hits = [o for o in cm.output
                if "calendar source unavailable" in o]
        self.assertEqual(len(hits), 1)          # logged once, not spam
        conn.close()

    def test_composio_read_error_falls_back_to_legacy(self):
        _connect(self.conn)
        cli = _fake_composio([])
        cli.execute.side_effect = composiomod.ComposioError("down")

        class FakeCal:
            def __init__(self, *a, **k):
                pass

            def list_events(self, **kw):
                return [_ev("Fallback", 30)]
        with mock.patch.object(composiomod, "ComposioClient",
                               return_value=cli), \
             mock.patch.object(gcal, "GCalClient", FakeCal):
            evs = calsource.list_events(
                self.env.settings, self.env.env, self.conn)
        self.assertEqual(evs[0]["summary"], "Fallback")


# ---------------------------------------------------------------------------
# end-to-end: cal-triage + leave-now alerts from a Composio-only calendar

class _API:
    def __init__(self):
        self.posts = []

    def post_message(self, channel, content, **kw):
        self.posts.append((channel, content))
        return {"id": "m1"}


class Bot:
    """Just enough DiscordBot surface for caltriage.run + notify()."""

    def __init__(self, env):
        self._env, self.env, self.s = env, env.env, env.settings
        self.api = _API()
        self._voice = None
        self.db_path = env.db_path
        self.s.quiet_hours = ""

    def _db(self):
        return self._env.conn()

    def _owner_dm_id(self):
        return "dm-1"


class TestTriageViaComposio(unittest.TestCase):
    def setUp(self):
        self.env = TmpEnv(COMPOSIO_API_KEY="k", PA_CAL_SOON="true",
                          PA_LEAVE_NOW="true", PA_HOME_LAT="51.5",
                          PA_HOME_LON="-0.12")
        self.conn = self.env.conn()
        _connect(self.conn)
        self.bot = Bot(self.env)
        self.cli = _fake_composio([])
        self._patch = mock.patch.object(
            composiomod, "ComposioClient", return_value=self.cli)
        self._patch.start()
        calsource._warned = False

    def tearDown(self):
        self._patch.stop()
        self.conn.close()

    def test_cal_soon_fires_via_composio(self):
        self.cli.execute.return_value = {
            "data": {"items": [_composio_ev("Standup", 14)]},
            "successful": True}
        out = caltriage.run(self.bot, {})
        self.assertIn("1 soon alert", out)
        self.assertEqual(len(self.bot.api.posts), 1)
        self.assertIn("Standup", self.bot.api.posts[0][1])
        rows = activity.list_activity(self.conn, kind="noticed")
        self.assertEqual(rows[0]["source"], "cal-soon")

    def test_leave_now_fires_via_composio(self):
        ev = _composio_ev("Dentist", 30, location="Paddington")
        self.cli.execute.return_value = {
            "data": {"items": [ev]}, "successful": True}
        with mock.patch.object(leavenow, "journey_minutes",
                               return_value=25):
            out = caltriage.run(self.bot, {})
        self.assertIn("1 leave alert", out)
        self.assertIn("leave in", self.bot.api.posts[0][1])
        rows = activity.list_activity(self.conn, kind="noticed")
        self.assertEqual(rows[0]["source"], "leave-now")

    def test_composio_only_no_legacy_env_needed(self):
        """The final-5 bug: calendar jobs silently skipped when the
        GMAIL_* env grant wasn't deployed. Now Composio alone feeds
        the tick — this test runs with no GMAIL_* creds at all."""
        self.assertFalse(
            self.env.env.get("GMAIL_CLIENT_ID"))
        self.cli.execute.return_value = {
            "data": {"items": [_composio_ev("Late call", 10)]},
            "successful": True}
        self.assertIn("1 soon alert", caltriage.run(self.bot, {}))


if __name__ == "__main__":
    unittest.main()
