"""scheduled encrypted backups."""

import os
import unittest

from pa import backupjob, sysjobs
from tests.helpers import TmpEnv


class _API:
    def post_message(self, *a, **kw):
        pass


class _FakeBot:
    def __init__(self, env):
        self.env, self.s = env.env, env.settings
        self.api = _API()

    def _db(self):
        return env.conn()

    def _owner_dm_id(self):
        return ""


class TestBackupJob(unittest.TestCase):
    def setUp(self):
        self.env = TmpEnv()
        self.env.conn()             # force schema

    def test_skips_without_passphrase(self):
        bot = _FakeBot(self.env)
        self.assertIn("skipped", backupjob.run(bot, {}))

    def test_exports_and_prunes(self):
        self.env.env["PA_BACKUP_PASSPHRASE"] = "pw"
        self.env.env["PA_BACKUP_KEEP"] = "2"
        bot = _FakeBot(self.env)
        status = backupjob.run(bot, {})
        self.assertIn("exported", status)
        d = os.path.join(self.env.settings.data_dir, "backups")
        files = [f for f in os.listdir(d) if f.endswith(".enc")]
        self.assertTrue(files)
        # keep=2: run twice more, expect pruning to hold at 2
        backupjob.run(bot, {})
        backupjob.run(bot, {})
        files = [f for f in os.listdir(d) if f.endswith(".enc")]
        self.assertLessEqual(len(files), 2)
        self.assertTrue(sysjobs.is_system("scheduled-backup"))
        self.assertIs(sysjobs.handler_for("scheduled-backup"),
                      backupjob.run)


if __name__ == "__main__":
    unittest.main()
