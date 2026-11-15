"""evolve-reflect sysjob (docs/EVOLVE.md #7): nightly, after
daily-journal. Reads today's journal text + incidents + bad
feedback and drafts at most PA_EVOLVE_REFLECT_MAX pending
lesson/skill items. Never writes owner_facts, never applies —
item 10's Track A does that.

Model failures can't kill the job: it reuses sysjobs.model_text
(fast tier) which already returns '' on any error.
"""

import hashlib
import logging
import re

from .. import journal, sysjobs
from ..util import now_iso
from . import items

log = logging.getLogger("pa.evolve.reflect")


def _today_incidents(conn) -> str:
    try:
        rows = conn.execute(
            "SELECT kind, source, summary, COUNT(*) n FROM incidents"
            " WHERE created_at LIKE ?"
            " GROUP BY fingerprint ORDER BY n DESC LIMIT 12",
            (now_iso()[:10] + "%",)).fetchall()
    except Exception:
        return ""
    return "\n".join(
        f"- {r['n']}x [{r['kind']}/{r['source']}] {r['summary']}"
        for r in rows)


def _today_feedback(conn) -> str:
    try:
        rows = conn.execute(
            "SELECT outcome, detail FROM turn_outcomes"
            " WHERE created_at LIKE ? AND outcome IN"
            " ('feedback_bad','low_confidence','error')"
            " ORDER BY id DESC LIMIT 10",
            (now_iso()[:10] + "%",)).fetchall()
    except Exception:
        return ""
    return "\n".join(f"- {r['outcome']}: {r['detail'][:120]}"
                     for r in rows)


def _prompt(day_text, incidents, feedback) -> str:
    return (
        "You are pal's nightly reflection. From today's journal, "
        "incidents and negative feedback, draft up to "
        "{n} self-improvement lessons or tiny skills — PENDING "
        "suggestions, nothing gets applied tonight.\n\n"
        "Reply with AT MOST {n} blocks, each exactly:\n"
        "LESSON: <one short rule pal should follow>\n"
        "WHY: <one line of evidence>\n"
        "or\n"
        "SKILL: <short skill name>: <what it would do>\n"
        "WHY: <one line of evidence>\n\n"
        "Only suggest what the evidence shows. Skip compliments. "
        "If nothing is worth a lesson, reply 'NONE'.\n\n"
        "== JOURNAL ==\n{j}\n== INCIDENTS ==\n{i}\n"
        "== FEEDBACK ==\n{f}").format(
        n="{MAX}", j=day_text[:2500] or "(quiet day)",
        i=incidents or "(none)", f=feedback or "(none)")


def _parse(text, cap):
    out = []
    for m in re.finditer(
            r"(LESSON|SKILL):\s*(.+?)\nWHY:\s*(.+?)(?=\n(?:LESSON|SKILL)"
            r":|\Z)", text, re.S):
        kind, title, why = (m.group(1).lower(), m.group(2).strip(),
                            m.group(3).strip())
        if not title:
            continue
        if kind == "skill" and ":" in title:
            name, desc = title.split(":", 1)
            title = f"{name.strip()}: {desc.strip()}"
        out.append((kind, title[:200], why[:300]))
        if len(out) >= cap:
            break
    return out


def run(bot, payload) -> str:
    if not bot.s.evolve:
        return ""
    conn = bot._db()
    try:
        day = now_iso()[:10]
        prompt = _prompt(journal.journal_text(conn, day),
                         _today_incidents(conn), _today_feedback(conn)
                         ).replace("{MAX}",
                                   str(bot.s.evolve_reflect_max))
        text = sysjobs.model_text(bot, conn, prompt)
        made = []
        for kind, title, why in _parse(text,
                                       bot.s.evolve_reflect_max):
            ded = "ref:" + hashlib.sha256(
                (kind + "|" + title.lower()).encode()).hexdigest()[:16]
            iid = items.open_item(
                conn, kind, "low", title=f"reflect: {title[:120]}",
                plan=f"evidence: {why}", dedupe_key=ded)
            if iid:
                made.append(iid)
                from . import apply
                apply.maybe_apply(conn, bot.s, iid, bot=bot)
        # final-2 M2: sleeptime curator — deterministic memory
        # curation once a night (proposals, never silent deletes).
        if getattr(bot.s, "sleeptime", False) and \
                getattr(bot.s, "memory_blocks", False):
            try:
                from .. import sleeptime
                st = sleeptime.run(conn, bot.s)
                made.append(f"sleeptime:{sum(st.values())}")
            except Exception:
                log.exception("sleeptime pass failed")
        return f"{len(made)} lesson item(s)" if made else ""
    finally:
        conn.close()
