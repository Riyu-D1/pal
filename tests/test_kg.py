"""final-2 M4: personal KG — deterministic extraction from
wikilinks/tags/frontmatter, temporal edges that close instead of
delete, changed-note-only builds, kg_query phrasings."""

import unittest

from pa import kg
from tests.helpers import TmpEnv


class TestKG(unittest.TestCase):
    def setUp(self):
        self.env = TmpEnv(PA_KG="true")
        self.conn = self.env.conn()
        self.notes = {
            "Alice.md": "---\ntype: person\n---\n# Alice\n"
                        "Alice works on [[Phoenix]] with [[Bob]]. "
                        "#friend",
            "Phoenix.md": "---\ntype: project\npeople: Alice, Bob\n"
                          "---\n# Phoenix\n"
                          "Phoenix ships in [[2026 Roadmap]].",
        }
        self.shas = {p: str(i) for i, p in
                     enumerate(self.notes)}

    def tearDown(self):
        self.conn.close()

    def _build(self):
        return kg.build(
            self.conn, lambda p: self.notes.get(p, ""),
            [(p, self.shas[p], p[:-3]) for p in self.notes],
            today="2026-01-05")

    def test_deterministic_entities_and_edges(self):
        out = self._build()
        self.assertEqual(out["notes"], 2)
        ents = {}
        for r in self.conn.execute(
                "SELECT name_l, type FROM kg_entities").fetchall():
            ents.setdefault(r["name_l"], set()).add(r["type"])
        self.assertIn("person", ents["alice"])
        self.assertIn("project", ents["phoenix"])
        self.assertTrue(ents["bob"] & {"note", "person"})
        self.assertIn("tag", ents["friend"])
        rels = {(r["s"], r["rel"], r["d"]) for r in self.conn.execute(
            "SELECT s.name_l s, e.rel, d.name_l d FROM kg_edges e"
            " JOIN kg_entities s ON s.id=e.src"
            " JOIN kg_entities d ON d.id=e.dst").fetchall()}
        self.assertIn(("alice", "links", "phoenix"), rels)
        self.assertIn(("alice", "tagged", "friend"), rels)
        self.assertIn(("phoenix", "mentions", "alice"), rels)

    def test_edges_close_not_delete(self):
        self._build()
        # Alice stops linking Phoenix
        self.notes["Alice.md"] = "---\ntype: person\n---\n# Alice\n"
        self.shas["Alice.md"] = "9"
        kg.build(self.conn, lambda p: self.notes[p],
                 [("Alice.md", "9", "Alice")], today="2026-01-08")
        closed = self.conn.execute(
            "SELECT rel, valid_to FROM kg_edges WHERE valid_to"
            " IS NOT NULL").fetchall()
        self.assertTrue(closed)
        self.assertEqual(closed[0]["valid_to"], "2026-01-08")
        alice = self.conn.execute(
            "SELECT id FROM kg_entities WHERE name_l='alice'"
            ).fetchone()
        open_ = self.conn.execute(
            "SELECT COUNT(*) c FROM kg_edges WHERE valid_to IS NULL"
            " AND src=?", (alice["id"],)).fetchone()["c"]
        self.assertEqual(open_, 0)

    def test_changed_notes_only(self):
        first = self._build()
        second = self._build()   # same shas — nothing re-read
        self.assertGreater(first["notes"], 0)
        self.assertEqual(second["notes"], 0)
        # bump one sha -> one note
        self.shas["Alice.md"] = "9"
        third = self._build()
        self.assertEqual(third["notes"], 1)

    def test_query_shapes(self):
        self._build()
        who = kg.query(self.conn, "who is alice")
        self.assertEqual(who["entities"][0]["name"], "Alice")
        self.assertTrue(any(e["to"] == "Phoenix"
                            for e in who["edges"]))
        src = who["edges"][0]["source"]
        self.assertEqual(src, "Alice.md")
        ch = kg.query(self.conn, "what changed this week",
                      today="2026-01-09")
        self.assertTrue(ch["changed"])
        self.assertEqual(ch["changed"][0]["valid_from"],
                         "2026-01-05")

    def test_model_cap_bounds_calls(self):
        calls = [0]

        def extra(body):
            calls[0] += 1
            return [("Acme", "org")]
        notes = [(f"n{i}.md", "s", f"N{i}") for i in range(30)]
        texts = {p: "# x" for p, _, _ in notes}
        kg.build(self.conn, lambda p: texts[p], notes,
                 model_extra=extra, model_cap=5)
        self.assertEqual(calls[0], 30)   # build() itself calls extra
        capped = [0]

        def capped_extra(body):
            capped[0] += 1
            return []
        # the vault path wraps it under the cap
        self.env.settings.vault_path = ""
        kg.vault_build(self.conn, self.env.settings,
                       model_extra=capped_extra, model_cap=5)


if __name__ == "__main__":
    unittest.main()
