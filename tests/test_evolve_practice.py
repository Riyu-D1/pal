"""final-4 E4: self-practice curriculum — candidates, budget stop,
no real connectors."""

import unittest
from unittest import mock

from pa.evolve import archive, incidents, practice
from tests.helpers import TmpEnv


class _Bot:
    def __init__(self, env):
        self._env, self.s, self.env = env, env.settings, env.env

    def _db(self):
        return self._env.conn()


def _env(**kw):
    base = dict(PA_EVOLVE_ARCHIVE="true", PA_EVOLVE_PRACTICE="true")
    base.update(kw)
    return TmpEnv(**base)


class TestPractice(unittest.TestCase):
    def test_off_by_default_quiet(self):
        env = TmpEnv()
        self.assertEqual(practice.run(_Bot(env), {}), "practice off")

    def test_needs_archive_on(self):
        env = TmpEnv(PA_EVOLVE_PRACTICE="true")
        out = practice.run(_Bot(env), {})
        self.assertIn("PA_EVOLVE_ARCHIVE", out)

    def test_approved_area_makes_candidates(self):
        env = _env()
        conn = env.conn()
        practice.add_area(conn, "weather")
        incidents.record(conn, "tool", "tool_error",
                         summary="weather lookup timed out")
        out = practice.run(_Bot(env), {})
        rows = archive.variants(conn, "skill", "practice-weather")
        conn.close()
        self.assertIn("candidate", out)
        self.assertTrue(rows)
        self.assertEqual(rows[0]["status"], "candidate")
        self.assertEqual(rows[0]["created_by"], "practice")

    def test_budget_stop(self):
        env = _env()
        conn = env.conn()
        practice.add_area(conn, "weather")
        incidents.record(conn, "tool", "tool_error",
                         summary="weather failed")
        with mock.patch("pa.budget.would_exceed",
                        return_value=True):
            out = practice.run(_Bot(env), {})
        conn.close()
        self.assertIn("budget", out)

    def test_sandbox_uses_fake_connectors(self):
        env = _env()
        called = {}

        def fake_run(fx, s):
            called["hit"] = True
            return {"ok": True, "id": "practice"}

        conn = env.conn()
        practice.add_area(conn, "notes")
        incidents.record(conn, "tool", "tool_error",
                         summary="notes failed")
        with mock.patch("pa.evolve.eval.run_fixture", fake_run):
            out = practice.run(_Bot(env), {})
        conn.close()
        self.assertTrue(called.get("hit"))
        self.assertIn("candidate", out)

    def test_cmd_add_and_list(self):
        env = _env()
        conn = env.conn()
        out = practice.cmd(conn, env.settings, ["add", "weather"])
        self.assertIn("approved", out)
        row = conn.execute(
            "SELECT enabled FROM schedules WHERE name='evolve-practice'"
        ).fetchone()
        out = practice.cmd(conn, env.settings, [])
        conn.close()
        self.assertIsNotNone(row)
        self.assertEqual(row["enabled"], 0)
        self.assertIn("weather", out)


class TestSeededSchedule(unittest.TestCase):
    """final-5 F1: migration 43 seeds the disabled evolve-practice
    schedule so `/jobs evolve-practice on` works on a fresh DB."""

    def _rows(self, conn):
        return conn.execute(
            "SELECT * FROM schedules WHERE name='evolve-practice'"
        ).fetchall()

    def test_fresh_db_has_one_disabled_row(self):
        env = TmpEnv()
        conn = env.conn()
        rows = self._rows(conn)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["enabled"], 0)
        self.assertEqual(rows[0]["kind"], "agent_job")
        self.assertEqual(rows[0]["freq"], "daily")
        self.assertEqual(rows[0]["time_of_day"], "23:45")
        conn.close()

    def test_migrate_and_ensure_do_not_duplicate(self):
        from pa import db as dbmod
        env = TmpEnv()
        conn = env.conn()
        dbmod.migrate(conn)                 # second migrate: no-op
        practice._ensure_schedule(conn)     # first-use path: ignored
        self.assertEqual(len(self._rows(conn)), 1)
        conn.close()

    def test_enable_dispatches_practice_run(self):
        from pa import scheduler as sch, sysjobs
        env = TmpEnv()
        conn = env.conn()
        self.assertTrue(sch.set_enabled(conn, "evolve-practice", True))
        self.assertIs(sysjobs.handler_for("evolve-practice"),
                      practice.run)
        # firing enqueues a job:* inbox row the sysjob drain picks up
        out = sch.fire_now(conn, "evolve-practice")
        self.assertTrue(out["fired"])
        row = conn.execute(
            "SELECT source FROM chat_inbox ORDER BY id DESC LIMIT 1"
        ).fetchone()
        conn.close()
        self.assertEqual(row["source"], "job:evolve-practice")


if __name__ == "__main__":
    unittest.main()
