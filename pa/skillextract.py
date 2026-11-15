"""skill auto-extraction (Voyager-style, docs/TOOLMAKING.md).

After an attended turn lands an 'ok' outcome with >= 3 successful
tool steps, maybe_extract() asks the fast model to distill the trace
into a reusable procedure {name, description, body}. The proposal is
deduped against existing skills (slug match or keyword overlap) and
against its own content hash (a pa_settings marker — no new table),
then queued as a job_action whose tool is `skill_save`: the owner
sees the card and the ordinary Approve path writes the skill — no
skill is ever saved without that click.

suggest_for() picks the top-k saved skills for an incoming message by
keyword overlap; chatagent annotates the skills block with them so
the model loads the right procedure.
"""

import hashlib
import logging
import re

from .util import json_loads_bounded

log = logging.getLogger("pa.skillextract")

MIN_STEPS = 3
_WORD_RE = re.compile(r"[a-z0-9]{3,}")

_EXTRACT_PROMPT = """\
A pal turn just succeeded. Distill it into ONE reusable skill.

User request: {request}

Steps taken:
{steps}

Reply: {reply}

Return ONLY a JSON object:
{{"name": "short-slug-name",
  "description": "one line — when to use this",
  "body": "the procedure: numbered steps, tools to call, gotchas"}}

Rules: generic, not this one request; <= 25 lines of body; name in
[a-z0-9-]; if the steps are not worth reusing return {{"skip": true}}."""


def _tool_steps(trace) -> list:
    """Successful tool calls in order, args summarised small."""
    out = []
    for step in trace or []:
        if not isinstance(step, dict) or "tool" not in step:
            continue
        res = step.get("result")
        if isinstance(res, dict) and res.get("error"):
            continue
        args = step.get("args")
        summary = ""
        if isinstance(args, dict):
            summary = ", ".join(
                f"{k}={str(v)[:40]}" for k, v in
                list(args.items())[:3])
        out.append(f"{step['tool']}({summary})")
    return out


def _words(text: str) -> set:
    return set(_WORD_RE.findall((text or "").lower()))


def _similar_exists(conn, s, name: str, description: str) -> bool:
    """Dedupe: identical slug, or >=60% keyword overlap with any
    existing skill's name+description."""
    from . import skills as skillsmod
    slug = skillsmod.slug(name)
    try:
        existing = skillsmod.list_skills(conn, s)
    except Exception:
        return False
    want = _words(f"{slug} {description}")
    for row in existing:
        have = _words(f"{row['name']} {row.get('description') or ''}")
        if row["name"] == slug:
            return True
        if want and len(want & have) / len(want) >= 0.6:
            return True
    return False


def _mark_seen(conn, key: str) -> bool:
    """True if this proposal was already queued (pa_settings marker —
    keeps this off the migration budget)."""
    from . import db as dbmod
    if dbmod.get_setting(conn, key):
        return True
    dbmod.set_setting(conn, key, "1")
    return False


def maybe_extract(brain, conn, s, channel_id, request, trace,
                  reply) -> dict:
    """Called from ChatAgent._answer on an 'ok' attended turn. Returns
    {"proposed": name} or {} — never raises, never writes a skill."""
    out = {}
    try:
        if not getattr(s, "skillextract", False) or brain is None:
            return out
        steps = _tool_steps(trace)
        if len(steps) < MIN_STEPS:
            return out
        raw = brain.complete(
            [{"role": "user", "content": _EXTRACT_PROMPT.format(
                request=str(request)[:800],
                steps="\n".join(f"{i}. {t}" for i, t in
                                enumerate(steps[:15], 1)),
                reply=str(reply)[:600])}],
            20.0, "fast") or ""
        data = json_loads_bounded(raw.strip().strip("`")) or {}
        if data.get("skip"):
            return out
        name = str(data.get("name") or "").strip()
        desc = str(data.get("description") or "").strip()
        body = str(data.get("body") or "").strip()
        if not (name and body):
            return out
        from . import skills as skillsmod
        name = skillsmod.slug(name)
        digest = hashlib.sha1(
            f"{name}\n{desc}\n{body}".encode()).hexdigest()[:16]
        if _mark_seen(conn, f"skillprop:{digest}") or \
                _similar_exists(conn, s, name, desc):
            return out
        from . import approvals
        approvals.create_approval(
            conn, "job_action",
            {"tool": "skill_save",
             "args": {"name": name, "description": desc,
                      "body": body},
             "channel_id": str(channel_id or "")})
        from . import db as dbmod
        dbmod.audit(conn, "assistant", "skill_propose", name)
        return {"proposed": name}
    except Exception:
        log.warning("skill extraction failed", exc_info=True)
        return out


def suggest_for(conn, s, text: str, k: int = 3) -> list:
    """Top-k saved skills for an incoming message, by keyword overlap
    of the query with name+description."""
    try:
        from . import skills as skillsmod
        q = _words(text)
        if not q:
            return []
        scored = []
        for row in skillsmod.list_skills(conn, s):
            have = _words(
                f"{row['name']} {row.get('description') or ''}")
            overlap = len(q & have)
            if overlap:
                scored.append((overlap, row["name"]))
        scored.sort(key=lambda t: (-t[0], t[1]))
        return [name for _, name in scored[:k]]
    except Exception:
        return []
