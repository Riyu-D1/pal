"""Track A auto-apply (docs/EVOLVE.md #10): low-risk evolve_items
apply themselves ONLY when PA_EVOLVE_AUTO_APPLY=true — snapshot,
apply, offline eval + deep health check, rollback on any failure.

Eligible: lesson, skill, prompt, and allowlisted config kinds
(schedule interval, a router model already in the catalog,
monitor every_minutes). A brand-new skill name still needs the
owner's card — updates to an accepted one may auto-apply.

Flag off: the same pipeline stops at awaiting_approval and posts
the Approve/Reject card. Failure restores the snapshot, marks
rolled_back, incidents eval_regress/health_fail, and the item
cools down — same dedupe can't retry for 24 h.
"""

import json
import logging
import time

from .. import approvals, db as dbmod, health, skills as skillsmod
from ..util import now_iso
from . import archive, changelog, eval as evalmod, evaluator, \
    gaming, incidents, items, snippets

log = logging.getLogger("pa.evolve.apply")

AUTO_SHAPES = {"lesson", "skill", "prompt", "config"}
CONFIG_KINDS = {"schedule_every", "router_model", "monitor_every"}
ROLLBACK_COOLDOWN_S = 24 * 3600

_APPLYABLE = ("planned", "open", "awaiting_approval")


def _artifact(item) -> dict:
    try:
        return json.loads(item.get("artifacts") or "{}")
    except Exception:
        return {}


def _targets(item, art):
    """(kind, apply_kwargs) pairs, or None when there's nothing
    concrete to apply."""
    shape = item["shape"]
    if shape in ("lesson", "prompt"):
        name = art.get("name") or f"{shape}-{item['id']}"
        body = art.get("body") or (
            item["title"].removeprefix("reflect: ") +
            ("\n" + item["plan"] if item["plan"] else ""))
        return [("snippet", {"name": name[:60], "body": body[:1900]})]
    if shape == "skill":
        if not art.get("name") or not art.get("body"):
            return None
        return [("skill", {"name": art["name"],
                           "description": art.get("description", ""),
                           "body": art["body"]})]
    if shape == "config":
        if art.get("kind") not in CONFIG_KINDS:
            return None
        return [("config", art)]
    return None


def _apply_config(conn, s, art) -> dict:
    kind = art["kind"]
    if kind == "schedule_every":
        from .. import scheduler as sch
        row = sch.get_schedule(conn, str(art.get("name") or ""))
        if row is None:
            return {"ok": False, "error": "no such schedule"}
        minutes = int(art.get("minutes") or 0)
        if minutes < 5 or minutes > 24 * 60:
            return {"ok": False, "error": "minutes out of range"}
        conn.execute("UPDATE schedules SET every_minutes=?,"
                     " updated_at=? WHERE name=?",
                     (minutes, now_iso(), row["name"]))
        conn.commit()
        return {"ok": True}
    if kind == "monitor_every":
        minutes = int(art.get("minutes") or 0)
        if minutes < 5 or minutes > 24 * 60:
            return {"ok": False, "error": "minutes out of range"}
        dbmod.set_setting(
            conn, f"monitor_every:{art.get('name')}", str(minutes))
        return {"ok": True}
    if kind == "router_model":
        from .. import router
        name = str(art.get("model") or "")
        catalog = getattr(router, "BACKENDS", {})
        if name not in catalog:
            return {"ok": False, "error": "model not in catalog"}
        tier = str(art.get("tier") or "fast")
        if tier not in ("fast", "smart"):
            return {"ok": False, "error": "bad tier"}
        dbmod.set_setting(conn, f"router_model:{tier}", name)
        return {"ok": True}
    return {"ok": False, "error": "unknown config kind"}


def apply_config_direct(conn, s, art) -> dict:
    """Public wrapper for _apply_config — archive.revert() re-applies
    a stored config variant through it."""
    if (art or {}).get("kind") not in CONFIG_KINDS:
        return {"ok": False, "error": "unknown config kind"}
    return _apply_config(conn, s, art)


def _snapshot(conn, s, kind, kw):
    if kind == "snippet":
        return snippets.snapshot_snippet(conn, kw["name"])
    if kind == "skill":
        return skillsmod.snapshot_skill(conn, s, kw["name"])
    if kind == "config":
        if kw["kind"] == "schedule_every":
            from .. import scheduler as sch
            row = sch.get_schedule(conn, str(kw.get("name") or ""))
            return {"minutes": row["every_minutes"] if row else None}
        if kw["kind"] == "router_model":
            return {"model": dbmod.get_setting(
                conn, f"router_model:{kw.get('tier','fast')}", "")}
        return {"minutes": dbmod.get_setting(
            conn, f"monitor_every:{kw.get('name')}", "")}
    return {}


def _restore(conn, s, kind, kw, snap):
    if kind == "snippet":
        if snap.get("exists"):
            snippets.save_snippet(conn, kw["name"], snap["body"],
                                  source="rollback", s=s)
        else:
            snippets.delete_snippet(conn, kw["name"])
    elif kind == "skill":
        if snap.get("exists"):
            skillsmod.save_skill(conn, s, kw["name"],
                                 snap.get("description", ""),
                                 snap["body"], source="rollback")
        # a never-existed skill stays deleted naturally — nothing
        # else references it
    elif kind == "config":
        if kw["kind"] == "schedule_every" and snap.get("minutes"):
            conn.execute("UPDATE schedules SET every_minutes=?,"
                         " updated_at=? WHERE name=?",
                         (snap["minutes"], now_iso(), kw["name"]))
            conn.commit()
        elif kw["kind"] == "router_model":
            dbmod.set_setting(
                conn, f"router_model:{kw.get('tier','fast')}",
                str(snap.get("model") or ""))
        elif kw["kind"] == "monitor_every":
            dbmod.set_setting(
                conn, f"monitor_every:{kw.get('name')}",
                str(snap.get("minutes") or ""))


def _target_name(kind, kw) -> str:
    if kind == "config":
        return f"{kw.get('kind')}:" \
               f"{kw.get('name') or kw.get('tier') or ''}"
    return str(kw.get("name") or "")


def _candidate_content(kind, kw):
    if kind == "skill":
        return {"body": kw.get("body") or "",
                "description": kw.get("description") or ""}
    if kind == "config":
        return json.dumps(kw, sort_keys=True)
    return kw.get("body") or ""


def _promotion_score(ev) -> float:
    """What a candidate must match-or-beat — the held-out score when
    the pinned set carries one, else the fixture score."""
    h = ev.get("heldout_score")
    if h is not None:
        return float(h)
    return float(ev.get("score") or 0.0)


def _candidate_paths(item, targets) -> list:
    """Diff paths for gaming checks — code items list them in
    artifacts['paths']; store targets map to their logical path."""
    art = item.get("artifacts") or {}
    if isinstance(art, str):
        try:
            art = json.loads(art) or {}
        except Exception:
            art = {}
    paths = [str(p) for p in (art.get("paths") or [])]
    for kind, kw in targets:
        if kind == "skill":
            paths.append("skills/" + str(kw.get("name", "")))
        elif kind == "snippet":
            paths.append("snippets/" + str(kw.get("name", "")))
    return paths


def _live_content(conn, kind, kw):
    v = archive.live(conn, kind, _target_name(kind, kw))
    return v["content"] if v else None


def _maybe_apply_archive(conn, s, item, targets, bot) -> str:
    """Track A under PA_EVOLVE_ARCHIVE: score live on the pinned
    set, write the change as candidate variants, apply, re-score —
    promote only when the candidate is all-green, at least as good as
    live, and healthy. Otherwise the store rolls back and the scored
    candidate stays on file for the owner."""
    before = evaluator.run(conn, s)
    live_score = _promotion_score(before)
    eval_hash = before.get("eval_set_hash") or ""
    if before.get("refused"):
        items.set_status(conn, item["id"], "awaiting_approval")
        post_card(conn, s, bot, item,
                  "the evaluator refused to score (manifest "
                  "mismatch) — nothing was applied")
        return "awaiting_approval"
    snaps = [(k, kw, _snapshot(conn, s, k, kw))
             for k, kw in targets]
    vids = [archive.note(conn, s, kind, _target_name(kind, kw),
                         _candidate_content(kind, kw),
                         created_by=f"item:{item['id']}",
                         status="candidate")
            for kind, kw in targets]
    with archive.suppressed():
        for kind, kw in targets:
            out = _do_apply(conn, s, kind, kw)
            if not out["ok"]:
                for k2, kw2, snap2 in reversed(snaps):
                    _restore(conn, s, k2, kw2, snap2)
                items.set_status(conn, item["id"], "rolled_back",
                                 error=out["error"])
                changelog.log_event(conn, s, item["id"], "rolled_back",
                                    out["error"])
                return "rolled_back"
        ev = evaluator.run(conn, s)
        ok_health = health.check(conn, s).get("ok", False)
        score = _promotion_score(ev)
        reasons = [] if ev.get("refused") else gaming.check_candidate(
            score_before=live_score, score_after=score,
            eval_hash_before=eval_hash,
            eval_hash_after=ev.get("eval_set_hash") or eval_hash,
            paths=_candidate_paths(item, targets),
            new_content=_candidate_content(*targets[0])
            if targets else None,
            old_content=_live_content(conn, *targets[0])
            if targets else None)
        promotable = not ev.get("refused") and not reasons \
            and (ev.get("score") or 0.0) >= 1.0 \
            and score >= live_score
        if not promotable or not ok_health:
            for k2, kw2, snap2 in reversed(snaps):
                _restore(conn, s, k2, kw2, snap2)
    if reasons:
        for vid in vids:
            if vid:
                archive.reject(conn, vid, reasons,
                               score=score,
                               eval_hash=ev.get("eval_set_hash")
                               or eval_hash)
        items.set_status(conn, item["id"], "rolled_back",
                         eval_score=score,
                         error="anti-gaming: " + "; ".join(reasons))
        changelog.log_event(conn, s, item["id"], "rejected",
                            "; ".join(reasons))
        incidents.record(conn, "health", "candidate_rejected",
                         summary=f"item #{item['id']}: "
                                 + "; ".join(reasons))
        _dm(bot, f"🚩 evolve #{item['id']} rejected by anti-gaming "
                 f"checks: {('; '.join(reasons))[:160]}")
        return "rolled_back"
    if promotable and ok_health:
        for vid in vids:
            if vid:
                archive.promote(
                    conn, vid, score=score,
                    eval_hash=ev.get("eval_set_hash") or eval_hash)
        items.set_status(conn, item["id"], "applied",
                         eval_score=score)
        changelog.log_event(
            conn, s, item["id"], "applied",
            f"{item['shape']} promoted (eval {score:.2f} "
            f">= live {live_score:.2f})")
        _dm(bot, f"✅ evolve #{item['id']} applied "
                 f"[{item['shape']}] {item['title'][:120]}")
        return "applied"
    for vid in vids:
        if vid:
            archive.set_score(
                conn, vid, score=score,
                eval_hash=ev.get("eval_set_hash") or eval_hash)
    if ev.get("refused") or (ev.get("score") or 0.0) < 1.0:
        kind = "eval_regress"
    elif score < live_score:
        kind = "candidate_worse"
    else:
        kind = "health_fail"
    incidents.record(conn, "health", kind,
                     summary=f"item #{item['id']} apply: "
                             f"score={score:.2f} live={live_score:.2f} "
                             f"health={ok_health}")
    items.set_status(conn, item["id"], "rolled_back",
                     eval_score=score,
                     error=f"{kind} after apply (kept as candidate)")
    changelog.log_event(conn, s, item["id"], "rolled_back",
                        f"{kind} (eval {score:.2f}, "
                        f"live {live_score:.2f})")
    _dm(bot, f"↩️ evolve #{item['id']} rolled back — {kind} "
             f"(eval {score:.2f}); kept as a candidate")
    return "rolled_back"


def _do_apply(conn, s, kind, kw) -> dict:
    if kind == "snippet":
        out = snippets.save_snippet(conn, kw["name"], kw["body"],
                                    source="evolve", s=s)
        return {"ok": bool(out.get("saved")),
                "error": out.get("error", "")}
    if kind == "skill":
        skillsmod.save_skill(conn, s, kw["name"],
                             kw.get("description", ""), kw["body"],
                             source="evolve")
        return {"ok": True}
    if kind == "config":
        return _apply_config(conn, s, kw)
    return {"ok": False, "error": "unknown kind"}


def _recently_rolled_back(conn, item) -> bool:
    row = conn.execute(
        "SELECT updated_at FROM evolve_items WHERE dedupe_key=?"
        " AND status='rolled_back' ORDER BY id DESC LIMIT 1",
        (item["dedupe_key"],)).fetchone() if item["dedupe_key"] else None
    if row is None:
        return False
    try:
        from ..util import utcnow
        import datetime
        ts = datetime.datetime.fromisoformat(
            row["updated_at"].replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=datetime.timezone.utc)
        return (utcnow() - ts).total_seconds() < ROLLBACK_COOLDOWN_S
    except Exception:
        return True                      # unparseable -> stay cool


def post_card(conn, s, bot, item, reason: str):
    """The awaiting_approval card: an approvals row + Approve/Reject
    DM. Returns the approval id."""
    aid = approvals.create_approval(
        conn, "evolve_apply",
        {"item_id": item["id"], "tool": f"evolve:{item['shape']}",
         "args": {"item_id": item["id"]}})
    if bot is not None:
        try:
            dm = bot._owner_dm_id()
            if dm:
                bot.api.post_message(
                    dm,
                    f"🧬 evolve item #{item['id']} wants to apply\n"
                    f"[{item['shape']}/{item['risk']}] {item['title']}\n"
                    f"{reason}"[:1900],
                    components=[[
                        {"type": 2, "style": 3, "label": "Approve",
                         "custom_id": f"evappr:{aid}:y"},
                        {"type": 2, "style": 4, "label": "Reject",
                         "custom_id": f"evappr:{aid}:n"}]])
        except Exception:
            log.info("evolve card post failed", exc_info=True)
    return aid


def maybe_apply(conn, s, item_id, bot=None) -> str:
    """Advance one item through Track A. Returns the resulting
    status. Never raises."""
    try:
        item = items.get(conn, item_id)
        if item is None or item["status"] not in _APPLYABLE:
            return item["status"] if item else "gone"
        already = item["status"] == "awaiting_approval"
        art = _artifact(item)
        targets = _targets(item, art)
        if not s.evolve_auto_apply or targets is None \
                or _recently_rolled_back(conn, item):
            if not already:
                items.set_status(conn, item_id, "awaiting_approval")
                post_card(conn, s, bot, item,
                          "auto-apply is off"
                          if not s.evolve_auto_apply
                          else "needs your approval first")
            return "awaiting_approval"
        # new skill names still need a card
        if item["shape"] == "skill":
            snap0 = skillsmod.snapshot_skill(conn, s,
                                             targets[0][1]["name"])
            if not snap0["exists"]:
                items.set_status(conn, item_id, "awaiting_approval")
                post_card(conn, s, bot, item,
                          "first time for this skill name")
                return "awaiting_approval"
        if getattr(s, "evolve_archive", False):
            return _maybe_apply_archive(conn, s, item, targets, bot)
        snaps = [(k, kw, _snapshot(conn, s, k, kw))
                 for k, kw in targets]
        for kind, kw in targets:
            out = _do_apply(conn, s, kind, kw)
            if not out["ok"]:
                for k2, kw2, snap2 in reversed(snaps):
                    _restore(conn, s, k2, kw2, snap2)
                items.set_status(conn, item_id, "rolled_back",
                                 error=out["error"])
                changelog.log_event(conn, s, item_id, "rolled_back",
                                    out["error"])
                return "rolled_back"
        # gate: offline eval + deep health
        ev = evalmod.run(s)
        ok_health = health.check(conn, s).get("ok", False)
        score = ev["score"]
        if score < 1.0 or not ok_health:
            for k2, kw2, snap2 in reversed(snaps):
                _restore(conn, s, k2, kw2, snap2)
            kind = "eval_regress" if score < 1.0 else "health_fail"
            incidents.record(conn, "health", kind,
                             summary=f"item #{item_id} apply: "
                                     f"score={score:.2f} "
                                     f"health={ok_health}")
            items.set_status(conn, item_id, "rolled_back",
                             eval_score=score,
                             error=f"{kind} after apply")
            changelog.log_event(conn, s, item_id, "rolled_back",
                                f"{kind} (eval {score:.2f})")
            _dm(bot, f"↩️ evolve #{item_id} rolled back — {kind} "
                     f"(eval {score:.2f})")
            return "rolled_back"
        items.set_status(conn, item_id, "applied", eval_score=score)
        changelog.log_event(conn, s, item_id, "applied",
                            f"{item['shape']} applied "
                            f"(eval {score:.2f})")
        _dm(bot, f"✅ evolve #{item_id} applied "
                 f"[{item['shape']}] {item['title'][:120]}")
        return "applied"
    except Exception:
        log.warning("maybe_apply failed", exc_info=True)
        return "error"


def apply_approved(conn, s, item_id, bot=None) -> str:
    """The owner's Approve on an evolve card: run the apply path
    even when auto-apply is off (the click IS the approval)."""
    flag = s.evolve_auto_apply
    try:
        s.evolve_auto_apply = True
        return maybe_apply(conn, s, item_id, bot=bot)
    finally:
        s.evolve_auto_apply = flag


def _dm(bot, text: str):
    if bot is None:
        return
    try:
        dm = bot._owner_dm_id()
        if dm:
            bot.api.post_message(dm, text[:1900])
    except Exception:
        log.info("evolve dm failed", exc_info=True)
