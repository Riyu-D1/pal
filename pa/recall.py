"""cross-source recall (PA_RECALL).

One FTS5 index (recall_fts, migration 33) over every local thing
pal might want to remember: chat turns, memories, owner facts,
activity rows and vault note titles+excerpts. Secret-like vault
names never index (vault._secretish). recall_meta is the
incremental manifest — reindex() only re-reads rows whose content
sha changed, so the hourly 'recall-index' job is cheap.

search() is a plain read: the chat 'recall' tool is ungated.
"""

import hashlib
import logging
import os
import re

from .util import now_iso

log = logging.getLogger("pa.recall")

EXCERPT = 500


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]


def _vault_excerpts(conn, settings):
    """(path, title, excerpt) for indexable vault notes — secret-
    looking names skip, oversized notes truncate."""
    from . import vault
    try:
        if not vault.configured(settings):
            return
        for n in conn.execute(
                "SELECT path, title FROM vault_notes").fetchall():
            if vault._secretish(os.path.basename(n["path"])):
                continue
            try:
                note = vault.read_note(conn, settings, n["path"])
                body = note["content"] if note else ""
            except Exception:
                body = ""
            body = "\n".join(
                l for l in body.splitlines()
                if "untrusted" not in l.lower())[:EXCERPT]
            yield n["path"], n["title"], body
    except Exception:
        return


def _each_source(conn, settings):
    """Yield (source, ref_id, title, body, day) for every local row."""
    for r in conn.execute(
            "SELECT id, content, created_at FROM chat_messages"
            " ORDER BY id DESC LIMIT 5000").fetchall():
        yield ("chat", str(r["id"]), "chat turn",
               r["content"][:EXCERPT], (r["created_at"] or "")[:10])
    for r in conn.execute(
            "SELECT id, content, created_at FROM memories").fetchall():
        yield ("memory", str(r["id"]), "memory",
               r["content"][:EXCERPT], (r["created_at"] or "")[:10])
    for r in conn.execute(
            "SELECT key, value, updated_at FROM owner_facts").fetchall():
        yield ("owner_fact", r["key"], f"fact: {r['key']}",
               str(r["value"])[:EXCERPT], (r["updated_at"] or "")[:10])
    for r in conn.execute(
            "SELECT id, kind, title, body, created_at"
            " FROM activity").fetchall():
        yield ("activity", str(r["id"]), r["title"],
               (r["title"] + "\n" + (r["body"] or ""))[:EXCERPT],
               (r["created_at"] or "")[:10])
    if settings is not None:
        for path, title, body in _vault_excerpts(conn, settings):
            yield ("vault", path, title or path, body,
                   now_iso()[:10])


def reindex(conn, settings=None) -> dict:
    """Incremental pass: only rows whose content sha changed are
    re-indexed. -> {added, skipped, dropped}."""
    out = {"added": 0, "skipped": 0, "dropped": 0}
    seen = set()
    try:
        meta = {(m["source"], m["ref_id"]): m
                for m in conn.execute(
                    "SELECT * FROM recall_meta").fetchall()}
        for source, ref_id, title, body, day in \
                _each_source(conn, settings):
            seen.add((source, ref_id))
            sha = _sha(title + "\n" + body)
            old = meta.get((source, ref_id))
            if old and old["sha"] == sha:
                out["skipped"] += 1
                continue
            if old:
                conn.execute(
                    "DELETE FROM recall_fts WHERE rowid=?",
                    (old["rowid_"],))
            cur = conn.execute(
                "INSERT INTO recall_fts(source, ref_id, title, body,"
                " day) VALUES(?,?,?,?,?)",
                (source, ref_id, title, body, day))
            conn.execute(
                "INSERT OR REPLACE INTO recall_meta(source, ref_id,"
                " rowid_, sha, indexed_at) VALUES(?,?,?,?,?)",
                (source, ref_id, cur.lastrowid, sha, now_iso()))
            out["added"] += 1
        # drop rows whose source vanished — notion rows are managed
        # by notionindex's own cursor, not this sweep
        for key, m in meta.items():
            if key[0] == "notion":
                continue
            if key not in seen:
                conn.execute("DELETE FROM recall_fts WHERE rowid=?",
                             (m["rowid_"],))
                conn.execute(
                    "DELETE FROM recall_meta WHERE source=? AND"
                    " ref_id=?", key)
                out["dropped"] += 1
        conn.commit()
    except Exception:
        log.exception("recall reindex failed")
    return out


_TOKEN = re.compile(r"[A-Za-z0-9_@.\-']+")


def job(bot, payload) -> str:
    """'recall-index' system job: incremental index refresh."""
    if not getattr(bot.s, "recall", False):
        return ""
    conn = bot._db()
    try:
        out = reindex(conn, bot.s)
        return (f"recall: {out['added']} indexed, "
                f"{out['dropped']} dropped"
                if out["added"] or out["dropped"] else "")
    finally:
        conn.close()


def _match(query: str) -> str:
    """FTS5-safe query: quote each token, OR them together."""
    toks = _TOKEN.findall(query or "")[:8]
    return " OR ".join(f'"{t}"' for t in toks)


def search(conn, query: str, limit: int = 8) -> list:
    """Ranked hits across all sources — each with source, ref, day
    and a snippet so the caller can cite where it came from."""
    match = _match(query)
    if not match:
        return []
    try:
        return [{
            "source": r["source"], "ref": r["ref_id"],
            "title": r["title"], "day": r["day"],
            "snippet": r["snip"],
        } for r in conn.execute(
            "SELECT source, ref_id, title, day,"
            " snippet(recall_fts, 3, '[', ']', '…', 10) snip"
            " FROM recall_fts WHERE recall_fts MATCH ?"
            " ORDER BY rank LIMIT ?", (match, limit)).fetchall()]
    except Exception:
        return []
