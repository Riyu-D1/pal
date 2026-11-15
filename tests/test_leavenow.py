"""A5: leave-now alerts — TfL journey + deduped notify() pings."""

import datetime
import json
import unittest
from unittest.mock import patch

from pa import db as dbmod, leavenow
from tests.helpers import TmpEnv


UTC = datetime.timezone.utc


class _API:
    def __init__(self, posts):
        self.posts = posts

    def post_message(self, channel, text, **kw):
        self.posts.append((channel, text))
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

    def _db(self):
        return dbmod.connect(self.db_path)


def _ev(eid, in_minutes, location="Waterloo Station",
        summary="dentist"):
    start = (datetime.datetime.now(UTC)
             + datetime.timedelta(minutes=in_minutes))
    return {"id": eid, "summary": summary, "location": location,
            "start": start.isoformat(),
            "end": (start + datetime.timedelta(hours=1)).isoformat()}


def _now():
    return datetime.datetime.now(UTC)


class Base(unittest.TestCase):
    def setUp(self):
        self.env = TmpEnv(PA_LEAVE_NOW="true")
        self.conn = self.env.conn()
        self.posts = []
        self.bot = _Bot(self.env, self.posts)


class TestLeaveNow(Base):
    def test_flag_off(self):
        self.env.settings.leave_now = False
        with patch("pa.leavenow.journey_minutes") as jm:
            n = leavenow.alerts(self.bot, self.conn,
                                [_ev("e1", 30)], _now())
        self.assertEqual(n, 0)
        jm.assert_not_called()

    def test_leave_in_10m_fires_once(self):
        evs = [_ev("e1", 30)]
        with patch("pa.leavenow.journey_minutes", return_value=25):
            n = leavenow.alerts(self.bot, self.conn, evs, _now())
        self.assertEqual(n, 1)
        self.assertEqual(len(self.posts), 1)
        self.assertIn("leave in", self.posts[0][1])
        self.assertIn("dentist", self.posts[0][1])
        self.assertIn("~25m journey", self.posts[0][1])
        # again -> deduped
        with patch("pa.leavenow.journey_minutes", return_value=25):
            n2 = leavenow.alerts(self.bot, self.conn, evs, _now())
        self.assertEqual(n2, 0)
        self.assertEqual(len(self.posts), 1)

    def test_leave_now_fires(self):
        evs = [_ev("e2", 25)]
        with patch("pa.leavenow.journey_minutes", return_value=25):
            n = leavenow.alerts(self.bot, self.conn, evs, _now())
        self.assertEqual(n, 1)
        self.assertIn("leave now", self.posts[0][1])

    def test_far_future_and_past_skipped(self):
        evs = [_ev("e3", 40), _ev("e4", -5)]
        with patch("pa.leavenow.journey_minutes", return_value=25):
            n = leavenow.alerts(self.bot, self.conn, evs, _now())
        self.assertEqual(n, 0)
        self.assertEqual(self.posts, [])

    def test_no_location_skipped(self):
        evs = [_ev("e5", 30, location="")]
        with patch("pa.leavenow.journey_minutes") as jm:
            n = leavenow.alerts(self.bot, self.conn, evs, _now())
        self.assertEqual(n, 0)
        jm.assert_not_called()

    def test_tfl_error_skips_quietly(self):
        evs = [_ev("e6", 30)]
        with patch("pa.leavenow.journey_minutes", return_value=0):
            n = leavenow.alerts(self.bot, self.conn, evs, _now())
        self.assertEqual(n, 0)
        self.assertEqual(self.posts, [])

    def test_quiet_hours_hold(self):
        from zoneinfo import ZoneInfo
        h = datetime.datetime.now(UTC).astimezone(
            ZoneInfo(self.env.settings.timezone)).hour
        self.env.settings.quiet_hours = f"{h}-{(h + 1) % 24}"
        evs = [_ev("e7", 30)]
        with patch("pa.leavenow.journey_minutes", return_value=25):
            n = leavenow.alerts(self.bot, self.conn, evs, _now())
        self.assertEqual(n, 1)                 # counted as held
        self.assertEqual(self.posts, [])       # nothing sent
        rows = self.conn.execute(
            "SELECT kind FROM activity WHERE source='leave-now'"
        ).fetchall()
        self.assertEqual(len(rows), 1)

    def test_journey_cached_per_event(self):
        evs = [_ev("e8", 60)]
        with patch("pa.leavenow.journey_minutes",
                   return_value=25) as jm:
            leavenow.alerts(self.bot, self.conn, evs, _now())
            leavenow.alerts(self.bot, self.conn, evs, _now())
        self.assertEqual(jm.call_count, 1)     # second tick used cache


class TestPhoneOrigin(unittest.TestCase):
    def setUp(self):
        self.env = TmpEnv(PA_LEAVE_NOW="true")
        self.conn = self.env.conn()
        self.bot = _Bot(self.env, [])

    def _store_fix(self, age_seconds, lat=51.5, lon=-0.1):
        ts = (datetime.datetime.now(UTC)
              - datetime.timedelta(seconds=age_seconds)).isoformat()
        self.conn.execute(
            "INSERT INTO node_jobs(node, cap, args_json, remote_id,"
            " status, result_json, scope, created_at, updated_at)"
            " VALUES('phone','location','{}','r1','done',?,"
            " 'owner-chat',?,?)",
            (json.dumps({"lat": lat, "lon": lon}), ts, ts))

    def test_phone_fix_unused_when_flag_off(self):
        self._store_fix(60)
        self.assertNotEqual(leavenow._latlon(self.bot, self.conn),
                            "51.5,-0.1")

    def test_fresh_phone_fix_used_when_flag_on(self):
        self.env.settings.leave_now_phone_loc = True
        self._store_fix(60)
        self.assertEqual(leavenow._latlon(self.bot, self.conn),
                         "51.5,-0.1")

    def test_stale_phone_fix_ignored(self):
        self.env.settings.leave_now_phone_loc = True
        self._store_fix(3600)
        self.assertNotEqual(leavenow._latlon(self.bot, self.conn),
                            "51.5,-0.1")

    def test_home_coords_then_briefing(self):
        self.assertEqual(leavenow._latlon(self.bot, self.conn),
                         "51.5074,-0.1278")   # briefing London default
        self.env.settings.home_lat = "51.1"
        self.env.settings.home_lon = "-0.3"
        self.assertEqual(leavenow._latlon(self.bot, self.conn),
                         "51.1,-0.3")


class TestJourneyMinutes(unittest.TestCase):
    def setUp(self):
        self.env = TmpEnv()
        self.bot = _Bot(self.env, [])

    def test_picks_min_duration(self):
        payload = {"journeys": [{"duration": 34}, {"duration": 25},
                                {"duration": 40}]}
        with patch("pa.leavenow.APIClient") as C:
            C.return_value.get_json.return_value = ({}, payload)
            self.assertEqual(
                leavenow.journey_minutes(self.bot, "51.5,-0.1",
                                         "Waterloo"), 25)

    def test_no_journeys_zero(self):
        with patch("pa.leavenow.APIClient") as C:
            C.return_value.get_json.return_value = ({}, {})
            self.assertEqual(
                leavenow.journey_minutes(self.bot, "a", "b"), 0)

    def test_http_error_zero(self):
        from pa.netclient import TransportError
        with patch("pa.leavenow.APIClient") as C:
            C.return_value.get_json.side_effect = TransportError("x")
            self.assertEqual(
                leavenow.journey_minutes(self.bot, "a", "b"), 0)

    def test_app_key_sent_not_logged(self):
        self.env.env["TFL_APP_KEY"] = "key-xyz"
        payload = {"journeys": [{"duration": 10}]}
        with patch("pa.leavenow.APIClient") as C:
            C.return_value.get_json.return_value = ({}, payload)
            leavenow.journey_minutes(self.bot, "a", "b")
        path = C.return_value.get_json.call_args[0][0]
        self.assertIn("app_key=key-xyz", path)

    def test_key_not_logged_on_error(self):
        import logging
        self.env.env["TFL_APP_KEY"] = "key-xyz"
        from pa.netclient import TransportError
        recs = []
        h = logging.Handler()
        h.emit = recs.append
        logging.getLogger("pa.leavenow").addHandler(h)
        try:
            with patch("pa.leavenow.APIClient") as C:
                C.return_value.get_json.side_effect = \
                    TransportError("down")
                leavenow.journey_minutes(self.bot, "a", "b")
        finally:
            logging.getLogger("pa.leavenow").removeHandler(h)
        for rec in recs:
            self.assertNotIn("key-xyz", rec.getMessage())


if __name__ == "__main__":
    unittest.main()
