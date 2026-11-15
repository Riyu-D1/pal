"""final-2 G1: standing goals — the things pal works on proactively.

A goal is owner-visible by design (/goals shows it): title, why,
success criteria, cadence, which read sources a tick may use, status,
the next action pal plans, and progress notes. Everything mutating a
goal goes through an owner-visible write (goal_* chat tools are
approval-gated, /goals buttons are owner clicks); goal_events keeps
the append-only history the owner can audit.

The proactive tick reads `due_goals` — active goals whose cadence
window elapsed — never paused/done ones.
"""

import datetime

from .util import now_iso, parse_iso, utcnow

CADENCES = ("daily", "weekly", "on_event")
STATUSES = ("active", "paused", "done")
DEFAULT_SOURCES = "mail,calendar,web,vault,monitors"
_EDITABLE = ("title", "why", "criteria", "cadence", "sources",
             "status", "next_action", "progress")
_CAPS = {"title": 200, "why": 400, "criteria": 300, "sources": 200,
         "next_action": 300, "progress": 500}

# 'daily' goals become due again after 20 h (a little slack so an
# hourly tick doesn't drift a whole day late); 'weekly' after 6 days.
_DUE = {"daily": datetime.timedelta(hours=20),
        "weekly": datetime.timedelta(days=6)}


def add_goal(conn, title, why="", criteria="", cadence="daily",
             sources="") -> int:
    title = str(title or "").strip()[:200]
    if not title:
        return 0
    cadence = cadence if cadence in CADENCES else "daily"
    sources = _clean_sources(sources)
    now = now_iso()
    cur = conn.execute(
        "INSERT INTO goals(title, why, criteria, cadence, sources,"
        " created_at, updated_at) VALUES(?,?,?,?,?,?,?)",
        (title, str(why or "")[:400], str(criteria or "")[:300],
         cadence, sources, now, now))
    gid = int(cur.lastrowid)
    log_event(conn, gid, "owner", "created", title)
    return gid


def _clean_sources(sources: str) -> str:
    allowed = {"mail", "calendar", "web", "vault", "monitors"}
    parts = [p.strip().lower() for p in str(sources or "").split(",")]
    keep = [p for p in parts if p in allowed]
    return ",".join(dict.fromkeys(keep))[:200] or DEFAULT_SOURCES


def get(conn, goal_id: int):
    r = conn.execute("SELECT * FROM goals WHERE id=?",
                     (int(goal_id),)).fetchone()
    return dict(r) if r else None


def list_goals(conn, status: str = "") -> list:
    if status:
        rows = conn.execute(
            "SELECT * FROM goals WHERE status=? ORDER BY id",
            (str(status),)).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM goals ORDER BY"
            " CASE status WHEN 'active' THEN 0 WHEN 'paused' THEN 1"
            " ELSE 2 END, id").fetchall()
    return [dict(r) for r in rows]


def update_goal(conn, goal_id: int, actor: str = "owner",
                **fields) -> dict:
    """Update editable fields; returns the goal (None when missing).
    Every applied change is logged as a goal_event."""
    g = get(conn, goal_id)
    if g is None:
        return None
    sets, args, changed = [], [], []
    for k in _EDITABLE:
        v = fields.get(k)
        if v is None:
            continue
        if k == "cadence":
            v = str(v).strip().lower()
            if v not in CADENCES:
                continue
        elif k == "status":
            v = str(v).strip().lower()
            if v not in STATUSES:
                continue
        elif k == "sources":
            v = _clean_sources(v)
        else:
            v = str(v)[:_CAPS.get(k, 300)]
        if v == g[k]:
            continue
        sets.append(f"{k}=?")
        args.append(v)
        changed.append(k)
    if not sets:
        return g
    conn.execute(
        f"UPDATE goals SET {', '.join(sets)}, updated_at=? WHERE id=?",
        (*args, now_iso(), g["id"]))
    log_event(conn, g["id"], actor, "update",
              ", ".join(changed))
    return get(conn, goal_id)


def set_status(conn, goal_id: int, status: str,
               actor: str = "owner", note: str = ""):
    g = get(conn, goal_id)
    if g is None or status not in STATUSES:
        return None
    if g["status"] != status:
        conn.execute(
            "UPDATE goals SET status=?, updated_at=? WHERE id=?",
            (status, now_iso(), g["id"]))
        log_event(conn, g["id"], actor, status,
                  str(note or g["title"])[:500])
    return get(conn, goal_id)


def mark_checked(conn, goal_id: int, next_action: str = "",
                 progress: str = "") -> None:
    """The proactive tick stamps the goal after working it. Empty
    next_action/progress keep the existing values."""
    if next_action or progress:
        conn.execute(
            "UPDATE goals SET last_checked=?, next_action=?,"
            " progress=?, updated_at=? WHERE id=?",
            (now_iso(), str(next_action or "")[:300],
             str(progress or "")[:500], now_iso(), int(goal_id)))
    else:
        conn.execute(
            "UPDATE goals SET last_checked=?, updated_at=?"
            " WHERE id=?", (now_iso(), now_iso(), int(goal_id)))


def log_event(conn, goal_id: int, actor: str, verb: str,
              note: str = "") -> None:
    conn.execute(
        "INSERT INTO goal_events(goal_id, actor, verb, note,"
        " created_at) VALUES(?,?,?,?,?)",
        (int(goal_id), str(actor)[:20], str(verb)[:40],
         str(note or "")[:500], now_iso()))


def events(conn, goal_id: int, limit: int = 20) -> list:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM goal_events WHERE goal_id=? ORDER BY id DESC"
        " LIMIT ?", (int(goal_id), int(limit))).fetchall()]


def due_goals(conn, now: datetime.datetime = None) -> list:
    """Active goals whose cadence window has elapsed since
    last_checked (never-checked goals are due). 'on_event' goals are
    never auto-due — they work off owner prompts."""
    now = now or utcnow()
    out = []
    for g in list_goals(conn, "active"):
        if g["cadence"] == "on_event":
            continue
        window = _DUE.get(g["cadence"], _DUE["daily"])
        last = parse_iso(g["last_checked"]) if g["last_checked"] \
            else None
        if last is None or last + window <= now:
            out.append(g)
    return out


def card_lines(conn, status: str = "") -> list:
    """Human-readable /goals lines."""
    rows = list_goals(conn, status)
    if not rows:
        return []
    out = []
    for g in rows:
        mark = {"active": "🎯", "paused": "⏸️", "done": "✅"}.get(
            g["status"], "•")
        line = (f"{mark} **#{g['id']}** {g['title']} "
                f"({g['cadence']})")
        if g["next_action"]:
            line += f"\n   next: {g['next_action'][:100]}"
        if g["last_checked"]:
            line += f" · checked {g['last_checked'][:16]}"
        out.append(line)
    return out
