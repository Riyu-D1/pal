"""anti-gaming rejection rules + digest surfacing."""

import unittest
from unittest import mock

from pa.evolve import apply, archive, changelog, digest, evaluator, \
    gaming, items
from tests.helpers import TmpEnv


def _ev(score, h="h1"):
    return {"score": score, "results": [], "regressions": [],
            "passed": int(score * 4), "total": 4,
            "eval_set_hash": h}


class TestRules(unittest.TestCase):
    def test_score_jump_over_30pct_flagged(self):
        r = gaming.check_candidate(score_before=0.5, score_after=0.7,
                                   eval_hash_before="h",
                                   eval_hash_after="h")
        self.assertTrue(any("score_jump" in x for x in r))

    def test_score_jump_with_fixture_change_ok(self):
        r = gaming.check_candidate(score_before=0.5, score_after=0.7,
                                   eval_hash_before="h",
                                   eval_hash_after="h2")
        self.assertEqual(r, [])

    def test_small_score_gain_ok(self):
        r = gaming.check_candidate(score_before=0.5, score_after=0.6,
                                   eval_hash_before="h",
                                   eval_hash_after="h")
        self.assertEqual(r, [])

    def test_protected_path_rejected(self):
        r = gaming.check_candidate(
            score_before=1.0, score_after=1.0,
            paths=["pa/evolve/policy.py"])
        self.assertTrue(any("protected_path" in x for x in r))
        r = gaming.check_candidate(
            score_before=1.0, score_after=1.0,
            paths=["evals/code_task_gated.json"])
        self.assertTrue(any("protected_path" in x for x in r))

    def test_assertion_removal_rejected(self):
        old = "assertEqual(a, 1)\nassertIn(x, y)\nassertTrue(ok)"
        new = "assertEqual(a, 1)"
        r = gaming.check_candidate(
            score_before=1.0, score_after=1.0,
            new_content=new, old_content=old)
        self.assertTrue(any("assertion_weakened" in x for x in r))

    def test_assertion_added_ok(self):
        r = gaming.check_candidate(
            score_before=1.0, score_after=1.0,
            new_content="assert a\nassert b", old_content="assert a")
        self.assertEqual(r, [])

    def test_logging_plus_code_coupled_rejected(self):
        r = gaming.check_candidate(
            score_before=1.0, score_after=1.0,
            paths=["pa/logsetup.py", "pa/memory.py"])
        self.assertTrue(any("log_metrics_coupled" in x for x in r))
        r = gaming.check_candidate(
            score_before=1.0, score_after=1.0,
            paths=["pa/metrics.py", "tests/test_x.py"])
        self.assertTrue(any("log_metrics_coupled" in x for x in r))

    def test_unrelated_paths_ok(self):
        r = gaming.check_candidate(
            score_before=1.0, score_after=1.0,
            paths=["pa/memory.py", "tests/test_memory.py"])
        self.assertEqual(r, [])


class TestApplyRejects(unittest.TestCase):
    def test_rejected_candidate_stays_on_file(self):
        env = TmpEnv(PA_EVOLVE="true", PA_EVOLVE_AUTO_APPLY="true",
                     PA_EVOLVE_ARCHIVE="true")
        conn = env.conn()
        iid = items.open_item(
            conn, "prompt", "low", title="weakens tests",
            dedupe_key="d1",
            artifacts={"paths": ["pa/evolve/policy.py"],
                       "kind": "snippet", "name": "tone",
                       "body": "x"})
        items.set_status(conn, iid, "planned")
        with mock.patch.object(evaluator, "run",
                               side_effect=[_ev(1.0), _ev(1.0)]):
            st = apply.maybe_apply(conn, env.settings, iid, bot=None)
        rows = archive.variants(conn, limit=10)
        item = items.get(conn, iid)
        conn.close()
        self.assertEqual(st, "rolled_back")
        self.assertEqual(item["status"], "rolled_back")
        self.assertIn("anti-gaming", item["error"] or "")
        self.assertEqual(rows[0]["status"], "rejected")

    def test_digest_lists_rejections(self):
        env = TmpEnv()
        conn = env.conn()
        iid = items.open_item(conn, "code", "low", title="bad")
        changelog.log_event(conn, env.settings, iid, "rejected",
                            "protected_path:pa/policy.py")

        class _API:
            def __init__(self):
                self.posts = []

            def post_message(self, ch, content, **kw):
                self.posts.append(content)
                return {"id": "1"}

        class B:
            def __init__(self, e):
                self._env, self.env, self.s = e, e.env, e.settings
                self.api = _API()

            def _db(self):
                return self._env.conn()

            def _owner_dm_id(self):
                return "DM"

        b = B(env)
        out = digest.run(b, {})
        conn.close()
        self.assertEqual(out, "digest sent")
        self.assertIn("anti-gaming rejected", b.api.posts[0])


if __name__ == "__main__":
    unittest.main()
