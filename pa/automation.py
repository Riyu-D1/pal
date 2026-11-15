"""'when X happens, do Y' -> ONE concrete artefact
proposal (monitor | schedule | webhook + action template), shown as
a single approval card and created only on Approve.

Deterministic parsing — no model call. The proposal is an
`automation_apply` gated tool call, so the card carries exactly what
will be created and what it will read; replaying through
execute_approved makes the artefact. Actions on owner accounts stay
drafts via the usual policy floor.
"""

import re

from .util import new_id

_SPLIT = re.compile(
    r"^\s*(?:(?:when|whenever|if|after|once)\s+)?(.+?)"
    r"(?:,|;|\bthen\b|\bdo\b)\s*(.+)$", re.I | re.S)

_TIME = re.compile(
    r"\b(every\s+(day|morning|evening|night|hour|week|month|"
    r"monday|tuesday|wednesday|thursday|friday|saturday|sunday)|"
    r"daily|hourly|weekly|monthly|mornings?|evenings?|nightly|"
    r"at\s+\d{1,2}(:\d{2})?\s*(am|pm)?)\b", re.I)
_HOOK = re.compile(
    r"\b(webhook|payload|github|gitlab|stripe|ci\b|build\s+fails?|"
    r"deploy|form\s+(submit|response)|posts?\s+(to|an?)\s+(event|"
    r"hook)|pagerduty)\b", re.I)
_URL = re.compile(r"https?://\S+", re.I)
_MAIL = re.compile(r"\b(email|e-mail|mail|inbox|newsletter|from\s+\S+@)\b",
                   re.I)
_CAL = re.compile(r"\b(calendar|meeting|event|call|appointment)\b",
                  re.I)
_DAY = {"monday": "mon", "tuesday": "tue", "wednesday": "wed",
        "thursday": "thu", "friday": "fri", "saturday": "sat",
        "sunday": "sun"}
_MAIL_SEARCH = re.compile(
    r"(?:from|sent by|mail(?:ing)?\s+list)\s+([\w.\-+@]+)", re.I)


def _when(trigger: str) -> str:
    """trigger text -> the job_add 'when' vocabulary."""
    t = trigger.lower()
    for d, ab in _DAY.items():
        if d in t or f"every {d}" in t:
            return f"weekly {ab} 09:00"
    if re.search(r"hourly|every hour", t):
        return "hourly :05"
    if "week" in t:
        return "weekly mon 09:00"
    if "month" in t:
        return "monthly 1 09:00"
    m = re.search(r"at\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", t)
    if m:
        h = int(m.group(1))
        if (m.group(3) or "").lower() == "pm" and h < 12:
            h += 12
        return f"daily {h:02d}:{int(m.group(2) or 0):02d}"
    if "evening" in t or "night" in t:
        return "daily 19:00"
    return "daily 09:00"


def _mail_query(trigger: str) -> str:
    m = _MAIL_SEARCH.search(trigger)
    if m:
        addr = m.group(1).strip("<>")
        if "@" in addr:
            return f"from:{addr} newer_than:1d"
    words = [w for w in re.findall(r"[a-z0-9]{4,}",
                                   trigger.lower())
             if w not in ("when", "gets", "sent", "from", "with",
                          "email", "mail", "about", "that", "have",
                          "there", "their")]
    q = " ".join(words[:4]) or trigger[:60]
    return f"in:inbox {q} newer_than:1d"


def propose(sentence: str):
    """'when X, do Y' -> {"artefact", "trigger", "action", "tool",
    "args", "explain"} or None when it can't parse."""
    m = _SPLIT.match(str(sentence or "").strip())
    if not m:
        return None
    trigger, action = m.group(1).strip(), m.group(2).strip()
    if not trigger or not action:
        return None
    trig_l = trigger.lower()
    name = "auto-" + new_id(4)
    prompt = (f"Trigger was '{trigger}'. {action} "
              "Draft, don't send, anything that would write to the "
              "owner's accounts.")
    if _HOOK.search(trig_l):
        return {"artefact": "webhook", "trigger": trigger,
                "action": action, "tool": "automation_apply",
                "args": {"artefact": "webhook", "name": name,
                         "prompt": prompt},
                "explain": f"a webhook endpoint '{name}' that runs "
                           "this action when it fires"}
    if _TIME.search(trig_l):
        when = _when(trig_l)
        return {"artefact": "schedule", "trigger": trigger,
                "action": action, "tool": "automation_apply",
                "args": {"artefact": "schedule", "name": name,
                         "when": when, "prompt": prompt},
                "explain": f"an agent job '{name}' ({when}) that does "
                           "the action on that schedule"}
    url = _URL.search(trigger)
    if url:
        kind, query = "web", url.group(0).rstrip(".,)")
    elif _CAL.search(trig_l):
        kind, query = "calendar", "30"
    else:
        kind, query = "gmail", _mail_query(trigger)
    return {"artefact": "monitor", "trigger": trigger,
            "action": action, "tool": "automation_apply",
            "args": {"artefact": "monitor", "kind": kind,
                     "query": query, "every_minutes": 15},
            "explain": f"a {kind} monitor ('{query[:60]}') that alerts "
                       "when the trigger fires"}


def apply(conn, args: dict) -> dict:
    """automation_apply's effect — runs only behind an Approve."""
    artefact = str(args.get("artefact") or "")
    if artefact == "monitor":
        from . import monitors
        out = monitors.add_monitor(
            conn, str(args.get("kind") or ""),
            str(args.get("query") or ""),
            int(args.get("every_minutes") or 15))
        if out.get("id"):
            return {"created": "monitor", "id": out["id"],
                    "kind": out["kind"], "query": out["query"]}
        return out
    if artefact == "schedule":
        from . import chattools, scheduler as sch
        when = str(args.get("when") or "")
        rec = chattools._parse_recurring(when)
        if rec is None:
            return {"error": "bad_arguments",
                    "message": f"bad 'when' {when!r}"}
        freq, tod, dow, dom = rec
        name = str(args.get("name") or "auto")
        sch.add_schedule(
            conn, name, freq, tod, "Europe/London",
            {"channel_id": str(args.get("channel_id") or ""),
             "prompt": str(args.get("prompt") or "")[:2000]},
            day_of_week=dow, day_of_month=dom, enabled=True,
            kind="agent_job")
        return {"created": "schedule", "name": name, "freq": freq,
                "at": tod}
    if artefact == "webhook":
        from . import webhooks
        out = webhooks.create(
            conn, str(args.get("name") or ""),
            prompt=str(args.get("prompt") or ""))
        if "error" not in out:
            out = {"created": "webhook", "name": out["name"],
                   "url_path": out["url_path"]}
        return out
    return {"error": "bad_arguments",
            "message": f"unknown artefact {artefact!r}"}
