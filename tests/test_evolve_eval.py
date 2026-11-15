"""Evolve item 9: the offline eval harness itself."""

import json
import os
import tempfile
import unittest

from pa.evolve import eval as evalmod
from tests.helpers import TmpEnv


def _fixture_dir(fixtures):
    d = tempfile.mkdtemp(prefix="pa-eval-fx-")
    for fx in fixtures:
        with open(os.path.join(d, fx["id"] + ".json"), "w") as f:
            json.dump(fx, f)
    return d


class TestEvalHarness(unittest.TestCase):
    def setUp(self):
        self.env = TmpEnv()

    def test_starter_set_passes_offline(self):
        out = evalmod.run(self.env.settings)
        self.assertEqual(out["regressions"], [])
        self.assertEqual(out["passed"], out["total"])
        self.assertEqual(out["score"], 1.0)

    def test_broken_fixture_fails(self):
        d = _fixture_dir([{
            "id": "broken", "prompt": "hi",
            "script": ['{"final": "hello"}'],
            "must_contain": "never-present"}])
        out = evalmod.run(self.env.settings, eval_dir=d)
        self.assertFalse(out["results"][0]["ok"])
        self.assertIn("missing", out["results"][0]["failures"][0])
        self.assertNotEqual(out["score"], 1.0)

    def test_baseline_regression_flagged(self):
        d = _fixture_dir([{
            "id": "reg", "prompt": "hi",
            "script": ['{"final": "hello"}'],
            "must_contain": "absent"}])
        with open(os.path.join(d, "baseline.json"), "w") as f:
            json.dump({"expected_pass": ["reg"]}, f)
        out = evalmod.run(self.env.settings, eval_dir=d)
        self.assertEqual(out["regressions"], ["reg"])

    def test_forbidden_tool_caught(self):
        d = _fixture_dir([{
            "id": "evil", "prompt": "x",
            "script": [
                '{"tool": "code_task", "args": {"prompt": "p"}}',
                '{"final": "done"}'],
            "forbidden_tools": ["code_task"]}])
        out = evalmod.run(self.env.settings, eval_dir=d)
        self.assertIn("forbidden tool code_task",
                      out["results"][0]["failures"][0])

    def test_live_mode_not_in_unittest(self):
        # --live needs a real provider; the runner must never be
        # invoked with it here. Assert the fixture path stays offline:
        # Scripted brains are the only brains unittest ever wires.
        fx = {"id": "x", "prompt": "hi", "script": ['{"final": "ok"}']}
        r = evalmod.run_fixture(fx, self.env.settings)
        self.assertTrue(r["ok"])


if __name__ == "__main__":
    unittest.main()
