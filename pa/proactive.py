"""the proactive read-only tick.

Each due standing goal gets ONE bounded unattended turn: fast-tier
model, `job:proactive-tick` budget (the shared background pot), only
read-classified tools plus `goal_report` on the menu. Whatever would
write to an owner account can only come back as a job_action Approve
card (or a local draft row) — policy.decide in unattended mode is the
hard floor underneath all of it.

Caps per goal: 90 s wall, 8 tool calls, PA_PROACTIVE_GOAL_TOKENS
pre-checked against the budget. Quiet hours skip the whole tick.
"""

import logging

from . import activity as actmod, budget, call as callmod, \
    chattools, goals as goalsmod, policy

log = logging.getLogger("pa.proactive")

SCOPE = "job:proactive-tick"
MAX_GOALS = 3        # per tick — the rest wait for the next run
WALL_SECONDS = 90    # per-goal turn deadline
TOOL_CAP = 8         # per-goal tool calls


def quiet(s, now=None) -> bool:
    return callmod.in_quiet_hours(
        getattr(s, "quiet_hours", ""), getattr(s, "timezone", ""),
        now)


def allowed_tools(s) -> set:
    """Read-classified tools (flags honoured) + goal_report. Write
    tools aren't just prompt-hidden — ChatToolContext.tool_allow
    refuses them outright, and policy denies underneath anyway."""
    names = {"goal_report"}
    for name, spec in chattools.CHAT_TOOLS.items():
        if not chattools._flag_on(s, spec):
            continue
        try:
            if policy.classify(name) == "read":
                names.add(name)
        except Exception:
            continue
    return names


def _prompt(g) -> str:
    return (
        "Proactive tick for standing goal "
        f"#{g['id']}: {g['title']}\n"
        f"Why: {g['why'] or '-'}\n"
        f"Success criteria: {g['criteria'] or '-'}\n"
        f"Progress so far: {g['progress'] or '-'}\n"
        f"Current next action: {g['next_action'] or '-'}\n"
        f"Allowed read sources: {g['sources'] or 'any'}\n\n"
        "Look, never act. Use only the read tools that map to those "
        "sources. Anything that would write to an owner account goes "
        "into drafts with the real tool + args so the owner can "
        "Approve it; plain text drafts use {summary, draft}. Finish "
        "by calling goal_report(goal_id, findings, drafts, "
        "next_action).")


def _tick_goal(conn, s, env, engine, g, channel_id, post_pending,
               allow, cap, now=None) -> dict:
    gid = g["id"]
    if budget.would_exceed(conn, SCOPE, "llm_tokens:smart", cap,
                           s=s, env=env, now=now):
        goalsmod.mark_checked(conn, gid)
        return {"goal_id": gid, "skipped": "budget"}
    calls = {"n": 0}

    def _count(_name):
        calls["n"] += 1

    out = {"text": "", "pending": []}
    try:
        out = engine.run_turn(
            f"proactive:{gid}", _prompt(g), unattended=True,
            deadline_seconds=WALL_SECONDS, progress=_count,
            stop_check=lambda: calls["n"] >= TOOL_CAP) or out
    except Exception as e:
        log.exception("proactive tick failed for goal %s", gid)
        goalsmod.mark_checked(conn, gid)
        actmod.record(conn, "noticed",
                      f"proactive tick on goal #{gid} failed: "
                      f"{str(e)[:120]}",
                      source="proactive-tick", ref={"goal_id": gid})
        return {"goal_id": gid, "error": str(e)[:200]}

    pending = out.get("pending") or []
    for p in pending:
        # gated tool calls the turn tried -> Approve cards + feed rows
        if p.get("tool") != "goal_report":
            actmod.record(conn, "needs_approval",
                          f"{p['tool']} (goal {gid})",
                          source="proactive-tick",
                          ref={"goal_id": gid,
                               "approval_id": p["id"]})
    if channel_id and pending and post_pending:
        try:
            post_pending(pending)
        except Exception:
            log.exception("posting proactive pending failed")
    text = str(out.get("text") or "").strip()
    if text:
        actmod.record(conn, "did",
                      f"proactive tick goal #{gid}: {text[:160]}",
                      source="proactive-tick", ref={"goal_id": gid})
    goalsmod.mark_checked(conn, gid)
    return {"goal_id": gid, "steps": calls["n"],
            "pending": len(pending), "text": text[:300]}


def run(conn, s, env, engine=None, channel_id="",
        post_pending=None, now=None) -> dict:
    """One proactive tick -> {"skipped": why} or {"results": [...]}.
    `engine` is a chatagent.ChatEngine built with
    tool_ctx_kwargs={"tool_allow": allowed_tools(s)} — the sysjob
    handler wires it; tests pass a fake with run_turn()."""
    if not (getattr(s, "goals", False)
            and getattr(s, "proactive", False)):
        return {"skipped": "flags"}
    if quiet(s, now):
        return {"skipped": "quiet_hours"}
    due = goalsmod.due_goals(conn, now)[:MAX_GOALS]
    projects_on = getattr(s, "projects", False)
    open_projects = []
    if projects_on:
        from . import projects as projmod
        open_projects = [p for p in projmod.list_projects(conn)
                         if p["state"] in ("running", "blocked")]
    if not due and not open_projects:
        return {"skipped": "no_due_goals"}
    out = {}
    if due:
        allow = allowed_tools(s)
        if engine is None:
            from . import chatagent
            engine = chatagent.ChatEngine(
                s, env=env, tool_ctx_kwargs={"tool_allow": allow})
        cap = int(getattr(s, "proactive_goal_tokens", 8000) or 8000)
        out["results"] = [
            _tick_goal(conn, s, env, engine, g, channel_id,
                       post_pending, allow, cap, now=now)
            for g in due]
    # multi-day project heartbeat — one step per running project
    if projects_on:
        try:
            from . import projects as projmod
            out["projects"] = projmod.heartbeat(conn, s, env, now=now)
        except Exception:
            log.exception("project heartbeat failed")
    return out


def job(bot, payload) -> str:
    """sysjobs handler for the seeded 'proactive-tick' schedule."""
    from . import db as dbmod, sysjobs
    conn = dbmod.connect(bot.db_path)
    try:
        dm = payload.get("channel_id") or sysjobs.owner_dm(bot)
        from . import chatagent
        allow = allowed_tools(bot.s)
        engine = chatagent.ChatEngine(
            bot.s, env=bot.env,
            tool_ctx_kwargs={"tool_allow": allow})
        out = run(conn, bot.s, bot.env, engine=engine,
                  channel_id=dm,
                  post_pending=(
                      (lambda items: bot._post_job_pending(dm, items))
                      if dm else None))
        if "skipped" in out:
            return f"proactive-tick skipped: {out['skipped']}"
        res = out.get("results") or []
        n = len(res)
        pend = sum(r.get("pending", 0) for r in res)
        proj = len(out.get("projects") or [])
        return (f"proactive-tick: {n} goal(s), {pend} held, "
                f"{proj} project step(s)")
    finally:
        conn.close()
