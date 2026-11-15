"""final-3 X2: the pa-node agent — in-process on a random port.

Covers auth (constant-time Bearer), the capability allowlist, file
jail traversal, private-IP fetch blocking, self-throttle on low
battery, the one-at-a-time job queue, and run_tool manifest gating.
The agent must be stdlib-only — a meta-test asserts it imports nothing
outside stdlib + pa.node_proto.
"""

import http.client
import json
import os
import tempfile
import time
import unittest

from pa import node_agent
from pa.node_proto import JOB_DONE


class RunningAgent:
    def __init__(self, token="tok123", **state_kw):
        self.dir = tempfile.mkdtemp(prefix="pa-node-test-")
        self.state = node_agent.NodeState(token, self.dir, **state_kw)
        self.srv = node_agent.serve(self.state, "127.0.0.1", 0)
        self.port = self.srv.server_address[1]
        import threading
        threading.Thread(target=self.srv.serve_forever,
                         daemon=True).start()

    def req(self, method, path, body=None, token="tok123"):
        conn = http.client.HTTPConnection("127.0.0.1", self.port,
                                          timeout=10)
        headers = {"Accept": "application/json"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        data = json.dumps(body).encode() if body is not None else None
        if data:
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body=data, headers=headers)
        r = conn.getresponse()
        raw = r.read()
        conn.close()
        try:
            return r.status, json.loads(raw.decode())
        except Exception:
            return r.status, {}

    def job_wait(self, job_id, timeout=10.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            _, job = self.req("GET", f"/jobs/{job_id}")
            if job.get("status") in ("done", "failed", "cancelled"):
                return job
            time.sleep(0.05)
        return job

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


class TestAuth(unittest.TestCase):
    def setUp(self):
        self.ag = RunningAgent()

    def tearDown(self):
        self.ag.close()

    def test_no_token_401(self):
        code, _ = self.ag.req("GET", "/health", token=None)
        self.assertEqual(code, 401)

    def test_wrong_token_401(self):
        code, _ = self.ag.req("GET", "/health", token="nope")
        self.assertEqual(code, 401)

    def test_health_ok(self):
        code, h = self.ag.req("GET", "/health")
        self.assertEqual(code, 200)
        self.assertTrue(h["ok"])
        self.assertIn("fetch", h["caps"])
        self.assertIn("mem_free_mb", h)


class TestAllowlist(unittest.TestCase):
    def setUp(self):
        self.ag = RunningAgent()

    def tearDown(self):
        self.ag.close()

    def _submit(self, cap, args=None, timeout=30):
        return self.ag.req("POST", "/jobs",
                           {"cap": cap, "args": args or {},
                            "timeout": timeout})

    def test_unknown_cap_rejected(self):
        code, out = self._submit("shell")
        self.assertEqual(code, 400)
        code, out = self._submit("exec", {"cmd": "id"})
        self.assertEqual(code, 400)

    def test_file_put_get_roundtrip(self):
        _, out = self._submit("file_put", {
            "path": "a/b.txt",
            "data_b64": "aGVsbG8="})
        job = self.ag.job_wait(out["id"])
        self.assertEqual(job["status"], JOB_DONE)
        _, out2 = self._submit("file_get", {"path": "a/b.txt"})
        job2 = self.ag.job_wait(out2["id"])
        self.assertEqual(job2["result"]["data_b64"], "aGVsbG8=")

    def test_traversal_blocked(self):
        for bad in ("../x", "a/../../x", "/etc/passwd",
                    "..%2Fx" if False else "a//../..//x"):
            _, out = self._submit("file_put", {
                "path": bad, "data_b64": "eA=="})
            job = self.ag.job_wait(out["id"])
            self.assertEqual(job["status"], "failed", bad)
            self.assertIn("bad path", job["error"])
        # nothing escaped the data dir
        parent = os.path.dirname(self.ag.dir)
        self.assertFalse(os.path.exists(
            os.path.join(parent, "x")))

    def test_file_get_missing(self):
        _, out = self._submit("file_get", {"path": "nope.txt"})
        job = self.ag.job_wait(out["id"])
        self.assertEqual(job["status"], "failed")

    def test_private_ip_fetch_blocked(self):
        for url in ("http://127.0.0.1/", "http://169.254.169.254/",
                    "http://10.0.0.1/", "file:///etc/passwd",
                    "http://[::1]/"):
            _, out = self._submit("fetch", {"url": url})
            job = self.ag.job_wait(out["id"])
            self.assertEqual(job["status"], "failed", url)
            self.assertTrue(
                "private" in job["error"]
                or "scheme" in job["error"]
                or "port" in job["error"], url)

    def test_rss_cap(self):
        _, out = self._submit("rss", {})
        job = self.ag.job_wait(out["id"])
        self.assertEqual(job["status"], JOB_DONE)
        self.assertIn("mem_free_mb", job["result"])


class TestThrottle(unittest.TestCase):
    def test_low_battery_pauses(self):
        ag = RunningAgent(battery_reader=lambda: 10)
        try:
            code, h = ag.req("GET", "/health")
            self.assertTrue(h["paused"])
            self.assertIn("battery", h["pause_reason"])
            code, out = ag.req("POST", "/jobs",
                               {"cap": "rss", "args": {}})
            self.assertEqual(code, 503)
        finally:
            ag.close()

    def test_hot_cpu_pauses(self):
        ag = RunningAgent(temp_reader=lambda: 55.0,
                          battery_reader=lambda: 90)
        try:
            code, h = ag.req("GET", "/health")
            self.assertTrue(h["paused"])
            code, out = ag.req("POST", "/jobs",
                               {"cap": "rss", "args": {}})
            self.assertEqual(code, 503)
        finally:
            ag.close()

    def test_normal_battery_runs(self):
        ag = RunningAgent(battery_reader=lambda: 90)
        try:
            code, out = ag.req("POST", "/jobs",
                               {"cap": "rss", "args": {}})
            self.assertEqual(code, 200)
            job = ag.job_wait(out["id"])
            self.assertEqual(job["status"], JOB_DONE)
        finally:
            ag.close()


class TestQueue(unittest.TestCase):
    def test_queue_full_429(self):
        # a cap that blocks lets the queue fill up
        import threading as _th
        ev = _th.Event()

        def slow(args, job, state):
            ev.wait(3)
            return {}

        node_agent.CAPS["__slow"] = slow
        try:
            ag = RunningAgent(max_queue=2)
            try:
                ag.req("POST", "/jobs", {"cap": "__slow", "args": {}})
                ag.req("POST", "/jobs", {"cap": "__slow", "args": {}})
                ag.req("POST", "/jobs", {"cap": "__slow", "args": {}})
                code, _ = ag.req("POST", "/jobs",
                                 {"cap": "__slow", "args": {}})
                self.assertEqual(code, 429)
            finally:
                ev.set()
                ag.close()
        finally:
            node_agent.CAPS.pop("__slow", None)


class TestRunTool(unittest.TestCase):
    def setUp(self):
        self.ag = RunningAgent()
        tdir = os.path.join(self.ag.dir, "tools")
        os.makedirs(tdir, exist_ok=True)
        with open(os.path.join(tdir, "dbl.py"), "w") as f:
            f.write("def run(args):\n    return args['n'] * 2\n")
        with open(os.path.join(tdir, "dbl.json"), "w") as f:
            json.dump({"node_ok": True, "network": False}, f)
        with open(os.path.join(tdir, "nokey.py"), "w") as f:
            f.write("def run(args):\n    return 1\n")
        with open(os.path.join(tdir, "nokey.json"), "w") as f:
            json.dump({"node_ok": False}, f)

    def tearDown(self):
        self.ag.close()

    def _run(self, name, args=None):
        _, out = self.ag.req("POST", "/jobs", {
            "cap": "run_tool",
            "args": {"name": name, "args": args or {}}})
        return self.ag.job_wait(out["id"], timeout=20)

    def test_tool_runs(self):
        job = self._run("dbl", {"n": 21})
        self.assertEqual(job["status"], JOB_DONE)
        self.assertEqual(job["result"], 42)

    def test_tool_not_node_ok(self):
        job = self._run("nokey")
        self.assertEqual(job["status"], "failed")
        self.assertIn("node_ok", job["error"])

    def test_tool_missing(self):
        job = self._run("nosuch")
        self.assertEqual(job["status"], "failed")


class TestStdlibOnly(unittest.TestCase):
    def test_agent_imports_are_stdlib(self):
        """node_agent must run on a bare Termux python — it may only
        import stdlib + pa.node_proto."""
        import ast
        path = os.path.join(os.path.dirname(node_agent.__file__),
                            "node_agent.py")
        tree = ast.parse(open(path).read())
        mods = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                mods.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    mods.add(node.module.split(".")[0])
        stdlib = set(getattr(__import__("sys"),
                             "stdlib_module_names", set()))
        bad = {m for m in mods
               if m not in stdlib and m not in ("pa", "node_proto")}
        self.assertEqual(bad, set())


if __name__ == "__main__":
    unittest.main()
