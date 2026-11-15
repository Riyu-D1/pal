"""final-2 M3: cross-source recall — FTS5 over chat turns, memories,
owner facts, activity and vault excerpts; incremental manifest;
ranked mixed-source hits that cite source+date."""

import os
import unittest

from pa import activity, recall, sysjobs
from tests.helpers import TmpEnv


class TestRecall(unittest.TestCase):
    def setUp(self):
        self.env = TmpEnv(PA_RECALL="true")
        self.conn = self.env.conn()
        self.aid = self.env.add_assistant(name="pal", tools=[])
        # a small vault
        self.vault = os.path.join(self.env.dir, "vault")
        os.makedirs(self.vault, exist_ok=True)
        self.env.settings.vault_path = self.vault
        self.env.settings.vault_note_cap = 4000
        with open(os.path.join(self.vault, "Milo.md"), "w") as f:
            f.write("# Milo\n\nThe owner's dog Milo likes walks.")
        with open(os.path.join(self.vault, "secrets.md"), "w") as f:
            f.write("SECRET_KEY=shh")
        from pa import vault
        vault.scan(self.conn, self.env.settings)

    def tearDown(self):
        self.conn.close()

    def _seed(self):
        self.conn.execute(
            "INSERT INTO chat_messages(channel_id, role, content,"
            " created_at) VALUES(?,?,?,?)",
            ("1", "user",
             "reminder that Milo the dog has a vet visit Friday",
             "2026-01-02T10:00:00"))
        self.conn.execute(
            "INSERT INTO memories(assistant_id, content,"
            " created_at, updated_at) VALUES(?,?,?,?)",
            (self.aid, "Milo's vet is Dr. Patel",
             "2026-01-02", "2026-01-02"))
        self.conn.execute(
            "INSERT INTO owner_facts(key, value, updated_at)"
            " VALUES('dog','Milo','2026-01-02')")
        activity.record(self.conn, "noticed",
                        "vet appointment coming up for Milo")

    def test_mixed_sources_ranked_with_citations(self):
        self._seed()
        out = recall.reindex(self.conn, self.env.settings)
        self.assertGreaterEqual(out["added"], 4)
        hits = recall.search(self.conn, "Milo vet")
        self.assertTrue(hits)
        srcs = {h["source"] for h in hits}
        self.assertTrue({"chat", "memory", "owner_fact",
                         "vault"} & srcs)
        for h in hits:
            self.assertTrue(h["day"])
            self.assertTrue(h["snippet"])

    def test_secretish_vault_notes_never_index(self):
        recall.reindex(self.conn, self.env.settings)
        hits = recall.search(self.conn, "SECRET_KEY")
        self.assertFalse(
            any(h["source"] == "vault" for h in hits))

    def test_incremental_only_reindexes_changes(self):
        self._seed()
        first = recall.reindex(self.conn, self.env.settings)
        second = recall.reindex(self.conn, self.env.settings)
        self.assertGreater(first["added"], 0)
        self.assertEqual(second["added"], 0)
        self.assertGreater(second["skipped"], 0)
        # change one memory -> exactly one re-add
        self.conn.execute(
            "UPDATE memories SET content='Milo the dog is nine' "
            "WHERE content LIKE '%Patel%'")
        third = recall.reindex(self.conn, self.env.settings)
        self.assertEqual(third["added"], 1)

    def test_dropped_sources_leave_index(self):
        self._seed()
        recall.reindex(self.conn, self.env.settings)
        self.conn.execute("DELETE FROM memories")
        out = recall.reindex(self.conn, self.env.settings)
        self.assertEqual(out["dropped"], 1)

    def test_recall_job_flag_off_noop(self):
        conn, s = self.conn, self.env.settings
        class B:
            def _db(self):
                return conn
        B.s = s
        b = B()
        b.s.recall = False
        self.assertEqual(recall.job(b, {}), "")
        b.s.recall = True
        self._seed()
        self.assertIn("indexed", recall.job(b, {}))
        self.assertTrue(sysjobs.is_system("recall-index"))


if __name__ == "__main__":
    unittest.main()
