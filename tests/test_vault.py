"""Vault mirror: scoped read-only indexing + untrusted retrieval.

- indexes only .md under VAULT_PATH; skips hidden, secret-like, non-md,
  symlinked and escaping paths; caps sizes and counts
- notes are returned wrapped in UNTRUSTED markers
- vault_read resolves strictly inside the vault root
"""

import os
import tempfile
import unittest

from pa import vault as vaultmod
from tests.helpers import TmpEnv


class VaultBase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="pa-vault-")
        self.env = TmpEnv(VAULT_PATH=self.dir)
        self.conn = self.env.conn()

    def tearDown(self):
        self.conn.close()

    def _write(self, rel, text="# t\nhello world"):
        path = os.path.join(self.dir, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)


class TestIndexing(VaultBase):
    def test_indexes_markdown_only(self):
        self._write("a.md", "# Alpha\nshopping list\n")
        self._write("sub/b.md", "# Beta\nflight booking\n")
        self._write("c.txt", "not markdown")
        stats = vaultmod.scan(self.conn, self.env.settings)
        self.assertEqual(stats["indexed"], 2)
        self.assertEqual(stats["skipped_nonmd"], 1)
        n = self.conn.execute("SELECT COUNT(*) c FROM vault_notes") \
            .fetchone()["c"]
        self.assertEqual(n, 2)

    def test_skips_hidden_and_secret_names(self):
        self._write(".obsidian/config.md", "hidden")
        self._write(".hidden.md", "hidden")
        self._write("api-secret.md", "do not index")
        self._write("prod.env.md", "secrets")
        self._write("fine.md", "# ok\ntext")
        stats = vaultmod.scan(self.conn, self.env.settings)
        self.assertEqual(stats["skipped_hidden"], 2)
        self.assertEqual(stats["skipped_secret"], 2)
        self.assertEqual(stats["indexed"], 1)

    def test_skips_symlinks_and_escapes(self):
        outside = tempfile.mktemp(prefix="pa-vault-out-")
        with open(outside, "w") as f:
            f.write("# outside\ndo not index")
        os.symlink(outside, os.path.join(self.dir, "linked.md"))
        subdir = os.path.join(self.dir, "real")
        os.makedirs(subdir)
        os.symlink("/etc", os.path.join(self.dir, "etcdir"))
        self._write("real/ok.md", "# fine\nhello")
        stats = vaultmod.scan(self.conn, self.env.settings)
        self.assertEqual(stats["skipped_symlink"], 2)
        self.assertEqual(stats["indexed"], 1)
        os.unlink(outside)

    def test_incremental_and_stale_removal(self):
        self._write("a.md", "# A\nfirst body")
        vaultmod.scan(self.conn, self.env.settings)
        self._write("b.md", "# B\nsecond body")
        stats = vaultmod.scan(self.conn, self.env.settings)
        self.assertEqual(stats["indexed"], 1)   # only the new file
        os.unlink(os.path.join(self.dir, "a.md"))
        stats = vaultmod.scan(self.conn, self.env.settings)
        self.assertEqual(stats["removed"], 1)

    def test_search_returns_untrusted_snippets(self):
        self._write("trip.md", "# Trip\ntrain departs 09:15 platform 4")
        hits = vaultmod.search(self.conn, self.env.settings, "platform")
        self.assertEqual(len(hits), 1)
        self.assertIn("UNTRUSTED NOTE trip.md", hits[0]["snippet"])
        self.assertIn("END UNTRUSTED", hits[0]["snippet"])

    def test_read_note_caps_and_escapes(self):
        self._write("ok.md", "# Fine\nbody text")
        note = vaultmod.read_note(self.conn, self.env.settings, "ok.md")
        self.assertIn("body text", note["content"])
        with self.assertRaises(vaultmod.VaultNotConfigured):
            vaultmod.read_note(self.conn, self.env.settings, "../ok.md")
        with self.assertRaises(vaultmod.VaultNotConfigured):
            vaultmod.read_note(self.conn, self.env.settings,
                               "/etc/passwd")
        self.assertIsNone(vaultmod.read_note(
            self.conn, self.env.settings, "missing.md"))

    def test_unconfigured_is_explicit(self):
        env = TmpEnv()  # no VAULT_PATH
        conn = env.conn()
        with self.assertRaises(vaultmod.VaultNotConfigured):
            vaultmod.scan(conn, env.settings)
        self.assertFalse(vaultmod.configured(env.settings))


if __name__ == "__main__":
    unittest.main()
