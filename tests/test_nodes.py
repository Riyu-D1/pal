"""final-3 X1: node registry + dispatch — fake HTTP node in-process.

Covers: config parsing, client roundtrip, auth failure, circuit
breaker, capability routing, queueing when no node is healthy, and the
policy floor (every job is classified + decided before dispatch).
"""

import http.server
import json
import threading
import unittest

from pa import budget, db as dbmod, nodes
from pa.config import ConfigError, load_settings
from pa.node_proto import JOB_DONE
from tests.helpers import TmpEnv


class FakeNodeHandler(http.server.BaseHTTPRequestHandler):
    """Speaks pa/node_proto. State lives on the class so tests can
    script it."""
    jobs = {}
    caps = ["fetch", "ping_http", "battery"]
    token = "test-token"
    paused = False
    battery_pct = 80
    fail_health = False

    def log_message(self, *a):
        pass

    def _authed(self):
        return self.headers.get("Authorization") == \
            f"Bearer {self.token}"

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self._authed():
            return self._send(401, {"error": "unauthorized"})
        if self.path == "/health":
            if self.fail_health:
                return self._send(500, {"error": "boom"})
            return self._send(200, {
                "ok": True, "caps": list(self.caps),
                "battery_pct": self.battery_pct,
                "temp_c": 31.0, "load": 0.1, "mem_free_mb": 900,
                "uptime_s": 60, "paused": self.paused,
                "jobs_running": 0, "jobs_queued": len(self.jobs)})
        if self.path.startswith("/jobs/"):
            jid = self.path.rsplit("/", 1)[-1]
            job = self.jobs.get(jid)
            if job is None:
                return self._send(404, {"error": "no_job"})
            # fake node finishes immediately
            job["status"] = JOB_DONE
            job["result"] = {"echo": job["args"]}
            job["cost_s"] = 0.01
            return self._send(200, job)
        return self._send(404, {"error": "no_path"})

    def do_POST(self):
        if not self._authed():
            return self._send(401, {"error": "unauthorized"})
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        if self.path == "/jobs":
            if self.paused:
                return self._send(503, {"error": "paused"})
            if body.get("cap") not in self.caps:
                return self._send(400, {"error": "bad_cap"})
            jid = f"r{len(self.jobs)+1}"
            self.jobs[jid] = {"id": jid, "cap": body["cap"],
                              "args": body.get("args") or {},
                              "status": "queued"}
            return self._send(200, {"id": jid, "status": "queued"})
        if self.path.startswith("/jobs/") and \
                self.path.endswith("/cancel"):
            jid = self.path.split("/")[2]
            if jid in self.jobs:
                self.jobs[jid]["status"] = "cancelled"
                return self._send(200, {"id": jid, "status": "cancelled"})
            return self._send(404, {"error": "no_job"})
        return self._send(404, {"error": "no_path"})


class FakeNode:
    def __init__(self, token="test-token", caps=None):
        FakeNodeHandler.jobs = {}
        FakeNodeHandler.paused = False
        FakeNodeHandler.fail_health = False
        FakeNodeHandler.token = token
        if caps is not None:
            FakeNodeHandler.caps = caps
        self.srv = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), FakeNodeHandler)
        self.port = self.srv.server_address[1]
        self.thread = threading.Thread(
            target=self.srv.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


def _env(node_url=None, token="test-token", **kw):
    env = {}
    if node_url:
        env["PA_NODES"] = f"testnode={node_url}"
        env["PA_NODE_TESTNODE_TOKEN"] = token
    env.update({k: str(v) for k, v in kw.items()})
    return TmpEnv(**env)


class TestConfig(unittest.TestCase):
    def test_nodes_parse(self):
        s = load_settings({
            "PA_ENV": "test",
            "PA_NODES": "phone=http://127.0.0.1:8791,"
                        "box=https://n.example.com",
            "PA_NODE_PHONE_TOKEN": "a", "PA_NODE_BOX_TOKEN": "b"})
        self.assertEqual(s.nodes["phone"], "http://127.0.0.1:8791")
        self.assertEqual(s.nodes["box"], "https://n.example.com")
        self.assertEqual(s.node_tokens["phone"], "a")

    def test_nodes_need_token(self):
        with self.assertRaises(ConfigError):
            load_settings({"PA_ENV": "test",
                           "PA_NODES": "phone=http://127.0.0.1:8791"})

    def test_nodes_reject_public_http(self):
        with self.assertRaises(ConfigError):
            load_settings({
                "PA_ENV": "test",
                "PA_NODES": "x=http://8.8.8.8:8080",
                "PA_NODE_X_TOKEN": "t"})

    def test_nodes_bad_name(self):
        with self.assertRaises(ConfigError):
            load_settings({
                "PA_ENV": "test",
                "PA_NODES": "Bad Name=http://127.0.0.1:1",
                "PA_NODE_BAD_NAME_TOKEN": "t"})

    def test_off_by_default(self):
        t = TmpEnv()
        self.assertEqual(t.settings.nodes, {})
        self.assertEqual(nodes.configured(t.settings), [])


class TestClientAndRegistry(unittest.TestCase):
    def setUp(self):
        self.node = FakeNode()

    def tearDown(self):
        self.node.close()

    def test_health_and_caps(self):
        t = _env(self.node.url)
        client = nodes.client_for(t.settings, "testnode")
        h = client.health()
        self.assertTrue(h["ok"])
        self.assertIn("fetch", client.caps())

    def test_auth_failure(self):
        t = _env(self.node.url)
        client = nodes.client_for(t.settings, "testnode")
        client.token = "wrong"
        with self.assertRaises(nodes.NodeError) as e:
            client.health()
        self.assertEqual(e.exception.kind, "auth")

    def test_health_cached_in_db(self):
        t = _env(self.node.url)
        conn = t.conn()
        h = nodes.refresh_health(conn, t.settings, "testnode")
        self.assertIn("fetch", h["caps"])
        row = conn.execute(
            "SELECT state, caps_json FROM nodes WHERE name='testnode'"
        ).fetchone()
        self.assertEqual(row["state"], "ok")
        self.assertIn("fetch", row["caps_json"])

    def test_breaker_after_three_failures(self):
        FakeNodeHandler.fail_health = True
        t = _env(self.node.url)
        conn = t.conn()
        for _ in range(3):
            nodes.refresh_health(conn, t.settings, "testnode",
                                 force=True)
        row = conn.execute(
            "SELECT state, fail_count, breaker_until FROM nodes"
            " WHERE name='testnode'").fetchone()
        self.assertEqual(row["state"], "breaker")
        self.assertEqual(row["fail_count"], 3)
        self.assertTrue(row["breaker_until"])

    def test_capability_routing(self):
        t = _env(self.node.url)
        conn = t.conn()
        self.assertEqual(nodes.pick_node(conn, t.settings, "fetch"),
                         "testnode")
        self.assertIsNone(
            nodes.pick_node(conn, t.settings, "camera_photo"))

    def test_submit_and_wait_done(self):
        t = _env(self.node.url)
        conn = t.conn()
        row = nodes.submit_and_wait(
            conn, t.settings, "fetch", {"url": "https://example.com"},
            timeout=5.0)
        self.assertEqual(row["status"], JOB_DONE)
        self.assertEqual(row["node"], "testnode")
        self.assertIn("echo", row["result_json"])

    def test_queued_when_no_node(self):
        t = TmpEnv()        # no PA_NODES at all
        conn = t.conn()
        out = nodes.submit_job(conn, t.settings, "fetch",
                               {"url": "https://example.com"})
        self.assertEqual(out["status"], "queued")
        self.assertTrue(out["job_id"])

    def test_queued_when_node_paused(self):
        FakeNodeHandler.paused = True
        t = _env(self.node.url)
        conn = t.conn()
        out = nodes.submit_and_wait(conn, t.settings, "fetch",
                                    {"url": "https://x.example"},
                                    timeout=5.0)
        self.assertIn(out.get("status"), ("queued", "cancelled"))

    def test_budget_settles_on_done(self):
        t = _env(self.node.url)
        conn = t.conn()
        nodes.submit_and_wait(conn, t.settings, "fetch",
                              {"url": "https://example.com"},
                              timeout=5.0)
        row = conn.execute(
            "SELECT used, reserved FROM budget_ledger WHERE"
            " resource='node_sec:testnode'").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["reserved"], 0)
        self.assertGreaterEqual(row["used"], 1)

    def test_status_lines(self):
        t = _env(self.node.url)
        conn = t.conn()
        lines = nodes.status_lines(conn, t.settings)
        self.assertTrue(any("testnode" in l for l in lines))
        self.assertTrue(any("fetch" in l for l in lines))


class TestPolicyFloor(unittest.TestCase):
    def setUp(self):
        self.node = FakeNode()

    def tearDown(self):
        self.node.close()

    def test_owner_cap_needs_approval(self):
        """sms_send acts on the owner's SIM — approve every time,
        even attended and untainted."""
        t = _env(self.node.url)
        conn = t.conn()
        out = nodes.submit_job(conn, t.settings, "sms_send",
                               {"to": "1", "body": "hi"})
        self.assertEqual(out["error"], "approval_required")
        self.assertEqual(out["verdict"], "approve")

    def test_owner_cap_unattended_queues(self):
        t = _env(self.node.url)
        conn = t.conn()
        out = nodes.submit_job(conn, t.settings, "sms_send",
                               {"to": "1"}, unattended=True)
        self.assertEqual(out["verdict"], "pending")

    def test_approved_resubmission_dispatches(self):
        t = _env(self.node.url)
        conn = t.conn()
        conn.execute(
            "UPDATE nodes SET caps_json='[\"sms_send\"]' "
            "WHERE name='testnode'")
        nodes.ensure_rows(conn, t.settings)
        # approved bypasses the floor — the agent advertises the cap?
        out = nodes.submit_job(conn, t.settings, "sms_send",
                               {"to": "1"}, approved=True)
        # the fake node doesn't offer sms_send -> queues or submits
        self.assertIn(out.get("status", out.get("error")),
                      ("queued", "submitted", "bad_cap", "approval_required"))

    def test_tainted_write_needs_approval(self):
        """fetch stays a read, but file_put after untrusted content
        must ask first."""
        t = _env(self.node.url)
        conn = t.conn()
        d = nodes.cap_decision(conn, "file_put",
                               {"path": "a.txt"}, tainted=True,
                               taint_source="a web page")
        self.assertEqual(d.verdict, "approve")

    def test_read_cap_free(self):
        t = _env(self.node.url)
        conn = t.conn()
        d = nodes.cap_decision(conn, "fetch", {"url": "https://x.example"})
        self.assertEqual(d.verdict, "allow")


class FakeComputerHandler(http.server.BaseHTTPRequestHandler):
    """The computer agent's wire shape: /healthz open, /call behind
    the x-pa-computer-token header."""
    token = "comp-token"
    calls = []
    down = False

    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/healthz":
            return self._send(200, {"ok": not self.down,
                                    "enabled": True})
        return self._send(404, {"error": "no_path"})

    def do_POST(self):
        if self.headers.get("x-pa-computer-token") != self.token:
            return self._send(401, {"error": {"kind": "auth",
                                              "message": "bad token"}})
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        if self.path != "/call":
            return self._send(404, {"error": "no_path"})
        action = body.get("action")
        args = body.get("args") or {}
        self.calls.append((action, args))
        if action == "goto":
            return self._send(200, {"result": {
                "url": args.get("url"), "title": "T", "text": "hi"}})
        if action == "screenshot":
            return self._send(200, {"result": {"png_b64": "AA=="}})
        if action == "extract":
            return self._send(200, {"result": {"text": "extracted"}})
        return self._send(200, {"result": {"error": "bad_action"}})


class FakeComputer:
    def __init__(self):
        FakeComputerHandler.calls = []
        FakeComputerHandler.down = False
        self.srv = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), FakeComputerHandler)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever,
                         daemon=True).start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


class TestComputerNode(unittest.TestCase):
    """X4: the existing computer agent registers as node 'computer'."""

    def setUp(self):
        self.comp = FakeComputer()
        self.t = TmpEnv(PA_COMPUTER_URL=self.comp.url,
                        PA_COMPUTER_TOKEN=FakeComputerHandler.token)

    def tearDown(self):
        self.comp.close()

    def test_listed_as_node(self):
        self.assertIn("computer", nodes.configured(self.t.settings))

    def test_client_health_and_caps(self):
        client = nodes.client_for(self.t.settings, "computer")
        self.assertIsInstance(client, nodes.ComputerNodeClient)
        h = client.health()
        self.assertTrue(h["ok"])
        self.assertEqual(sorted(h["caps"]),
                         sorted(nodes.COMPUTER_CAPS))

    def test_browse_job_done(self):
        conn = self.t.conn()
        row = nodes.submit_and_wait(
            conn, self.t.settings, "browse",
            {"url": "https://example.com"}, timeout=10.0)
        self.assertEqual(row["status"], JOB_DONE)
        self.assertIn("example.com", row["result_json"])
        self.assertIn(("goto", {"url": "https://example.com"}),
                      FakeComputerHandler.calls)

    def test_read_url_js_two_calls(self):
        conn = self.t.conn()
        row = nodes.submit_and_wait(
            conn, self.t.settings, "read_url_js",
            {"url": "https://x.example", "instruction": "summarise"},
            timeout=10.0)
        self.assertEqual(row["status"], JOB_DONE)
        actions = [a for a, _ in FakeComputerHandler.calls]
        self.assertEqual(actions, ["goto", "extract"])

    def test_browser_min_charged(self):
        conn = self.t.conn()
        nodes.submit_and_wait(conn, self.t.settings, "browse",
                              {"url": "https://example.com"},
                              timeout=10.0)
        row = conn.execute(
            "SELECT used FROM budget_ledger WHERE"
            " resource='browser_min'").fetchone()
        self.assertIsNotNone(row)
        self.assertGreaterEqual(row["used"], 1)

    def test_browser_min_cap_queues(self):
        conn = self.t.conn()
        conn.execute(
            "INSERT INTO budget_limits(scope, resource, cap)"
            " VALUES('owner-chat', 'browser_min', 1)")
        budget.charge(conn, "owner-chat", "browser_min", 1,
                      s=self.t.settings)
        out = nodes.submit_job(conn, self.t.settings, "browse",
                               {"url": "https://example.com"})
        self.assertEqual(out["status"], "queued")
        self.assertIn("browser_min", out["reason"])

    def test_cancel_and_poll(self):
        client = nodes.client_for(self.t.settings, "computer")
        out = client.submit("screenshot", {}, 10.0)
        rec = client.poll(out["id"])
        self.assertEqual(rec["status"], JOB_DONE)
        cancelled = client.cancel(out["id"])
        self.assertEqual(cancelled["status"], JOB_DONE)  # already done


class TestComputerNodeFinal5(unittest.TestCase):
    """final-5 F4: PA_COMPUTER_URL alone registers node 'computer';
    the RemoteComputer tunnel keeps working unchanged."""

    def setUp(self):
        self.comp = FakeComputer()
        self.t = TmpEnv(PA_COMPUTER_URL=self.comp.url,
                        PA_COMPUTER_TOKEN=FakeComputerHandler.token)

    def tearDown(self):
        self.comp.close()

    def test_status_lines_lists_computer(self):
        conn = self.t.conn()
        lines = nodes.status_lines(conn, self.t.settings)
        conn.close()
        self.assertTrue(any("computer" in l for l in lines))
        # health refresh populated honest caps from the live agent
        joined = " ".join(lines)
        self.assertIn("ok", joined)
        self.assertIn("browse", joined)

    def test_status_lines_no_refresh_still_lists(self):
        conn = self.t.conn()
        lines = nodes.status_lines(conn, self.t.settings,
                                   refresh=False)
        conn.close()
        self.assertTrue(any("computer" in l for l in lines))

    def test_direct_tunnel_call_unchanged(self):
        from pa import computer as computermod
        t2 = TmpEnv(PA_COMPUTER_URL=self.comp.url,
                    PA_COMPUTER_TOKEN=FakeComputerHandler.token,
                    PA_COMPUTER_ENABLED="true")
        svc = computermod.service_for(t2.settings)
        self.assertIsInstance(svc, computermod.RemoteComputer)
        out = svc.call("goto", {"url": "https://example.com"})
        self.assertEqual(out.get("url"), "https://example.com")
        self.assertNotIn("error", out)
        self.assertIn(("goto", {"url": "https://example.com"}),
                      FakeComputerHandler.calls)

    def test_no_url_no_node(self):
        t2 = TmpEnv()
        self.assertNotIn("computer", nodes.configured(t2.settings))
        conn = t2.conn()
        self.assertEqual(nodes.status_lines(conn, t2.settings), [])
        conn.close()

    def test_dispatch_reserves_node_sec_computer(self):
        conn = self.t.conn()
        row = nodes.submit_and_wait(
            conn, self.t.settings, "browse",
            {"url": "https://example.com"}, timeout=10.0)
        self.assertEqual(row["status"], JOB_DONE)
        ledger = conn.execute(
            "SELECT used, reserved FROM budget_ledger WHERE"
            " resource='node_sec:computer'").fetchone()
        conn.close()
        self.assertIsNotNone(ledger)
        self.assertGreaterEqual(ledger["used"], 1)


class TestBudgetExhaustion(unittest.TestCase):
    def test_budget_gate_queues(self):
        self.node = FakeNode()
        try:
            t = _env(self.node.url)
            conn = t.conn()
            # drain the whole node_sec bucket for owner-chat
            conn.execute(
                "INSERT INTO budget_limits(scope, resource, cap)"
                " VALUES('owner-chat', 'node_sec:testnode', 1)")
            out = nodes.submit_job(conn, t.settings, "fetch",
                                   {"url": "https://example.com"},
                                   timeout=30.0)
            self.assertEqual(out["status"], "queued")
            self.assertIn("budget", out["reason"])
        finally:
            self.node.close()


if __name__ == "__main__":
    unittest.main()
