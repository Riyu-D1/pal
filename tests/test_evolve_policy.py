"""Evolve item 11: protected-path policy helper."""

import unittest

from pa.evolve import policy


class TestPolicy(unittest.TestCase):
    def test_flagged_paths(self):
        bad = [
            "pa/approvals.py", "pa/allowrules.py", "pa/review.py",
            "pa/vault.py", "pa/credvault.py", "pa/secretsbox.py",
            "pa/passwords.py", "pa/egress.py", "pa/untrusted.py",
            "pa/discordbot.py", "pa/webapp.py",
            "pa/evolve/policy.py",
            "pa/evolve/evaluator.py",          # final-4 E2
            ".github/workflows/ci.yml",
            "deploy/pa.service", "deploy/pa-watchdog.sh",
            ".env", ".env.example",
            "evals/new.json",                  # final-4 E2
            "evals/heldout/h.json",
        ]
        for p in bad:
            self.assertTrue(policy.is_protected(p), p)
        self.assertEqual(
            sorted(policy.touches_protected(bad)), sorted(bad))

    def test_clean_paths(self):
        clean = [
            "pa/journal.py", "pa/monitors.py", "pa/evolve/apply.py",
            "skills/make-tea.md", "docs/EVOLVE.md",
            "tests/test_new.py", "pa/chatagent.py",
        ]
        self.assertEqual(policy.touches_protected(clean), [])

    def test_normalisation(self):
        self.assertTrue(policy.is_protected("./pa/vault.py"))
        self.assertTrue(policy.is_protected("pa\\approvals.py"))
        self.assertFalse(policy.is_protected("pa/vaultx.py"))
        # not a prefix typo: vault notes / doc mentions are fine
        self.assertFalse(policy.is_protected("docs/vault-notes.md"))


if __name__ == "__main__":
    unittest.main()
