"""evolve_items CRUD + status vocabulary (docs/EVOLVE.md #4).

An item is one unit of self-improvement work — a gap, a lesson, a
skill/prompt/config change, a connector to set up, a tool to build, a
fix to ship, or an eval to add. Nothing here applies anything; items
move through statuses as later stages act on them.
"""

import json
import logging
import sqlite3

from ..util import now_iso

log = logging.getLogger("pa.evolve.items")

SHAPES = ("lesson", "skill", "prompt", "config", "connector", "tool",
          "fix", "eval")
RISKS = ("low", "high")
STATUSES = ("open", "needs_plan", "planned", "building",
            "awaiting_eval", "awaiting_approval", "applied",
            "rolled_back", "rejected", "closed")

_OPEN = ("open", "needs_plan", "planned", "building",
         "awaiting_eval", "awaiting_approval")


def _exists(conn) -> bool:
    try:
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table'"
            " AND name='evolve_items'").fetchone() is not None
    except sqlite3.Error:
        return False


def open_statuses():
    return _OPEN


def get(conn, item_id):
    if not _exists(conn):
        return None
    row = conn.execute("SELECT * FROM evolve_items WHERE id=?",
                       (item_id,)).fetchone()
    return dict(row) if row else None


def find_open(conn, dedupe_key):
    """Open item with this dedupe key, or None."""
    if not dedupe_key or not _exists(conn):
        return None
    marks = ",".join("?" for _ in _OPEN)
    row = conn.execute(
        f"SELECT * FROM evolve_items WHERE dedupe_key=?"
        f" AND status IN ({marks}) ORDER BY id LIMIT 1",
        (dedupe_key, *_OPEN)).fetchone()
    return dict(row) if row else None


def open_item(conn, shape, risk, title, plan="",
              incident_fingerprint="", dedupe_key="", artifacts=None):
    """Create or bump the open item for dedupe_key. Returns item id or
    None. Never raises."""
    try:
        if not _exists(conn):
            return None
        existing = find_open(conn, dedupe_key)
        if existing is not None:
            conn.execute(
                "UPDATE evolve_items SET updated_at=?, hits=hits+1"
                " WHERE id=?", (now_iso(), existing["id"]))
            conn.commit()
            return existing["id"]
        cur = conn.execute(
            "INSERT INTO evolve_items("
            "created_at, updated_at, shape, risk, status, title, plan,"
            " incident_fingerprint, dedupe_key, artifacts)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (now_iso(), now_iso(),
             str(shape)[:20] if shape in SHAPES else "fix",
             str(risk)[:10] if risk in RISKS else "low",
             "open", str(title)[:300], str(plan or "")[:4000],
             str(incident_fingerprint or "")[:40],
             str(dedupe_key or "")[:200],
             json.dumps(artifacts or {})[:4000]))
        conn.commit()
        return cur.lastrowid
    except Exception:
        log.warning("evolve item open failed", exc_info=True)
        return None


def set_status(conn, item_id, status, error="", eval_score=None,
               plan=None, artifacts=None):
    """Move an item; error/score/plan/artifacts update when given."""
    try:
        sets, vals = ["updated_at=?", "status=?"], [now_iso(), status]
        if error is not None:
            sets.append("error=?")
            vals.append(str(error)[:1000])
        if eval_score is not None:
            sets.append("eval_score=?")
            vals.append(float(eval_score))
        if plan is not None:
            sets.append("plan=?")
            vals.append(str(plan)[:4000])
        if artifacts is not None:
            sets.append("artifacts=?")
            vals.append(json.dumps(artifacts)[:4000])
        vals.append(item_id)
        conn.execute(
            f"UPDATE evolve_items SET {', '.join(sets)} WHERE id=?",
            vals)
        conn.commit()
        return True
    except Exception:
        log.warning("evolve item status failed", exc_info=True)
        return False


def list_items(conn, statuses=None, limit=20):
    if not _exists(conn):
        return []
    statuses = statuses or _OPEN
    marks = ",".join("?" for _ in statuses)
    return [dict(r) for r in conn.execute(
        f"SELECT * FROM evolve_items WHERE status IN ({marks})"
        f" ORDER BY updated_at DESC, id DESC LIMIT ?",
        (*statuses, int(limit)))]
