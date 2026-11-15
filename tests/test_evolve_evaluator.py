"""final-4 E2: hash-pinned evaluator, tamper refusal, held-out
isolation."""

import json
import os
import tempfile
import unittest

from pa import db as dbmod
from pa.evolve import eval as evalmod, evaluator, policy
from tests.helpers import TmpEnv


def _fixture(fid, extra=None):
    fx = {"id": fid,
          "prompt": "hi",
          "script": ["```json\n{\"final\": \"ok\"}\n```"],
          "must_contain": "ok"}
    fx.update(extra or {})
    return fx


class TestPinning(unittest.TestCase):
    def test_first_run_bootstraps_pin(self):
        env = TmpEnv()
        conn = env.conn()
        st = evaluator.check(conn)
        self.assertTrue(st["ok"])
        self.assertTrue(st["bootstrapped"])
        self.assertEqual(dbmod.get_setting(conn, "eval_manifest_sha256"),
                         st["pinned"])
        conn.close()

    def test_tampered_fixture_refuses_scoring(self):
        env = TmpEnv()
        conn = env.conn()
        s = env.settings
        d = tempfile.mkdtemp()
        json.dump(_fixture("a"), open(os.path.join(d, "a.json"), "w"))
        evaluator.pin(conn, eval_dir=d)
        # candidate "improves" the suite by relaxing a fixture
        weak = _fixture("a")
        weak.pop("must_contain")
        json.dump(weak, open(os.path.join(d, "a.json"), "w"))
        out = evaluator.run(conn, s, eval_dir=d)
        conn.close()
        self.assertTrue(out["refused"])
        self.assertEqual(out["score"], 0.0)
        self.assertIn("manifest", out["refused"])

    def test_repin_restores_scoring(self):
        env = TmpEnv()
        conn = env.conn()
        d = tempfile.mkdtemp()
        json.dump(_fixture("a"), open(os.path.join(d, "a.json"), "w"))
        evaluator.pin(conn, eval_dir=d)
        json.dump(_fixture("b"),
                  open(os.path.join(d, "b.json"), "w"))
        out = evaluator.run(conn, env.settings, eval_dir=d)
        self.assertTrue(out["refused"])
        evaluator.pin(conn, eval_dir=d)          # owner-approved deploy
        out = evaluator.run(conn, env.settings, eval_dir=d)
        conn.close()
        self.assertFalse(out.get("refused"))
        self.assertEqual(out["score"], 1.0)

    def test_evaluator_and_evals_are_protected(self):
        self.assertTrue(policy.is_protected("pa/evolve/evaluator.py"))
        self.assertTrue(policy.is_protected("evals/code_task_gated.json"))
        self.assertTrue(policy.is_protected(
            "evals/heldout/refusal_heldout.json"))


class TestHeldOut(unittest.TestCase):
    def test_heldout_never_in_main_fixtures(self):
        main_ids = {f.get("id") for f in evalmod.load_fixtures()}
        held = evalmod.load_fixtures(evaluator.HELDOUT_DIR)
        self.assertTrue(held)
        self.assertFalse(main_ids & {f.get("id") for f in held})

    def test_run_reports_heldout_score(self):
        env = TmpEnv()
        conn = env.conn()
        out = evaluator.run(conn, env.settings)
        conn.close()
        self.assertEqual(out["heldout_score"], 1.0)
        self.assertTrue(out["eval_set_hash"])

    def test_full_suite_passes(self):
        env = TmpEnv()
        out = evalmod.run(env.settings)
        self.assertEqual(out["score"], 1.0)
        out = evalmod.run(env.settings,
                          eval_dir=evaluator.HELDOUT_DIR)
        self.assertEqual(out["score"], 1.0)


if __name__ == "__main__":
    unittest.main()
