"""ChatGPT data-export importer.

The owner's ChatGPT export (Settings -> Data controls -> Export) arrives
as a zip whose payload is conversations.json: a list of conversations,
each with {id, title, create_time, mapping}. mapping is a node tree;
walking children from the root yields the transcript.

run() writes one Markdown note per conversation plus one monthly digest
per month under Imports/ChatGPT/, and records each conversation id in
imported_items so repeats are skipped.
"""

import io
import json
import re
import time
import zipfile

SOURCE = "chatgpt"
PREFIX = "Imports/ChatGPT"


def _conversations(blob: bytes, filename: str):
    """-> parsed conversations.json list from a zip or the raw JSON."""
    data = blob
    if filename.lower().endswith(".zip") or blob[:4] == b"PK\x03\x04":
        zf = zipfile.ZipFile(io.BytesIO(blob))
        pick = next((n for n in zf.namelist()
                     if n.split("/")[-1] == "conversations.json"), None)
        if pick is None:
            raise ValueError("no conversations.json in that zip")
        data = zf.read(pick)
    convs = json.loads(data.decode("utf-8"))
    if not isinstance(convs, list):
        raise ValueError("conversations.json isn't a list")
    return convs


def _walk(conv) -> list:
    """mapping node tree -> [(role, text)] on the main branch."""
    mapping = conv.get("mapping") or {}
    msgs = []
    node = next((n for n in mapping.values()
                 if not n.get("parent")), None)
    while node:
        msg = node.get("message") or {}
        role = ((msg.get("author") or {}).get("role") or "")
        parts = ((msg.get("content") or {}).get("parts") or [])
        text = "\n".join(p for p in parts if isinstance(p, str)).strip()
        if role in ("user", "assistant") and text:
            msgs.append((role, text))
        children = node.get("children") or []
        node = mapping.get(children[0]) if children else None
    return msgs


def _slug(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (title or "untitled").lower())
    return slug.strip("-")[:60] or "untitled"


def _note(conv, msgs) -> str:
    title = conv.get("title") or "Untitled"
    when = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(
        conv.get("create_time") or 0))
    lines = [f"# {title}", "",
             f"*ChatGPT conversation — {when} "
             f"(id `{conv.get('id')}`)*", "", "---", ""]
    for role, text in msgs:
        who = "**You:**" if role == "user" else "**ChatGPT:**"
        lines += [who, "", text, ""]
    return "\n".join(lines)


def _digest(month: str, convs) -> str:
    lines = [f"# ChatGPT imports — {month}", ""]
    for conv, msgs in convs:
        first = next((t for r, t in msgs if r == "user"), "")[:120]
        first = first.replace("\n", " ")
        lines.append(f"- **{conv.get('title') or 'Untitled'}** "
                     f"({len(msgs)} msgs) — {first}")
    lines.append("")
    return "\n".join(lines)


def run(conn, s, env, blob: bytes, filename: str = "") -> dict:
    from .. import util, vault
    convs = _conversations(blob, filename)
    seen = {r[0] for r in conn.execute(
        "SELECT external_id FROM imported_items WHERE source=?",
        (SOURCE,))}
    items, months, new_ids = [], {}, []
    skipped = 0
    for conv in convs:
        cid = str(conv.get("id") or "")
        if not cid:
            continue
        if cid in seen:
            skipped += 1
            continue
        msgs = _walk(conv)
        if not msgs:
            skipped += 1
            continue
        month = time.strftime("%Y-%m", time.gmtime(
            conv.get("create_time") or 0))
        path = f"{PREFIX}/{month}-{_slug(conv.get('title'))}-{cid[:8]}"
        items.append((path, _note(conv, msgs)))
        months.setdefault(month, []).append((conv, msgs))
        new_ids.append((cid, path))
    for month, group in months.items():
        items.append((f"{PREFIX}/_digest-{month}", _digest(month, group)))
    out = vault.write_notes(conn, s, items, git_add="Imports")
    now = util.now_iso()
    for cid, path in new_ids:
        conn.execute(
            "INSERT OR IGNORE INTO imported_items(source, external_id,"
            " path, imported_at) VALUES(?,?,?,?)",
            (SOURCE, cid, path, now))
    return {"source": SOURCE, "imported": len(new_ids),
            "skipped": skipped, "digests": len(months),
            "git": out["git"]}
