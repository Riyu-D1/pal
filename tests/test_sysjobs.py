"""System-job groundwork: the 'interval' schedule frequency and the
seeded jarvis jobs (inbox-watch, morning-briefing) shipped by schema
migration 10 — OFF by default.
"""

import datetime
import unittest

from pa import db as dbmod, scheduler as sch
from tests.helpers import TmpEnv

UTC = datetime.timezone.utc


def _now(y, m, d, hh=0, mm=0):
    return datetime.datetime(y, m, d, hh, mm, tzinfo=UTC)


class TestIntervalFreq(unittest.TestCase):
    def _sched(self, mins):
        return {"timezone": "Europe/London", "time_of_day": "00:00",
                "freq": "interval", "day_of_week": None,
                "day_of_month": mins, "id": 1, "payload_json": "{}"}

    def test_floored_slots(self):
        s = self._sched(15)
        self.assertEqual(sch.latest_occurrence(s, _now(2026, 3, 4, 10, 3)),
                         "2026-03-04T10:00")
        self.assertEqual(sch.latest_occurrence(s, _now(2026, 3, 4, 10, 17)),
                         "2026-03-04T10:15")
        self.assertEqual(sch.latest_occurrence(s, _now(2026, 3, 4, 23, 59)),
                         "2026-03-04T23:45")

    def test_dst_uses_local_wall_clock(self):
        # BST (UTC+1): 10:05 UTC == 11:05 local -> 11:00 slot
        s = self._sched(15)
        self.assertEqual(sch.latest_occurrence(s, _now(2026, 7, 1, 10, 5)),
                         "2026-07-01T11:00")

    def test_non_15_intervals(self):
        s = self._sched(90)
        self.assertEqual(sch.latest_occurrence(s, _now(2026, 3, 4, 10, 3)),
                         "2026-03-04T09:00")
        s = self._sched(120)
        self.assertEqual(sch.latest_occurrence(s, _now(2026, 3, 4, 10, 3)),
                         "2026-03-04T10:00")

    def test_validation(self):
        env = TmpEnv()
        conn = env.conn()
        try:
            payload = {"prompt": "x", "channel_id": "c"}
            with self.assertRaises(ValueError):
                sch.add_schedule(conn, "i1", "interval", "00:00",
                                 "Europe/London", payload, kind="agent_job")
            with self.assertRaises(ValueError):
                sch.add_schedule(conn, "i2", "interval", "00:00",
                                 "Europe/London", payload,
                                 day_of_month=2, kind="agent_job")
            with self.assertRaises(ValueError):
                sch.add_schedule(conn, "i3", "interval", "00:00",
                                 "Europe/London", payload,
                                 day_of_month=2000, kind="agent_job")
            sid = sch.add_schedule(conn, "ok", "interval", "00:00",
                                   "Europe/London", payload,
                                   day_of_month=15, kind="agent_job")
            self.assertIsInstance(sid, int)
        finally:
            conn.close()

    def test_fires_once_per_slot_and_catches_up(self):
        env = TmpEnv()
        conn = env.conn()
        try:
            sch.add_schedule(conn, "iw", "interval", "00:00",
                             "Europe/London",
                             {"prompt": "p", "channel_id": "c"},
                             day_of_month=15, enabled=True,
                             kind="agent_job")
            out = sch.tick(conn, env.settings, _now(2026, 3, 4, 10, 3))
            self.assertEqual(len(out), 1)
            self.assertEqual(out[0]["fire_key"], "2026-03-04T10:00")
            # same slot: no second fire
            out = sch.tick(conn, env.settings, _now(2026, 3, 4, 10, 10))
            self.assertEqual(out, [])
            # next slot fires; missed slots don't storm
            out = sch.tick(conn, env.settings, _now(2026, 3, 4, 10, 47))
            self.assertEqual(len(out), 1)
            self.assertEqual(out[0]["fire_key"], "2026-03-04T10:45")
            srcs = {r["source"] for r in
                    conn.execute("SELECT source FROM chat_inbox")}
            self.assertEqual(srcs, {"job:iw"})
        finally:
            conn.close()


class TestSeededJobs(unittest.TestCase):
    def test_seeded_disabled(self):
        env = TmpEnv()
        conn = env.conn()
        try:
            iw = sch.get_schedule(conn, "inbox-watch")
            mb = sch.get_schedule(conn, "morning-briefing")
            self.assertIsNotNone(iw)
            self.assertIsNotNone(mb)
            self.assertEqual(iw["enabled"], 0)
            self.assertEqual(mb["enabled"], 0)
            self.assertEqual(iw["kind"], "agent_job")
            self.assertEqual(iw["freq"], "interval")
            self.assertEqual(iw["day_of_month"], 15)
            self.assertEqual(mb["freq"], "daily")
            self.assertEqual(mb["time_of_day"], "07:30")
            self.assertEqual(mb["timezone"], "Europe/London")
            # disabled: nothing fires
            out = sch.tick(conn, env.settings, _now(2026, 3, 4, 7, 31))
            self.assertEqual(out, [])
        finally:
            conn.close()

    def test_new_tables_exist(self):
        env = TmpEnv()
        conn = env.conn()
        try:
            for t in ("chat_history_backups", "watch_rules",
                      "inbox_seen", "imported_items", "voice_usage"):
                conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()
            self.assertEqual(dbmod.current_version(conn),
                             dbmod.SCHEMA_VERSION)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
