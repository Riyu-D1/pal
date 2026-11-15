"""the local activity feed — ONE durable store for
everything unattended pal work produces.

kind:
  noticed        something pal saw worth surfacing
  draft          a proposed artifact (reply text, move note) — local
                 only; ref_json.draft holds the text or send args
  needs_approval a gated action waiting on the owner's Approve
                 (ref_json.approval_id points at the approvals row)
  did            something pal did on its own accounts
  prep           meeting-prep / briefing material
  ask            a question pal needs answered to keep going

status: open | acted | dismissed | done. Rows are append-mostly —
the feed survives restarts and `/activity` is the owner's window in.
"""

import datetime

from .util import json_dumps, json_loads_bounded, now_iso

KINDS = ("noticed", "draft", "needs_approval", "did", "prep", "ask")
OPEN = "open"


def record(conn, kind: str, title: str, body: str = "",
           source: str = "", ref: dict = None,
           status: str = OPEN) -> int:
    """Append one feed row; returns its id. kind not in KINDS folds
    into 'noticed' rather than failing a caller."""
    if kind not in KINDS:
        kind = "noticed"
    now = now_iso()
    cur = conn.execute(
        "INSERT INTO activity(kind, title, body, source, ref_json,"
        " status, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
        (kind, str(title or "")[:300], str(body or "")[:4000],
         str(source or "")[:60], json_dumps(ref or {}), status,
         now, now))
    return int(cur.lastrowid)


def get(conn, aid: int):
    r = conn.execute("SELECT * FROM activity WHERE id=?",
                     (int(aid),)).fetchone()
    return dict(r) if r else None


def set_status(conn, aid: int, status: str) -> bool:
    cur = conn.execute(
        "UPDATE activity SET status=?, updated_at=? WHERE id=?",
        (str(status)[:20], now_iso(), int(aid)))
    return cur.rowcount > 0


def ref(conn, aid: int) -> dict:
    r = get(conn, aid)
    if r is None:
        return {}
    try:
        return json_loads_bounded(r["ref_json"]) or {}
    except ValueError:
        return {}


def list_activity(conn, since_hours: float = None,
                  kind: str = "", status: str = "",
                  limit: int = 40) -> list:
    """Feed rows newest-first. since_hours bounds by age."""
    q, args = "SELECT * FROM activity WHERE 1=1", []
    if since_hours is not None:
        cutoff = (datetime.datetime.now(datetime.timezone.utc)
                  - datetime.timedelta(hours=float(since_hours))
                  ).isoformat()
        q += " AND created_at>=?"
        args.append(cutoff)
    if kind:
        q += " AND kind=?"
        args.append(str(kind))
    if status:
        q += " AND status=?"
        args.append(str(status))
    q += " ORDER BY id DESC LIMIT ?"
    args.append(int(limit))
    out = []
    for r in conn.execute(q, args).fetchall():
        d = dict(r)
        try:
            d["ref"] = json_loads_bounded(d["ref_json"]) or {}
        except ValueError:
            d["ref"] = {}
        out.append(d)
    return out


def _icon(kind: str) -> str:
    return {"noticed": "👀", "draft": "✏️", "needs_approval": "✋",
            "did": "✅", "prep": "📋", "ask": "❓"}.get(kind, "•")


def card_lines(conn, since_hours: float = None, limit: int = 20):
    """/activity rendering: one line per row, refs trimmed."""
    rows = list_activity(conn, since_hours=since_hours, limit=limit)
    lines = []
    for r in rows:
        stamp = r["created_at"][5:16].replace("T", " ")
        tail = ""
        if r["status"] != "open":
            tail = f" [{r['status']}]"
        line = f"{_icon(r['kind'])} `{stamp}` {r['title'][:110]}{tail}"
        if r["kind"] == "draft" and r["ref"].get("draft"):
            line += f"\n   draft: {str(r['ref']['draft'])[:140]}"
        elif r["kind"] == "needs_approval" and \
                r["ref"].get("approval_id"):
            line += f"\n   approve `{str(r['ref']['approval_id'])[:8]}`"
        lines.append(line)
    return lines


def pending_approvals(conn, limit: int = 5) -> list:
    """Feed rows whose approval is still pending — /activity buttons
    re-post these cards."""
    out = []
    for r in list_activity(conn, kind="needs_approval",
                           status="open", limit=limit):
        aid = r["ref"].get("approval_id")
        if aid:
            ap = conn.execute(
                "SELECT id, payload_json FROM approvals WHERE id=?"
                " AND status='pending'", (aid,)).fetchone()
            if ap:
                try:
                    payload = json_loads_bounded(ap["payload_json"])
                except ValueError:
                    payload = {}
                out.append({"id": ap["id"],
                            "tool": payload.get("tool") or "action",
                            "args": payload.get("args") or {}})
    return out


def while_away(conn, hours: float = 4.0, limit: int = 6) -> str:
    """The 3-line 'while you were away' preamble — '' when nothing
    happened."""
    rows = list_activity(conn, since_hours=hours, limit=limit)
    if not rows:
        return ""
    lines = [f"**while you were away** ({len(rows)}):"]
    for r in rows:
        lines.append(f"{_icon(r['kind'])} {r['title'][:120]}")
    return "\n".join(lines)
