"""morning-briefing — the 07:30 Europe/London daily job
(docs/JARVIS.md).

Seeded DISABLED; '/jobs morning-briefing on' turns it on, '/briefing
now' fires it any time. It gathers today's calendar, important
overnight mail, vault todos coming due and London weather (open-meteo,
no key), posts one text summary and speaks a <=~90s voice note.
"""

import datetime
import logging
import os
import re
import time
import urllib.parse

from . import busy as busymod, chattools, db as dbmod, netclient, \
    sysjobs, vault as vaultmod
from .util import json_loads_bounded

log = logging.getLogger("pa.briefing")

_TODO_RE = re.compile(r"^\s*-\s*\[ \]\s*(.*)$")
_DATE_RE = re.compile(r"(20\d\d-\d\d-\d\d)")

_WMO = {0: "clear", 1: "mostly clear", 2: "partly cloudy",
        3: "overcast", 45: "fog", 48: "icy fog", 51: "light drizzle",
        53: "drizzle", 55: "heavy drizzle", 56: "freezing drizzle",
        57: "freezing drizzle", 61: "light rain", 63: "rain",
        65: "heavy rain", 66: "freezing rain", 67: "freezing rain",
        71: "light snow", 73: "snow", 75: "heavy snow",
        77: "snow grains", 80: "light showers", 81: "showers",
        82: "heavy showers", 85: "snow showers", 86: "snow showers",
        95: "thunderstorm", 96: "thunderstorm + hail",
        99: "thunderstorm + hail"}


# ---------------------------------------------------------------- gather

def _find_list(res, keys):
    """First list of dicts (holding one of `keys`) inside a Composio
    tool result — shapes vary by toolkit version."""
    def hit(v):
        return (isinstance(v, list) and v and all(
            isinstance(m, dict) for m in v) and
            any(k in m for m in v for k in keys))

    def walk(v, depth=0):
        if depth > 6:
            return []
        if hit(v):
            return v
        if isinstance(v, dict):
            for child in v.values():
                found = walk(child, depth + 1)
                if found:
                    return found
        return []
    return walk(res)


def _calendar(ctx):
    out = chattools.run(ctx, "calendar_list",
                        {"days_ahead": 1, "max_results": 12})
    events = out.get("events")
    if events is None:
        events = _find_list(out.get("result") or out,
                            ("summary", "start", "title"))
    lines = []
    for e in (events or [])[:10]:
        if not isinstance(e, dict):
            continue
        when = (e.get("start") or "")
        if isinstance(when, dict):
            when = when.get("dateTime") or when.get("date") or ""
        when = str(when)[5:16].replace("T", " ")
        title = (e.get("summary") or e.get("title") or "(untitled)")
        lines.append(f"• {when} — {str(title)[:80]}".strip(" —"))
    return lines


def _overnight_mail(bot, conn, ctx):
    """What the watcher classified important+ in the last 14h; when the
    watcher is off/empty, fall back to a direct unread scan."""
    cutoff = (datetime.datetime.now(datetime.timezone.utc)
              - datetime.timedelta(hours=14)).isoformat()
    rows = conn.execute(
        "SELECT subject, frm, classification FROM inbox_seen"
        " WHERE classification IN ('critical','urgent','important')"
        " AND created_at>=? ORDER BY created_at DESC LIMIT 10",
        (cutoff,)).fetchall()
    if rows:
        return [f"• {r['subject'][:90]} — {r['frm'][:50]}"
                for r in rows]
    out = chattools.run(ctx, "email_list",
                        {"query": "is:unread newer_than:14h",
                         "max_results": 8})
    msgs = out.get("messages")
    if msgs is None:
        msgs = _find_list(out.get("result") or out,
                          ("subject", "from", "sender"))
    lines = []
    for m in (msgs or [])[:8]:
        if not isinstance(m, dict):
            continue
        frm = m.get("from") or m.get("sender") or "?"
        if isinstance(frm, dict):
            frm = frm.get("email") or frm.get("name") or "?"
        lines.append(
            f"• {str(m.get('subject') or '(no subject)')[:90]}"
            f" — {str(frm)[:50]}")
    return lines


def _vault_due(s, today: datetime.date):
    """Unchecked '- [ ]' lines holding a YYYY-MM-DD <= today."""
    if not vaultmod.configured(s):
        return []
    try:
        root = vaultmod._root(s)
        files, _stats = vaultmod._walk_files(root, s.vault_max_files)
    except Exception:
        return []
    out = []
    for rel, real in files:
        if len(out) >= 12:
            break
        try:
            with open(real, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    m = _TODO_RE.match(line)
                    if not m:
                        continue
                    d = _DATE_RE.search(m.group(1))
                    if not d:
                        continue
                    try:
                        when = datetime.date.fromisoformat(d.group(1))
                    except ValueError:
                        continue
                    if when <= today:
                        text = m.group(1).strip()[:90]
                        out.append(f"• {text} ({rel})")
        except OSError:
            continue
    return out


def _weather(s) -> str:
    """One line of London weather from open-meteo; '' on any failure."""
    try:
        cli = netclient.APIClient(s.open_meteo_base, timeout=10)
        q = urllib.parse.urlencode({
            "latitude": s.briefing_lat, "longitude": s.briefing_lon,
            "current": "temperature_2m,weather_code,wind_speed_10m",
            "daily": "temperature_2m_max,temperature_2m_min,"
                     "precipitation_probability_max",
            "timezone": s.timezone, "forecast_days": 1})
        _h, data = cli.get_json(f"/v1/forecast?{q}")
        cur = data.get("current") or {}
        day = data.get("daily") or {}
        temp = cur.get("temperature_2m")
        code = int(cur.get("weather_code") or 0)
        desc = _WMO.get(code, f"code {code}")
        hi = (day.get("temperature_2m_max") or [None])[0]
        lo = (day.get("temperature_2m_min") or [None])[0]
        rain = (day.get("precipitation_probability_max") or [None])[0]
        bits = [f"{desc} in {s.briefing_city}"]
        if temp is not None:
            bits[0] += f", now {temp}°C"
        if hi is not None and lo is not None:
            bits.append(f"high {hi}°C / low {lo}°C")
        if rain is not None:
            bits.append(f"{rain}% chance of rain")
        return "; ".join(bits)
    except Exception as e:
        log.info("weather fetch failed: %s", e)
        return ""


def _goals(bot, conn):
    """the Goals section — progress since yesterday
    blockers and pal's next action. Read-only; needs PA_GOALS +
    PA_BRIEFING_GOALS."""
    if not (getattr(bot.s, "goals", False)
            and getattr(bot.s, "briefing_goals", False)):
        return []
    try:
        from . import goals as goalsmod, projects as projmod
        lines = []
        for g in goalsmod.list_goals(conn, "active")[:5]:
            bits = [f"• {g['title'][:70]}"]
            if g["progress"]:
                bits.append(f"progress: {g['progress'][:70]}")
            if g["next_action"]:
                bits.append(f"next: {g['next_action'][:70]}")
            blocked = [p for p in projmod.list_projects(
                conn, "blocked") if p["goal_id"] == g["id"]]
            if blocked:
                bits.append(f"blocked on: {blocked[0]['blockers'][:60]}")
            lines.append(" — ".join(bits))
        paused = len(goalsmod.list_goals(conn, "paused"))
        if paused:
            lines.append(f"• {paused} goal(s) paused")
        return lines
    except Exception:
        return []


def gather(bot, conn):
    ctx = chattools.ChatToolContext(
        conn, bot.s, None, "morning-briefing", env=bot.env,
        unattended=True, voice=bot._voice)
    today = datetime.datetime.now(
        _tz(bot.s.timezone)).date()
    return {
        "events": _calendar(ctx),
        "emails": _overnight_mail(bot, conn, ctx),
        "due": _vault_due(bot.s, today),
        "weather": _weather(bot.s),
        "goals": _goals(bot, conn),
        "date": today.isoformat(),
    }


def _tz(name):
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name or "Europe/London")
    except Exception:
        return datetime.timezone.utc


# --------------------------------------------------------------- compose

_BRIEF_PROMPT = """Write a spoken morning briefing for the owner —
plain sentences a voice can read out in under 90 seconds (<= 160
words). No markdown, no bullet characters, no emoji. Warm but terse;
lead with what needs attention. Here is the material:

Weather: {weather}
Calendar today:
{events}
Overnight mail that matters:
{emails}
Vault todos due:
{due}
Standing goals:
{goals}
"""


def compose(bot, conn, parts):
    """-> (text_for_discord, spoken_script)."""
    def block(title, lines):
        if not lines:
            return f"**{title}:** —"
        return f"**{title}:**\n" + "\n".join(lines[:10])

    goal_lines = parts.get("goals") or []
    text = (f"🌅 **Morning briefing — {parts['date']}**\n\n"
            + block(f"Today ({parts['weather'] or 'weather n/a'})",
                    parts["events"])
            + "\n\n" + block("Overnight mail", parts["emails"])
            + "\n\n" + block("Vault todos due", parts["due"])
            + ("\n\n" + block("Goals", goal_lines)
               if goal_lines else ""))

    prompt = _BRIEF_PROMPT.format(
        weather=parts["weather"] or "unavailable",
        events="\n".join(parts["events"]) or "nothing on the calendar",
        emails="\n".join(parts["emails"]) or "nothing important",
        due="\n".join(parts["due"]) or "nothing due",
        goals="\n".join(goal_lines) or "none active")
    spoken = sysjobs.model_text(bot, conn, prompt, budget=40.0,
                                tier="fast")
    if not spoken:
        spoken = _fallback_spoken(parts)
    return text, spoken[:4000]


def _fallback_spoken(parts):
    bits = [f"Good morning. It's {parts['date']}."]
    if parts["weather"]:
        bits.append(f"{parts['weather']}.")
    bits.append(f"{len(parts['events'])} thing"
                f"{'s' if len(parts['events']) != 1 else ''} on the "
                "calendar today.")
    if parts["events"]:
        bits.append(parts["events"][0].lstrip("• ") + ".")
    if parts["emails"]:
        bits.append(f"{len(parts['emails'])} overnight email"
                    f"{'s' if len(parts['emails']) != 1 else ''} "
                    "worth a look.")
    if parts["due"]:
        bits.append(f"{len(parts['due'])} vault todo"
                    f"{'s' if len(parts['due']) != 1 else ''} due.")
    if parts.get("goals"):
        bits.append(f"{len(parts['goals'])} standing goal"
                    f"{'s' if len(parts['goals']) != 1 else ''} "
                    "in play.")
    return " ".join(bits)


# ------------------------------------------------------------------- run

def run(bot, payload) -> str:
    conn = dbmod.connect(bot.db_path)
    try:
        parts = gather(bot, conn)
        text, spoken = compose(bot, conn, parts)
        target = str(payload.get("channel_id") or "") \
            or sysjobs.owner_dm(bot)
        if not target:
            return "no channel to post the briefing to"
        bot.api.post_message(target, text[:1900])
        if bot._voice is not None and \
                busymod.voice_should_send(bot):
            bot._voice.send_voice_note(target, spoken)
        return (f"briefing posted: {len(parts['events'])} events, "
                f"{len(parts['emails'])} mails, "
                f"{len(parts['due'])} due")
    finally:
        conn.close()
