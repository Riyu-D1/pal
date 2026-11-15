"""approval-gated /update + self-update --dry-run."""

import unittest
from unittest import mock

from pa import selfupdate
from tests.helpers import TmpEnv


def _runner_script(replies):
    """argv-list -> canned (rc, out, err) per command, in order."""
    calls = []

    def runner(argv, **kw):
        calls.append(argv)
        rc, out, err = replies.pop(0)
        return type("P", (), {"returncode": rc,
                              "stdout": out, "stderr": err})()
    runner.calls = calls
    return runner


class TestPlan(unittest.TestCase):
    def test_update_available(self):
        r = _runner_script([
            (0, "aaaabbbbccccdddd\n", ""),          # rev-parse HEAD
            (0, "origin/feature/parity\n", ""),     # upstream
            (0, "eeeeffff00001111\trefs/heads/parity\n", ""),
            (0, "4\n", ""),                          # rev-list count
        ])
        out = selfupdate.plan(root="/x", runner=r)
        self.assertTrue(out["update_available"])
        self.assertEqual(out["remote_sha"], "eeeeffff0000")
        self.assertEqual(out["behind"], 4)
        self.assertIn("update available", selfupdate.describe(out))

    def test_up_to_date(self):
        r = _runner_script([
            (0, "aaaabbbbccccdddd\n", ""),
            (0, "origin/main\n", ""),
            (0, "aaaabbbbccccdddd\trefs/heads/main\n", ""),
            (0, "0\n", ""),
        ])
        out = selfupdate.plan(root="/x", runner=r)
        self.assertFalse(out["update_available"])

    def test_not_a_repo(self):
        r = _runner_script([(128, "", "fatal: not a git repo")])
        self.assertEqual(
            selfupdate.plan(root="/x", runner=r)["error"], "no_repo")

    def test_plan_never_fetches(self):
        r = _runner_script([
            (0, "aaaa\n", ""), (0, "origin/main\n", ""),
            (0, "aaaa\trefs/heads/main\n", ""), (0, "0\n", "")])
        selfupdate.plan(root="/x", runner=r)
        self.assertFalse(any("fetch" in c or "pull" in c
                             for c in r.calls))


class TestApply(unittest.TestCase):
    def test_fetch_then_ff_merge(self):
        r = _runner_script([
            (0, "aaaa\n", ""),                      # rev-parse prev
            (0, "", ""),                            # fetch
            (0, "Updating aaaa..bbbb\n", ""),       # merge
            (0, "bbbb\n", ""),                      # rev-parse head
        ])
        restarts = []
        out = selfupdate.apply(
            root="/x", runner=r,
            restart=lambda: restarts.append(1) or True,
            health_probe=lambda: True)
        self.assertTrue(out["ok"])
        self.assertEqual(len(restarts), 1)          # canary ran
        self.assertNotIn("needs_restart", out)
        self.assertIn("merge", r.calls[2])
        self.assertIn("--ff-only", r.calls[2])
        self.assertFalse(any("reset" in c for c in r.calls))

    def test_unhealthy_canary_rolls_back(self):
        r = _runner_script([
            (0, "aaaa\n", ""),                      # rev-parse prev
            (0, "", ""),                            # fetch
            (0, "Updating aaaa..bbbb\n", ""),       # merge
            (0, "bbbb\n", ""),                      # rev-parse head
            (0, "", ""),                            # reset --hard aaaa
        ])
        restarts = []
        out = selfupdate.apply(
            root="/x", runner=r,
            restart=lambda: restarts.append(1) or True,
            health_probe=lambda: False,
            health_seconds=0)
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "health_failed")
        self.assertEqual(len(restarts), 2)          # deploy + rollback
        resets = [c for c in r.calls if "reset" in c]
        self.assertEqual(resets,
                         [["git", "-C", "/x", "reset", "--hard",
                           "aaaa"]])

    def test_merge_failure(self):
        r = _runner_script([(0, "aaaa\n", ""), (0, "", ""),
                            (1, "", "not possible to fast-forward")])
        out = selfupdate.apply(root="/x", runner=r)
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "merge_failed")


class TestWiring(unittest.TestCase):
    def test_tool_gated_and_command(self):
        from pa import chattools, discordbot
        self.assertTrue(chattools.CHAT_TOOLS["self_update"]["approval"])
        from pa import allowrules
        self.assertNotIn("self_update", allowrules.ALWAYS_CAPABLE)
        self.assertIn("update", discordbot._COMMAND_NAMES)


class TestSystemdRestart(unittest.TestCase):
    """PA_SYSTEMD_SCOPE/UNIT steer the restart command."""

    def _argv(self, scope="user", unit="pa"):
        calls = []

        def runner(argv, **kw):
            calls.append(argv)
            return type("P", (), {"returncode": 0, "stdout": "",
                                  "stderr": ""})()
        with mock.patch.object(selfupdate.shutil, "which",
                               return_value="/bin/systemctl"):
            selfupdate._default_restart(runner, scope, unit)
        return calls[0]

    def test_default_is_user_pa(self):
        self.assertEqual(
            self._argv(), ["systemctl", "--user", "restart", "pa"])

    def test_system_scope_drops_user_flag(self):
        self.assertEqual(self._argv("system"),
                         ["systemctl", "restart", "pa"])

    def test_unit_rename(self):
        self.assertEqual(self._argv("system", "pal-svc"),
                         ["systemctl", "restart", "pal-svc"])

    def test_settings_plumbed_into_apply(self):
        env = TmpEnv(PA_SYSTEMD_SCOPE="system",
                     PA_SYSTEMD_UNIT="pal-svc")
        calls = []

        shas = iter(["aaaa\n", "bbbb\n"])

        def runner(argv, **kw):
            calls.append(argv)
            return type("P", (), {
                "returncode": 0,
                "stdout": next(shas) if "rev-parse" in argv else "",
                "stderr": ""})()
        with mock.patch.object(selfupdate.shutil, "which",
                               return_value="/bin/systemctl"):
            selfupdate.apply(
                root="/x", runner=runner,
                health_probe=lambda: True, settings=env.settings)
        self.assertIn(["systemctl", "restart", "pal-svc"], calls)

    def test_config_validation(self):
        from pa.config import ConfigError, load_settings
        env = TmpEnv()
        base = dict(env.env)
        base["PA_SYSTEMD_SCOPE"] = "bogus"
        with self.assertRaises(ConfigError):
            load_settings(base)
        base["PA_SYSTEMD_SCOPE"] = "system"
        base["PA_SYSTEMD_UNIT"] = "pa.service;rm -rf /"
        with self.assertRaises(ConfigError):
            load_settings(base)
        base["PA_SYSTEMD_UNIT"] = "pa-web.service"
        self.assertEqual(load_settings(base).systemd_unit,
                         "pa-web.service")


if __name__ == "__main__":
    unittest.main()
