"""evolve_items + the evolve-detect gap detector."""

import unittest

from pa import scheduler as sch, sysjobs
from pa.evolve import detect, incidents, items
from tests.helpers import TmpEnv


class _FakeBot:
    def __init__(self, env):
        self.env, self.s = env.env, env.settings
        self._env = env

    def _db(self):
        return self._env.conn()


def _items(conn):
    return conn.execute("SELECT * FROM evolve_items ORDER BY id") \
        .fetchall()


class TestTableAndSeed(unittest.TestCase):
    def setUp(self):
        self.env = TmpEnv()

    def test_job_is_system_and_seeded_disabled(self):
        conn = self.env.conn()
        self.assertTrue(sysjobs.is_system("evolve-detect"))
        self.assertIsNotNone(sysjobs.handler_for("evolve-detect"))
        row = sch.get_schedule(conn, "evolve-detect")
        conn.close()
        self.assertIsNotNone(row)
        self.assertEqual(row["enabled"], 0)
        self.assertEqual(row["freq"], "interval")
        self.assertEqual(row["day_of_month"], 30)

    def test_flag_off_does_nothing_even_when_forced(self):
        env = TmpEnv()                      # PA_EVOLVE unset -> false
        conn = env.conn()
        for _ in range(5):
            incidents.record(conn, "chat", "tool_error",
                             summary="weather: request 1 failed")
        out = detect.run(_FakeBot(env), {})
        conn.close()
        self.assertEqual(out, "evolve off")
        self.assertEqual(_items(env.conn()), [])


class TestDetection(unittest.TestCase):
    def setUp(self):
        self.env = TmpEnv(PA_EVOLVE="true")
        self.bot = _FakeBot(self.env)
        self.conn = self.env.conn()

    def tearDown(self):
        self.conn.close()

    def _record_n(self, n, kind="tool_error", tool="weather"):
        for i in range(n):
            incidents.record(self.conn, "chat", kind,
                             summary=f"{tool}: request {i} failed",
                             tool=tool)

    def test_threshold_opens_one_item_not_three(self):
        self._record_n(4)
        out = detect.run(self.bot, {})
        self.assertEqual(out, "opened 1 item(s)")
        rows = _items(self.conn)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["shape"], "fix")
        self.assertEqual(rows[0]["risk"], "high")
        self.assertEqual(rows[0]["status"], "open")
        self.assertTrue(rows[0]["incident_fingerprint"])

    def test_baseline_and_no_new_incidents_are_quiet(self):
        self.assertEqual(detect.run(self.bot, {}), "quiet")
        self._record_n(2)           # under threshold
        self.assertEqual(detect.run(self.bot, {}), "quiet")
        self.assertEqual(_items(self.conn), [])
        # seen marker advanced -> second run over same rows is quiet
        self.assertEqual(detect.run(self.bot, {}), "quiet")

    def test_health_fail_opens_on_first_hit(self):
        incidents.record(self.conn, "health", "health_fail",
                         summary="worker stale")
        self.assertEqual(detect.run(self.bot, {}),
                         "opened 1 item(s)")
        rows = _items(self.conn)
        self.assertEqual(rows[0]["shape"], "fix")

    def test_repeated_not_configured_same_tool_opens_connector(self):
        self._record_n(2, kind="not_configured", tool="browse_goto")
        self.assertEqual(detect.run(self.bot, {}),
                         "opened 1 item(s)")
        rows = _items(self.conn)
        self.assertEqual(rows[0]["shape"], "connector")
        self.assertEqual(rows[0]["risk"], "low")

    def test_same_fingerprint_updates_not_duplicates(self):
        self._record_n(4)
        detect.run(self.bot, {})
        self._record_n(1)           # same fingerprint again
        detect.run(self.bot, {})
        rows = _items(self.conn)
        self.assertEqual(len(rows), 1)
        self.assertGreaterEqual(rows[0]["hits"], 2)

    def test_open_cap_per_run(self):
        env = TmpEnv(PA_EVOLVE="true",
                     PA_EVOLVE_DETECT_MAX_ITEMS="2")
        conn = env.conn()
        for t in ("weather", "email", "vault"):
            for i in range(4):
                incidents.record(conn, "chat", "tool_error",
                                 summary=f"{t}: request {i} failed",
                                 tool=t)
        out = detect.run(_FakeBot(env), {})
        self.assertEqual(out, "opened 2 item(s)")
        self.assertEqual(len(_items(conn)), 2)
        conn.close()


if __name__ == "__main__":
    unittest.main()
