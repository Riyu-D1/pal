"""Profile.md sync + the daily-journal system job."""

import os
import tempfile
import unittest

from pa import approvals, chattools, db as dbmod, journal
from pa import memory as memmod
from pa import sysjobs
from tests.helpers import TmpEnv


def _vault_settings(env):
    d = tempfile.mkdtemp()
    env.env["VAULT_PATH"] = d
    from pa.config import load_settings
    env.settings = load_settings(env.env)
    return d


class TestProfile(unittest.TestCase):
    def test_sync_writes_profile_md(self):
        env = TmpEnv()
        d = _vault_settings(env)
        conn = env.conn()
        memmod.upsert_owner_fact(conn, "tea", "green")
        path = journal.sync_profile(conn, env.settings)
        self.assertEqual(path, "Profile.md")
        with open(os.path.join(d, "Profile.md")) as f:
            text = f.read()
        self.assertIn("**tea**: green", text)
        conn.close()

    def test_approved_owner_fact_syncs_profile(self):
        env = TmpEnv()
        d = _vault_settings(env)
        aid = env.add_assistant(name="pal", tools=[])
        conn = env.conn()
        ast = conn.execute(
            "SELECT * FROM assistants WHERE id=?", (aid,)).fetchone()
        ctx = chattools.ChatToolContext(
            conn, env.settings, ast, "c", env=env.env)
        ctx.request_approval = lambda n, a, x: (
            approvals.decide(conn, x, True), "approved")[1]
        chattools.run(ctx, "owner_fact",
                      {"key": "hometown", "value": "bristol"})
        with open(os.path.join(d, "Profile.md")) as f:
            self.assertIn("bristol", f.read())
        conn.close()

    def test_no_vault_is_a_noop(self):
        env = TmpEnv()
        conn = env.conn()
        self.assertEqual(journal.sync_profile(conn, env.settings), "")
        conn.close()


class _FakeBot:
    def __init__(self, env):
        self.env, self.s = env.env, env.settings
        self._conn = env.conn()

    def _db(self):
        return self._conn

    def _engine(self):
        raise RuntimeError("no brain in tests")


class TestJournalJob(unittest.TestCase):
    def setUp(self):
        self.env = TmpEnv()
        self.env.add_assistant(name="pal", tools=[])

    def test_job_seeded_disabled_and_system(self):
        conn = self.env.conn()
        row = conn.execute(
            "SELECT enabled FROM schedules WHERE name='daily-journal'"
        ).fetchone()
        self.assertEqual(row["enabled"], 0)
        self.assertTrue(sysjobs.is_system("daily-journal"))
        self.assertIs(sysjobs.handler_for("daily-journal"),
                      journal.run)
        conn.close()

    def test_journal_written_to_vault(self):
        d = _vault_settings(self.env)
        bot = _FakeBot(self.env)
        status = journal.run(bot, {})
        self.assertIn("journal written", status)
        day_files = os.listdir(os.path.join(d, "Journal"))
        self.assertEqual(len(day_files), 1)
        with open(os.path.join(d, "Journal", day_files[0])) as f:
            self.assertIn("# Journal", f.read())

    def test_journal_fallback_without_vault(self):
        bot = _FakeBot(self.env)
        status = journal.run(bot, {})
        self.assertIn("journal written", status)
        jdir = os.path.join(os.path.dirname(
            os.path.abspath(self.env.settings.db_path)), "journal")
        self.assertEqual(len(os.listdir(jdir)), 1)

    def test_journal_counts_today(self):
        conn = self.env.conn()
        memmod.insert_memory(conn, 1, "x")
        text = journal.journal_text(conn, "2100-01-01")
        self.assertIn("quiet day", text)   # future day has nothing
        conn.close()


if __name__ == "__main__":
    unittest.main()
