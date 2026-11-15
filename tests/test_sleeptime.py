"""sleeptime curator — repeated facts promote to the
human block via a card, duplicate memories become delete proposals,
stale world lines demote (versioned), twice-successful paths become
skill proposals. Nothing deletes without approval."""

import unittest

from pa import activity, approvals, memblocks, sleeptime
from tests.helpers import TmpEnv


class TestSleeptime(unittest.TestCase):
    def setUp(self):
        self.env = TmpEnv(PA_SLEEPTIME="true", PA_MEMORY_BLOCKS="true")
        self.conn = self.env.conn()
        self.aid = self.env.add_assistant(name="pal", tools=[])

    def tearDown(self):
        self.conn.close()

    def test_repeated_fact_promotes_via_card(self):
        self.conn.execute(
            "INSERT INTO owner_facts(key, value, updated_at)"
            " VALUES('coffee','oat flat white','2026-01-01')")
        for _ in range(2):
            self.conn.execute(
                "INSERT INTO memories(assistant_id, content,"
                " created_at, updated_at) VALUES(?,?,?,?)",
                (self.aid, "owner likes an oat flat white",
                 "2026-01-01", "2026-01-01"))
        out = sleeptime.run(self.conn)
        self.assertEqual(out["promoted"], 1)
        # a job_action approval for memory_block_set, nothing written
        nap = activity.list_activity(self.conn, kind="needs_approval")
        jobs = [a for a in nap if approvals.get(
            self.conn, a["ref"]["approval_id"])["kind"]
                == "job_action"]
        self.assertEqual(len(jobs), 1)
        ap = approvals.get(self.conn, jobs[0]["ref"]["approval_id"])
        self.assertIn("oat flat white", ap["payload_json"])
        self.assertEqual(memblocks.get(self.conn, "human")
                         ["content"], "")

    def test_dupe_memories_become_delete_proposals(self):
        for _ in range(2):
            self.conn.execute(
                "INSERT INTO memories(assistant_id, content,"
                " created_at, updated_at) VALUES(?,?,?,?)",
                (self.aid, "same fact about the owner",
                 "2026-01-01", "2026-01-01"))
        out = sleeptime.run(self.conn)
        self.assertEqual(out["merge_proposals"], 1)
        # both rows still there — nothing deleted
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) c FROM memories").fetchone()["c"], 2)
        ap = self.conn.execute(
            "SELECT * FROM approvals WHERE kind='memory_write'"
            ).fetchone()
        self.assertIsNotNone(ap)
        # approving applies the delete via apply_if_new
        approvals.decide(self.conn, ap["id"], True)
        approvals.apply_if_new(self.conn, ap["id"])
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) c FROM memories").fetchone()["c"], 1)

    def test_stale_world_demoted_and_revertible(self):
        memblocks.set_block(
            self.conn, "world",
            "- 2020-01-01 old project done\n- 2099-01-01 future plan")
        out = sleeptime.run(self.conn)
        self.assertEqual(out["demoted"], 1)
        self.assertEqual(memblocks.get(self.conn, "world")["content"],
                         "- 2099-01-01 future plan")
        # revertible via history
        memblocks.undo(self.conn)
        self.assertIn("2020-01-01",
                      memblocks.get(self.conn, "world")["content"])

    def test_no_owner_fact_deleted_silently(self):
        self.conn.execute(
            "INSERT INTO owner_facts(key, value, updated_at)"
            " VALUES('k','v','2026-01-01')")
        sleeptime.run(self.conn)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) c FROM owner_facts").fetchone()["c"], 1)


if __name__ == "__main__":
    unittest.main()
