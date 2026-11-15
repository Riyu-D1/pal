"""Turn outcomes + owner feedback (docs/EVOLVE.md item 2).

Every attended turn lands one cheap `turn_outcomes` row from a
deterministic heuristic; /good /bad, the reply buttons and the
`feedback` chat tool add `feedback_good`/`feedback_bad` rows plus an
incident. Everything here is best-effort: never raises, no-ops when
the table is missing.
"""

import logging
import sqlite3

from ..logsetup import scrub_text
from ..util import now_iso
from . import incidents

log = logging.getLogger("pa.evolve.outcomes")

_OK = "ok"
_ERROR = "error"
_LOW = "low_confidence"

_ERROR_PREFIXES = ("(internal error", "(model error", "(not configured")
_LOW_MARKERS = ("(empty reply)", "(no answer)",
                "i ran out of time", "step limit")
_REFUSAL_PREFIXES = ("i can't", "i cannot", "sorry, i can't",
                     "sorry, i cannot", "i'm unable")


def _exists(conn) -> bool:
    try:
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table'"
            " AND name='turn_outcomes'").fetchone() is not None
    except sqlite3.Error:
        return False


def heuristic(text: str, trace=None) -> str:
    """Deterministic outcome for a finished turn — no model call."""
    t = (text or "").strip().lower()
    if t.startswith(_ERROR_PREFIXES):
        return _ERROR
    if any(m in t for m in _LOW_MARKERS):
        return _LOW
    if t.startswith(_REFUSAL_PREFIXES):
        return _LOW
    errs = 0
    for step in trace or []:
        res = step.get("result") if isinstance(step, dict) else None
        err = res.get("error") if isinstance(res, dict) else None
        if err and "not_configured" not in str(err):
            errs += 1
    return _LOW if errs >= 2 else _OK


def record(conn, channel_id, outcome, detail=""):
    """Append one outcome row. Returns row id or None. Never raises."""
    try:
        if conn is None or not _exists(conn):
            return None
        cur = conn.execute(
            "INSERT INTO turn_outcomes(created_at, channel_id, outcome,"
            " detail) VALUES(?,?,?,?)",
            (now_iso(), str(channel_id), str(outcome)[:30],
             scrub_text(detail)[:400]))
        conn.commit()
        return cur.lastrowid
    except Exception:
        log.warning("outcome record failed", exc_info=True)
        return None


def mark(conn, outcome_id, outcome):
    """Update an outcome row (the async grader downgrading ok -> low)."""
    try:
        conn.execute("UPDATE turn_outcomes SET outcome=? WHERE id=?",
                     (str(outcome)[:30], outcome_id))
        conn.commit()
    except Exception:
        pass


def feedback(conn, channel_id, good: bool, note: str = "",
             source: str = "owner"):
    """/good /bad, reply buttons and the feedback tool land here:
    one outcome row + one incident. Never raises."""
    rating = "feedback_good" if good else "feedback_bad"
    oid = record(conn, channel_id, rating, detail=note)
    incidents.record(
        conn, source, rating,
        summary=("owner liked the last reply" if good
                 else "owner disliked the last reply"),
        detail=note, channel_id=channel_id)
    return oid
