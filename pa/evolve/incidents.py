"""Incident capture: one small scrubbed row per failure signal.

`record()` never raises and no-ops when the `incidents` table is
missing, so hooks are safe to call from any deploy mid-migration.
Detail is run through the same credential patterns as the log
filter and size-capped; no secret or whole page body ever lands here.
"""

import hashlib
import logging
import re
import sqlite3

from ..logsetup import scrub_text
from ..util import now_iso

log = logging.getLogger("pa.evolve.incidents")

SUMMARY_MAX = 300
DETAIL_MAX = 4000

_HEX_RE = re.compile(r"0x[0-9a-fA-F]+|\b[0-9a-f]{16,}\b")
_NUM_RE = re.compile(r"\d+")
_WS_RE = re.compile(r"\s+")


def fingerprint(kind: str, message: str) -> str:
    """Stable hash of kind + normalised message: same failure, same
    fingerprint, so the detector can count repeats."""
    msg = str(message or "").lower()
    msg = _HEX_RE.sub("#", msg)
    msg = _NUM_RE.sub("#", msg)
    msg = _WS_RE.sub(" ", msg).strip()[:200]
    return hashlib.sha256(f"{kind}|{msg}".encode()).hexdigest()[:20]


def _table_exists(conn) -> bool:
    try:
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table'"
            " AND name='incidents'").fetchone() is not None
    except sqlite3.Error:
        return False


def record(conn, source: str, kind: str, summary: str = "",
           detail: str = "", channel_id=None, task_id=None,
           tool: str = ""):
    """Append one incident row. Returns the row id or None. Never
    raises; never stores secrets."""
    try:
        if conn is None or not _table_exists(conn):
            return None
        summary = scrub_text(summary)[:SUMMARY_MAX]
        detail = scrub_text(detail)[:DETAIL_MAX]
        cur = conn.execute(
            "INSERT INTO incidents("
            "created_at, source, kind, fingerprint, summary, detail,"
            " channel_id, task_id, tool)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (now_iso(), str(source or "?")[:30],
             str(kind or "exception")[:40],
             fingerprint(kind, summary or detail), summary, detail,
             str(channel_id)[:80] if channel_id else None,
             task_id, str(tool)[:80] if tool else None))
        conn.commit()
        return cur.lastrowid
    except Exception:
        log.warning("incident record failed", exc_info=True)
        return None
