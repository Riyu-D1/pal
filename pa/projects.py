"""multi-day projects — a goal-linked plan pal advances
one step per proactive tick.

plan_json:
  {"steps": [{"text": str, "state": "pending|done",
              "tool"?: str, "args"?: dict,
              "ask"?: str}],
   "inputs": {...},          # observed inputs the plan was built on
   "replans": n}

The heartbeat only ever advances a read-only or pal-account step:
- a step carrying "ask" blocks the project and posts an ask_owner
  approval — the owner's answer unblocks the next tick;
- a step carrying "tool" runs only if it survives policy.decide in
  unattended mode as a pal-owned/read action — anything else becomes
  a job_action Approve card and the step waits;
- a plain {"text"} step is a note-step pal marks done (the real work
  is describing it on the activity feed).

Replanning: callers may pass a probe(pid, plan) -> dict; when the
fresh inputs differ from plan["inputs"] the plan is flagged
"replanned" (event + activity row) and the probe result replaces
them — the plan itself is the owner's, pal doesn't silently rewrite
the steps.
"""

import json

from . import activity as actmod, approvals
from .util import json_dumps, json_loads_bounded, now_iso

STATES = ("running", "blocked", "done", "stopped")


def start(conn, goal_id: int, title: str, steps: list,
          actor: str = "owner") -> int:
    steps = [{"text": str(s.get("text") if isinstance(s, dict)
                            else s)[:200],
              "state": "pending",
              **({k: s[k] for k in ("tool", "args", "ask")
                  if isinstance(s, dict) and k in s})}
             for s in (steps or []) if s]
    if not steps:
        return 0
    plan = {"steps": steps, "inputs": {}, "replans": 0}
    now = now_iso()
    cur = conn.execute(
        "INSERT INTO projects(goal_id, title, plan_json, state,"
        " next_step, blockers, ask_id, created_at, updated_at)"
        " VALUES(?,?,?,?,?,?,?,?,?)",
        (int(goal_id), str(title or "")[:200],
         json_dumps(plan), "running", steps[0]["text"], "", "", now,
         now))
    pid = int(cur.lastrowid)
    from . import goals as goalsmod
    goalsmod.log_event(conn, goal_id, actor,
                       "project_start", f"#{pid} {title}")
    return pid


def get(conn, pid: int):
    r = conn.execute("SELECT * FROM projects WHERE id=?",
                     (int(pid),)).fetchone()
    if r is None:
        return None
    d = dict(r)
    try:
        d["plan"] = json_loads_bounded(d["plan_json"]) or {}
    except ValueError:
        d["plan"] = {}
    return d


def list_projects(conn, state: str = "") -> list:
    q = "SELECT * FROM projects"
    args = []
    if state:
        q += " WHERE state=?"
        args.append(state)
    return [dict(r) | {"plan": {}} for r in
            conn.execute(q + " ORDER BY id", args).fetchall()]


def _plan(conn, pid: int) -> dict:
    p = get(conn, pid)
    return p["plan"] if p else {}


def _save(conn, pid: int, plan: dict, state: str = "",
          next_step: str = "", blockers: str = "",
          ask_id: str = "") -> None:
    sets, args = "plan_json=?, updated_at=?", [json_dumps(plan),
                                              now_iso()]
    for col, val in (("state", state), ("next_step", next_step),
                     ("blockers", blockers), ("ask_id", ask_id)):
        if val:
            sets += f", {col}=?"
            args.append(val)
        elif col in ("blockers", "ask_id") and val == "":
            sets += f", {col}=''"
    args.append(int(pid))
    conn.execute(f"UPDATE projects SET {sets} WHERE id=?", args)


def _pending_idx(plan: dict):
    for i, st in enumerate(plan.get("steps") or []):
        if st.get("state") == "pending":
            return i
    return None


def set_state(conn, pid: int, state: str, actor: str = "owner",
              note: str = "") -> dict:
    p = get(conn, pid)
    if p is None or state not in STATES:
        return None
    conn.execute(
        "UPDATE projects SET state=?, updated_at=? WHERE id=?",
        (state, now_iso(), int(pid)))
    from . import goals as goalsmod
    goalsmod.log_event(conn, p["goal_id"], actor,
                       f"project_{state}",
                       note or f"#{pid} {p['title']}")
    return get(conn, pid)


def _next_text(plan: dict) -> str:
    i = _pending_idx(plan)
    return "" if i is None else plan["steps"][i]["text"]


def _safe_step(tool: str) -> bool:
    """Unattended heartbeat may run only reads or pal-account writes
    — everything else becomes an Approve card instead."""
    from . import policy
    try:
        cls = policy.classify(tool)
    except Exception:
        return False
    if cls == "read":
        return True
    from . import chattools
    return cls in ("write",) and tool in policy._PAL_OWNED


def _block(conn, p, step, reason: str) -> str:
    """needs_input/ask steps hold the project on an ask_owner-style
    approval — the owner's click resumes it on the next tick."""
    aid = approvals.create_approval(
        conn, "ask",
        {"kind": "project", "project_id": p["id"],
         "question": str(step.get("ask") or reason)[:500],
         "goal_id": p["goal_id"], "channel_id": ""})
    _save(conn, p["id"], _plan(conn, p["id"]), state="blocked",
          blockers=reason[:200], ask_id=aid)
    actmod.record(conn, "ask",
                  f"project #{p['id']} blocked: {reason[:140]}",
                  source="project-heartbeat",
                  ref={"project_id": p["id"], "approval_id": aid})
    return aid


def _run_step_tool(conn, s, env, p, step) -> dict:
    """Execute a read/pal-account tool step directly (bounded: one
    call, no model)."""
    from . import chattools
    ctx = chattools.ChatToolContext(
        conn, s, None, "", env=env, unattended=True,
        tool_allow={"goal_list", "goal_report", "project_status"} |
        {t for t in chattools.CHAT_TOOLS
         if chattools._flag_on(s, chattools.CHAT_TOOLS[t])
         and _safe_step(t)})
    return chattools.run(ctx, step["tool"], step.get("args") or {})


def heartbeat(conn, s, env, probe=None, now=None) -> list:
    """One step per running project per tick -> [{project_id,...}].
    Blocked projects first re-check their ask: a decided approval
    unblocks, a denied one stops the project."""
    out = []
    for p in list_projects(conn, "blocked"):
        aid = p["ask_id"]
        ap = approvals.get(conn, aid) if aid else None
        if ap is None:
            set_state(conn, p["id"], "stopped", actor="pal",
                      note="blocking approval vanished")
            out.append({"project_id": p["id"], "stopped": True})
        elif ap["status"] != "pending":
            if ap["status"] == "approved":
                plan = _plan(conn, p["id"])
                idx = _pending_idx(plan)
                if idx is not None and \
                        plan["steps"][idx].get("ask"):
                    # the owner's answer IS that step's output
                    try:
                        payload = json_loads_bounded(
                            ap["payload_json"]) or {}
                    except ValueError:
                        payload = {}
                    plan["steps"][idx]["state"] = "done"
                    plan["steps"][idx]["answer"] = str(
                        payload.get("answer") or "approved")[:300]
                _save(conn, p["id"], plan, state="running",
                      next_step=_next_text(plan))
                actmod.record(conn, "did",
                              f"project #{p['id']} unblocked by owner",
                              source="project-heartbeat",
                              ref={"project_id": p["id"]})
                out.append({"project_id": p["id"],
                            "unblocked": True})
            else:
                set_state(conn, p["id"], "stopped", actor="pal",
                          note="owner denied the unblock")
                out.append({"project_id": p["id"], "stopped": True})
    for p in list_projects(conn, "running"):
        p = get(conn, p["id"])
        plan = p["plan"]
        steps = plan.get("steps") or []
        idx = _pending_idx(plan)
        if idx is None:
            set_state(conn, p["id"], "done", actor="pal",
                      note="plan complete")
            actmod.record(conn, "did",
                          f"project #{p['id']} {p['title']} finished",
                          source="project-heartbeat",
                          ref={"project_id": p["id"]})
            out.append({"project_id": p["id"], "done": True})
            continue
        if probe is not None:
            try:
                fresh = probe(p["id"], plan) or {}
            except Exception:
                fresh = {}
            if fresh != (plan.get("inputs") or {}):
                plan["inputs"] = fresh
                plan["replans"] = int(plan.get("replans") or 0) + 1
                _save(conn, p["id"], plan)
                actmod.record(conn, "noticed",
                              f"project #{p['id']} inputs changed —"
                              " replanning",
                              source="project-heartbeat",
                              ref={"project_id": p["id"]})
                out.append({"project_id": p["id"], "replanned": True})
                continue      # replan this tick; advance next one
        step = steps[idx]
        if step.get("ask"):
            _block(conn, p, step, step["ask"])
            out.append({"project_id": p["id"], "blocked": True})
            continue
        tool = str(step.get("tool") or "")
        if tool and not _safe_step(tool):
            # owner-account action -> Approve card, step stays pending
            aid = approvals.create_approval(
                conn, "job_action",
                {"tool": tool, "args": step.get("args") or {},
                 "channel_id": "", "project_id": p["id"]})
            actmod.record(conn, "needs_approval",
                          f"project #{p['id']} step needs a yes:"
                          f" {tool}",
                          source="project-heartbeat",
                          ref={"project_id": p["id"],
                               "approval_id": aid})
            out.append({"project_id": p["id"], "held": aid})
            continue
        if tool:
            try:
                res = _run_step_tool(conn, s, env, p, step)
            except Exception as e:
                res = {"error": str(e)[:200]}
            if res.get("error"):
                actmod.record(conn, "noticed",
                              f"project #{p['id']} step failed:"
                              f" {res['error'][:120]}",
                              source="project-heartbeat",
                              ref={"project_id": p["id"]})
                out.append({"project_id": p["id"], "error":
                            res["error"]})
                continue
        steps[idx]["state"] = "done"
        plan["steps"] = steps
        _save(conn, p["id"], plan, next_step=_next_text(plan))
        actmod.record(conn, "did",
                      f"project #{p['id']}: {step['text'][:140]}",
                      source="project-heartbeat",
                      ref={"project_id": p["id"]})
        out.append({"project_id": p["id"], "advanced": idx})
    return out
