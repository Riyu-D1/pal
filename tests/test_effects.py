"""Acceptance #3: idempotent effects under crash/double-apply."""

import threading
import unittest

from pa import approvals, memory as memmod
from tests.helpers import TmpEnv


class TestIdempotentApply(unittest.TestCase):
    def setUp(self):
        self.env = TmpEnv()
        self.aid = self.env.add_assistant()

    def test_apply_twice_one_memory(self):
        conn = self.env.conn()
        aid = approvals.create_approval(
            conn, "memory_write",
            {"op": "create", "assistant_id": self.aid,
             "assistant_name": "a1", "content": "the sky is blue"})
        approvals.decide(conn, aid, True)
        self.assertTrue(approvals.apply_if_new(conn, aid))
        # crash simulation: call again - already applied
        self.assertFalse(approvals.apply_if_new(conn, aid))
        self.assertFalse(approvals.apply_if_new(conn, aid))
        mems = memmod.list_memories(conn, self.aid)
        self.assertEqual(len(mems), 1)
        self.assertEqual(mems[0]["content"], "the sky is blue")
        # exactly one effect row, keyed by the approval id
        eff = conn.execute("SELECT * FROM effects").fetchall()
        self.assertEqual(len(eff), 1)
        self.assertEqual(eff[0]["effect_key"], aid)

    def test_concurrent_apply_exactly_once(self):
        conn = self.env.conn()
        aid = approvals.create_approval(
            conn, "memory_write",
            {"op": "create", "assistant_id": self.aid,
             "assistant_name": "a1", "content": "race me"})
        approvals.decide(conn, aid, True)
        results = []
        errs = []

        def apply():
            c = self.env.conn()
            try:
                results.append(approvals.apply_if_new(c, aid))
            except Exception as e:
                errs.append(e)
            finally:
                c.close()

        ts = [threading.Thread(target=apply) for _ in range(4)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertFalse(errs)
        self.assertEqual(results.count(True), 1)
        self.assertEqual(len(memmod.list_memories(self.env.conn(),
                                                  self.aid)), 1)

    def test_deterministic_approval_id_converges(self):
        """A retried propose step produces the same approval row, not a
        duplicate."""
        conn = self.env.conn()
        payload = {"op": "create", "assistant_id": self.aid,
                   "assistant_name": "a1", "content": "same"}
        import hashlib, json
        raw = f"7:memory_write:{json.dumps(payload, sort_keys=True)}"
        aid = __import__("hashlib").sha256(raw.encode()).hexdigest()[:32]
        approvals.create_approval(conn, "memory_write", payload,
                                  task_id=7, approval_id=aid)
        approvals.create_approval(conn, "memory_write", payload,
                                  task_id=7, approval_id=aid)
        self.assertEqual(
            conn.execute("SELECT COUNT(*) c FROM approvals WHERE id=?",
                         (aid,)).fetchone()["c"], 1)


if __name__ == "__main__":
    unittest.main()
