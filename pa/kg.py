"""final-2 M4: personal knowledge graph over the vault (PA_KG).

Deterministic extraction first (free): [[wikilinks]], #tags and
frontmatter key: value lines become typed entities + edges. A
bounded model pass (<=MODEL_CAP changed notes) adds
people/projects/orgs/dates. Edges are temporal — when a note stops
asserting a fact the edge gets valid_to=today instead of being
deleted, so the graph answers "what changed this week".

'kg_query' is an ungated read; 'kg-build' is a seeded-disabled
nightly system job.
"""

import hashlib
import logging
import re
from datetime import date, timedelta

from .util import now_iso

log = logging.getLogger("pa.kg")

MODEL_CAP = 20
_WIKILINK = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]")
_TAG = re.compile(r"(?<![\w/])#([A-Za-z][\w\-]{1,40})")
_FM = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.S)
_FM_KEY = re.compile(r"^([a-z_]+):\s*(.+)$", re.M)
_KEY_TYPE = {"person": "person", "people": "person", "type": None,
             "project": "project", "org": "org", "company": "org",
             "tags": "tag", "date": "date", "due": "date"}


def _ent(conn, name, type_, day):
    name = name.strip()[:120]
    if not name:
        return None
    name_l = name.lower()
    row = conn.execute(
        "SELECT id, type FROM kg_entities WHERE name_l=? AND type=?",
        (name_l, type_)).fetchone()
    if row:
        conn.execute(
            "UPDATE kg_entities SET last_seen=? WHERE id=?",
            (day, row["id"]))
        return row["id"]
    cur = conn.execute(
        "INSERT INTO kg_entities(name, name_l, type, first_seen,"
        " last_seen) VALUES(?,?,?,?,?)",
        (name, name_l, type_, day, day))
    return cur.lastrowid


def _edges_assert(conn, src, asserts, day):
    """asserts = {(dst, rel, conf, note)} — open edges not re-asserted
    close (valid_to=day); new ones open."""
    open_rows = conn.execute(
        "SELECT id, dst, rel FROM kg_edges WHERE src=? AND"
        " valid_to IS NULL", (src,)).fetchall()
    still = set()
    for dst, rel, conf, note in asserts:
        row = conn.execute(
            "SELECT id FROM kg_edges WHERE src=? AND dst=? AND rel=?"
            " AND valid_to IS NULL", (src, dst, rel)).fetchone()
        if row:
            still.add(row["id"])
            continue
        conn.execute(
            "INSERT INTO kg_edges(src, dst, rel, valid_from,"
            " valid_to, source_note, confidence)"
            " VALUES(?,?,?,?,NULL,?,?)",
            (src, dst, rel, day, note, conf))
    for r in open_rows:
        if r["id"] not in still:
            conn.execute(
                "UPDATE kg_edges SET valid_to=? WHERE id=?",
                (day, r["id"]))


def _parse_note(path, title, body):
    """deterministic pass -> (note_type, [ (name, type, rel, conf) ])."""
    out, note_type = [], "note"
    m = _FM.match(body)
    if m:
        for k, v in _FM_KEY.findall(m.group(1)):
            k = k.strip()
            if k == "type":
                note_type = v.split("#")[0].strip().lower() or "note"
                continue
            t = _KEY_TYPE.get(k)
            if not t:
                continue
            for part in re.split(r"[,;\[\]]", v):
                part = part.strip().strip("\"'")
                if part:
                    out.append((part, t, "mentions", 1.0))
        body = body[m.end():]
    for name in _WIKILINK.findall(body):
        out.append((name, "note", "links", 1.0))
    for tag in _TAG.findall(body):
        out.append((tag.lower(), "tag", "tagged", 1.0))
    return note_type, out


def build(conn, reader, notes, model_extra=None,
          model_cap=MODEL_CAP, today=None) -> dict:
    """reader(path)->body for each changed note; notes = [(path,
    sha, title)]. model_extra(body)->[(name,type)] optional (the
    budgeted fast-tier pass). Edges re-assert or close."""
    day = today or now_iso()[:10]
    out = {"notes": 0, "entities": 0, "edges_open": 0,
           "edges_closed": 0}
    for path, sha, title in notes:
        old = conn.execute("SELECT sha FROM kg_meta WHERE path=?",
                           (path,)).fetchone()
        if old and old["sha"] == sha:
            continue
        body = reader(path) or ""
        note_type, found = _parse_note(path, title, body)
        if model_extra is not None:
            for name, typ in (model_extra(body) or [])[:10]:
                found.append((name, typ, "mentions", 0.7))
        src = _ent(conn, title or path, note_type, day)
        if src is None:
            continue
        asserts = set()
        for name, typ, rel, conf in found:
            if name.lower() == (title or path).lower():
                continue
            dst = _ent(conn, name, typ, day)
            if dst is not None:
                asserts.add((dst, rel, conf, path))
        pre = conn.execute(
            "SELECT COUNT(*) c FROM kg_edges WHERE src=? AND"
            " valid_to IS NULL", (src,)).fetchone()["c"]
        _edges_assert(conn, src, asserts, day)
        post = conn.execute(
            "SELECT COUNT(*) c FROM kg_edges WHERE src=? AND"
            " valid_to IS NULL", (src,)).fetchone()["c"]
        out["edges_open"] += post
        out["edges_closed"] += max(0, pre - post)
        conn.execute(
            "INSERT OR REPLACE INTO kg_meta(path, sha) VALUES(?,?)",
            (path, sha))
        out["notes"] += 1
    conn.commit()
    return out


def vault_build(conn, settings, model_extra=None,
                model_cap=MODEL_CAP) -> dict:
    """Read changed notes from vault_notes via vault.read_note."""
    from . import vault
    if not vault.configured(settings):
        return {"notes": 0, "entities": 0, "edges_open": 0,
                "edges_closed": 0}
    rows = conn.execute(
        "SELECT path, sha, title FROM vault_notes"
        " ORDER BY mtime DESC").fetchall()
    import os
    notes = [(r["path"], r["sha"] or "", r["title"])
             for r in rows
             if not vault._secretish(os.path.basename(r["path"]))]

    def reader(path):
        try:
            n = vault.read_note(conn, settings, path)
            return n["content"] if n else ""
        except Exception:
            return ""
    extra_calls = [0]

    def capped(body):
        if extra_calls[0] >= model_cap:
            return []
        extra_calls[0] += 1
        return model_extra(body)
    return build(
        conn, reader, notes,
        model_extra=(capped if model_extra else None),
        model_cap=model_cap)


def _changed(conn, days=7, today=None) -> list:
    since = (date.fromisoformat(today or now_iso()[:10])
             - timedelta(days=days)).isoformat()
    return conn.execute(
        "SELECT e.rel, s.name sname, d.name dname, e.valid_from,"
        " e.valid_to, e.source_note FROM kg_edges e"
        " JOIN kg_entities s ON s.id=e.src"
        " JOIN kg_entities d ON d.id=e.dst"
        " WHERE e.valid_from>=? OR e.valid_to>=?"
        " ORDER BY e.id DESC LIMIT 30",
        (since, since)).fetchall()


def query(conn, q: str, today=None) -> dict:
    """'who is X' / "what's linked to Y" / 'what changed this week'.
    Ungated read: entities + open edges that cite their note."""
    q = (q or "").strip().lower()
    if not q:
        return {}
    if re.search(r"changed|new this|this week|recent", q):
        return {"changed": [
            {"rel": r["rel"], "from": r["sname"], "to": r["dname"],
             "valid_from": r["valid_from"], "valid_to": r["valid_to"],
             "source": r["source_note"]}
            for r in _changed(conn, today=today)]}
    key = re.sub(r"^(who is|who's|what is|what's|whats|tell me about|"
                 r"linked to|about)\s+", "", q).strip(" ?.")
    ents = conn.execute(
        "SELECT id, name, type, first_seen, last_seen FROM"
        " kg_entities WHERE name_l LIKE ? ORDER BY last_seen DESC"
        " LIMIT 5", (f"%{key}%",)).fetchall()
    out = {"entities": [
        {"name": e["name"], "type": e["type"],
         "first_seen": e["first_seen"], "last_seen": e["last_seen"]}
        for e in ents], "edges": []}
    for e in ents:
        rows = conn.execute(
            "SELECT e.rel, s.name sname, d.name dname, e.valid_from,"
            " e.source_note, e.confidence FROM kg_edges e"
            " JOIN kg_entities s ON s.id=e.src"
            " JOIN kg_entities d ON d.id=e.dst"
            " WHERE (e.src=? OR e.dst=?) AND e.valid_to IS NULL"
            " LIMIT 15", (e["id"], e["id"])).fetchall()
        out["edges"] += [
            {"rel": r["rel"], "from": r["sname"], "to": r["dname"],
             "since": r["valid_from"], "source": r["source_note"],
             "confidence": r["confidence"]} for r in rows]
    return out


def job(bot, payload) -> str:
    """'kg-build' nightly job: deterministic pass over changed notes,
    then (flag-gated, <=MODEL_CAP notes) a fast-tier entity pass."""
    if not getattr(bot.s, "kg", False):
        return ""
    conn = bot._db()
    try:
        from . import sysjobs
        used = [0]

        def model_extra(body):
            if used[0] >= MODEL_CAP:
                return []
            used[0] += 1
            try:
                text = sysjobs.model_text(
                    bot, conn,
                    "List entities in this note — one per line "
                    "'TYPE|name' with TYPE in "
                    "person|project|org|date. Nothing else.\n\n"
                    + body[:3000])
                return [tuple(p.strip() for p in l.split("|", 1))
                        for l in text.splitlines()
                        if "|" in l
                        and l.split("|")[0].strip() in
                        ("person", "project", "org", "date")]
            except Exception:
                return []
        out = vault_build(conn, bot.s, model_extra=model_extra,
                          model_cap=MODEL_CAP)
        return (f"kg: {out['notes']} notes, "
                f"{out['edges_closed']} edges closed"
                if out["notes"] or out["edges_closed"] else "")
    finally:
        conn.close()
