"""phone capabilities — fake termux-* binaries on PATH
agent-side, and the chattools policy matrix pal-side.

No real Android here: tiny stub scripts stand in for termux-*; the
agent runs in-process on loopback like in test_node_agent.
"""

import http.client
import json
import os
import stat
import tempfile
import unittest

from pa import chattools, node_agent
from pa.node_proto import JOB_DONE
from tests.helpers import TmpEnv


STUBS = {
    "termux-notification": "#!/bin/sh\necho '{\"ok\":1}'\n",
    "termux-tts-speak": "#!/bin/sh\necho ok\n",
    "termux-sms-list": (
        "#!/bin/sh\necho '[{\"address\":\"+44\",\"date\":\"t1\",\"body\":\"hi\"},"
        "{\"address\":\"bank\",\"date\":\"t2\",\"body\":\"OTP 123\"}]'\n"),
    "termux-sms-send": "#!/bin/sh\necho sent\n",
    "termux-camera-photo": (
        "#!/bin/sh\nprintf 'FAKEJPEG' > \"$3\"\n"),
    "termux-location": (
        "#!/bin/sh\necho '{\"latitude\":51.5,\"longitude\":-0.1,"
        "\"accuracy\":20,\"provider\":\"gps\"}'\n"),
    "termux-battery-status": (
        "#!/bin/sh\necho '{\"percentage\":88,\"temperature\":33}'\n"),
    "termux-vibrate": "#!/bin/sh\necho ok\n",
}


class FakePhone:
    """node_agent in-process with stub termux binaries on PATH."""

    def __init__(self, token="ptok", stubs=True):
        self.bindir = tempfile.mkdtemp(prefix="pa-termux-")
        for name, script in (STUBS if stubs else {}).items():
            p = os.path.join(self.bindir, name)
            with open(p, "w") as f:
                f.write(script)
            os.chmod(p, 0o755)
        self._old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = self.bindir + os.pathsep + self._old_path
        self.dir = tempfile.mkdtemp(prefix="pa-node-")
        self.state = node_agent.NodeState(token, self.dir)
        self.srv = node_agent.serve(self.state, "127.0.0.1", 0)
        self.port = self.srv.server_address[1]
        import threading
        threading.Thread(target=self.srv.serve_forever,
                         daemon=True).start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()
        os.environ["PATH"] = self._old_path

    def submit(self, cap, args=None, timeout=30):
        conn = http.client.HTTPConnection("127.0.0.1", self.port,
                                          timeout=20)
        conn.request("POST", "/jobs",
                     body=json.dumps({"cap": cap, "args": args or {},
                                      "timeout": timeout}),
                     headers={"Authorization": "Bearer ptok",
                              "Content-Type": "application/json"})
        r = conn.getresponse()
        out = json.loads(r.read().decode())
        conn.close()
        return out

    def wait(self, jid, timeout=10):
        import time
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            conn = http.client.HTTPConnection("127.0.0.1", self.port,
                                              timeout=10)
            conn.request("GET", f"/jobs/{jid}",
                         headers={"Authorization": "Bearer ptok"})
            r = conn.getresponse()
            job = json.loads(r.read().decode())
            conn.close()
            if job.get("status") in ("done", "failed", "cancelled"):
                return job
            time.sleep(0.05)
        return job


def _env(phone_url, token="ptok", **kw):
    env = {"PA_NODES": f"phone={phone_url}",
           "PA_NODE_PHONE_TOKEN": token}
    env.update({k: str(v) for k, v in kw.items()})
    return TmpEnv(**env)


def _ctx(env, assistant=None, unattended=False, **kw):
    conn = env.conn()
    return chattools.ChatToolContext(
        conn, env.settings, assistant, "chan-1", env=env.env,
        unattended=unattended, **kw)


class TestTermuxCaps(unittest.TestCase):
    def setUp(self):
        self.phone = FakePhone()

    def tearDown(self):
        self.phone.close()

    def _run(self, cap, args=None):
        out = self.phone.submit(cap, args)
        return self.phone.wait(out["id"])

    def test_notify(self):
        job = self._run("notify", {"text": "hello"})
        self.assertEqual(job["status"], JOB_DONE)
        self.assertTrue(job["result"]["sent"])

    def test_sms_list(self):
        job = self._run("sms_list", {"max": 5})
        self.assertEqual(job["status"], JOB_DONE)
        self.assertEqual(len(job["result"]["messages"]), 2)
        self.assertEqual(job["result"]["messages"][0]["from"], "+44")

    def test_sms_send(self):
        job = self._run("sms_send", {"to": "1", "body": "x"})
        self.assertEqual(job["status"], JOB_DONE)

    def test_camera_photo(self):
        job = self._run("camera_photo", {"camera": "0"})
        self.assertEqual(job["status"], JOB_DONE)
        import base64
        self.assertEqual(base64.b64decode(
            job["result"]["data_b64"]), b"FAKEJPEG")

    def test_location(self):
        job = self._run("location")
        self.assertEqual(job["result"]["lat"], 51.5)

    def test_battery(self):
        job = self._run("battery")
        self.assertEqual(job["result"]["percentage"], 88)

    def test_missing_binary_fails_cleanly(self):
        phone = FakePhone(stubs=False)
        os.environ["PATH"] = phone.bindir   # no termux-* anywhere
        try:
            out = phone.submit("notify", {"text": "x"})
            job = phone.wait(out["id"])
            self.assertEqual(job["status"], "failed")
            self.assertIn("not installed", job["error"])
        finally:
            phone.close()


class TestPhoneTools(unittest.TestCase):
    def setUp(self):
        self.phone = FakePhone()
        self.env = _env(self.phone.url)

    def tearDown(self):
        self.phone.close()

    def test_tools_hidden_without_node(self):
        env = TmpEnv()       # no PA_NODES
        lines = chattools.prompt_lines(env.settings)
        self.assertNotIn("phone_sms_send", lines)

    def test_tools_listed_with_node(self):
        lines = chattools.prompt_lines(self.env.settings)
        self.assertIn("phone_sms_send", lines)
        self.assertIn("phone_locate", lines)

    def test_not_configured_when_called_anyway(self):
        env = TmpEnv()
        ctx = _ctx(env)
        out = chattools.run(ctx, "phone_notify", {"text": "x"})
        self.assertEqual(out["error"], "not_configured")

    def test_notify_free_and_runs(self):
        ctx = _ctx(self.env)
        out = chattools.run(ctx, "phone_notify", {"text": "hi"})
        self.assertEqual(out["status"], "done")
        self.assertTrue(out["result"]["sent"])

    def test_sms_send_needs_owner(self):
        """the owner's SIM: approve every time — even unattended with
        an allow rule (sends can't be allow-ruled at all)."""
        ctx = _ctx(self.env,
                   request_approval=lambda *a: "denied")
        out = chattools.run(ctx, "phone_sms_send",
                            {"to": "1", "body": "x"})
        self.assertEqual(out["error"], "not_approved")

    def test_sms_send_after_approval(self):
        ctx = _ctx(self.env,
                   request_approval=lambda *a: "approved")
        out = chattools.run(ctx, "phone_sms_send",
                            {"to": "1", "body": "x"})
        self.assertEqual(out["status"], "done")

    def test_photo_sensitive_gate(self):
        ctx = _ctx(self.env,
                   request_approval=lambda *a: "denied")
        out = chattools.run(ctx, "phone_photo", {})
        self.assertEqual(out["error"], "not_approved")

    def test_photo_after_approval_attaches(self):
        posted = {}
        ctx = _ctx(self.env,
                   request_approval=lambda *a: "approved",
                   post_file=lambda ch, f, m, d: posted.update(
                       {"f": f, "d": d}) or True)
        out = chattools.run(ctx, "phone_photo", {"camera": "0"})
        self.assertEqual(out["attached"], posted["f"])
        self.assertEqual(posted["d"], b"FAKEJPEG")

    def test_sms_read_attended_free(self):
        ctx = _ctx(self.env)
        out = chattools.run(ctx, "phone_sms_read", {"max": 3})
        self.assertIn("messages", json.dumps(out))

    def test_sms_read_unattended_queues(self):
        """job turns may not read the owner's inbox unless the flag is
        on — they queue an approval instead of running."""
        ctx = _ctx(self.env, unattended=True)
        out = chattools.run(ctx, "phone_sms_read", {})
        self.assertTrue(out.get("queued"))

    def test_sms_read_unattended_flag_allows(self):
        env = _env(self.phone.url,
                   PA_NODE_SMS_READ_UNATTENDED="true")
        ctx = _ctx(env, unattended=True)
        out = chattools.run(ctx, "phone_sms_read", {})
        self.assertIn("messages", json.dumps(out))

    def test_locate_gated(self):
        ctx = _ctx(self.env,
                   request_approval=lambda *a: "approved")
        out = chattools.run(ctx, "phone_locate", {})
        self.assertEqual(out["result"]["lat"], 51.5)


class TestPolicyMatrix(unittest.TestCase):
    """cap_decision mirrors the chattools floor: who may do what."""

    def setUp(self):
        self.phone = FakePhone()
        self.env = _env(self.phone.url)

    def tearDown(self):
        self.phone.close()

    def _d(self, cap, **kw):
        conn = self.env.conn()
        from pa import nodes
        return nodes.cap_decision(conn, cap, {}, **kw)

    def test_notify_free(self):
        self.assertEqual(self._d("notify").verdict, "allow")
        self.assertEqual(self._d("tts_speak").verdict, "allow")

    def test_sms_send_hard(self):
        self.assertEqual(self._d("sms_send").verdict, "approve")
        self.assertEqual(
            self._d("sms_send", unattended=True).verdict, "pending")

    def test_photo_locate_sensitive(self):
        self.assertEqual(self._d("camera_photo").verdict, "approve")
        self.assertEqual(self._d("location").verdict, "approve")

    def test_battery_free(self):
        self.assertEqual(self._d("battery").verdict, "allow")

    def test_sms_list_attended_free(self):
        self.assertEqual(self._d("sms_list").verdict, "allow")


if __name__ == "__main__":
    unittest.main()
