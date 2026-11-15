"""W1: file workspace — durable per-project files with versions.

Root is ``<PA_DATA_DIR>/workspace/<project>/``. Version metadata lives
in the ``files`` table (migration 36); bytes live on disk: the latest
copy at ``<project>/<path>`` and every version archived under
``<project>/.versions/<path>.v<N>`` (hard-linked when possible so the
latest file and its archive share blocks).

- ``PA_WORKSPACE`` (default off) enables the file_* tools and /files;
  off means exactly base behaviour — file_write still only attaches.
- ``PA_WORKSPACE_MB`` (default 500) caps total bytes across every
  project and every archived version.
- ``PA_WORKSPACE_PROJECT`` names the default project ("main").

Paths are relative POSIX names: ``..``, absolute paths, NUL, the
``.versions`` prefix and anything that escapes the project dir are
rejected before touching the filesystem.
"""

import hashlib
import os
import re
import shutil

from .util import now_iso

MAX_FILE_BYTES = 10 * 1024 * 1024       # one file, latest version
PROJECT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,60}$")
_PATH_PART = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class WorkspaceError(Exception):
    """kind in .kind: bad_path | bad_project | too_large | not_found |
    cap_exceeded"""


def root(s) -> str:
    return os.path.join(s.data_dir, "workspace")


def _check_project(project: str) -> str:
    project = (project or "").strip().lower()
    if not PROJECT_RE.match(project):
        raise WorkspaceError(f"bad_project:{project or '(empty)'}")
    return project


def project_dir(s, project: str) -> str:
    return os.path.join(root(s), _check_project(project))


def _safe_relpath(path: str) -> str:
    """Normalise a user-supplied name to a safe relative path."""
    p = (path or "").strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    if not p or p.startswith("/") or "\x00" in p:
        raise WorkspaceError(f"bad_path:{path[:80]}")
    parts = p.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise WorkspaceError(f"bad_path:{path[:80]}")
    if parts[0] == ".versions":
        raise WorkspaceError("bad_path:reserved .versions")
    if any(not _PATH_PART.match(part) for part in parts):
        raise WorkspaceError(f"bad_path:{path[:80]}")
    if len(parts) > 8 or len(p) > 200:
        raise WorkspaceError("bad_path:too deep/long")
    return "/".join(parts)


def _real_path(s, project: str, relpath: str):
    """Absolute path inside the project dir — re-checks containment
    after abspath so a symlinked component can't escape."""
    base = os.path.abspath(project_dir(s, project))
    real = os.path.abspath(os.path.join(base, relpath))
    if real != base and not real.startswith(base + os.sep):
        raise WorkspaceError(f"bad_path:{relpath[:80]}")
    return base, real


def _version_path(s, project, relpath, version):
    base = os.path.abspath(project_dir(s, project))
    real = os.path.abspath(os.path.join(
        base, ".versions", f"{relpath}.v{int(version)}"))
    if not real.startswith(os.path.join(base, ".versions") + os.sep):
        raise WorkspaceError(f"bad_path:{relpath[:80]}")
    return real


def _used_bytes(conn, project=None) -> int:
    """Committed bytes: sum over every stored version row."""
    if project:
        row = conn.execute(
            "SELECT COALESCE(SUM(size_bytes),0) b FROM files"
            " WHERE project=?", (project,)).fetchone()
    else:
        row = conn.execute(
            "SELECT COALESCE(SUM(size_bytes),0) b FROM files").fetchone()
    return int(row["b"] if row else 0)


def _latest(conn, project, path):
    return conn.execute(
        "SELECT * FROM files WHERE project=? AND path=?"
        " ORDER BY version DESC LIMIT 1",
        (project, path)).fetchone()


def _link(src, dst):
    try:
        os.link(src, dst)
    except OSError:
        shutil.copyfile(src, dst)


def _atomic_write(path, data):
    """New inode for the new content — never truncates in place, so a
    hard-linked .versions copy keeps the OLD bytes."""
    tmp = f"{path}.tmp{os.getpid()}"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def put(conn, s, project, path, data: bytes, project_default="main"):
    """Write a new version of project/path. Returns a dict with
    version+sha; WorkspaceError on bad input or the byte cap."""
    project = _check_project(project or project_default)
    rel = _safe_relpath(path)
    data = bytes(data or b"")
    if len(data) > MAX_FILE_BYTES:
        raise WorkspaceError(
            f"too_large:{len(data)}>{MAX_FILE_BYTES}")
    cap = int(getattr(s, "workspace_mb", 500) or 500) * 1024 * 1024
    if _used_bytes(conn) + len(data) > cap:
        raise WorkspaceError("cap_exceeded:PA_WORKSPACE_MB")

    row = _latest(conn, project, rel)
    version = int(row["version"]) + 1 if row else 1
    _base, real = _real_path(s, project, rel)
    os.makedirs(os.path.dirname(real), exist_ok=True)
    if row and not row["deleted"] and os.path.exists(real):
        # archive the outgoing latest copy before overwriting
        prev = _version_path(s, project, rel, version - 1)
        os.makedirs(os.path.dirname(prev), exist_ok=True)
        if not os.path.exists(prev):
            _link(real, prev)
    elif row:
        # resurrecting a deleted file: keep tombstone history only
        pass
    _atomic_write(real, data)
    vpath = _version_path(s, project, rel, version)
    os.makedirs(os.path.dirname(vpath), exist_ok=True)
    if not os.path.exists(vpath):
        _link(real, vpath)

    sha = hashlib.sha256(data).hexdigest()[:32]
    conn.execute(
        "INSERT INTO files(project, path, version, size_bytes, sha256,"
        " deleted, created_at) VALUES(?,?,?,?,?,0,?)",
        (project, rel, version, len(data), sha, now_iso()))
    _index_title(conn, s, project, rel, data)
    return {"project": project, "path": rel, "version": version,
            "size": len(data), "sha256": sha}


def read(conn, s, project, path, version=None, max_bytes=200_000):
    """Return {project,path,version,size,sha256,content} — content is
    decoded utf-8 (replace) capped at max_bytes."""
    project = _check_project(project or "main")
    rel = _safe_relpath(path)
    latest = _latest(conn, project, rel)
    if version is None:
        row = latest
    else:
        row = conn.execute(
            "SELECT * FROM files WHERE project=? AND path=? AND"
            " version=?", (project, rel, int(version))).fetchone()
    if row is None:
        raise WorkspaceError(f"not_found:{rel}")
    if row["deleted"]:
        raise WorkspaceError(f"not_found:{rel} (deleted)")
    if latest is not None and \
            int(row["version"]) == int(latest["version"]):
        _, real = _real_path(s, project, rel)
        vpath = real if os.path.exists(real) else \
            _version_path(s, project, rel, row["version"])
    else:
        vpath = _version_path(s, project, rel, row["version"])
    try:
        with open(vpath, "rb") as f:
            data = f.read(int(max_bytes) + 1)
    except OSError:
        raise WorkspaceError(f"not_found:{rel} bytes missing")
    truncated = len(data) > max_bytes
    return {"project": project, "path": rel,
            "version": int(row["version"]), "size": int(row["size_bytes"]),
            "sha256": row["sha256"], "deleted": bool(row["deleted"]),
            "content": data[:max_bytes].decode("utf-8", "replace"),
            "truncated": truncated}


def remove(conn, s, project, path):
    """Tombstone: a new version row with deleted=1; the working copy
    is removed, archived versions stay readable."""
    project = _check_project(project or "main")
    rel = _safe_relpath(path)
    row = _latest(conn, project, rel)
    if row is None or row["deleted"]:
        raise WorkspaceError(f"not_found:{rel}")
    _, real = _real_path(s, project, rel)
    if os.path.exists(real):
        prev = _version_path(s, project, rel, row["version"])
        os.makedirs(os.path.dirname(prev), exist_ok=True)
        if not os.path.exists(prev):
            _link(real, prev)
        os.unlink(real)
    conn.execute(
        "INSERT INTO files(project, path, version, size_bytes, sha256,"
        " deleted, created_at) VALUES(?,?,?,0,'',1,?)",
        (project, rel, int(row["version"]) + 1, now_iso()))
    return {"project": project, "path": rel,
            "version": int(row["version"]) + 1, "deleted": True}


def versions(conn, s, project, path) -> list:
    project = _check_project(project or "main")
    rel = _safe_relpath(path)
    rows = conn.execute(
        "SELECT version, size_bytes, sha256, deleted, created_at"
        " FROM files WHERE project=? AND path=? ORDER BY version",
        (project, rel)).fetchall()
    return [dict(r) for r in rows]


def list_files(conn, s, project=None, limit=200) -> list:
    """Latest non-deleted row per path."""
    sql = (
        "SELECT f.project, f.path, f.version, f.size_bytes, f.sha256,"
        " f.created_at FROM files f JOIN (SELECT project, path,"
        " MAX(version) v FROM files GROUP BY project, path) m"
        " ON m.project=f.project AND m.path=f.path AND m.v=f.version"
        " WHERE f.deleted=0")
    args = []
    if project:
        sql += " AND f.project=?"
        args.append(_check_project(project))
    sql += " ORDER BY f.project, f.path LIMIT ?"
    args.append(int(limit))
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def usage(conn, s) -> dict:
    mb = int(getattr(s, "workspace_mb", 500) or 500)
    row = conn.execute(
        "SELECT COUNT(DISTINCT project) p FROM files").fetchone()
    used = _used_bytes(conn)
    return {"bytes": used, "cap_bytes": mb * 1024 * 1024,
            "projects": int(row["p"] if row else 0)}


def files_line(conn, s, project=None) -> str:
    """/files reply: usage + latest files."""
    u = usage(conn, s)
    files = list_files(conn, s, project, limit=30)
    head = (f"workspace: {u['bytes'] // 1024} KiB of "
            f"{u['cap_bytes'] // (1024 * 1024)} MiB "
            f"across {u['projects']} project(s)")
    if not files:
        return head + "\n(no files yet)"
    lines = [f"{f['project']}/{f['path']} v{f['version']}"
             f" ({f['size_bytes']} B)" for f in files]
    return head + "\n" + "\n".join(lines)


def _index_title(conn, s, project, path, data):
    """Feed file titles into Run 2's recall index when present —
    feature-detected: final-2 may land pa/recall.py later."""
    try:
        from . import recall as recallmod
    except ImportError:
        return
    try:
        title = f"{project}/{path}"
        if hasattr(recallmod, "index_file"):
            recallmod.index_file(
                conn, title, data[:4000].decode("utf-8", "replace"))
    except Exception:
        pass


def decode_content(obj) -> bytes:
    """file_write's content arg: str, or {data_b64} for binary."""
    if obj is None:
        return b""
    if isinstance(obj, bytes):
        return obj
    if isinstance(obj, dict) and obj.get("data_b64") is not None:
        import base64
        return base64.b64decode(obj["data_b64"])
    if isinstance(obj, str):
        return obj.encode("utf-8")
    if isinstance(obj, (list, dict)):
        import json
        return json.dumps(obj, indent=2, ensure_ascii=False) \
            .encode("utf-8")
    return str(obj).encode("utf-8")
