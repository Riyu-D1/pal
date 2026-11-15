"""Standing allow rules (docs/PARITY.md #10).

After an owner approves a gated tool once they can say "always
allow this" — a row in `allow_rules` that auto-approves matching
future requests (tool name + optional substring of the args JSON).

Rules are STRICTLY whitelisted to low-risk tools. Anything that
sends, pays, deletes, runs on the Mac, fills secrets or executes
code can never get a rule — the card never even shows "Always".

`auto_approve` runs at the head of ChatToolContext.approve(), so a
matching rule short-circuits the whole card (chat) or queue
(unattended) path and is audited.
"""

import json

from . import db as dbmod
from .util import now_iso

# the ONLY tools a standing rule may cover
ALWAYS_CAPABLE = frozenset({
    "watch_add", "watch_set", "skill_save", "owner_fact",
    "memory_save", "vault_write", "calendar_read",
    "goal_add", "goal_update", "goal_done",
    "project_start", "project_stop",
    # phone sensitive reads — gated by the 'sensitive' class (non-hard),
    # so a standing rule may cover them; sends stay hard-gated
    "phone_photo", "phone_locate",
})


def add(conn, tool: str, pattern: str = "") -> dict:
    tool = (tool or "").strip()
    if tool not in ALWAYS_CAPABLE:
        return {"error": "not_allowable",
                "message": f"no standing rules for {tool!r} — it "
                           "must be approved every time"}
    conn.execute(
        "INSERT INTO allow_rules(tool, pattern, created_at)"
        " VALUES(?,?,?)",
        (tool, (pattern or "").strip()[:300], now_iso()))
    dbmod.audit(conn, "owner", "allow_add", f"{tool}:{pattern or '*'}")
    return {"saved": True, "tool": tool,
            "pattern": pattern or "*"}


def list_rules(conn) -> list:
    return [dict(r) for r in conn.execute(
        "SELECT id, tool, pattern, created_at FROM allow_rules"
        " ORDER BY id").fetchall()]


def remove(conn, rule_id: int) -> bool:
    cur = conn.execute("DELETE FROM allow_rules WHERE id=?",
                       (int(rule_id),))
    if cur.rowcount:
        dbmod.audit(conn, "owner", "allow_remove", f"#{rule_id}")
    return cur.rowcount > 0


def _matches(row, args: dict) -> bool:
    pat = row["pattern"]
    if not pat:
        return True
    try:
        return pat in json.dumps(args, sort_keys=True)
    except Exception:
        return False


def auto_approve(conn, tool: str, args: dict) -> bool:
    """True when a standing rule covers this call — and audits the
    auto-approval so it's visible in /audit."""
    if tool not in ALWAYS_CAPABLE:
        return False
    rows = conn.execute(
        "SELECT id, pattern FROM allow_rules WHERE tool=?",
        (tool,)).fetchall()
    if any(_matches(r, args) for r in rows):
        dbmod.audit(conn, "owner", "auto_allow", tool)
        return True
    return False
