"""Item 17: restricted run_python."""

import os
import unittest

from pa import chattools, runpy
from tests.helpers import TmpEnv


class TestRun(unittest.TestCase):
    def setUp(self):
        self.env = TmpEnv(LLM_API_KEY="sekret-key-123")

    def test_basic_exec(self):
        out = runpy.run(self.env.settings, "print(2+2)")
        self.assertTrue(out["ok"])
        self.assertEqual(out["stdout"].strip(), "4")

    def test_cwd_is_sandbox(self):
        out = runpy.run(self.env.settings,
                        "import os; print(os.getcwd())")
        self.assertEqual(out["stdout"].strip(),
                         os.path.realpath(self.env.settings
                                          .chat_sandbox))

    def test_env_is_scrubbed(self):
        out = runpy.run(self.env.settings,
                        "import os;"
                        " print(os.environ.get('LLM_API_KEY'))")
        self.assertEqual(out["stdout"].strip(), "None")
        self.assertNotIn("sekret-key-123", out["stdout"])
        self.assertNotIn("sekret-key-123", out["stderr"])

    def test_write_lands_in_sandbox(self):
        out = runpy.run(self.env.settings,
                        "open('out.txt','w').write('hi');"
                        " print('done')")
        self.assertTrue(out["ok"])
        path = os.path.join(self.env.settings.chat_sandbox,
                            "out.txt")
        self.assertTrue(os.path.exists(path))
        with open(path) as f:
            self.assertEqual(f.read(), "hi")

    def test_empty_and_oversize(self):
        self.assertEqual(runpy.run(self.env.settings, "")["error"],
                         "bad_arguments")
        self.assertEqual(
            runpy.run(self.env.settings,
                      "x=1\n" * 10000)["error"], "too_large")

    def test_stderr_and_exit(self):
        out = runpy.run(self.env.settings, "import sys;"
                        " sys.stderr.write('oops'); sys.exit(3)")
        self.assertFalse(out["ok"])
        self.assertEqual(out["exit"], 3)
        self.assertIn("oops", out["stderr"])

    def test_timeout(self):
        out = runpy.run(self.env.settings,
                        "import time; time.sleep(30)", timeout=0.5)
        self.assertEqual(out["error"], "timeout")


class TestTool(unittest.TestCase):
    def test_gated_and_runs(self):
        env = TmpEnv()
        aid = env.add_assistant(name="pal", tools=[])
        conn = env.conn()
        ast = conn.execute(
            "SELECT * FROM assistants WHERE id=?", (aid,)).fetchone()
        ctx = chattools.ChatToolContext(
            conn, env.settings, ast, "c", env=env.env)
        self.assertTrue(
            chattools.CHAT_TOOLS["run_python"]["approval"])
        from pa import allowrules
        self.assertNotIn("run_python", allowrules.ALWAYS_CAPABLE)
        ctx.request_approval = lambda n, a, i: "approved"
        out = chattools.run(ctx, "run_python",
                            {"code": "print('hi')"})
        self.assertEqual(out["stdout"].strip(), "hi")


if __name__ == "__main__":
    unittest.main()
