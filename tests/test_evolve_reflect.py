"""Evolve item 7: nightly reflection -> pending lesson/skill items."""

import unittest

from pa import scheduler as sch, sysjobs
from pa.evolve import incidents, items, reflect
from tests.helpers import TmpEnv


class _Brain:
    def __init__(self, reply):
        self.reply = reply

    def complete(self, messages, budget, tier):
        self._prompt = messages[0]["content"]
        return self.reply


class _Eng:
    def __init__(self, brain):
        self._brain = brain

    def _resolve_brains(self, conn):
        pass


class _Bot:
    def __init__(self, env, brain):
        self.s, self.env = env.settings, env.env
        self._env, self._eng = env, _Eng(brain)

    def _db(self):
        return self._env.conn()

    def _engine(self):
        return self._eng


class TestReflect(unittest.TestCase):
    def test_seeded_disabled_and_system(self):
        env = TmpEnv()
        conn = env.conn()
        row = sch.get_schedule(conn, "evolve-reflect")
        conn.close()
        self.assertTrue(sysjobs.is_system("evolve-reflect"))
        self.assertEqual(row["enabled"], 0)
        self.assertEqual(row["time_of_day"], "23:30")

    def test_failure_pattern_makes_pending_lesson(self):
        env = TmpEnv(PA_EVOLVE="true")
        conn = env.conn()
        for _ in range(3):
            incidents.record(conn, "chat", "tool_error",
                             "weather tool timed out")
        brain = _Brain(
            "LESSON: say when weather data is stale\n"
            "WHY: weather timed out 3x today\n")
        conn.close()
        out = reflect.run(_Bot(env, brain), {})
        self.assertEqual(out, "1 lesson item(s)")
        conn = env.conn()
        open_items = items.list_items(conn)
        facts = conn.execute("SELECT COUNT(*) c FROM owner_facts"
                             ).fetchone()["c"]
        conn.close()
        self.assertEqual(len(open_items), 1)
        self.assertEqual(open_items[0]["shape"], "lesson")
        self.assertIn(open_items[0]["status"],
                      ("open", "needs_plan", "awaiting_approval"))
        self.assertEqual(facts, 0)            # never owner_facts

    def test_empty_day_is_quiet(self):
        env = TmpEnv(PA_EVOLVE="true")
        out = reflect.run(_Bot(env, _Brain("NONE")), {})
        self.assertEqual(out, "")
        conn = env.conn()
        self.assertEqual(items.list_items(conn), [])
        conn.close()

    def test_flag_off_noop(self):
        env = TmpEnv(PA_EVOLVE="false")
        out = reflect.run(_Bot(env, _Brain("LESSON: x\nWHY: y\n")), {})
        self.assertEqual(out, "")
        conn = env.conn()
        self.assertEqual(items.list_items(conn), [])
        conn.close()

    def test_cap_respected(self):
        env = TmpEnv(PA_EVOLVE="true", PA_EVOLVE_REFLECT_MAX="1")
        brain = _Brain("LESSON: a\nWHY: x\nLESSON: b\nWHY: y\n"
                       "SKILL: c: d\nWHY: z\n")
        reflect.run(_Bot(env, brain), {})
        conn = env.conn()
        self.assertEqual(len(items.list_items(conn)), 1)
        conn.close()


if __name__ == "__main__":
    unittest.main()
