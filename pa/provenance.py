"""Provenance + poisoning defences (final-4 E5).

Every memory/fact/block/skill/KG edge carries source
(owner|pal|email|web|file|import) + source_ref (migration 42).
Anything from an UNTRUSTED source can never become durable,
policy-relevant state without an explicit owner approval —
`gate()` is the single choke point callers use before writing.

Cards and `/memory audit` show the source; the audit lists this
week's untrusted-source items with one-tap remove.
"""

import logging

log = logging.getLogger("pa.provenance")

TRUSTED = frozenset({"owner", "pal"})
UNTRUSTED = frozenset({"email", "web", "file", "import"})
SOURCES = TRUSTED | UNTRUSTED


def norm_source(source: str) -> str:
    s = (source or "owner").strip().lower()
    return s if s in SOURCES else "import"


def is_trusted(source: str) -> bool:
    return norm_source(source) in TRUSTED


def gate(conn, s, source: str, what: str, detail: str = ""):
    """Returns None when the write may proceed; an approval-needed
    dict when the source is untrusted and the action would change
    policy-relevant state (skill / allow rule / goal / memory block /
    config). The caller must surface the dict instead of writing."""
    if is_trusted(source):
        return None
    log.info("provenance gate: %s from %s held for owner",
             what, source)
    return {"held_for_owner": True, "reason":
            f"{what} came from {norm_source(source)} "
            f"({detail[:80] or 'untrusted source'}) — needs your "
            f"approval, not silent application"}


def untrusted_this_week(conn, table: str, days: int = 7) -> list:
    """Rows in `table` whose source is untrusted, newest first —
    backs `/memory audit`. Returns [] when the table lacks columns."""
    try:
        cols = {r[1] for r in conn.execute(
            f"PRAGMA table_info({table})").fetchall()}
        if "source" not in cols:
            return []
        return conn.execute(
            f"SELECT * FROM {table} WHERE source IN "
            "('email','web','file','import') AND created_at >= "
            "datetime('now', ?) ORDER BY rowid DESC LIMIT 50",
            (f"-{days} days",)).fetchall()
    except Exception:
        return []
