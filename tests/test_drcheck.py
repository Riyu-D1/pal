"""final-4 R1: dr-check — good/stale/wrong-key backups, red-only DM."""

import os
import time
import unittest
from unittest import mock

from pa import backup as backupmod, drcheck, sysjobs
from tests.helpers import TmpEnv


def _env_dict(env, **kw):
    d = dict(env.env)
    d.update(kw)
    return d


def _make_backup(env, days_old=0, passphrase="pw-123"):
    path = os.path.join(env.settings.data_dir, "backups",
                        "pa-test.db.enc")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    backupmod.export_backup(env.db_path, path, passphrase)
    if days_old:
        t = time.time() - days_old * 86400
        os.utime(path, (t, t))
    return path


class TestDrCheck(unittest.TestCase):
    def test_good_backup_green(self):
        env = TmpEnv(PA_BACKUP_PASSPHRASE="pw-123",
                     DISCORD_BOT_TOKEN="t", DISCORD_OWNER_ID="1")
        env.conn().close()                    # migrate schema first
        _make_backup(env)
        e = _env_dict(env)
        out = drcheck.run(env.settings, env=e)
        rows = {r["check"]: r for r in out["rows"]}
        self.assertTrue(rows["backup age"]["ok"])
        self.assertTrue(rows["backup restores"]["ok"])
        self.assertIn("integrity ok", rows["backup restores"]["detail"])
        self.assertTrue(rows["schema version"]["ok"])
        self.assertTrue(rows["env names present"]["ok"])
        self.assertTrue(rows["disk free"]["ok"])
        self.assertIn("green", drcheck.table(out))

    def test_stale_backup_red(self):
        env = TmpEnv(PA_BACKUP_PASSPHRASE="pw-123",
                     DISCORD_BOT_TOKEN="t", DISCORD_OWNER_ID="1")
        _make_backup(env, days_old=4)
        out = drcheck.run(env.settings, env=_env_dict(env))
        rows = {r["check"]: r for r in out["rows"]}
        self.assertFalse(rows["backup age"]["ok"])
        self.assertFalse(out["ok"])
        self.assertIn("RED", drcheck.table(out))

    def test_wrong_key_red(self):
        env = TmpEnv(PA_BACKUP_PASSPHRASE="pw-123",
                     DISCORD_BOT_TOKEN="t", DISCORD_OWNER_ID="1")
        _make_backup(env, passphrase="real-key")
        e = _env_dict(env, PA_BACKUP_PASSPHRASE="wrong-key")
        out = drcheck.run(env.settings, env=e)
        rows = {r["check"]: r for r in out["rows"]}
        self.assertFalse(rows["backup restores"]["ok"])
        self.assertFalse(out["ok"])
        # the wrong passphrase never appears in output
        self.assertNotIn("wrong-key", drcheck.table(out))
        self.assertNotIn("real-key", drcheck.table(out))

    def test_no_backup_red(self):
        env = TmpEnv(PA_BACKUP_PASSPHRASE="pw-123")
        out = drcheck.run(env.settings, env=_env_dict(env))
        self.assertFalse(out["ok"])

    def test_truncated_backup_red(self):
        env = TmpEnv(PA_BACKUP_PASSPHRASE="pw-123",
                     DISCORD_BOT_TOKEN="t", DISCORD_OWNER_ID="1")
        path = _make_backup(env)
        with open(path, "r+b") as f:
            f.truncate(32)               # corrupt: too short to open
        out = drcheck.run(env.settings, env=_env_dict(env))
        rows = {r["check"]: r for r in out["rows"]}
        self.assertFalse(rows["backup restores"]["ok"])
        self.assertFalse(out["ok"])

    def test_verify_restored_missing_table_red(self):
        import sqlite3, tempfile
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "bare.db")
            c = sqlite3.connect(p)
            c.execute("CREATE TABLE schema_migrations("
                      "version INTEGER)")
            c.execute("INSERT INTO schema_migrations VALUES(99)")
            c.execute("CREATE TABLE schedules(id INTEGER)")
            c.commit(); c.close()
            ok, detail = drcheck._verify_restored(p)
        self.assertFalse(ok)
        self.assertIn("missing tables", detail)

    def test_verify_restored_low_schema_red(self):
        import sqlite3, tempfile
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "old.db")
            c = sqlite3.connect(p)
            c.execute("CREATE TABLE schema_migrations("
                      "version INTEGER)")
            c.execute("INSERT INTO schema_migrations VALUES(5)")
            for t in drcheck.PROBE_TABLES:
                c.execute(f"CREATE TABLE {t}(id INTEGER)")
            c.commit(); c.close()
            ok, detail = drcheck._verify_restored(p)
        self.assertFalse(ok)
        self.assertIn("below floor", detail)

    def test_offsite_unset_is_skip_not_fail(self):
        env = TmpEnv(PA_BACKUP_PASSPHRASE="pw-123",
                     DISCORD_BOT_TOKEN="t", DISCORD_OWNER_ID="1")
        _make_backup(env)
        out = drcheck.run(env.settings, env=_env_dict(env))
        rows = {r["check"]: r for r in out["rows"]}
        # skip row: present but excluded from the green/red overall
        self.assertIsNone(rows["offsite copy"]["ok"])
        reds = [r["check"] for r in out["rows"]
                if r["ok"] is False]
        self.assertNotIn("offsite copy", reds)
        self.assertIn("skip", drcheck.table(out))

    def test_offsite_last_ok_green(self):
        env = TmpEnv(PA_BACKUP_PASSPHRASE="pw-123",
                     DISCORD_BOT_TOKEN="t", DISCORD_OWNER_ID="1",
                     PA_BACKUP_OFFSITE="gdrive@pal")
        conn = env.conn()
        from pa import db as dbmod
        dbmod.set_setting(conn, "offsite_last",
            '{"ok": true, "via": "gdrive@pal", "at": "t"}')
        conn.close()
        _make_backup(env)
        out = drcheck.run(env.settings, env=_env_dict(env))
        rows = {r["check"]: r for r in out["rows"]}
        self.assertTrue(rows["offsite copy"]["ok"])

    def test_offsite_configured_no_copy_red(self):
        env = TmpEnv(PA_BACKUP_PASSPHRASE="pw-123",
                     DISCORD_BOT_TOKEN="t", DISCORD_OWNER_ID="1",
                     PA_BACKUP_OFFSITE="gdrive@pal")
        env.conn().close()               # migrate schema first
        out = drcheck.run(env.settings, env=_env_dict(env))
        rows = {r["check"]: r for r in out["rows"]}
        self.assertFalse(rows["offsite copy"]["ok"])
        self.assertFalse(out["ok"])

    def test_offsite_last_failed_red(self):
        env = TmpEnv(PA_BACKUP_PASSPHRASE="pw-123",
                     DISCORD_BOT_TOKEN="t", DISCORD_OWNER_ID="1",
                     PA_BACKUP_OFFSITE="gdrive@pal")
        conn = env.conn()
        from pa import db as dbmod
        dbmod.set_setting(conn, "offsite_last",
            '{"ok": false, "via": "gdrive@pal", "at": "t",'
            ' "detail": "quota"}')
        conn.close()
        _make_backup(env)
        out = drcheck.run(env.settings, env=_env_dict(env))
        rows = {r["check"]: r for r in out["rows"]}
        self.assertFalse(rows["offsite copy"]["ok"])

    def test_backupjob_persists_offsite_status(self):
        env = TmpEnv(PA_BACKUP_PASSPHRASE="pw-123",
                     DISCORD_BOT_TOKEN="t", DISCORD_OWNER_ID="1",
                     PA_BACKUP_OFFSITE="gdrive@pal")

        class _API:
            def post_message(self, ch, content, **kw):
                pass

        class B:
            def __init__(self, e):
                self._env, self.env, self.s = e, e.env, e.settings
                self.api = _API()

            def _db(self):
                return self._env.conn()

            def _owner_dm_id(self):
                return "DM"

        from unittest.mock import patch
        from pa import backupjob
        with patch("pa.offsite.copy",
                   return_value={"ok": True, "via": "gdrive@pal",
                                 "detail": "fid1"}):
            backupjob.run(B(env), {})
        conn = env.conn()
        val = conn.execute(
            "SELECT value FROM pa_settings WHERE key='offsite_last'"
        ).fetchone()["value"]
        conn.close()
        import json
        self.assertTrue(json.loads(val)["ok"])

    def test_env_names_reported_never_values(self):
        env = TmpEnv(PA_BACKUP_PASSPHRASE="pw-123",
                     DISCORD_BOT_TOKEN="secret-token-xyz",
                     DISCORD_OWNER_ID="1")
        _make_backup(env)
        e = _env_dict(env, DISCORD_BOT_TOKEN="")
        out = drcheck.run(env.settings, env=e)
        text = drcheck.table(out)
        self.assertIn("DISCORD_BOT_TOKEN", text)
        self.assertNotIn("secret-token-xyz", text)

    def test_seeded_disabled_and_red_only_dm(self):
        env = TmpEnv()
        conn = env.conn()
        self.assertTrue(sysjobs.is_system("dr-check"))
        self.assertIsNotNone(sysjobs.handler_for("dr-check"))
        row = conn.execute(
            "SELECT enabled, freq FROM schedules WHERE name='dr-check'"
        ).fetchone()
        conn.close()
        self.assertEqual(row["enabled"], 0)
        self.assertEqual(row["freq"], "weekly")

    def test_job_dms_only_on_red(self):
        env = TmpEnv(PA_BACKUP_PASSPHRASE="pw-123",
                     DISCORD_BOT_TOKEN="t", DISCORD_OWNER_ID="1")

        class _API:
            def __init__(self):
                self.posts = []

            def post_message(self, ch, content, **kw):
                self.posts.append(content)

        class B:
            def __init__(self, e):
                self._env, self.env, self.s = e, e.env, e.settings
                self.api = _API()

            def _db(self):
                return self._env.conn()

            def _owner_dm_id(self):
                return "DM"

        b = B(env)
        with mock.patch.object(drcheck, "run",
                               return_value={"ok": True, "rows": []}):
            out = drcheck.job(b, {})
        self.assertEqual(out, "dr-check green")
        self.assertEqual(b.api.posts, [])
        with mock.patch.object(
                drcheck, "run",
                return_value={"ok": False,
                              "rows": [{"check": "x", "ok": False,
                                        "detail": "bad"}]}):
            out = drcheck.job(b, {})
        self.assertIn("red", out)
        self.assertEqual(len(b.api.posts), 1)


if __name__ == "__main__":
    unittest.main()
