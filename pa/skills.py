"""Skills library (docs/PARITY.md #6).

A skill is a short owner-authored procedure — a Markdown note under
`Skills/<name>.md` in the vault (source of truth when VAULT_PATH is
configured, so it syncs to the owner's notes), or a row in the SQLite
`skills` table when it isn't.

- skill_list: name + description for the system prompt.
- skill_load: full body — fenced as an owner procedure but size-capped;
  it is still model input, not a command channel.
- skill_save: write through to the vault (with front matter) or SQLite;
  names are slugged so paths can never escape `Skills/`.
"""

import re

from . import db as dbmod
from .util import now_iso

BODY_CAP = 20000
SKILL_DIR = "Skills"

_SLUG_OK = re.compile(r"^[a-z0-9-]+$")


def slug(name: str) -> str:
    """'Make Pancakes!' -> 'make-pancakes'. Pure [a-z0-9-]: the slug
    becomes the filename, so / . and escapes are impossible by
    construction."""
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return s[:80] or "skill"


def _from_vault(conn, settings, name: str):
    from . import vault
    try:
        return vault.read_note(conn, settings, f"{SKILL_DIR}/{name}.md")
    except Exception:
        return None


def _front_body(text: str):
    """Split `---\nname:/description:...\n---` front matter from body."""
    desc = ""
    body = text or ""
    if body.startswith("---"):
        end = body.find("\n---", 3)
        if end != -1:
            head, body = body[3:end], body[end + 4:]
            for line in head.splitlines():
                k, _, v = line.partition(":")
                if k.strip() == "description":
                    desc = v.strip().strip('"').strip("'")
    return desc, body.strip()


def list_skills(conn, settings) -> list:
    """[{name, description}] for prompts/cards — vault first, then
    SQLite rows not already in the vault list."""
    out, seen = [], set()
    from . import vault
    if vault.configured(settings):
        rows = conn.execute(
            "SELECT path, title FROM vault_notes"
            " WHERE path LIKE ? ORDER BY path",
            (f"{SKILL_DIR}/%",)).fetchall()
        for r in rows:
            name = r["path"].rsplit("/", 1)[-1][:-3]
            if not _SLUG_OK.match(name):
                continue
            note = _from_vault(conn, settings, name)
            desc = ""
            if note:
                desc = _front_body(note["content"])[0] or r["title"]
            out.append({"name": name, "description": desc})
            seen.add(name)
    for r in conn.execute(
            "SELECT name, description FROM skills ORDER BY name"
            ).fetchall():
        if r["name"] not in seen:
            out.append({"name": r["name"],
                        "description": r["description"]})
    return out


def load_skill(conn, settings, name: str) -> dict:
    """The full body of one skill (capped), or {error: not_found}."""
    name = slug(name)
    note = _from_vault(conn, settings, name)
    if note is not None:
        _, body = _front_body(note["content"])
        return {"name": name, "body": body[:BODY_CAP],
                "source": "vault",
                "truncated": len(body) > BODY_CAP}
    row = conn.execute(
        "SELECT body FROM skills WHERE name=?", (name,)).fetchone()
    if row is None:
        return {"error": "not_found", "name": name}
    body = row["body"]
    return {"name": name, "body": body[:BODY_CAP], "source": "sqlite",
            "truncated": len(body) > BODY_CAP}


def _versions_exist(conn) -> bool:
    try:
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table'"
            " AND name='skill_versions'").fetchone() is not None
    except Exception:
        return False


def _next_version(conn, name: str) -> int:
    row = conn.execute(
        "SELECT MAX(version) v FROM skill_versions WHERE name=?",
        (name,)).fetchone()
    return (row["v"] or 0) + 1


def _record_version(conn, name, body, description, source) -> int:
    """Append this body as a new version row. Returns the version."""
    if not _versions_exist(conn):
        return 0
    v = _next_version(conn, name)
    conn.execute(
        "INSERT OR REPLACE INTO skill_versions"
        "(name, version, body, description, created_at, source)"
        " VALUES(?,?,?,?,?,?)",
        (name, v, body[:BODY_CAP], (description or "")[:200],
         now_iso(), str(source or "owner")[:20]))
    return v


def _live(conn, settings, name: str):
    """Current body+desc wherever the skill lives, or None."""
    note = _from_vault(conn, settings, name)
    if note is not None:
        desc, body = _front_body(note["content"])
        return body, desc
    row = conn.execute(
        "SELECT body, description FROM skills WHERE name=?",
        (name,)).fetchone()
    return (row["body"], row["description"]) if row else None


def save_skill(conn, settings, name: str, description: str,
               body: str, source: str = "owner") -> dict:
    """Write one skill — vault note `Skills/<slug>.md` (front matter +
    body) when the mirror is configured, else upsert the SQLite row.
    Every saved body is recorded in skill_versions so Track A can
    snapshot/restore (docs/EVOLVE.md #8)."""
    name = slug(name)
    desc = (description or "").strip().replace("\n", " ")[:200]
    body = (body or "")[:BODY_CAP]
    text = (f"---\nname: {name}\ndescription: {desc}\n---\n\n"
            f"{body}\n")
    version = _record_version(conn, name, body, desc, source)
    from .evolve import archive
    archive.note(conn, settings, "skill", name,
                 {"body": body, "description": desc},
                 created_by=source, status="live")
    from . import vault
    if vault.configured(settings):
        out = vault.write_note(conn, settings, f"{SKILL_DIR}/{name}.md",
                               text)
        dbmod.audit(conn, "assistant", "skill_save",
                    f"vault:{out['path']}")
        return {"saved": True, "name": name, "path": out["path"],
                "git": out["git"], "version": version}
    conn.execute(
        "INSERT INTO skills(name, description, body, updated_at)"
        " VALUES(?,?,?,?) ON CONFLICT(name) DO UPDATE SET"
        " description=excluded.description, body=excluded.body,"
        " updated_at=excluded.updated_at",
        (name, desc, body, now_iso()))
    dbmod.audit(conn, "assistant", "skill_save", f"sqlite:{name}")
    return {"saved": True, "name": name, "path": f"skills:{name}",
            "git": "no vault", "version": version}


def snapshot_skill(conn, settings, name: str) -> dict:
    """What Track A restores on failure: the current live body."""
    name = slug(name)
    live = _live(conn, settings, name)
    if live is None:
        return {"name": name, "exists": False, "body": "",
                "description": ""}
    return {"name": name, "exists": True,
            "body": live[0], "description": live[1]}


def restore_skill(conn, settings, name: str, version: int) -> dict:
    """Write a recorded version back (itself recorded, so a rollback
    is reversible)."""
    name = slug(name)
    row = conn.execute(
        "SELECT body, description, source FROM skill_versions"
        " WHERE name=? AND version=?", (name, int(version))
        ).fetchone()
    if row is None:
        return {"error": "not_found", "name": name,
                "version": version}
    out = save_skill(conn, settings, name, row["description"],
                     row["body"], source=f"rollback:{row['source']}")
    out["restored_version"] = int(version)
    return out


def prompt_block(conn, settings) -> str:
    """One-line-per-skill listing for the system prompt (or '')."""
    skills = list_skills(conn, settings)
    if not skills:
        return ""
    lines = ["Saved skills (call skill_load to run one):"]
    for s in skills[:50]:
        tail = f" — {s['description']}" if s["description"] else ""
        lines.append(f"- {s['name']}{tail}")
    return "\n".join(lines)
