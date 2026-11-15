"""Approvals and effects.

The only agent write path: a tool creates an approval (pending); the
owner approves or denies it; applying an approval runs inside one
BEGIN IMMEDIATE transaction:

    INSERT effect row keyed by effect_key = approval_id
    -> only if that insert created a NEW row, apply the change
    -> mark approval applied
    -- everything commits or rolls back together.

So replaying an application is a no-op: the unique effect_key makes the
whole thing exactly-once.
"""

import datetime
import json

from . import memory as memmod
from .util import json_loads_bounded, new_id, now_iso, parse_iso, utcnow

APPROVAL_TTL = datetime.timedelta(hours=24)


def expired(ap) -> bool:
    """A pending approval older than the TTL can no longer be decided."""
    try:
        return (ap["status"] == "pending" and
                parse_iso(ap["created_at"]) + APPROVAL_TTL < utcnow())
    except Exception:
        return False


def create_approval(conn, kind: str, payload: dict, task_id=None,
                    approval_id: str = None) -> str:
    """Insert a pending approval. Agent-created approvals pass a
    deterministic approval_id derived from the step's request hash, so a
    retried step converges instead of duplicating."""
    aid = approval_id or new_id()
    conn.execute(
        "INSERT OR IGNORE INTO approvals"
        "(id, task_id, kind, payload_json, status, created_at)"
        " VALUES(?,?,?,?,'pending',?)",
        (aid, task_id, kind, json.dumps(payload), now_iso()))
    return aid


def get(conn, approval_id: str):
    r = conn.execute("SELECT * FROM approvals WHERE id=?",
                     (approval_id,)).fetchone()
    return dict(r) if r else None


def list_pending(conn):
    return [dict(r) for r in conn.execute(
        "SELECT a.*, t.title AS task_title FROM approvals a "
        "LEFT JOIN tasks t ON t.id=a.task_id "
        "WHERE a.status='pending' ORDER BY a.created_at")]


def _apply_payload(conn, kind: str, payload: dict) -> None:
    if kind == "memory_write":
        op = payload.get("op", "create")
        if op == "create":
            memmod.insert_memory(conn, payload["assistant_id"],
                                 payload["content"])
        elif op == "update":
            memmod.update_memory(conn, payload["memory_id"],
                                 payload["assistant_id"], payload["content"])
        elif op == "delete":
            memmod.delete_memory(conn, payload["memory_id"],
                                 payload["assistant_id"])
        else:
            raise ValueError(f"unknown memory op {op!r}")
    elif kind == "owner_fact":
        if payload.get("delete"):
            memmod.delete_owner_fact(conn, payload["key"])
        else:
            memmod.upsert_owner_fact(conn, payload["key"], payload["value"])
    elif kind == "search_query":
        pass  # approval only gates execution; nothing to apply
    elif kind == "ask":
        pass  # the click IS the effect; answer lives on the payload
    else:
        raise ValueError(f"unknown approval kind {kind!r}")


def apply_if_new(conn, approval_id: str, actor: str = "owner",
                 settings=None) -> bool:
    """Idempotently apply one approved approval.

    Returns True if the effect was applied by THIS call, False if it had
    already been applied. The whole unit commits or rolls back together.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        ap = conn.execute("SELECT * FROM approvals WHERE id=?",
                          (approval_id,)).fetchone()
        if ap is None:
            raise ValueError("no such approval")
        if ap["status"] == "applied":
            conn.execute("ROLLBACK")
            return False
        if ap["status"] != "approved":
            raise ValueError(f"approval is {ap['status']}, not approved")
        cur = conn.execute(
            "INSERT OR IGNORE INTO effects(effect_key, kind, payload_json,"
            " created_at) VALUES(?,?,?,?)",
            (approval_id, ap["kind"], ap["payload_json"], now_iso()))
        if cur.rowcount == 1:
            _apply_payload(conn, ap["kind"],
                           json_loads_bounded(ap["payload_json"]))
        conn.execute(
            "UPDATE approvals SET status='applied', applied_at=? WHERE id=?",
            (now_iso(), approval_id))
        conn.execute("COMMIT")
        if ap["kind"] == "owner_fact" and settings is not None:
            try:
                from . import journal
                journal.sync_profile(conn, settings)
            except Exception:
                pass
        return True
    except Exception:
        conn.execute("ROLLBACK")
        raise


def decide(conn, approval_id: str, approve: bool,
           edited_payload: dict = None) -> str:
    """Owner decision. Returns resulting status."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        ap = conn.execute("SELECT * FROM approvals WHERE id=?",
                          (approval_id,)).fetchone()
        if ap is None:
            raise ValueError("no such approval")
        if ap["status"] != "pending":
            conn.execute("ROLLBACK")
            return ap["status"]
        if expired(ap):
            conn.execute(
                "UPDATE approvals SET status='expired', decided_at=? "
                "WHERE id=?", (now_iso(), approval_id))
            conn.execute("COMMIT")
            return "expired"
        if approve:
            if edited_payload is not None:
                conn.execute(
                    "UPDATE approvals SET payload_json=? WHERE id=?",
                    (json.dumps(edited_payload), approval_id))
            conn.execute(
                "UPDATE approvals SET status='approved', decided_at=? "
                "WHERE id=?", (now_iso(), approval_id))
            new_status = "approved"
        else:
            conn.execute(
                "UPDATE approvals SET status='denied', decided_at=? "
                "WHERE id=?", (now_iso(), approval_id))
            new_status = "denied"
        conn.execute("COMMIT")
        try:
            from . import db as dbmod
            tool = (json_loads_bounded(ap["payload_json"]) or {}).get(
                "tool") or ap["kind"]
            dbmod.audit(conn, "owner", f"approval_{new_status}",
                        f"{tool} {approval_id[:8]}")
        except Exception:
            pass
        return new_status
    except Exception:
        conn.execute("ROLLBACK")
        raise


def void_for_task(conn, task_id: int) -> int:
    """Cancel path: pending approvals for the task become void."""
    cur = conn.execute(
        "UPDATE approvals SET status='void', decided_at=? "
        "WHERE task_id=? AND status='pending'",
        (now_iso(), task_id))
    return cur.rowcount


def decided_search(conn, task_id: int):
    """Return the latest decided search_query approval for a task."""
    return conn.execute(
        "SELECT * FROM approvals WHERE task_id=? AND kind='search_query' "
        "AND status IN ('approved','denied','applied') "
        "ORDER BY decided_at DESC LIMIT 1", (task_id,)).fetchone()


def pending_for_task(conn, task_id: int):
    return conn.execute(
        "SELECT * FROM approvals WHERE task_id=? AND status='pending'",
        (task_id,)).fetchall()
