"""K1 acceptance: a fake model produces a tool; failing tests block
registration; socket/subprocess imports rejected statically; tool_run
executes in the sandbox; version flow + approval gate."""

import json
import unittest

from pa import chattools, madetools
from tests.helpers import TmpEnv


GOOD_CODE = '''\
import json, math

def run(args):
    """double a number"""
    n = float(args.get("n", 0))
    return {"doubled": n * 2}
'''

GOOD_TESTS = '''\
def test_1():
    assert run({"n": 2})["doubled"] == 4

def test_2():
    assert run({"n": 0})["doubled"] == 0

def test_3():
    assert run({"n": -3})["doubled"] == -6
'''

BAD_TESTS = '''\
def test_1():
    assert run({"n": 2})["doubled"] == 5   # wrong on purpose
'''

SOCKET_CODE = '''\
import socket

def run(args):
    return {"ip": socket.gethostbyname("example.com")}
'''

SUBP_CODE = '''\
from subprocess import run as sprun

def run(args):
    return {"out": sprun(["id"]).returncode}
'''

EVAL_CODE = '''\
def run(args):
    return eval(args.get("expr", "0"))
'''


def _reply(code=GOOD_CODE, tests=GOOD_TESTS, **kw):
    obj = {"code": code, "tests": tests,
           "inputs": {"type": "object",
                      "properties": {"n": {"type": "number"}}}}
    obj.update(kw)
    return "```json\n" + json.dumps(obj) + "\n```"


class Brain:
    """ctx.brain seam — complete(messages, remaining, tier, scope)."""
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def complete(self, messages, remaining, tier=None,
                 scope="owner-chat"):
        self.calls.append(messages)
        return self.replies.pop(0)


def _ctx(t, brain=None, approve="approved", unattended=False):
    class C:
        pass
    c = C()
    c.s = t.settings
    c.conn = t.conn()
    c.channel_id = "c1"
    c.env = {}
    c.brain = brain
    c.unattended = unattended
    c.pending = []
    c.waited = 0.0
    c.approved_replay = False
    c.tainted = None
    c.progress = None
    c.computer = None
    c._approve = approve
    c.approve = lambda name, args, decision=None: c._approve
    return c


class MtEnv(TmpEnv):
    def __init__(self, **kw):
        kw.setdefault("PA_MADETOOLS", "1")
        super().__init__(**kw)


class TestStaticCheck(unittest.TestCase):
    def test_clean_passes(self):
        self.assertEqual(madetools.static_check(GOOD_CODE), "")

    def test_socket_rejected(self):
        self.assertIn("socket", madetools.static_check(SOCKET_CODE))

    def test_subprocess_rejected(self):
        self.assertIn("subprocess",
                      madetools.static_check(SUBP_CODE))

    def test_eval_rejected(self):
        self.assertIn("eval", madetools.static_check(EVAL_CODE))

    def test_os_rejected(self):
        self.assertEqual(madetools.static_check(
            "import os\ndef run(args):\n    return os.environ\n"),
            "banned import: os")

    def test_urllib_request_rejected(self):
        out = madetools.static_check(
            "import urllib.request\ndef run(args):\n    return 1\n")
        self.assertEqual(out, "banned import: urllib.request")
        # but urllib.parse is allowed
        self.assertEqual(madetools.static_check(
            "import urllib.parse\ndef run(args):\n"
            "    return urllib.parse.quote('x')\n"), "")

    def test_getattr_dunder_rejected(self):
        self.assertIn("getattr", madetools.static_check(
            "def run(args):\n    return getattr(object, '__class__')\n"))
        self.assertIn("__class__", madetools.static_check(
            "def run(args):\n    return (1).__class__\n"))


class TestMakeFlow(unittest.TestCase):
    def setUp(self):
        self.t = MtEnv()

    def test_happy_path_registers_v1(self):
        ctx = _ctx(self.t, Brain([_reply()]))
        out = madetools.make(ctx, "doubler", "double a number", "")
        self.assertTrue(out["registered"], out)
        self.assertEqual(out["version"], 1)
        # files on disk, outside pa/
        self.assertIn("/tools/doubler", out["path"])
        self.assertNotIn("/pa/", out["path"])

    def test_first_registration_needs_approval(self):
        ctx = _ctx(self.t, Brain([_reply()]), approve="denied")
        out = madetools.make(ctx, "doubler", "d", "")
        self.assertEqual(out["error"], "not_approved")
        self.assertIsNone(madetools._active(ctx.conn, "doubler"))

    def test_failing_tests_block(self):
        ctx = _ctx(self.t, Brain([_reply(tests=BAD_TESTS)]))
        out = madetools.make(ctx, "doubler", "d", "")
        self.assertEqual(out["error"], "tests_failed")
        self.assertIsNone(madetools._active(ctx.conn, "doubler"))

    def test_static_reject_blocks_before_tests(self):
        ctx = _ctx(self.t, Brain([_reply(code=SOCKET_CODE)]))
        out = madetools.make(ctx, "dialer", "d", "")
        self.assertEqual(out["error"], "static_check")
        self.assertIn("socket", out["message"])

    def test_bad_model_output(self):
        ctx = _ctx(self.t, Brain(["sorry, can't do that"]))
        out = madetools.make(ctx, "doubler", "d", "")
        self.assertEqual(out["error"], "bad_model_output")

    def test_bad_name(self):
        ctx = _ctx(self.t, Brain([_reply()]))
        out = madetools.make(ctx, "Bad-Name!", "d", "")
        self.assertEqual(out["error"], "bad_arguments")


class TestRun(unittest.TestCase):
    def setUp(self):
        self.t = MtEnv()
        self.ctx = _ctx(self.t, Brain([_reply()]))
        madetools.make(self.ctx, "doubler", "d", "")

    def test_run_local(self):
        out = madetools.run(self.ctx, "doubler", {"n": 21})
        self.assertEqual(out["result"], {"doubled": 42.0})
        self.assertEqual(out["ran_on"], "local")

    def test_unknown_tool(self):
        out = madetools.run(self.ctx, "nope", {})
        self.assertEqual(out["error"], "not_found")

    def test_list(self):
        tools = madetools.list_tools(self.ctx.conn)
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0]["name"], "doubler")


class TestVersions(unittest.TestCase):
    def setUp(self):
        self.t = MtEnv()
        self.ctx = _ctx(self.t, Brain([_reply()]))
        madetools.make(self.ctx, "doubler", "d", "")

    def test_same_surface_auto_registers(self):
        code2 = GOOD_CODE.replace("n * 2", "n + n")
        ctx = _ctx(self.t, Brain([_reply(code=code2)]),
                   approve="denied")     # must NOT reach approval
        out = madetools.make(ctx, "doubler", "d", "")
        self.assertTrue(out["registered"], out)
        self.assertEqual(out["version"], 2)
        self.assertTrue(out["auto_registered"])
        # v2 now runs
        out = madetools.run(ctx, "doubler", {"n": 3})
        self.assertEqual(out["result"], {"doubled": 6.0})
        self.assertEqual(out["version"], 2)

    def test_manifest_change_requires_retire(self):
        ctx = _ctx(self.t, Brain([_reply(network=True)]))
        out = madetools.make(ctx, "doubler", "d", "")
        self.assertEqual(out["error"], "manifest_changed")
        self.assertIn("network", out["diff"])


class TestChatGating(unittest.TestCase):
    def test_tools_hidden_when_off(self):
        t = TmpEnv()
        out = chattools.prompt_lines(t.settings)
        for name in ("tool_make", "tool_run", "tool_list"):
            self.assertNotIn(f"- {name} ", out)

    def test_tools_listed_when_on(self):
        t = MtEnv()
        out = chattools.prompt_lines(t.settings)
        for name in ("tool_make", "tool_run", "tool_list"):
            self.assertIn(f"- {name} ", out)

    def test_fns_answer_off(self):
        t = TmpEnv()
        ctx = _ctx(t)
        out = chattools.run(ctx, "tool_list", {})
        self.assertEqual(out["error"], "not_configured")

    def test_policy_pure_tool_is_free(self):
        """a pure (network:false) made tool is a pal-owned write ->
        runs without approval even unattended."""
        from pa import policy
        t = MtEnv()
        ctx = _ctx(t, Brain([_reply()]))
        madetools.make(ctx, "doubler", "d", "")
        d = policy.decide(ctx.conn, "tool_run",
                          {"name": "doubler"}, unattended=True)
        self.assertEqual(d.verdict, "allow")

    def test_policy_network_tool_is_exec(self):
        from pa import policy
        t = MtEnv()
        ctx = _ctx(t, Brain([_reply(network=True)]))
        madetools.make(ctx, "doubler", "d", "")
        d = policy.decide(ctx.conn, "tool_run",
                          {"name": "doubler"})
        self.assertEqual(d.verdict, "approve")
        self.assertEqual(d.action_class, "exec")

    def test_approved_replay_registers(self):
        """unattended registration → job_action → Approve applies."""
        t = MtEnv()
        ctx = _ctx(t, Brain([_reply()]), unattended=True)
        out = madetools.make(ctx, "doubler", "d", "")
        self.assertTrue(out["queued"])
        self.assertIsNone(madetools._active(ctx.conn, "doubler"))
        # owner approves -> execute_approved -> register_approved
        row = ctx.conn.execute(
            "SELECT payload_json FROM approvals ORDER BY id DESC"
            " LIMIT 1").fetchone()
        payload = json.loads(row["payload_json"])
        out2 = chattools.execute_approved(
            ctx, "tool_register", payload["args"])
        self.assertTrue(out2["registered"], out2)
        self.assertIsNotNone(madetools._active(ctx.conn, "doubler"))


if __name__ == "__main__":
    unittest.main()
