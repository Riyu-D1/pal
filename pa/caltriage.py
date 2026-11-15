"""calendar triage + meeting prep (PA_CAL_TRIAGE).

'cal-triage' system job (seeded disabled, every 5 min). For every
event starting within PREP_MINUTES pal posts a prep card: the event
plus KG/recall context and open todos — a local activity row always,
an owner DM only outside quiet hours. Overlapping events produce a
proposal card with move/decline draft text. Nothing ever writes to
the calendar — events come read-only from pa.calsource (Composio
googlecalendar first, the legacy GCalClient as fallback).

the same tick also carries a cheaper "meeting soon" ping
(PA_CAL_SOON, leads PA_CAL_SOON_LEADS, default 15m/5m) that runs even
when full prep is off and routes through notify() — deduped per
event+lead, held during quiet hours.
"""

import datetime
import logging

from . import activity, db as dbmod, sysjobs
from .util import now_iso

log = logging.getLogger("pa.caltriage")

PREP_MINUTES = 30
_CONFLICT_DEDUPE = 12 * 3600  # seconds between repeat conflict cards


def _parse(dt):
    try:
        return datetime.datetime.fromisoformat(
            str(dt).replace("Z", "+00:00"))
    except ValueError:
        return None


def _prepped(conn, event_id) -> bool:
    return bool(dbmod.get_setting(conn, f"cal_prep:{event_id}", ""))


def _mark(conn, event_id):
    dbmod.set_setting(conn, f"cal_prep:{event_id}", now_iso())


def _context_lines(conn, summary: str) -> list:
    """KG + recall context + open todos, each capped — all reads."""
    lines = []
    try:
        from . import kg
        out = kg.query(conn, summary)
        for e in (out.get("edges") or [])[:2]:
            lines.append(
                f"  KG: {e['from']} —{e['rel']}→ {e['to']}"
                f" ({e['source']})")
    except Exception:
        pass
    try:
        from . import recall
        for h in recall.search(conn, summary, limit=2):
            lines.append(
                f"  recall [{h['source']} {h['day']}]: "
                f"{h['title'][:60]}")
    except Exception:
        pass
    try:
        rows = conn.execute(
            "SELECT text FROM chat_todos WHERE done=0"
            " ORDER BY id DESC LIMIT 2").fetchall()
        for r in rows:
            lines.append(f"  open todo: {r['text'][:60]}")
    except Exception:
        pass
    return lines


def _conflicts(evs) -> list:
    out = []
    evs = [e for e in evs
           if _parse(e.get("start")) and not e.get("all_day")]
    evs.sort(key=lambda e: _parse(e["start"]))
    for i, a in enumerate(evs):
        ae = _parse(a.get("end"))
        for b in evs[i + 1:]:
            bs = _parse(b["start"])
            if ae and bs < ae:
                out.append((a, b))
    return out


def _last_conflict_card(conn) -> float:
    try:
        return float(dbmod.get_setting(
            conn, "cal_conflict_last", "0") or 0)
    except ValueError:
        return 0.0


# -- meeting-soon alerts --------------------------------------

def soon_leads(settings) -> list:
    """Configured lead minutes, ascending (default 15 and 5)."""
    raw = getattr(settings, "cal_soon_leads", "") or "15,5"
    out = []
    for part in str(raw).split(","):
        try:
            v = int(part.strip())
        except ValueError:
            continue
        if 1 <= v <= 120:
            out.append(v)
    return sorted(set(out)) or [15, 5]


def _soon_alerts(bot, conn, evs, now) -> int:
    """Fire the tightest applicable lead per event, once per lead,
    through notify() (kind=cal-soon, high — held in quiet hours)."""
    from . import notify
    fired = 0
    leads = soon_leads(bot.s)
    for ev in evs:
        start = _parse(ev.get("start"))
        if not start or start.tzinfo is None or ev.get("all_day"):
            continue
        away = (start - now).total_seconds() / 60.0
        if away < 0:
            continue
        applicable = [l for l in leads if away <= l]
        if not applicable:
            continue
        lead = applicable[0]   # tightest (leads sorted ascending)
        mins = int(away + 0.5)
        text = (f"⏰ in {mins}m: **{ev['summary'] or '(untitled)'}**"
                f"\n  {ev['start']}"
                + (f" @ {ev['location']}" if ev.get("location") else ""))
        out = notify.notify(bot, conn, "cal-soon", "high", text,
                            dedupe_key=f"cal-soon:{ev['id']}:{lead}")
        if out in ("sent", "held"):
            activity.record(conn, "noticed",
                            f"meeting soon: {ev['summary'][:80]}",
                            body=text, source="cal-soon",
                            ref={"event_id": ev["id"], "lead": lead})
            fired += 1
    return fired


def run(bot, payload) -> str:
    triage_on = getattr(bot.s, "cal_triage", False)
    soon_on = getattr(bot.s, "cal_soon", False)
    leave_on = getattr(bot.s, "leave_now", False)
    if not (triage_on or soon_on or leave_on):
        return ""
    conn = bot._db()
    try:
        from . import call as callmod, calsource
        quiet = callmod.in_quiet_hours(
            getattr(bot.s, "quiet_hours", ""), bot.s.timezone)
        try:
            evs = calsource.list_events(
                bot.s, bot.env, conn, days_ahead=1, max_results=25)
        except Exception as e:
            log.info("cal-triage calendar read failed: %s", e)
            return ""
        if evs is None:
            return ""   # no calendar source (logged in calsource)
        now = datetime.datetime.now(datetime.timezone.utc)
        soon = _soon_alerts(bot, conn, evs, now) if soon_on else 0
        left = 0
        if leave_on:
            try:
                from . import leavenow
                left = leavenow.alerts(bot, conn, evs, now)
            except Exception as e:
                log.info("leave-now tick failed: %s", e)
        horizon = now + datetime.timedelta(minutes=PREP_MINUTES)
        prepped, cards = 0, 0
        if not triage_on:
            parts = []
            if soon:
                parts.append(f"{soon} soon alert(s)")
            if left:
                parts.append(f"{left} leave alert(s)")
            return ", ".join(parts)
        for ev in evs:
            start = _parse(ev.get("start"))
            if not start or start.tzinfo is None or ev.get("all_day"):
                continue
            if not (now <= start <= horizon) \
                    or _prepped(conn, ev["id"]):
                continue
            lines = [
                f"📅 in {int((start - now).total_seconds() // 60)}m: "
                f"**{ev['summary'] or '(untitled)'}**",
                f"  {ev['start']} → {ev['end']}"
                + (f" @ {ev['location']}" if ev.get("location")
                   else "")]
            lines += _context_lines(conn, ev["summary"])
            activity.record(conn, "noticed",
                            f"meeting prep: {ev['summary'][:80]}",
                            body="\n".join(lines),
                            source="cal-triage",
                            ref={"event_id": ev["id"]})
            if not quiet:
                try:
                    sysjobs.dm_and_voice(bot, "\n".join(lines))
                except Exception:
                    pass
            _mark(conn, ev["id"])
            prepped += 1
        # conflict proposals — drafts only, throttled
        for a, b in _conflicts(evs):
            import time
            if time.time() - _last_conflict_card(conn) \
                    < _CONFLICT_DEDUPE:
                break
            text = (f"clash: **{a['summary']}** ({a['start']})"
                    f" overlaps **{b['summary']}** ({b['start']})")
            drafts = (f"• decline '{b['summary']}': \"Sorry — I have "
                      f"{a['summary']} then; could we move ours?\"\n"
                      f"• or propose moving '{a['summary']}' 30m")
            activity.record(conn, "draft",
                            f"calendar conflict: {a['summary'][:40]}"
                            f" / {b['summary'][:40]}",
                            body=f"{text}\n{drafts}",
                            source="cal-triage",
                            ref={"a": a["id"], "b": b["id"]})
            dbmod.set_setting(conn, "cal_conflict_last",
                              str(time.time()))
            cards += 1
        parts = []
        if soon:
            parts.append(f"{soon} soon alert(s)")
        if left:
            parts.append(f"{left} leave alert(s)")
        if prepped or cards:
            parts.append(f"{prepped} prep card(s), "
                         f"{cards} conflict card(s)")
        return ", ".join(parts)
    finally:
        conn.close()
