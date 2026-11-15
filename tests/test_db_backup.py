"""Acceptance #14: migrations + backup/restore round-trips."""

import os
import unittest

from pa import backup as bk, db as dbmod, memory as memmod
from tests.helpers import TmpEnv


class TestMigrations(unittest.TestCase):
    def test_fresh_to_latest(self):
        env = TmpEnv()
        conn = env.conn()
        self.assertEqual(dbmod.current_version(conn), dbmod.SCHEMA_VERSION)

    def test_previous_version_to_latest(self):
        env = TmpEnv()
        c = dbmod.connect(env.db_path)
        # apply only migration 1, mark v1, then run migrate()
        dbmod.MIGRATIONS[1](c)
        c.execute("INSERT INTO schema_migrations(version, applied_at)"
                  " VALUES(1,'x')")
        self.assertEqual(dbmod.current_version(c), 1)
        dbmod.migrate(c)
        self.assertEqual(dbmod.current_version(c), dbmod.SCHEMA_VERSION)
        # sanity: later tables exist
        c.execute("SELECT COUNT(*) FROM audit_log")
        c.execute("SELECT COUNT(*) FROM task_messages")


class TestBackupRestore(unittest.TestCase):
    def test_backup_restore_roundtrip(self):
        env = TmpEnv()
        conn = env.conn()
        aid = env.add_assistant()
        memmod.insert_memory(conn, aid, "important note")
        dest = os.path.join(env.dir, "backups", "b.db")
        bk.backup(env.db_path, dest)
        # wipe-ish: new empty db
        env2 = TmpEnv()
        bk.restore(env2.db_path, dest)
        c2 = env2.conn()
        mems = memmod.list_memories(c2)
        self.assertEqual(len(mems), 1)
        self.assertEqual(mems[0]["content"], "important note")

    def test_encrypted_export_import(self):
        env = TmpEnv()
        conn = env.conn()
        aid = env.add_assistant()
        memmod.upsert_owner_fact(conn, "k", "v-value")
        dest = os.path.join(env.dir, "export.bin")
        bk.export_backup(env.db_path, dest, "pw-12345")
        with open(dest, "rb") as f:
            raw = f.read()
        self.assertTrue(raw.startswith(b"PABK1\n"))
        self.assertNotIn(b"v-value", raw)  # ciphertext only
        env2 = TmpEnv()
        bk.import_backup(env2.db_path, dest, "pw-12345")
        c2 = env2.conn()
        self.assertEqual(memmod.list_owner_facts(c2)[0]["value"], "v-value")

    def test_import_wrong_passphrase_fails(self):
        env = TmpEnv()
        env.conn()
        dest = os.path.join(env.dir, "e.bin")
        bk.export_backup(env.db_path, dest, "right")
        with self.assertRaises(bk.BackupError):
            bk.import_backup(env.db_path, dest, "wrong")


if __name__ == "__main__":
    unittest.main()
