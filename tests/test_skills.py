"""skills library — vault-backed when configured, SQLite
otherwise; slugged names can never escape Skills/."""

import os
import unittest

from pa import chattools, db as dbmod, skills
from tests.helpers import TmpEnv


class Base(unittest.TestCase):
    def setUp(self):
        self.env = TmpEnv()
        self.aid = self.env.add_assistant(name="pal", tools=[])
        conn = self.env.conn()
        self.assistant = conn.execute(
            "SELECT * FROM assistants WHERE id=?", (self.aid,)
        ).fetchone()
        self.ctx = chattools.ChatToolContext(
            conn, self.env.settings, self.assistant, "c",
            env=self.env.env)


class TestSlug(Base):
    def test_names_cannot_escape(self):
        for raw in ("../../etc/passwd", "..\\secrets", "x/./y",
                    "My Skill!!"):
            s = skills.slug(raw)
            self.assertNotIn("/", s)
            self.assertNotIn(".", s)
            self.assertTrue(s)

    def test_sqlite_round_trip(self):
        skills.save_skill(self.ctx.conn, self.env.settings,
                          "make-pancakes", "Sunday pancakes",
                          "1. flour\n2. eggs")
        got = skills.load_skill(self.ctx.conn, self.env.settings,
                                "Make Pancakes")
        self.assertEqual(got["source"], "sqlite")
        self.assertIn("flour", got["body"])
        listed = skills.list_skills(self.ctx.conn, self.env.settings)
        self.assertEqual(listed[0]["name"], "make-pancakes")
        self.assertEqual(listed[0]["description"], "Sunday pancakes")

    def test_load_missing(self):
        out = skills.load_skill(self.ctx.conn, self.env.settings, "nope")
        self.assertEqual(out["error"], "not_found")


class TestVault(unittest.TestCase):
    def setUp(self):
        self.env = TmpEnv(VAULT_PATH="")   # set per-test below

    def _vault_env(self):
        import tempfile
        d = tempfile.mkdtemp()
        self.env.env["VAULT_PATH"] = d
        from pa.config import load_settings
        self.env.settings = load_settings(self.env.env)
        return d

    def test_save_writes_vault_note(self):
        d = self._vault_env()
        conn = self.env.conn()
        out = skills.save_skill(conn, self.env.settings,
                                "tea-brew", "how I like tea",
                                "steep 4 minutes")
        self.assertEqual(out["path"], "Skills/tea-brew.md")
        with open(os.path.join(d, "Skills", "tea-brew.md")) as f:
            text = f.read()
        self.assertIn("description: how I like tea", text)
        self.assertIn("steep 4 minutes", text)
        got = skills.load_skill(conn, self.env.settings, "tea-brew")
        self.assertEqual(got["source"], "vault")
        self.assertIn("steep 4 minutes", got["body"])
        conn.close()

    def test_vault_skill_wins_over_sqlite(self):
        self._vault_env()
        conn = self.env.conn()
        skills.save_skill(conn, self.env.settings, "dup", "vault", "vb")
        conn.execute(
            "INSERT INTO skills(name, description, body, updated_at)"
            " VALUES('dup','sqlite','sb','x')")
        got = skills.load_skill(conn, self.env.settings, "dup")
        self.assertEqual(got["source"], "vault")
        conn.close()


class TestTools(Base):
    def test_tool_round_trip_and_prompt_block(self):
        self.ctx.request_approval = lambda n, a, i: "approved"
        out = chattools.run(self.ctx, "skill_save",
                            {"name": "coffee", "description": "brew",
                             "body": "pour over"})
        self.assertTrue(out["saved"])
        # gated → approval row existed
        self.assertTrue(self.ctx.conn.execute(
            "SELECT 1 FROM approvals WHERE kind='chat_action'")
            .fetchone())
        out = chattools.run(self.ctx, "skill_load", {"name": "coffee"})
        self.assertIn("OWNER SKILL coffee", out["body"])
        self.assertIn("pour over", out["body"])
        out = chattools.run(self.ctx, "skill_list", {})
        self.assertEqual(out["skills"][0]["name"], "coffee")
        block = skills.prompt_block(self.ctx.conn, self.env.settings)
        self.assertIn("coffee", block)


class TestBackupBeforeMigrate(unittest.TestCase):
    def test_backup_written_for_existing_db(self):
        env = TmpEnv()
        conn = env.conn()
        dbmod._backup_before_migrate(conn, 10)
        bdir = os.path.join(os.path.dirname(
            os.path.abspath(env.settings.db_path)), "backups")
        files = [f for f in os.listdir(bdir)
                 if f.startswith("pre-migrate-v10-")]
        self.assertEqual(len(files), 1)
        conn.close()


if __name__ == "__main__":
    unittest.main()
