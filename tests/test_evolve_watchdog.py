"""Evolve item 14: watchdog — one restart + one DM per outage."""

import unittest

from pa import watchdog
from tests.helpers import TmpEnv


class Seq:
    """Scripted health endpoint: True/False pattern per call."""
    def __init__(self, seq):
        self.seq = list(seq)

    def __call__(self):
        return self.seq.pop(0) if self.seq else True


class TestWatchdog(unittest.TestCase):
    def test_fail_then_ok_restarts_once_notifies_once(self):
        env = TmpEnv()
        probe = Seq([False] * 5 + [True] * 10)
        restarts, notes = [], []
        watchdog.loop(
            env.settings, probe=probe,
            restart=lambda: restarts.append(1) or True,
            notify=notes.append,
            interval=0.001, fail_after=0.004, max_loops=8)
        self.assertEqual(len(restarts), 1)
        self.assertEqual(len(notes), 1)
        self.assertIn("watchdog restarted", notes[0])

    def test_healthy_never_restarts(self):
        env = TmpEnv()
        restarts, notes = [], []
        watchdog.loop(
            env.settings, probe=Seq([True] * 20),
            restart=lambda: restarts.append(1) or True,
            notify=notes.append,
            interval=0.001, fail_after=0.004, max_loops=10)
        self.assertEqual(restarts, [])
        self.assertEqual(notes, [])

    def test_restart_failure_alerts_once(self):
        env = TmpEnv()
        restarts, notes = [], []
        watchdog.loop(
            env.settings, probe=Seq([False] * 20),
            restart=lambda: restarts.append(1) or False,
            notify=notes.append,
            interval=0.001, fail_after=0.004, max_loops=8)
        self.assertTrue(restarts)
        self.assertEqual(len(notes), 1)
        self.assertIn("could not restart", notes[0])

    def test_no_hardcoded_secrets(self):
        import inspect
        src = inspect.getsource(watchdog)
        for tok in ("xoxb-", "ghp_", "sk-", "AIza", "password="):
            self.assertNotIn(tok, src)


if __name__ == "__main__":
    unittest.main()
