"""Acceptance #2/#4: claiming, leases, heartbeat, resume, cancel."""

import datetime
import json
import os
import signal
import subprocess
import sys
import threading
import time
import unittest

from pa import db as dbmod, worker as workmod
from pa.util import now_iso, parse_iso, utcnow
from tests.helpers import Fixture, TmpEnv, wait_for


class KillSwitch(BaseException):
    """Simulates a hard kill: propagates out of the attempt without any
    cleanup, like SIGKILL."""


def make_worker(env, fixture, fetch_impl=None):
    return workmod.Worker(env.db_path, env.settings,
                          provider_factory=None, fetch_impl=fetch_impl)


class TestClaiming(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()
        self.env = TmpEnv(PA_OLLAMA_BASE_URL=self.fx.base_url)
        self.aid = self.env.add_assistant()

    def tearDown(self):
        self.fx.close()

    def test_queued_task_is_claimed(self):
        conn = self.env.conn()
        tid = workmod.create_task(conn, self.aid, "t", "do it")
        w = make_worker(self.env, self.fx)
        got = w.claim()
        self.assertIsNotNone(got)
        task, attempt = got
        self.assertEqual(task["id"], tid)
        self.assertEqual(attempt["state"], "active")
        self.assertEqual(attempt["lease_owner"], w.worker_id)
        self.assertEqual(conn.execute(
            "SELECT state FROM tasks WHERE id=?", (tid,)).fetchone()["state"],
            "running")

    def test_terminal_states_never_claimed(self):
        conn = self.env.conn()
        for st in ("succeeded", "failed", "cancelled"):
            tid = workmod.create_task(conn, self.aid, f"t-{st}", "x")
            conn.execute("UPDATE tasks SET state=? WHERE id=?", (st, tid))
        w = make_worker(self.env, self.fx)
        self.assertIsNone(w.claim())

    def test_expired_lease_is_reclaimed(self):
        conn = self.env.conn()
        tid = workmod.create_task(conn, self.aid, "t", "x")
        w1 = make_worker(self.env, self.fx, )
        task, old = w1.claim()
        # expire the lease on the active attempt
        past = (utcnow() - datetime.timedelta(seconds=5)).isoformat(
            timespec="milliseconds")
        conn.execute("UPDATE attempts SET lease_until=? WHERE id=?",
                     (past, old["id"]))
        w2 = workmod.Worker(self.env.db_path, self.env.settings)
        got = w2.claim()
        self.assertIsNotNone(got)
        task2, new_attempt = got
        self.assertEqual(task2["id"], tid)
        self.assertNotEqual(new_attempt["id"], old["id"])
        self.assertEqual(conn.execute(
            "SELECT state FROM attempts WHERE id=?",
            (old["id"],)).fetchone()["state"], "abandoned")

    def test_running_with_live_lease_not_reclaimed(self):
        conn = self.env.conn()
        tid = workmod.create_task(conn, self.aid, "t", "x")
        w1 = make_worker(self.env, self.fx)
        w1.claim()
        w2 = workmod.Worker(self.env.db_path, self.env.settings)
        self.assertIsNone(w2.claim())


class TestHeartbeat(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()
        # lease 1.0s, heartbeat 0.2s, provider call takes ~0.8s
        self.env = TmpEnv(PA_OLLAMA_BASE_URL=self.fx.base_url,
                          PA_LEASE_TTL_SECONDS="1.0",
                          PA_HEARTBEAT_SECONDS="0.2")
        self.aid = self.env.add_assistant()

    def tearDown(self):
        self.fx.close()

    def test_heartbeat_keeps_long_call_alive(self):
        def sleepy(handler):
            time.sleep(0.8)
            handler._json(200, self.fx.chat_text("slow done"))
        self.fx.push_chat(sleepy)
        conn = self.env.conn()
        tid = workmod.create_task(conn, self.aid, "t", "x")
        w = workmod.Worker(self.env.db_path, self.env.settings)
        leases = []

        def watch():
            c2 = dbmod.connect(self.env.db_path)
            for _ in range(30):
                a = c2.execute(
                    "SELECT lease_until FROM attempts WHERE task_id=?",
                    (tid,)).fetchone()
                if a:
                    leases.append(a["lease_until"])
                time.sleep(0.1)
            c2.close()

        t = threading.Thread(target=watch)
        t.start()
        w.run_once()
        t.join()
        self.assertGreater(len(leases), 1)
        # the lease visibly advanced while the call was in flight
        self.assertGreater(parse_iso(leases[-1]), parse_iso(leases[0]))
        st = conn.execute("SELECT state FROM tasks WHERE id=?",
                          (tid,)).fetchone()["state"]
        self.assertEqual(st, "succeeded")


class TestResume(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()
        self.env = TmpEnv(PA_OLLAMA_BASE_URL=self.fx.base_url,
                          PA_LEASE_TTL_SECONDS="1.2")
        self.aid = self.env.add_assistant()

    def tearDown(self):
        self.fx.close()

    def test_crash_mid_step_resume_no_duplication(self):
        """Turn 1: model asks for fetch_url. The fetch crashes hard.
        Lease expires, a second worker resumes: the done fetch result is
        replayed, the provider is not asked again for turn 1's call, and
        the task finishes once."""
        fetched = []

        def crashing_fetch(url, settings=None, **kw):
            fetched.append(url)
            raise KillSwitch()

        conn = self.env.conn()
        tid = workmod.create_task(conn, self.aid, "t", "research x")
        # script: turn1 -> tool_call fetch; turn2 -> finish with claim
        self.fx.push_chat(self.fx.chat_tool(
            "fetch_url", {"url": "http://a.test/x"}, call_id="c1"))
        # turn2's response is pushed after we know the fetch id; use a
        # callable that replies when hit (its own conn: fixture thread)
        def turn2(handler):
            c2 = dbmod.connect(self.env.db_path)
            fid = c2.execute(
                "SELECT id FROM fetches WHERE task_id=?", (tid,)
            ).fetchone()["id"]
            c2.close()
            handler._json(200, self.fx.chat_tool(
                "finish", {"claims": [{"text": "x is y",
                                       "source_ids": [fid]}],
                           "summary": "ok"}, call_id="c2"))
        self.fx.push_chat(turn2)

        w1 = workmod.Worker(self.env.db_path, self.env.settings,
                            fetch_impl=crashing_fetch)
        with self.assertRaises(KillSwitch):
            w1.run_once()
        # attempt is still 'active' with a dying lease; expire it
        conn.execute(
            "UPDATE attempts SET lease_until=? WHERE state='active'",
            ((utcnow() - datetime.timedelta(seconds=1))
             .isoformat(timespec="milliseconds"),))

        # fetch_url is read-only, so the orphaned (started-not-done) step
        # is allowed to re-run on resume - with a working fetch this time.
        def real_fetch(url, settings=None, **kw):
            fetched.append(url)
            return {"text": "page body about x", "content_type": "text/plain",
                    "title": "t", "final_url": url, "status": 200}
        w2 = workmod.Worker(self.env.db_path, self.env.settings,
                            fetch_impl=real_fetch)
        w2.run_once()

        task = conn.execute("SELECT * FROM tasks WHERE id=?",
                            (tid,)).fetchone()
        self.assertEqual(task["state"], "succeeded")
        # exactly one successful fetch row
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) c FROM fetches WHERE task_id=? AND"
            " status='fetched'", (tid,)).fetchone()["c"], 1)
        # turn-1's model response came from the ledger: the provider saw
        # exactly 2 calls total (turn1 by w1, turn2 by w2), never a repeat
        self.assertEqual(len(self.fx.calls), 2)
        # no two DONE steps share a request hash - done work is never
        # recorded as if it ran twice
        rows = conn.execute(
            "SELECT request_hash, COUNT(*) c FROM steps WHERE task_id=? "
            "AND status='done' GROUP BY request_hash HAVING c>1",
            (tid,)).fetchall()
        self.assertEqual(rows, [])
        # the crashed tool step is visible as aborted, not silently lost
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) c FROM steps WHERE task_id=? AND"
            " status='aborted'", (tid,)).fetchone()["c"], 1)


class TestCancel(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()
        self.env = TmpEnv(PA_OLLAMA_BASE_URL=self.fx.base_url)
        self.aid = self.env.add_assistant()

    def tearDown(self):
        self.fx.close()

    def test_cancel_during_fetch(self):
        """Cancel lands mid-fetch: the task ends cancelled, no further
        steps are written, pending approvals become void."""
        gate = threading.Event()
        release = threading.Event()

        def slow_fetch(url, settings=None, **kw):
            gate.set()
            release.wait(10)
            return {"text": "late body", "content_type": "text/plain",
                    "title": "t", "final_url": url, "status": 200}

        conn = self.env.conn()
        tid = workmod.create_task(conn, self.aid, "t", "x")
        self.fx.push_chat(self.fx.chat_tool(
            "fetch_url", {"url": "http://a.test/x"}, call_id="c1"))
        self.fx.push_chat(self.fx.chat_tool(
            "propose_memory", {"kind": "memory",
                               "content": "note that must void"},
            call_id="c2"))
        self.fx.push_chat(self.fx.chat_text("late answer"))

        w = workmod.Worker(self.env.db_path, self.env.settings,
                           fetch_impl=slow_fetch)
        t = threading.Thread(target=w.run_once)
        t.start()
        self.assertTrue(gate.wait(5))
        self.assertTrue(workmod.request_cancel(conn, tid))
        release.set()
        t.join(10)
        task = conn.execute("SELECT * FROM tasks WHERE id=?",
                            (tid,)).fetchone()
        self.assertEqual(task["state"], "cancelled")
        self.assertEqual(task["error_kind"], "cancelled")
        steps = conn.execute(
            "SELECT COUNT(*) c FROM steps WHERE task_id=?",
            (tid,)).fetchone()["c"]
        # only the model call + the in-flight fetch step; nothing after
        self.assertLessEqual(steps, 2)
        # any pending approvals voided
        pend = conn.execute(
            "SELECT COUNT(*) c FROM approvals WHERE task_id=? AND"
            " status='pending'", (tid,)).fetchone()["c"]
        self.assertEqual(pend, 0)

    def test_cancel_queued_task(self):
        conn = self.env.conn()
        tid = workmod.create_task(conn, self.aid, "t", "x")
        self.assertTrue(workmod.request_cancel(conn, tid))
        self.assertEqual(conn.execute(
            "SELECT state FROM tasks WHERE id=?", (tid,)).fetchone()["state"],
            "cancelled")


class TestSubprocessKill(unittest.TestCase):
    """A real SIGKILL mid-step, then a fresh worker resumes."""

    def test_sigkill_then_resume(self):
        fx = Fixture()
        env = TmpEnv(PA_OLLAMA_BASE_URL=fx.base_url,
                     PA_LEASE_TTL_SECONDS="2.0",
                     PA_HEARTBEAT_SECONDS="0.3",
                     PA_WORKER_POLL_SECONDS="0.1")
        aid = env.add_assistant()
        conn = env.conn()
        tid = workmod.create_task(conn, aid, "t", "x")

        # response 1 hangs forever: the worker is killed while inside it
        hung = threading.Event()

        def hang(handler):
            hung.wait(30)
            handler._json(200, fx.chat_text("never"))

        fx.push_chat(hang)
        fx.push_chat(fx.chat_tool(
            "finish", {"claims": [{"text": "resumed answer",
                                   "source_ids": []}],
                       "summary": "ok"}, call_id="c2"))

        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        e = dict(os.environ)
        e.update(env.env)
        e["PYTHONPATH"] = repo
        e["PA_ENV"] = "test"
        proc = subprocess.Popen(
            [sys.executable, "-m", "pa", "worker"],
            cwd=repo, env=e,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            ok = wait_for(lambda: conn.execute(
                "SELECT COUNT(*) c FROM steps WHERE task_id=? AND"
                " status='started'", (tid,)).fetchone()["c"] >= 1,
                timeout=15)
            self.assertTrue(ok, "worker never started the model call")
            time.sleep(0.4)  # ensure it is inside the provider call
            proc.send_signal(signal.SIGKILL)
            proc.wait(10)
            # the killed attempt keeps 'active' until its lease expires;
            # expire it and resume with an in-process worker
            conn.execute(
                "UPDATE attempts SET lease_until=? WHERE state='active'",
                ((utcnow() - datetime.timedelta(seconds=1))
                 .isoformat(timespec="milliseconds"),))
            w = workmod.Worker(env.db_path, env.settings)
            w.run_once()
            task = conn.execute("SELECT * FROM tasks WHERE id=?",
                                (tid,)).fetchone()
            self.assertEqual(task["state"], "succeeded")
            # the in-flight model call's outcome was never committed, so
            # it was retried: provider saw 2 calls, and the ledger marks
            # the killed step 'aborted' (not 'done')
            self.assertEqual(len(fx.calls), 2)
            ab = conn.execute(
                "SELECT COUNT(*) c FROM attempts WHERE task_id=? AND"
                " state='abandoned'", (tid,)).fetchone()["c"]
            self.assertEqual(ab, 1)
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) c FROM steps WHERE task_id=? AND"
                " status='aborted'", (tid,)).fetchone()["c"], 1)
        finally:
            try:
                proc.kill()
            except Exception:
                pass
            hung.set()
            fx.close()


if __name__ == "__main__":
    unittest.main()
