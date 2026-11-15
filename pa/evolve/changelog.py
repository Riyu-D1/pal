"""evolve_changelog (docs/EVOLVE.md #5): an append-only ledger of what
pal changed about itself — one row per event, mirrored to the vault
note Evolve/YYYY-MM.md (or <data>/evolve/ when no vault is set).
"""

import logging
import os
import sqlite3

from ..logsetup import scrub_text
from ..util import now_iso

log = logging.getLogger("pa.evolve.changelog")

VERBS = ("opened", "planned", "building", "awaiting_eval",
         "awaiting_approval", "applied", "rolled_back", "rejected",
         "closed", "noted")


def _exists(conn) -> bool:
    try:
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table'"
            " AND name='evolve_changelog'").fetchone() is not None
    except sqlite3.Error:
        return False


def _note_line(item_id, verb, text) -> str:
    tag = f"item #{item_id} — " if item_id else ""
    return f"- {now_iso()[:16]} **{verb}** {tag}{text}\n"


def _note_append(settings, conn, line):
    """Append to Evolve/YYYY-MM.md — vault when configured, else
    <data>/evolve/. Never raises."""
    rel = f"Evolve/{now_iso()[:7]}.md"
    try:
        from .. import vault
        if vault.configured(settings):
            vault.write_note(conn, settings, rel, line, append=True)
            return
        base = os.path.join(os.path.dirname(
            os.path.abspath(settings.db_path)), "evolve")
        os.makedirs(base, exist_ok=True)
        with open(os.path.join(base, now_iso()[:7] + ".md"),
                  "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        log.info("evolve note append failed", exc_info=True)


def log_event(conn, settings, item_id, verb, text):
    """Append one changelog row + one vault/evolve note line. Never
    raises; no-ops when the table is missing."""
    text = scrub_text(text)[:500]
    try:
        if _exists(conn):
            conn.execute(
                "INSERT INTO evolve_changelog(created_at, item_id,"
                " verb, text) VALUES(?,?,?,?)",
                (now_iso(), item_id, str(verb)[:30], text))
            conn.commit()
    except Exception:
        log.warning("changelog write failed", exc_info=True)
    _note_append(settings, conn, _note_line(item_id, verb, text))


def recent(conn, limit=10, verbs=None, days=None):
    if not _exists(conn):
        return []
    q = ("SELECT * FROM evolve_changelog ORDER BY id DESC LIMIT ?")
    rows = [dict(r) for r in conn.execute(q, (int(limit) * 4,))]
    if verbs:
        rows = [r for r in rows if r["verb"] in verbs]
    if days is not None:
        from ..util import utcnow
        import datetime
        cut = (utcnow() - datetime.timedelta(days=days)).isoformat()
        rows = [r for r in rows if r["created_at"] >= cut]
    return rows[:int(limit)]
