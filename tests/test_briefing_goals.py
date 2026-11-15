"""final-2 G5: the Goals section of the morning briefing — gated by
PA_GOALS + PA_BRIEFING_GOALS, read-only."""

import unittest

from pa import briefing, goals, projects
from tests.helpers import TmpEnv


class _Bot:
    def __init__(self, env):
        self.s = env.settings
        self.env = env.env
        self._voice = None


class TestGoalsSection(unittest.TestCase):
    def test_flag_off_empty(self):
        env = TmpEnv(PA_GOALS="true")   # PA_BRIEFING_GOALS unset
        conn = env.conn()
        try:
            goals.add_goal(conn, "job hunt")
            self.assertEqual(briefing._goals(_Bot(env), conn), [])
        finally:
            conn.close()

    def test_section_lines(self):
        env = TmpEnv(PA_GOALS="true", PA_BRIEFING_GOALS="true",
                     PA_PROJECTS="true")
        conn = env.conn()
        try:
            gid = goals.add_goal(conn, "stay on top of recruiter mail")
            goals.update_goal(conn, gid, next_action="draft reply",
                              progress="2 recruiter mails")
            pid = projects.start(conn, gid, "apply", ["x"])
            # block it so the blocker line shows
            conn.execute(
                "UPDATE projects SET state='blocked', blockers='need "
                "the right role' WHERE id=?", (pid,))
            lines = briefing._goals(_Bot(env), conn)
            self.assertEqual(len(lines), 1)
            self.assertIn("recruiter mail", lines[0])
            self.assertIn("draft reply", lines[0])
            self.assertIn("blocked", lines[0])
        finally:
            conn.close()

    def test_compose_includes_section(self):
        env = TmpEnv(PA_GOALS="true", PA_BRIEFING_GOALS="true")
        conn = env.conn()
        try:
            bot = _Bot(env)
            bot.db_path = env.settings.db_path
            parts = {"events": [], "emails": [], "due": [],
                     "weather": "", "goals": ["• g1 — next: do x"],
                     "date": "2026-03-04"}
            text, _sp = briefing.compose(bot, conn, parts)
            self.assertIn("**Goals:**", text)
            self.assertIn("do x", text)
            parts["goals"] = []
            text2, _ = briefing.compose(bot, conn, parts)
            self.assertNotIn("**Goals:**", text2)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
