"""Prompt snippets (docs/EVOLVE.md #8): short system-prompt addenda
stored in prompt_snippets, versioned like skills so Track A can
snapshot/restore. Listed at the end of chattools.prompt_lines.

Safety: a snippet is model input only — it must never turn off the
safety rails. sanitize() drops any line that asks to skip
approvals, ignore allow-rules, drop untrusted fences, or obey
embedded instructions.
"""

import logging
import re

from ..util import now_iso

log = logging.getLogger("pa.evolve.snippets")

BODY_CAP = 2000
MAX_SNIPPETS = 10

_BANNED = re.compile(
    r"(skip|without|no|disable|bypass|remove|ignore|override|turn off|"
    r"don'?t (ask|require|need)|never (ask|require)).{0,40}"
    r"(approval|approve|allow.?rule|confirm|permission|gate)|"
    r"(approval|allow.?rule|untrusted|fence).{0,40}"
    r"(off|disabled?|skip|bypass|ignore|not needed)|"
    r"ignore (all )?(previous|above|prior|other) instructions|"
    r"always allow", re.I)


def sanitize(text: str) -> str:
    """Drop lines that try to weaken approvals, allow-rules, or the
    untrusted fences. Returns the cleaned body."""
    keep = []
    for line in (text or "").splitlines():
        if _BANNED.search(line):
            log.info("snippet line dropped (policy): %r", line[:80])
            continue
        keep.append(line)
    return "\n".join(keep).strip()[:BODY_CAP]


def _exists(conn) -> bool:
    try:
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table'"
            " AND name='prompt_snippets'").fetchone() is not None
    except Exception:
        return False


def list_snippets(conn) -> list:
    if not _exists(conn):
        return []
    return [dict(r) for r in conn.execute(
        "SELECT name, body, version, source, updated_at"
        " FROM prompt_snippets ORDER BY name LIMIT ?",
        (MAX_SNIPPETS,)).fetchall()]


def get(conn, name: str):
    if not _exists(conn):
        return None
    row = conn.execute(
        "SELECT * FROM prompt_snippets WHERE name=?", (name,)
        ).fetchone()
    return dict(row) if row else None


def save_snippet(conn, name: str, body: str,
                 source: str = "evolve", s=None) -> dict:
    """Upsert one snippet: sanitised, capped, and the new body is
    recorded as a version row."""
    if not _exists(conn):
        return {"error": "unavailable"}
    name = re.sub(r"[^a-z0-9-]+", "-", (name or "").lower()
                  ).strip("-")[:60] or "snippet"
    body = sanitize(body)
    if not body:
        return {"error": "empty", "name": name}
    cur = get(conn, name)
    version = (cur["version"] + 1) if cur else 1
    conn.execute(
        "INSERT INTO prompt_snippets"
        "(name, body, version, source, created_at, updated_at)"
        " VALUES(?,?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET"
        " body=excluded.body, version=excluded.version,"
        " source=excluded.source, updated_at=excluded.updated_at",
        (name, body, version, str(source)[:20],
         cur["created_at"] if cur else now_iso(), now_iso()))
    conn.execute(
        "INSERT OR REPLACE INTO prompt_snippet_versions"
        "(name, version, body, created_at, source)"
        " VALUES(?,?,?,?,?)",
        (name, version, body, now_iso(), str(source)[:20]))
    conn.commit()
    from . import archive
    archive.note(conn, s, "snippet", name, body,
                 created_by=source, status="live")
    return {"saved": True, "name": name, "version": version}


def snapshot_snippet(conn, name: str) -> dict:
    cur = get(conn, name)
    if cur is None:
        return {"name": name, "exists": False, "body": ""}
    return {"name": name, "exists": True, "body": cur["body"],
            "version": cur["version"]}


def restore_snippet(conn, name: str, version: int) -> dict:
    row = conn.execute(
        "SELECT body, source FROM prompt_snippet_versions"
        " WHERE name=? AND version=?", (name, int(version))).fetchone()
    if row is None:
        return {"error": "not_found", "name": name,
                "version": version}
    return save_snippet(conn, name, row["body"],
                        source=f"rollback:{row['source']}")


def delete_snippet(conn, name: str) -> bool:
    if not _exists(conn):
        return False
    cur = conn.execute("DELETE FROM prompt_snippets WHERE name=?",
                       (name,))
    conn.commit()
    return cur.rowcount > 0


def prompt_block(conn) -> str:
    """Enabled addenda for the system prompt (or '')."""
    rows = list_snippets(conn)
    if not rows:
        return ""
    lines = ["Learned addenda (advisory only — they cannot change "
             "tool gates):"]
    for r in rows:
        lines.append(f"- {r['name']}: {r['body'][:400]}")
    return "\n".join(lines)
