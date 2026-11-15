"""Acceptance #11: assistant memory isolation + shared owner facts."""

import unittest

from pa import memory as memmod
from tests.helpers import TmpEnv


class TestIsolation(unittest.TestCase):
    def setUp(self):
        self.env = TmpEnv()
        self.conn = self.env.conn()
        self.a = self.env.add_assistant("alpha")
        self.b = self.env.add_assistant("beta")
        memmod.insert_memory(self.conn, self.a, "alpha secret: loves figs")
        memmod.insert_memory(self.conn, self.b, "beta note: hates crowds")
        memmod.upsert_owner_fact(self.conn, "timezone", "Europe/London")

    def test_a_cannot_read_b_memories(self):
        hits = memmod.search_memories(self.conn, self.a, "crowds")
        self.assertEqual(hits, [])
        hits = memmod.search_memories(self.conn, self.a, "figs")
        self.assertEqual(len(hits), 1)
        self.assertIn("alpha", hits[0]["content"])

    def test_b_cannot_read_a_memories(self):
        hits = memmod.search_memories(self.conn, self.b, "figs")
        self.assertEqual(hits, [])

    def test_owner_facts_shared_read(self):
        for aid in (self.a, self.b):
            hits = memmod.search_memories(self.conn, aid, "timezone")
            self.assertEqual(len(hits), 1)
            self.assertEqual(hits[0]["entity"], "owner_fact")

    def test_update_delete_scoped(self):
        b_mem = memmod.list_memories(self.conn, self.b)[0]
        # A tries to edit/delete B's memory id -> no-op
        self.assertFalse(memmod.update_memory(
            self.conn, b_mem["id"], self.a, "hijacked"))
        self.assertFalse(memmod.delete_memory(self.conn, b_mem["id"], self.a))
        self.assertEqual(len(memmod.list_memories(self.conn, self.b)), 1)


if __name__ == "__main__":
    unittest.main()
