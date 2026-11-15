"""Scheduler: timezone/DST correctness + restart-safe idempotency.

- Europe/London daily 07:30 fires at the right UTC instant on both sides
  of the spring-forward and fall-back transitions.
- A fired occurrence is one schedule_runs row keyed (schedule_id,
  fire_key): tick twice, reconnect and tick again -> still exactly one
  task.
- Missed slots catch up once (latest due slot only).
- Disabled schedules never fire; a missing assistant marks 'skipped'.
"""

import datetime
import unittest

from pa import scheduler as sch, worker as workmod
from tests.helpers import TmpEnv

UTC = datetime.timezone.utc


def _now(y, m, d, hh=0, mm=0):
    return datetime.datetime(y, m, d, hh, mm, tzinfo=UTC)


class TestOccurrenceMath(unittest.TestCase):
    def _sched(self, tz="Europe/London", tod="07:30", freq="daily",
             dow=None):
        return {"timezone": tz, "time_of_day": tod, "freq": freq,
                "day_of_week": dow, "id": 1, "payload_json": "{}"}

    def test_daily_gmt_vs_bst(self):
        s = self._sched()
        # January: London is GMT -> 07:30 local = 07:30 UTC
        key = sch.latest_occurrence(s, _now(2026, 1, 15, 9, 0))
        self.assertEqual(key, "2026-01-15T07:30")
        inst = sch.occurrence_instant(s, key)
        self.assertEqual(inst, _now(2026, 1, 15, 7, 30))
        # July: London is BST (+1) -> 07:30 local = 06:30 UTC
        key = sch.latest_occurrence(s, _now(2026, 7, 15, 9, 0))
        self.assertEqual(key, "2026-07-15T07:30")
        inst = sch.occurrence_instant(s, key)
        self.assertEqual(inst, _now(2026, 7, 15, 6, 30))

    def test_dst_transition_days(self):
        s = self._sched()
        # 2026-03-29: spring forward 01:00->02:00. 07:30 exists -> 06:30 UTC
        key = sch.latest_occurrence(s, _now(2026, 3, 29, 12, 0))
        inst = sch.occurrence_instant(s, key)
        self.assertEqual((inst.hour, inst.minute), (6, 30))
        self.assertEqual(inst.date().isoformat(), "2026-03-29")
        # 2026-10-25: fall back 02:00->01:00. 07:30 unambiguous -> 07:30 UTC
        # (by 12:00 UTC the clock is GMT again)
        key = sch.latest_occurrence(s, _now(2026, 10, 25, 12, 0))
        inst = sch.occurrence_instant(s, key)
        self.assertEqual((inst.hour, inst.minute), (7, 30))

    def test_before_slot_uses_yesterday(self):
        s = self._sched()
        key = sch.latest_occurrence(s, _now(2026, 1, 15, 6, 0))
        self.assertEqual(key, "2026-01-14T07:30")

    def test_weekly_day_of_week(self):
        s = self._sched(freq="weekly", dow=0, tod="09:00")
        # 2026-01-14 is a Wednesday -> latest Monday slot is Jan 12
        key = sch.latest_occurrence(s, _now(2026, 1, 14, 12, 0))
        self.assertEqual(key, "2026-01-12T09:00")
        inst = sch.occurrence_instant(s, key)
        self.assertEqual(inst, _now(2026, 1, 12, 9, 0))

    def test_hourly(self):
        s = self._sched(freq="hourly", tod="00:15")
        key = sch.latest_occurrence(s, _now(2026, 1, 15, 10, 30))
        self.assertEqual(key, "2026-01-15T10:15")
        key = sch.latest_occurrence(s, _now(2026, 1, 15, 10, 5))
        self.assertEqual(key, "2026-01-15T09:15")


class TestTickIdempotency(unittest.TestCase):
    def setUp(self):
        self.env = TmpEnv()
        self.conn = self.env.conn()
        self.aid = self.env.add_assistant(name="email")
        sch.set_enabled(self.conn, "daily-email-summary", True)

    def tearDown(self):
        self.conn.close()

    def _tasks(self):
        return self.conn.execute(
            "SELECT * FROM tasks ORDER BY id").fetchall()

    def _runs(self):
        return self.conn.execute(
            "SELECT * FROM schedule_runs ORDER BY id").fetchall()

    def test_disabled_by_default(self):
        # the shipped job exists but is DISABLED until the owner enables it
        sch.set_enabled(self.conn, "daily-email-summary", False)
        out = sch.tick(self.conn, self.env.settings, _now(2026, 1, 15, 12))
        self.assertEqual(out, [])
        self.assertEqual(self._tasks(), [])

    def test_fires_once_per_occurrence(self):
        now = _now(2026, 1, 15, 12)
        out = sch.tick(self.conn, self.env.settings, now)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["fire_key"], "2026-01-15T07:30")
        self.assertEqual(len(self._tasks()), 1)
        # same tick again -> nothing new
        out = sch.tick(self.conn, self.env.settings, now)
        self.assertEqual(out, [])
        # "restart": a brand new connection sees the durable run row
        self.conn.close()
        self.conn = self.env.conn()
        out = sch.tick(self.conn, self.env.settings, now)
        self.assertEqual(out, [])
        self.assertEqual(len(self._tasks()), 1)
        self.assertEqual(len(self._runs()), 1)

    def test_catch_up_after_downtime(self):
        # service was down through Jan 15's slot; tick on Jan 16 fires the
        # most recent due occurrence only (Jan 16 07:30 by 12:00)
        out = sch.tick(self.conn, self.env.settings, _now(2026, 1, 16, 12))
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["fire_key"], "2026-01-16T07:30")
        self.assertEqual(len(self._tasks()), 1)

    def test_missing_assistant_marks_skipped(self):
        self.conn.execute(
            "INSERT INTO schedules(name, kind, freq, time_of_day,"
            " timezone, enabled, payload_json, created_at, updated_at)"
            " VALUES('x','create_task','daily','07:30','Europe/London',1,"
            " '{\"assistant_name\":\"ghost\",\"title\":\"t\",\"body\":\"b\"}'"
            ",?,?)", ("2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"))
        out = sch.tick(self.conn, self.env.settings, _now(2026, 1, 15, 12))
        names = {o["schedule"]: o for o in out}
        self.assertIn("x", names)
        self.assertIn("skipped", names["x"]["detail"])
        run = self.conn.execute(
            "SELECT status, task_id FROM schedule_runs sr JOIN schedules s"
            " ON s.id=sr.schedule_id WHERE s.name='x'").fetchone()
        self.assertEqual(run["status"], "skipped")
        self.assertIsNone(run["task_id"])

    def test_manual_fire(self):
        res = sch.fire_now(self.conn, "daily-email-summary")
        self.assertTrue(res["fired"])
        self.assertIsNotNone(res["task_id"])
        res2 = sch.fire_now(self.conn, "daily-email-summary")
        # manual fires always queue (owner-intent) but never collide
        self.assertIsNotNone(res2["task_id"])
        self.assertEqual(len(self._tasks()), 2)

    def test_add_schedule_validation(self):
        with self.assertRaises(ValueError):
            sch.add_schedule(self.conn, "bad", "fortnightly", "07:30",
                             "Europe/London",
                             {"assistant_name": "a", "title": "t",
                              "body": "b"})
        with self.assertRaises(ValueError):
            sch.add_schedule(self.conn, "bad2", "daily", "25:00",
                             "Europe/London",
                             {"assistant_name": "a", "title": "t",
                              "body": "b"})
        sid = sch.add_schedule(
            self.conn, "weekly-ok", "weekly", "09:00", "Europe/London",
            {"assistant_name": "email", "title": "t", "body": "b"},
            day_of_week=2)
        self.assertIsInstance(sid, int)


if __name__ == "__main__":
    unittest.main()
