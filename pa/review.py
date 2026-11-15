"""Auto-review labels for approval cards (docs/PARITY.md #11).

A cheap deterministic classifier (no model call) that stamps every
approval card with a risk label + the reason, so the owner can skim:

  🔴 high   — sends/dispatches/executes money-or-data-affecting work
  🟠 review — changes standing configuration, profile or schedules
  🟢 low    — writes pal's own state only

The label is advisory; it never changes the decision and never
auto-approves anything.
"""

_HIGH = {"send_email", "shell", "mac", "run_python",
         "browse_fill_secret", "code_task", "calendar_create",
         "email_send", "call_owner"}
_REVIEW = {"owner_fact", "watch_add", "watch_set", "skill_save",
           "memory_save", "schedule_add", "vault_write",
           "webhook_create"}

_DOMAINS = ("@",)


def label(tool: str, args: dict) -> dict:
    """{level: high|review|low, emoji, reason, text} for one call."""
    tool = str(tool or "?")
    reasons = []
    if tool in _HIGH:
        level = "high"
        if tool in ("send_email", "email_send"):
            to = str((args or {}).get("to") or "")
            if "@" in to:
                reasons.append(f"sends mail to {to.split('@')[-1][:40]}")
        elif tool in ("shell", "mac"):
            reasons.append("runs a command")
        elif tool == "browse_fill_secret":
            reasons.append("fills a stored secret into a page")
        elif tool == "code_task":
            reasons.append("starts an external coding agent")
        else:
            reasons.append("dispatches work outside pal")
    elif tool in _REVIEW or tool.startswith("watch_"):
        level = "review"
        if tool == "owner_fact":
            reasons.append("deletes an owner fact"
                           if (args or {}).get("delete")
                           else "changes the shared owner profile")
        elif tool.startswith("watch_"):
            reasons.append("changes a standing monitor")
        elif tool == "skill_save":
            reasons.append("edits the skills library")
        else:
            reasons.append("changes saved configuration")
    else:
        level = "low"
        reasons.append("writes pal's own state only")
    emoji = {"high": "🔴", "review": "🟠", "low": "🟢"}[level]
    return {"level": level, "emoji": emoji,
            "reason": reasons[0] if reasons else "",
            "text": f"{emoji} {level} — {reasons[0] if reasons else 'ok'}"}
