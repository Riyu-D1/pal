"""Task worker: claim, heartbeat, execute, terminal states.

One worker process/thread owns a claimed task via a lease. The lease is
renewed while network calls are in flight; if renewal fails because the
attempt was reclaimed, the worker stops and writes nothing more.

Claim (single BEGIN IMMEDIATE):
- pick a queued task, or a running task whose ACTIVE attempt's lease has
  expired; terminal tasks are never picked;
- mark the expired attempt abandoned and create a new attempt.
"""

import datetime
import logging
import threading
import time

from . import approvals, db as dbmod, health as healthmod, \
    provider as provmod, scheduler
from .evolve import incidents as incidentsmod
from .agent import (AbortAttempt, AgentRunner, Cancelled, DeadlineReached,
                    OverBudget)
from .pacer import Pacer, QuotaExhausted
from .tools import WaitForApproval
from .util import json_dumps, new_id, now_iso, parse_iso, utcnow

log = logging.getLogger("pa.worker")

TERMINAL = ("succeeded", "failed", "cancelled")


def _lease_deadline(ttl):
    return (utcnow() + datetime.timedelta(seconds=ttl)).isoformat(
        timespec="milliseconds")


class Worker:
    def __init__(self, db_path: str, settings, provider_factory=None,
                 fetch_impl=None, search_impl=None, worker_id=None):
        self.db_path = db_path
        self.s = settings
        self.worker_id = worker_id or new_id(8)
        self._provider_factory = provider_factory
        self._fetch = fetch_impl
        self._search = search_impl
        self.conn = dbmod.connect(db_path)
        self.pacer = Pacer(settings, self.conn)
        self._hb_stop = threading.Event()
        self._lost = threading.Event()

    # -- claiming ------------------------------------------------------------
    def claim(self):
        """Return (task, attempt) or None. Single BEGIN IMMEDIATE txn."""
        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT * FROM tasks WHERE state='queued' ORDER BY id "
                "LIMIT 1").fetchone()
            if row is None:
                row = conn.execute(
                    "SELECT t.* FROM tasks t JOIN attempts a ON "
                    "a.id=t.active_attempt_id WHERE t.state='running' "
                    "AND a.state='active' AND a.lease_until < ? "
                    "ORDER BY t.id LIMIT 1", (now_iso(),)).fetchone()
                if row is None:
                    conn.execute("COMMIT")
                    return None
                conn.execute(
                    "UPDATE attempts SET state='abandoned', ended_at=? "
                    "WHERE id=?",
                    (now_iso(), row["active_attempt_id"]))
            n = conn.execute(
                "SELECT COALESCE(MAX(n),0)+1 n FROM attempts WHERE task_id=?",
                (row["id"],)).fetchone()["n"]
            cur = conn.execute(
                "INSERT INTO attempts(task_id, n, state, lease_owner,"
                " lease_until, started_at) VALUES(?,?,'active',?,?,?)",
                (row["id"], n, self.worker_id,
                 _lease_deadline(self.s.lease_ttl_seconds), now_iso()))
            attempt_id = cur.lastrowid
            conn.execute(
                "UPDATE tasks SET state='running', active_attempt_id=?,"
                " updated_at=? WHERE id=?",
                (attempt_id, now_iso(), row["id"]))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        task = conn.execute("SELECT * FROM tasks WHERE id=?",
                            (row["id"],)).fetchone()
        attempt = conn.execute("SELECT * FROM attempts WHERE id=?",
                               (attempt_id,)).fetchone()
        return task, attempt

    # -- heartbeat -------------------------------------------------------------
    def _heartbeat(self, attempt_id):
        stop = self._hb_stop
        conn = dbmod.connect(self.db_path)
        try:
            while not stop.is_set():
                time.sleep(self.s.heartbeat_seconds)
                if stop.is_set():
                    return
                try:
                    cur = conn.execute(
                        "UPDATE attempts SET lease_until=? WHERE id=? AND "
                        "state='active' AND lease_owner=?",
                        (_lease_deadline(self.s.lease_ttl_seconds),
                         attempt_id, self.worker_id))
                    if cur.rowcount == 0:
                        self._lost.set()
                        return
                except Exception:
                    log.exception("heartbeat failed")
                    self._lost.set()
                    return
        finally:
            conn.close()

    # -- terminal writes --------------------------------------------------------
    def _finish_task(self, task_id, attempt_id, state, error_kind=None,
                     error_detail=None, reset_at=None, result=None):
        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "UPDATE tasks SET state=?, error_kind=?, error_detail=?,"
                " reset_at=?, result_json=?, updated_at=? WHERE id=?",
                (state, error_kind, (error_detail or "")[:500], reset_at,
                 json_dumps(result) if result is not None else None,
                 now_iso(), task_id))
            conn.execute(
                "UPDATE attempts SET state=?, ended_at=?, error=? WHERE id=?",
                ("succeeded" if state == "succeeded" else
                 "cancelled" if state == "cancelled" else "failed",
                 now_iso(), (error_detail or "")[:500], attempt_id))
            conn.execute("COMMIT")
            if state == "failed":
                try:
                    kind = ("not_configured"
                            if error_kind == "provider_not_configured"
                            else (error_kind or "exception"))
                    incidentsmod.record(
                        conn, "worker", kind,
                        summary=(error_detail or "")[:200],
                        detail=error_detail or "", task_id=task_id)
                except Exception:
                    pass            # never let capture break a finish
        except Exception:
            conn.execute("ROLLBACK")
            raise

    def _wait_task(self, task_id, attempt_id):
        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "UPDATE tasks SET state='waiting_approval', updated_at=? "
                "WHERE id=?", (now_iso(), task_id))
            conn.execute(
                "UPDATE attempts SET state='ended_waiting', ended_at=? "
                "WHERE id=?", (now_iso(), attempt_id))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    def _cancel_task(self, task_id, attempt_id):
        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            approvals.void_for_task(conn, task_id)
            conn.execute(
                "UPDATE tasks SET state='cancelled', error_kind='cancelled',"
                " updated_at=? WHERE id=?", (now_iso(), task_id))
            conn.execute(
                "UPDATE attempts SET state='cancelled', ended_at=? WHERE id=?",
                (now_iso(), attempt_id))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    # -- one attempt ------------------------------------------------------------
    def run_attempt(self, task, attempt):
        self._lost.clear()
        self._hb_stop.clear()
        hb = threading.Thread(target=self._heartbeat,
                              args=(attempt["id"],), daemon=True)
        hb.start()
        try:
            provider = self._provider(task)
            runner = AgentRunner(
                self.conn, self.s, provider, self.pacer,
                fetch_impl=self._fetch, search_impl=self._search,
                lease_lost=self._lost.is_set)
            result = runner.run(task, attempt)
            self._finish_task(task["id"], attempt["id"], "succeeded",
                              result=result)
            log.info("task %s succeeded", task["id"])
        except AbortAttempt:
            log.warning("task %s attempt %s lost lease; leaving untouched",
                        task["id"], attempt["id"])
        except WaitForApproval:
            self._wait_task(task["id"], attempt["id"])
            log.info("task %s waiting on owner approval", task["id"])
        except Cancelled:
            self._cancel_task(task["id"], attempt["id"])
            log.info("task %s cancelled", task["id"])
        except DeadlineReached:
            self._finish_task(task["id"], attempt["id"], "failed",
                              "deadline", "task deadline reached")
        except OverBudget as e:
            self._finish_task(task["id"], attempt["id"], "failed",
                              "over_budget", str(e))
        except QuotaExhausted as e:
            self._finish_task(task["id"], attempt["id"], "failed",
                              "quota_exceeded", str(e), reset_at=e.reset_at)
        except provmod.ProviderAuthError as e:
            self._finish_task(task["id"], attempt["id"], "failed",
                              "provider_auth", str(e))
        except provmod.ProviderNotConfigured as e:
            self._finish_task(task["id"], attempt["id"], "failed",
                              "provider_not_configured", str(e))
        except provmod.ProviderError as e:
            self._finish_task(task["id"], attempt["id"], "failed",
                              "provider_error", str(e))
        except Exception as e:
            log.exception("task %s failed", task["id"])
            try:
                self._finish_task(task["id"], attempt["id"], "failed",
                                  "internal", str(e))
            except Exception:
                log.exception("could not record failure for task %s",
                              task["id"])
        finally:
            self._hb_stop.set()

    def _provider(self, task):
        assistant = self.conn.execute(
            "SELECT * FROM assistants WHERE id=?",
            (task["assistant_id"],)).fetchone()
        if self._provider_factory:
            return self._provider_factory(assistant)
        return provmod.provider_for(
            self.s, assistant["provider_host"], assistant["provider_model"])

    # -- outer loop --------------------------------------------------------------
    def run_once(self) -> bool:
        """Process one claimable task. Returns True if work happened."""
        try:
            scheduler.tick(self.conn, self.s)
        except Exception:
            log.exception("scheduler tick failed")
        got = self.claim()
        if not got:
            return False
        task, attempt = got
        self.run_attempt(task, attempt)
        return True

    def run_forever(self):
        log.info("worker %s starting", self.worker_id)
        last_beat = 0.0
        while True:
            # liveness stamp for deep /healthz
            # (PA_HEALTH_EXPECT_WORKER); at most one write per
            # heartbeat interval
            if time.monotonic() - last_beat >= \
                    self.s.heartbeat_seconds:
                healthmod.beat(self.conn)
                last_beat = time.monotonic()
            try:
                did = self.run_once()
            except Exception:
                log.exception("worker loop error")
                did = False
            if not did:
                time.sleep(self.s.worker_poll_seconds)


# -- task lifecycle helpers used by the web app -------------------------------

def create_task(conn, assistant_id, title, body, private=False,
                deadline_seconds=600):
    now = now_iso()
    dl = (utcnow() + datetime.timedelta(seconds=deadline_seconds)).isoformat(
        timespec="milliseconds")
    cur = conn.execute(
        "INSERT INTO tasks(assistant_id, title, body, private, state,"
        " deadline_at, created_at, updated_at)"
        " VALUES(?,?,?,?,'queued',?,?,?)",
        (assistant_id, title[:200], body[:16000], int(bool(private)),
         dl, now, now))
    return cur.lastrowid


def request_cancel(conn, task_id) -> bool:
    """Set the cancel flag; worker checks between steps. Returns True if
    the task transitioned to cancelled now (queued/waiting) or will do so
    at the next step boundary (running)."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        t = conn.execute("SELECT * FROM tasks WHERE id=?",
                         (task_id,)).fetchone()
        if t is None:
            conn.execute("ROLLBACK")
            return False
        if t["state"] in TERMINAL:
            conn.execute("ROLLBACK")
            return False
        conn.execute(
            "UPDATE tasks SET cancel_requested=1, updated_at=? WHERE id=?",
            (now_iso(), task_id))
        if t["state"] in ("queued", "waiting_approval"):
            approvals.void_for_task(conn, task_id)
            conn.execute(
                "UPDATE tasks SET state='cancelled',"
                " error_kind='cancelled', updated_at=? WHERE id=?",
                (now_iso(), task_id))
        conn.execute("COMMIT")
        return True
    except Exception:
        conn.execute("ROLLBACK")
        raise


def request_retry(conn, task_id, deadline_seconds=600) -> bool:
    """Queue a terminal task for a fresh attempt. Done steps are reused
    via request-hash replay; applied effects are never re-applied."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        t = conn.execute("SELECT * FROM tasks WHERE id=?",
                         (task_id,)).fetchone()
        if t is None or t["state"] not in TERMINAL:
            conn.execute("ROLLBACK")
            return False
        dl = (utcnow() + datetime.timedelta(seconds=deadline_seconds)
              ).isoformat(timespec="milliseconds")
        conn.execute(
            "UPDATE tasks SET state='queued', cancel_requested=0,"
            " error_kind=NULL, error_detail=NULL, reset_at=NULL,"
            " deadline_at=?, updated_at=? WHERE id=?",
            (dl, now_iso(), task_id))
        conn.execute("COMMIT")
        return True
    except Exception:
        conn.execute("ROLLBACK")
        raise


def resume_waiting(conn, approval_row) -> None:
    """After an owner decides a search_query approval, requeue the task."""
    if approval_row["kind"] != "search_query" or not approval_row["task_id"]:
        return
    conn.execute(
        "UPDATE tasks SET state='queued', updated_at=? WHERE id=? AND"
        " state='waiting_approval'",
        (now_iso(), approval_row["task_id"]))
