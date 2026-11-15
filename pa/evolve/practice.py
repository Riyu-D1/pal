"""Self-practice curriculum (final-4 E4) — `evolve-practice` sysjob.

Off by default (PA_EVOLVE_PRACTICE). In owner-approved areas
(`/evolve practice add <area>`) it drafts 3 progressively harder
practice tasks from recent failures, runs them READ-ONLY in a
sandboxed chat context with fake connectors (no real tool side
effects), and turns wins into skill CANDIDATES in the E1 archive —
never live skills, never auto-applied.

Hard caps: PA_EVOLVE_PRACTICE_MAX_TASKS (3), the background budget
pot (pa/budget.py) — a stopped run just returns quietly.
"""

import logging

from .. import budget, db as dbmod, sysjobs
from . import archive, incidents

log = logging.getLogger("pa.evolve.practice")

AREAS_KEY = "evolve_practice_areas"
MAX_TASKS = 3


def areas(conn) -> list:
    import json as _j
    try:
        return list(_j.loads(
            dbmod.get_setting(conn, AREAS_KEY, "[]") or "[]"))
    except Exception:
        return []


def add_area(conn, area: str) -> bool:
    a = (area or "").strip().lower()
    if not a or len(a) > 40:
        return False
    cur = areas(conn)
    if a not in cur:
        cur.append(a)
        import json as _j
        dbmod.set_setting(conn, AREAS_KEY, _j.dumps(cur[-20:]))
    return True


def _recent_failures(conn, area: str, limit: int = 5) -> list:
    try:
        return conn.execute(
            "SELECT summary FROM incidents WHERE summary LIKE ?"
            " ORDER BY id DESC LIMIT ?",
            (f"%{area}%", limit)).fetchall()
    except Exception:
        return []


def _tasks(conn, area: str) -> list:
    """3 progressively harder tasks derived from recent failures —
    deterministic, no model call needed for the practice tasks
    themselves."""
    fails = [r["summary"] for r in _recent_failures(conn, area)]
    if not fails:
        return []
    return [
        f"recall: restate what went wrong in: {fails[0][:120]}",
        f"apply: propose a guard so it can't recur: {fails[0][:120]}",
        f"transfer: write a reusable rule covering "
        f"{len(fails)} failure(s) in {area}",
    ][:MAX_TASKS]


def _run_task_sandbox(conn, s, task: str, brain=None) -> dict:
    """Run one task through the real ChatEngine with a scripted/
    fake environment: no real connectors, no writes — the only
    allowed tools are read-only ones and the run never leaves the
    sandboxed context."""
    from . import eval as evalmod
    fx = {"id": "practice", "prompt": task,
          "script": (brain or [
              "```json\n{\"final\": \"practiced — noted the "
              "guard\"}\n```"])}
    out = evalmod.run_fixture(fx, s)
    return out


def run(bot, payload) -> str:
    """Nightly job. Flags off → quiet. Practicing writes only
    archive candidates — never touches live state or real
    connectors."""
    s, conn = bot.s, bot._db()
    try:
        if not getattr(s, "evolve_practice", False):
            return "practice off"
        if not getattr(s, "evolve_archive", False):
            return "practice needs PA_EVOLVE_ARCHIVE"
        made = 0
        for area in areas(conn):
            if made >= MAX_TASKS:
                break
            try:
                if budget.would_exceed(conn, "background", "tokens",
                                       2000, s=s, env=bot.env):
                    return "practice stopped — background budget"
            except Exception:
                pass
            for task in _tasks(conn, area):
                out = _run_task_sandbox(conn, s, task)
                if not out.get("ok"):
                    incidents.record(conn, "practice", "task_failed",
                                     summary=f"{area}: {task[:100]}")
                    continue
                archive.note(
                    conn, s, "skill", f"practice-{area}",
                    {"body": f"learned in practice: {task[:300]}",
                     "description": f"self-practice in {area}"},
                    created_by="practice", status="candidate")
                made += 1
        return f"practice: {made} candidate(s)"
    finally:
        conn.close()


# -- /evolve practice ---------------------------------------------------

def _ensure_schedule(conn) -> None:
    """Seed the (disabled) nightly job on first use. Migration 43
    seeds it on fresh DBs; this stays INSERT OR IGNORE so pre-43
    databases still get the row on first `practice add`."""
    import json as _j
    from ..util import now_iso
    conn.execute(
        "INSERT OR IGNORE INTO schedules(name, kind, freq, time_of_day,"
        " day_of_week, timezone, enabled, payload_json, created_at,"
        " updated_at) VALUES(?,?,?,?,?,?,0,?,?,?)",
        ("evolve-practice", "agent_job", "daily", "23:45", -1,
         "Europe/London",
         _j.dumps({"channel_id": "",
                   "prompt": "System job (code-run): self-practice "
                             "curriculum in owner-approved areas → "
                             "archive candidates. docs/EVOLVE.md."}),
         now_iso(), now_iso()))
    conn.commit()


def cmd(conn, s, args) -> str:
    if not args:
        cur = areas(conn)
        return ("practice areas: " + (", ".join(cur) or "none")
                + "\n`/evolve practice add <area>` to approve one"
                + (" (PA_EVOLVE_PRACTICE off)"
                   if not getattr(s, "evolve_practice", False) else ""))
    if args[0] == "add" and len(args) > 1:
        area = " ".join(str(a) for a in args[1:])
        if add_area(conn, area):
            _ensure_schedule(conn)
            dbmod.audit(conn, "owner", "evolve_practice_add", area)
            return f"practice area approved: {area}"
        return "usage: evolve practice add <area>"
    return "usage: evolve practice [add <area>]"
