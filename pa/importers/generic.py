"""Generic chat-history importer — same shape as chatgpt.py but for
loose .md/.txt/.json files (inside a zip or uploaded bare). Each file
becomes one note under Imports/Files/<name>; a JSON file holding a list
of {title, messages:[{role,text}]} dicts is split per item instead.
"""

import io
import json
import re
import zipfile

from .. import util, vault

SOURCE = "generic"
PREFIX = "Imports/Files"


def _slug(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (title or "untitled").lower())
    return slug.strip("-")[:60] or "untitled"


def _entries(blob: bytes, filename: str):
    """-> [(name, kind, bytes)] for each importable member."""
    if filename.lower().endswith(".zip") or blob[:4] == b"PK\x03\x04":
        zf = zipfile.ZipFile(io.BytesIO(blob))
        return [(n, n.rsplit(".", 1)[-1].lower(), zf.read(n))
                for n in zf.namelist()
                if not n.endswith("/") and
                n.rsplit(".", 1)[-1].lower() in ("md", "txt", "json")]
    kind = filename.rsplit(".", 1)[-1].lower() \
        if "." in filename else "txt"
    return [(filename or "upload.txt", kind, blob)]


def _json_items(data: bytes):
    """A JSON list of {id?, title?, messages?} -> [(ext_id, title, md)]
    or None when it isn't that shape."""
    try:
        obj = json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(obj, list):
        return None
    out = []
    for i, item in enumerate(obj):
        if not isinstance(item, dict) or "messages" not in item:
            return None
        msgs = item.get("messages") or []
        lines = [f"# {item.get('title') or f'Conversation {i+1}'}", ""]
        for m in msgs:
            role = (m.get("role") or "?") if isinstance(m, dict) else "?"
            text = m.get("text") or m.get("content") or "" \
                if isinstance(m, dict) else str(m)
            lines += [f"**{role}:**", "", str(text), ""]
        out.append((str(item.get("id") or f"{i}"),
                    item.get("title") or f"conversation-{i+1}",
                    "\n".join(lines)))
    return out


def run(conn, s, env, blob: bytes, filename: str = "") -> dict:
    seen = {r[0] for r in conn.execute(
        "SELECT external_id FROM imported_items WHERE source=?",
        (SOURCE,))}
    items, new_ids, skipped = [], [], 0
    for name, kind, data in _entries(blob, filename):
        ext = name
        if kind == "json":
            convs = _json_items(data)
            if convs:
                for cid, title, md in convs:
                    key = f"{name}:{cid}"
                    if key in seen:
                        skipped += 1
                        continue
                    items.append((f"{PREFIX}/{_slug(title)}", md))
                    new_ids.append((key,
                                    f"{PREFIX}/{_slug(title)}.md"))
                continue
            kind = "txt"              # opaque JSON -> keep as a note
        if ext in seen:
            skipped += 1
            continue
        try:
            body = data.decode("utf-8", "replace")
        except Exception:
            skipped += 1
            continue
        base = name.split("/")[-1].rsplit(".", 1)[0]
        path = f"{PREFIX}/{_slug(base)}"
        items.append((path, f"# {base}\n\n{body}"))
        new_ids.append((ext, f"{path}.md"))
    out = vault.write_notes(conn, s, items, git_add="Imports")
    now = util.now_iso()
    for ext, path in new_ids:
        conn.execute(
            "INSERT OR IGNORE INTO imported_items(source, external_id,"
            " path, imported_at) VALUES(?,?,?,?)",
            (SOURCE, ext, path, now))
    return {"source": SOURCE, "imported": len(new_ids),
            "skipped": skipped, "digests": 0, "git": out["git"]}
