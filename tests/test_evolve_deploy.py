"""canary deploy — apply_sha + rollback."""

import unittest

from pa import selfupdate


class P:
    def __init__(self, rc=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = rc, stdout, stderr


def runner_factory(heads, diffs=None, merge_rc=0, reset_rc=0):
    """heads: queue of rev-parse answers; diffs: {spec: [paths]}."""
    calls, state = [], {"head_i": 0}

    def run(argv, capture_output=True, text=True, timeout=20):
        calls.append(list(argv)[3:])        # strip git -C root
        if "rev-parse" in argv:
            i = min(state["head_i"], len(heads) - 1)
            state["head_i"] += 1
            return P(0, heads[i])
        if "diff" in argv:
            spec = argv[-1]
            return P(0, "\n".join((diffs or {}).get(spec, [])))
        if "merge" in argv:
            return P(merge_rc, "", "not ff" if merge_rc else "")
        if "reset" in argv:
            return P(reset_rc, "HEAD is now at prev")
        if "fetch" in argv:
            return P(0)
        return P(0)
    return run, calls


class TestCanary(unittest.TestCase):
    def test_healthy_apply_records_previous(self):
        run, calls = runner_factory(["aaa111"], diffs={
            "aaa111..bbb222": ["pa/journal.py"]})
        restarts = []
        out = selfupdate.apply_sha(
            "bbb222", root="/r", runner=run,
            restart=lambda: restarts.append(1) or True,
            health_probe=lambda: True, health_seconds=1)
        self.assertTrue(out["ok"])
        self.assertEqual(out["previous_sha"], "aaa111")
        self.assertEqual(len(restarts), 1)
        self.assertIn(["merge", "--ff-only", "bbb222"], calls)

    def test_health_fail_rolls_back_and_restarts_twice(self):
        run, calls = runner_factory(["aaa111"], diffs={
            "aaa111..bbb222": ["pa/journal.py"]})
        restarts = []
        out = selfupdate.apply_sha(
            "bbb222", root="/r", runner=run,
            restart=lambda: restarts.append(1) or True,
            health_probe=lambda: False, health_seconds=0)
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "health_failed")
        self.assertEqual(len(restarts), 2)           # apply + rollback
        self.assertIn(["reset", "--hard", "aaa111"], calls)

    def test_protected_diff_refused(self):
        run, calls = runner_factory(["aaa111"], diffs={
            "aaa111..bbb222": ["pa/approvals.py"]})
        out = selfupdate.apply_sha(
            "bbb222", root="/r", runner=run,
            restart=lambda: True, health_probe=lambda: True,
            health_seconds=0)
        self.assertEqual(out["error"], "protected_paths")
        self.assertIn("pa/approvals.py", out["protected"])
        self.assertNotIn(["merge", "--ff-only", "bbb222"], calls)

    def test_ff_failure_reported_no_reset(self):
        run, calls = runner_factory(["aaa111"], merge_rc=1)
        out = selfupdate.apply_sha(
            "bbb222", root="/r", runner=run,
            restart=lambda: True, health_probe=lambda: True,
            health_seconds=0)
        self.assertEqual(out["error"], "not_ff")
        self.assertNotIn(["reset", "--hard", "aaa111"], calls)

    def test_no_systemd_needs_restart(self):
        run, calls = runner_factory(["aaa111"], diffs={
            "aaa111..bbb222": []})
        out = selfupdate.apply_sha(
            "bbb222", root="/r", runner=run,
            restart=lambda: False, health_probe=lambda: True,
            health_seconds=0)
        self.assertTrue(out["ok"])
        self.assertTrue(out["needs_restart"])
        self.assertNotIn(["reset", "--hard", "aaa111"], calls)


if __name__ == "__main__":
    unittest.main()
