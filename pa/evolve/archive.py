"""the improvement archive (Darwin-Gödel-Machine style).

Every change to an evolvable thing — a skill, a prompt snippet, a
config kind, a made tool, a memory block — lands as a *variant* row
in archive_variants with a parent pointer, a content hash, and the
eval score it earned. At most one variant per (kind, target) is
'live' — the content actually in the store. Track A no longer
applies in place: it writes a *candidate*, scores it on the pinned
eval set, and promotes only when the candidate is at least as good
as live (and still green); otherwise the store rolls back and the
candidate stays on file, scored, for the owner to inspect.

Flag: PA_EVOLVE_ARCHIVE (default off — no rows, base behaviour).

  /evolve archive <target>   lineage + scores for one target
  /evolve revert <variant>   owner: make a stored variant live again

Mutation paths call note() *after* writing (source becomes
created_by). Track A wraps its inner writes in suppressed() so the
apply itself records candidates instead of the stores recording
premature 'live' rows. revert() flips the pointer inside
suppressed() too — the variant becoming live is already on file.
"""

import contextlib
import hashlib
import json
import logging
import re
import threading

from ..util import json_dumps, json_loads_bounded, now_iso

log = logging.getLogger("pa.evolve.archive")

KINDS = ("skill", "snippet", "config", "made_tool", "memory_block")
STATUSES = ("live", "candidate", "retired")

_tls = threading.local()


def _exists(conn) -> bool:
    try:
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table'"
            " AND name='archive_variants'").fetchone() is not None
    except Exception:
        return False


def _on(s) -> bool:
    return bool(getattr(s, "evolve_archive", False)) and s is not None


@contextlib.contextmanager
def suppressed():
    """Inner store writes inside Track A / revert must not note()
    themselves — the caller records the variant explicitly."""
    old = getattr(_tls, "suppress", False)
    _tls.suppress = True
    try:
        yield
    finally:
        _tls.suppress = old


def hash_content(content) -> str:
    if not isinstance(content, str):
        content = json_dumps(content)
    return hashlib.sha256((content or "").encode()).hexdigest()[:16]


def _norm_target(kind: str, target: str) -> str:
    """Canonical target names — slugged like the stores so lineage
    keys match what save_skill/save_snippet actually wrote."""
    t = str(target or "")
    if kind in ("skill", "snippet", "made_tool"):
        t = re.sub(r"[^a-z0-9]+", "-", t.lower()).strip("-")[:80] \
            or "unnamed"
    return t[:120]


def note(conn, s, kind: str, target: str, content, *,
         created_by: str = "", status: str = "live",
         parent_id=None, score=None, eval_hash: str = "",
         extra: dict = None):
    """Record one variant. -> variant id, or None (flag off /
    suppressed / table missing). Never raises.

    status='live' flips any current live row for the target to
    'retired' (idempotent: re-saving identical live content is a
    no-op). status='candidate' just appends the row."""
    try:
        if not _on(s) or getattr(_tls, "suppress", False) \
                or not _exists(conn) or kind not in KINDS:
            return None
        target = _norm_target(kind, target)
        chash = hash_content(content)
        cur = live(conn, kind, target)
        if status == "live" and cur and cur["content_hash"] == chash:
            return cur["id"]
        if status not in STATUSES:
            status = "candidate"
        if parent_id is None:
            parent_id = cur["id"] if cur else None
        if status == "live" and cur:
            conn.execute(
                "UPDATE archive_variants SET status='retired'"
                " WHERE id=? AND status='live'", (cur["id"],))
        sj = {"score": score} if score is not None else {}
        if extra:
            sj.update(extra)
        conn.execute(
            "INSERT INTO archive_variants(kind, target, parent_id,"
            " content_hash, content, score_json, eval_set_hash,"
            " status, created_by, created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (kind, target, parent_id, chash,
             content if isinstance(content, str)
             else json_dumps(content),
             json_dumps(sj), str(eval_hash or "")[:80], status,
             str(created_by or "")[:60], now_iso()))
        conn.commit()
        return conn.execute("SELECT last_insert_rowid() i"
                            ).fetchone()["i"]
    except Exception:
        log.info("archive note failed", exc_info=True)
        return None


def get(conn, vid):
    if not _exists(conn):
        return None
    row = conn.execute(
        "SELECT * FROM archive_variants WHERE id=?",
        (int(vid),)).fetchone()
    return dict(row) if row else None


def live(conn, kind: str, target: str):
    if not _exists(conn):
        return None
    row = conn.execute(
        "SELECT * FROM archive_variants WHERE kind=? AND target=?"
        " AND status='live' ORDER BY id DESC LIMIT 1",
        (kind, _norm_target(kind, target))).fetchone()
    return dict(row) if row else None


def variants(conn, kind: str = "", target: str = "", limit=40) -> list:
    if not _exists(conn):
        return []
    q, args = "SELECT * FROM archive_variants", []
    if kind:
        q += " WHERE kind=?"
        args.append(kind)
        if target:
            q += " AND target=?"
            args.append(_norm_target(kind, target))
    q += " ORDER BY id DESC LIMIT ?"
    args.append(int(limit))
    return [dict(r) for r in conn.execute(q, args).fetchall()]


def targets(conn) -> list:
    """(kind, target, live_id, n_variants) — for `/evolve archive`."""
    if not _exists(conn):
        return []
    return [dict(r) for r in conn.execute(
        "SELECT kind, target,"
        " MAX(CASE WHEN status='live' THEN id END) live_id,"
        " COUNT(*) n, MAX(id) last_id FROM archive_variants"
        " GROUP BY kind, target ORDER BY kind, target").fetchall()]


def set_score(conn, vid, score=None, eval_hash: str = "",
              extra: dict = None):
    row = get(conn, vid)
    if row is None:
        return False
    sj = json_loads_bounded(row["score_json"]) or {}
    if score is not None:
        sj["score"] = float(score)
    if extra:
        sj.update(extra)
    conn.execute(
        "UPDATE archive_variants SET score_json=?, eval_set_hash=?"
        " WHERE id=?",
        (json_dumps(sj), str(eval_hash or row["eval_set_hash"])[:80],
         int(vid)))
    conn.commit()
    return True


def promote(conn, vid, score=None, eval_hash: str = "",
            extra: dict = None):
    """candidate -> live; the previous live row retires. The caller
    already wrote the content to the store."""
    row = get(conn, vid)
    if row is None or row["status"] == "retired":
        return False
    cur = live(conn, row["kind"], row["target"])
    if cur and cur["id"] != row["id"]:
        conn.execute(
            "UPDATE archive_variants SET status='retired'"
            " WHERE id=?", (cur["id"],))
    conn.execute(
        "UPDATE archive_variants SET status='live' WHERE id=?",
        (row["id"],))
    conn.commit()
    if score is not None or eval_hash or extra:
        set_score(conn, row["id"], score=score,
                  eval_hash=eval_hash, extra=extra)
    return True


def reject(conn, vid, reasons, score=None, eval_hash: str = ""):
    """Anti-gaming rejection: keep the variant, mark it 'rejected'
    with the reasons recorded for the owner/digest."""
    row = get(conn, vid)
    if row is None:
        return False
    conn.execute(
        "UPDATE archive_variants SET status='rejected' WHERE id=?",
        (int(vid),))
    conn.commit()
    set_score(conn, vid, score=score, eval_hash=eval_hash,
              extra={"rejected_reasons": list(reasons)})
    flag(conn, vid, "suspicious")
    return True


def flag(conn, vid, key: str, value=True):
    """Mark a candidate for the owner (e.g. reject reasons /
    suspicious score jump) without promoting or deleting it."""
    row = get(conn, vid)
    if row is None:
        return False
    sj = json_loads_bounded(row["score_json"]) or {}
    flags = sj.get("flags") or []
    entry = {"key": key, "value": value, "at": now_iso()}
    flags.append(entry)
    sj["flags"] = flags[-10:]
    conn.execute(
        "UPDATE archive_variants SET score_json=? WHERE id=?",
        (json_dumps(sj), row["id"]))
    conn.commit()
    return True


# ---------------------------------------------------------------------------
# revert — write a variant's content back to its store, flip live to it

def _write_back(conn, s, kind: str, target: str, content: str) -> dict:
    if kind == "snippet":
        from . import snippets
        return snippets.save_snippet(
            conn, target, content, source="archive:revert", s=s)
    if kind == "skill":
        from .. import skills as skillsmod
        body, desc = content, ""
        try:
            obj = json.loads(content)
            body, desc = obj.get("body", ""), obj.get("description", "")
        except Exception:
            pass
        return skillsmod.save_skill(
            conn, s, target, desc, body, source="archive:revert")
    if kind == "memory_block":
        from .. import memblocks
        return memblocks.set_block(
            conn, target, content, edited_by="archive:revert", s=s)
    if kind == "config":
        from . import apply as applymod
        try:
            art = json.loads(content)
        except Exception:
            return {"ok": False, "error": "bad config content"}
        return applymod.apply_config_direct(conn, s, art)
    if kind == "made_tool":
        from .. import madetools
        try:
            obj = json.loads(content)
        except Exception:
            return {"ok": False, "error": "bad tool content"}
        return madetools.register(
            conn, s, target, obj.get("code") or "",
            obj.get("tests") or "", obj.get("manifest") or {},
            source="archive:revert")
    return {"ok": False, "error": f"unknown kind {kind}"}


def revert(conn, s, vid) -> dict:
    """Owner action: make an archived variant live — its content goes
    back to the store and the live pointer flips to it."""
    row = get(conn, vid)
    if row is None:
        return {"ok": False, "error": f"no variant {vid}"}
    cur = live(conn, row["kind"], row["target"])
    with suppressed():
        out = _write_back(conn, s, row["kind"], row["target"],
                          row["content"])
    if isinstance(out, dict) and out.get("error") \
            and not out.get("saved") and not out.get("registered"):
        return {"ok": False,
                "error": out.get("message") or out["error"]}
    if cur and cur["id"] != row["id"]:
        conn.execute(
            "UPDATE archive_variants SET status='retired' WHERE id=?",
            (cur["id"],))
    conn.execute(
        "UPDATE archive_variants SET status='live' WHERE id=?",
        (row["id"],))
    conn.commit()
    return {"ok": True, "kind": row["kind"],
            "target": row["target"], "variant": row["id"],
            "previous_live": cur["id"] if cur else None}


def lineage_lines(conn, kind: str, target: str) -> list:
    """/evolve archive <target> card lines."""
    rows = list(reversed(variants(conn, kind, target, limit=25)))
    if not rows:
        return []
    out = [f"**{kind}:{target}** — {len(rows)} variant(s)"]
    for r in rows:
        sj = json_loads_bounded(r["score_json"]) or {}
        score = sj.get("score")
        tag = {"live": "🟢 live", "candidate": "🟡 candidate",
               "retired": "⚪ retired"}.get(r["status"], r["status"])
        par = f" ← #{r['parent_id']}" if r["parent_id"] else ""
        flag_txt = ""
        if sj.get("flags"):
            flag_txt = " ⚑" + ",".join(
                str(f["key"]) for f in sj["flags"][-3:])
        out.append(
            f"  #{r['id']} {tag}{par} "
            f"score={score if score is not None else '-'} "
            f"by {r['created_by'] or '?'} "
            f"{str(r['created_at'])[:16]} sha:{r['content_hash'][:8]}"
            f"{flag_txt}")
    return out
