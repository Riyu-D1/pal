"""Obsidian vault mirror — read-only scoped indexing and retrieval.

VAULT_PATH points at a Markdown mirror synced onto the cloud host (the
owner mounts a synced folder; we never touch the Mac filesystem). Rules:

- .md files only; everything under the vault root after realpath —
  symlinks, symlinked dirs and path escapes are skipped and counted.
- Hidden files/dirs (anything starting '.', which includes .obsidian/)
  and secret-like names (*.key, *.pem, *.env, id_rsa*, *secret*,
  *credential*, *.kdbx, *.p12, *.pfx, *.keystore) are never read.
- Per-file byte cap, a max file count, and a per-answer character cap.
- Note text is UNTRUSTED DATA: tool results wrap it in explicit markers
  and the system prompt tells the model never to follow instructions
  found inside.
- Index lives in vault_notes + the shared FTS5 search_index as
  entity='vault_note'; assistants see excerpts, never raw paths outside
  the vault.
"""

import datetime
import hashlib
import os
import re

from .util import now_iso, parse_iso, truncate_middle, utcnow

MAX_RESULTS_HARD = 20

_SECRET_NAME = re.compile(
    r"(?i)(^id_rsa|^id_ed25519|\.pem$|\.key$|\.env$|\.env\.|secret|"
    r"credential|\.kdbx$|\.p12$|\.pfx$|\.keystore$|\.asc$|\.gpg$)")

_UNTRUSTED_OPEN = "<<<UNTRUSTED NOTE {path} >>>"
_UNTRUSTED_CLOSE = "<<<END UNTRUSTED NOTE>>>"


class VaultNotConfigured(Exception):
    kind = "vault_not_configured"


def configured(settings) -> bool:
    return bool(settings.vault_path)


def _root(settings) -> str:
    path = (settings.vault_path or "").strip()
    if not path:
        raise VaultNotConfigured(
            "VAULT_PATH is not set — mount a synced Markdown mirror and "
            "set VAULT_PATH to it")
    root = os.path.realpath(path)
    if not os.path.isdir(root):
        raise VaultNotConfigured(f"VAULT_PATH {path!r} is not a directory")
    return root


def _secretish(name: str) -> bool:
    return bool(_SECRET_NAME.search(name))


def _walk_files(root: str, max_files: int):
    """Yield relative paths of indexable .md files; count skips."""
    stats = {"seen": 0, "skipped_hidden": 0, "skipped_secret": 0,
             "skipped_symlink": 0, "skipped_escape": 0,
             "skipped_oversize": 0, "skipped_nonmd": 0}
    out = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        # prune hidden + symlinked dirs in place
        keep = []
        for d in dirnames:
            full = os.path.join(dirpath, d)
            if d.startswith("."):
                stats["skipped_hidden"] += 1
            elif os.path.islink(full):
                stats["skipped_symlink"] += 1
            elif not os.path.realpath(full).startswith(root + os.sep):
                stats["skipped_escape"] += 1
            else:
                keep.append(d)
        dirnames[:] = keep
        for fn in filenames:
            stats["seen"] += 1
            if len(out) >= max_files:
                break
            full = os.path.join(dirpath, fn)
            if fn.startswith("."):
                stats["skipped_hidden"] += 1
                continue
            if _secretish(fn):
                stats["skipped_secret"] += 1
                continue
            if not fn.lower().endswith(".md"):
                stats["skipped_nonmd"] += 1
                continue
            if os.path.islink(full):
                stats["skipped_symlink"] += 1
                continue
            real = os.path.realpath(full)
            if not real.startswith(root + os.sep):
                stats["skipped_escape"] += 1
                continue
            rel = os.path.relpath(real, root)
            out.append((rel, real))
    return out, stats


def _title_of(text: str, fallback: str) -> str:
    for line in text.split("\n")[:20]:
        line = line.strip()
        if line.startswith("#"):
            return line.lstrip("#").strip()[:200] or fallback
    return fallback


def scan(conn, settings) -> dict:
    """Incremental reindex. Returns stats. Read-only on the vault."""
    root = _root(settings)
    files, stats = _walk_files(root, settings.vault_max_files)
    indexed = 0
    seen_paths = set()
    for rel, real in files:
        seen_paths.add(rel)
        try:
            st = os.stat(real)
        except OSError:
            continue
        if st.st_size > settings.vault_max_file_bytes:
            stats["skipped_oversize"] += 1
            continue
        row = conn.execute(
            "SELECT id, sha FROM vault_notes WHERE path=?",
            (rel,)).fetchone()
        try:
            with open(real, "rb") as f:
                raw = f.read(settings.vault_max_file_bytes + 1)
        except OSError:
            continue
        sha = hashlib.sha256(raw).hexdigest()[:32]
        if row and row["sha"] == sha:
            continue  # unchanged
        text = raw.decode("utf-8", "replace")
        title = _title_of(text, os.path.basename(rel))
        if row is None:
            cur = conn.execute(
                "INSERT INTO vault_notes(path, title, sha, mtime, size,"
                " indexed_at) VALUES(?,?,?,?,?,?)",
                (rel, title, sha, st.st_mtime, st.st_size, now_iso()))
            nid = cur.lastrowid
        else:
            nid = row["id"]
            conn.execute(
                "UPDATE vault_notes SET title=?, sha=?, mtime=?, size=?,"
                " indexed_at=? WHERE id=?",
                (title, sha, st.st_mtime, st.st_size, now_iso(), nid))
        body = f"{title}\n{truncate_middle(text, 60000)}"
        conn.execute(
            "DELETE FROM search_index WHERE entity='vault_note' AND"
            " entity_id=?", (str(nid),))
        conn.execute(
            "INSERT INTO search_index(entity, entity_id, body)"
            " VALUES('vault_note',?,?)", (str(nid), body))
        indexed += 1
    # drop notes that vanished from the mirror
    stale = conn.execute("SELECT id, path FROM vault_notes").fetchall()
    removed = 0
    for r in stale:
        if r["path"] not in seen_paths:
            conn.execute("DELETE FROM vault_notes WHERE id=?", (r["id"],))
            conn.execute(
                "DELETE FROM search_index WHERE entity='vault_note'"
                " AND entity_id=?", (str(r["id"]),))
            removed += 1
    stats.update({"indexed": indexed, "removed": removed,
                  "total": len(seen_paths)})
    return stats


def _ensure_scan(conn, settings):
    """Scan on first use and refresh at most every 60s."""
    row = conn.execute(
        "SELECT MAX(indexed_at) m FROM vault_notes").fetchone()
    fresh = False
    if row and row["m"]:
        try:
            fresh = (utcnow() - parse_iso(row["m"])) < \
                    datetime.timedelta(seconds=60)
        except Exception:
            fresh = False
    if row is None or row["m"] is None or not fresh:
        scan(conn, settings)


def search(conn, settings, query: str, limit: int = None):
    """FTS5 over vault notes. Returns [{path, title, snippet}] with the
    snippet wrapped as untrusted data."""
    root = _root(settings)
    _ensure_scan(conn, settings)
    q = (query or "").strip()
    if not q:
        return []
    fts_q = " OR ".join(t.replace('"', "") for t in q.split() if t) or q
    lim = max(1, min(int(limit or settings.vault_max_results),
                     MAX_RESULTS_HARD))
    rows = conn.execute(
        "SELECT v.path, v.title, snippet(search_index, 2, '[', ']',"
        " '…', 40) snip FROM search_index s JOIN vault_notes v ON"
        " s.entity_id = CAST(v.id AS TEXT) WHERE s.entity='vault_note'"
        " AND search_index MATCH ? LIMIT ?",
        (fts_q, lim)).fetchall()
    return [{"path": r["path"], "title": r["title"],
             "snippet": _wrap(r["path"], r["snip"])} for r in rows]


def read_note(conn, settings, rel_path: str):
    """One note's text (capped). rel_path must resolve inside the vault."""
    root = _root(settings)
    _ensure_scan(conn, settings)
    rel = (rel_path or "").strip()
    if not rel or rel.startswith("/") or rel.startswith("..") \
            or rel.startswith("."):
        raise VaultNotConfigured("bad note path")
    real = os.path.realpath(os.path.join(root, rel))
    if not real.startswith(root + os.sep):
        raise VaultNotConfigured("path escapes the vault")
    if os.path.islink(os.path.join(root, rel)):
        raise VaultNotConfigured("note is a symlink")
    row = conn.execute(
        "SELECT title, size FROM vault_notes WHERE path=?",
        (rel,)).fetchone()
    if row is None:
        return None
    with open(real, "rb") as f:
        raw = f.read(settings.vault_note_cap + 1)
    text = raw.decode("utf-8", "replace")
    return {"path": rel, "title": row["title"],
            "content": _wrap(rel, truncate_middle(
                text, settings.vault_note_cap)),
            "truncated": len(raw) > settings.vault_note_cap}


def _wrap(path: str, body: str) -> str:
    return f"{_UNTRUSTED_OPEN.format(path=path)}\n{body}\n{_UNTRUSTED_CLOSE}"


# -- agent writes (approval-gated at the tool layer) -------------------------

def _resolve_write_path(settings, rel_path: str):
    """Same safety rules as reads: inside the vault, no hidden/secret/
    symlink paths, .md only. Returns (root, rel, real)."""
    root = _root(settings)
    rel = (rel_path or "").strip().lstrip("/")
    if not rel or rel.startswith("..") or rel.startswith("."):
        raise VaultNotConfigured("bad note path")
    if not rel.lower().endswith(".md"):
        rel += ".md"
    if _secretish(os.path.basename(rel)):
        raise VaultNotConfigured("refusing to write a secret-like name")
    parts = rel.split("/")
    if any(p.startswith(".") for p in parts):
        raise VaultNotConfigured("refusing to write inside a hidden dir")
    real = os.path.realpath(os.path.join(root, rel))
    if not real.startswith(root + os.sep):
        raise VaultNotConfigured("path escapes the vault")
    if os.path.islink(real):
        raise VaultNotConfigured("path is a symlink")
    return root, rel, real


def write_note(conn, settings, rel_path: str, content: str,
               append: bool = False):
    """Write (or append to) one Markdown note inside the vault mirror,
    then reindex. Returns {path, bytes, existed, git}."""
    root, rel, real = _resolve_write_path(settings, rel_path)
    existed = os.path.exists(real)
    text = str(content or "")[:200_000]
    if append and text and not text.startswith("\n"):
        text = "\n" + text
    os.makedirs(os.path.dirname(real), exist_ok=True)
    with open(real, "a" if append else "w", encoding="utf-8") as f:
        f.write(text)
    scan(conn, settings)            # reindex so search sees it at once
    return {"path": rel, "bytes": len(text), "existed": existed,
            "appended": append, "git": _git_sync(root, rel, settings)}


def write_notes(conn, settings, items, git_add: str = ""):
    """Batch-write several .md notes with ONE reindex + one git sync.
    items: [(rel_path, content), ...]. git_add is the vault-relative
    pathspec committed (defaults to the first note's top dir)."""
    outs = []
    for rel_path, content in items:
        root, rel, real = _resolve_write_path(settings, rel_path)
        existed = os.path.exists(real)
        text = str(content or "")[:200_000]
        os.makedirs(os.path.dirname(real), exist_ok=True)
        with open(real, "w", encoding="utf-8") as f:
            f.write(text)
        outs.append({"path": rel, "bytes": len(text),
                     "existed": existed})
    scan(conn, settings)
    if not outs:
        return {"notes": [], "git": "skipped"}
    root = _root(settings)
    spec = git_add or outs[0]["path"].split("/")[0]
    return {"notes": outs, "git": _git_sync(root, spec, settings)}


def _git_sync(root: str, rel: str, settings) -> str:
    """Best-effort `git add+commit+push` for a synced vault mirror.
    Off unless VAULT_GIT_SYNC=true and the mirror is a git repo."""
    if not getattr(settings, "vault_git_sync", False):
        return "disabled"
    if not os.path.isdir(os.path.join(root, ".git")):
        return "not a git repo"
    import subprocess
    try:
        for argv in (["git", "add", "--", rel],
                     ["git", "commit", "-m", f"pa: update {rel}"],
                     ["git", "push"]):
            r = subprocess.run(argv, cwd=root, capture_output=True,
                               text=True, timeout=30)
            if r.returncode != 0:
                tail = (r.stderr or r.stdout or "").strip()[:200]
                if argv[1] == "commit" and "nothing to commit" in \
                        (r.stdout or ""):
                    continue
                return f"git {argv[1]} failed: {tail}"
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"git sync failed: {e}"
    return "pushed"
