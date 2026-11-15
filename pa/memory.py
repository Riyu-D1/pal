"""Per-assistant memories (FTS5) and the shared owner-facts store.

Isolation rule: an assistant reads only its own memories; every assistant
may read owner facts. All writes to either store go through approvals +
effects (pa.approvals) except direct owner UI edits, which the owner
applies through the same effect path.
"""

from . import provenance as _prov
from .util import now_iso


def _index(conn, entity: str, entity_id: int, body: str) -> None:
    conn.execute(
        "INSERT INTO search_index(entity, entity_id, body) VALUES(?,?,?)",
        (entity, str(entity_id), body))
    # keep at most one index row per entity
    conn.execute(
        "DELETE FROM search_index WHERE rowid NOT IN "
        "(SELECT max(rowid) FROM search_index WHERE entity=? AND entity_id=?)"
        " AND entity=? AND entity_id=?",
        (entity, str(entity_id), entity, str(entity_id)))


def _unindex(conn, entity: str, entity_id: int) -> None:
    conn.execute(
        "DELETE FROM search_index WHERE entity=? AND entity_id=?",
        (entity, str(entity_id)))


def insert_memory(conn, assistant_id: int, content: str,
                  source: str = "owner", source_ref: str = "") -> int:
    now = now_iso()
    cur = conn.execute(
        "INSERT INTO memories(assistant_id, content, created_at,"
        " updated_at, source, source_ref) VALUES(?,?,?,?,?,?)",
        (assistant_id, content, now, now,
         _prov.norm_source(source), source_ref[:300]))
    mid = cur.lastrowid
    _index(conn, "memory", mid, content)
    return mid


def update_memory(conn, memory_id: int, assistant_id: int, content: str) -> bool:
    cur = conn.execute(
        "UPDATE memories SET content=?, updated_at=? "
        "WHERE id=? AND assistant_id=?",
        (content, now_iso(), memory_id, assistant_id))
    if cur.rowcount:
        _index(conn, "memory", memory_id, content)
        return True
    return False


def delete_memory(conn, memory_id: int, assistant_id: int) -> bool:
    cur = conn.execute(
        "DELETE FROM memories WHERE id=? AND assistant_id=?",
        (memory_id, assistant_id))
    if cur.rowcount:
        _unindex(conn, "memory", memory_id)
        return True
    return False


def upsert_owner_fact(conn, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO owner_facts(key, value, updated_at) VALUES(?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
        "updated_at=excluded.updated_at",
        (key, value, now_iso()))
    row = conn.execute("SELECT id FROM owner_facts WHERE key=?", (key,)).fetchone()
    _index(conn, "owner_fact", row["id"], f"{key}: {value}")


def delete_owner_fact(conn, key: str) -> bool:
    row = conn.execute("SELECT id FROM owner_facts WHERE key=?", (key,)).fetchone()
    if not row:
        return False
    conn.execute("DELETE FROM owner_facts WHERE id=?", (row["id"],))
    _unindex(conn, "owner_fact", row["id"])
    return True


def list_owner_facts(conn):
    return [dict(r) for r in conn.execute(
        "SELECT id, key, value, updated_at FROM owner_facts ORDER BY key")]


def search_memories(conn, assistant_id: int, query: str, limit: int = 8):
    """FTS5 search over this assistant's memories plus owner facts.

    Returns [{"entity","id","content"}]. Assistant A can never match B's
    rows - the join constrains to assistant_id.
    """
    q = (query or "").strip()
    if not q:
        return []
    fts_q = " OR ".join(t.replace('"', "") for t in q.split() if t) or q
    rows = conn.execute(
        "SELECT s.entity, s.entity_id, s.body FROM search_index s "
        "WHERE search_index MATCH ? "
        "AND ((s.entity='memory' AND s.entity_id IN "
        "      (SELECT id FROM memories WHERE assistant_id=?)) "
        "     OR s.entity='owner_fact') "
        "LIMIT ?",
        (fts_q, assistant_id, limit)).fetchall()
    return [{"entity": r["entity"], "id": int(r["entity_id"]),
             "content": r["body"]} for r in rows]


def list_memories(conn, assistant_id: int = None):
    if assistant_id is None:
        rows = conn.execute(
            "SELECT m.*, a.name AS assistant_name FROM memories m "
            "JOIN assistants a ON a.id=m.assistant_id "
            "ORDER BY m.updated_at DESC").fetchall()
    else:
        rows = conn.execute(
            "SELECT m.*, a.name AS assistant_name FROM memories m "
            "JOIN assistants a ON a.id=m.assistant_id "
            "WHERE m.assistant_id=? ORDER BY m.updated_at DESC",
            (assistant_id,)).fetchall()
    return [dict(r) for r in rows]
