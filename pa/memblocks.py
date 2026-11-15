"""final-2 M1: pinned memory blocks — persona / human / goals /
world, always injected so pal sounds the same in chat, calls and
jobs (MemGPT-style).

Caps keep the blocks honest; every write pushes the previous content
onto memory_block_history so /memory undo can walk it back. 'human'
is the owner profile — pal writes it only behind an approval (the
tool gates it), same rule as owner_fact.
"""

from .util import now_iso

NAMES = ("persona", "human", "goals", "world")
CAPS = {"persona": 1200, "human": 1500, "goals": 800, "world": 800}
DEFAULTS = {
    "persona": "",
    "human": "",
    "goals": "",
    "world": "",
}


def cap(name: str) -> int:
    return CAPS.get(name, 800)


def get(conn, name: str) -> dict:
    r = conn.execute(
        "SELECT * FROM memory_blocks WHERE name=?",
        (name,)).fetchone()
    if r is None:
        return {"name": name, "content": "", "version": 0,
                "updated_by": "", "updated_at": ""}
    return dict(r)


def list_blocks(conn) -> list:
    have = {r["name"]: dict(r) for r in conn.execute(
        "SELECT * FROM memory_blocks").fetchall()}
    return [have.get(n, {"name": n, "content": "", "version": 0,
                         "updated_by": "", "updated_at": ""})
            for n in NAMES]


def set_block(conn, name: str, content: str,
              edited_by: str = "owner", s=None) -> dict:
    """Write one block (capped); the old content goes to history.
    -> the new row, or {"error": ...}."""
    name = str(name or "").strip().lower()
    if name not in NAMES:
        return {"error": "bad_block",
                "message": f"block must be one of "
                           f"{', '.join(NAMES)}"}
    content = str(content or "")[:cap(name)]
    old = get(conn, name)
    now = now_iso()
    conn.execute(
        "INSERT INTO memory_block_history(name, content, edited_by,"
        " created_at) VALUES(?,?,?,?)",
        (name, old["content"], edited_by, now))
    conn.execute(
        "INSERT INTO memory_blocks(name, content, version,"
        " updated_by, updated_at) VALUES(?,?,?,?,?)"
        " ON CONFLICT(name) DO UPDATE SET content=excluded.content,"
        " version=excluded.version, updated_by=excluded.updated_by,"
        " updated_at=excluded.updated_at",
        (name, content, int(old["version"]) + 1, edited_by, now))
    from .evolve import archive
    archive.note(conn, s, "memory_block", name, content,
                 created_by=edited_by, status="live")
    return get(conn, name)


def history(conn, name: str = "", limit: int = 20) -> list:
    q = "SELECT * FROM memory_block_history"
    args = []
    if name:
        q += " WHERE name=?"
        args.append(name)
    return [dict(r) for r in conn.execute(
        q + " ORDER BY id DESC LIMIT ?",
        (*args, int(limit))).fetchall()]


def undo(conn, hid: int = 0, edited_by: str = "owner") -> dict:
    """Restore a history row's content (itself recorded in history).
    hid=0 undoes the most recent edit."""
    q = ("SELECT * FROM memory_block_history"
         + (" WHERE id=?" if hid else " ORDER BY id DESC LIMIT 1"))
    r = conn.execute(q, (int(hid),) if hid else ()).fetchone()
    if r is None:
        return {"error": "not_found", "message": "nothing to undo"}
    return set_block(conn, r["name"], r["content"],
                     edited_by=edited_by)


def prompt_block(conn) -> str:
    """The always-on injection block — '' when every block is empty
    so flagged-off costs zero prompt tokens."""
    parts = []
    for b in list_blocks(conn):
        if b["content"].strip():
            label = {"persona": "How you sound",
                     "human": "The owner",
                     "goals": "Standing goals",
                     "world": "World context"}[b["name"]]
            parts.append(f"[{label}]\n{b['content'].strip()}")
    if not parts:
        return ""
    return "\n\nPinned context (always applies):\n" + "\n\n".join(parts)


def card_lines(conn) -> list:
    out = []
    for b in list_blocks(conn):
        body = b["content"].strip() or "(empty)"
        out.append(
            f"**{b['name']}** v{b['version']} "
            f"({len(b['content'])}/{cap(b['name'])} chars, last by "
            f"{b['updated_by'] or '-'})\n{body[:600]}")
    return out
