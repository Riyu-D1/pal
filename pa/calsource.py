"""final-6: ONE calendar read path for the background jobs.

Production connects the owner's Google Calendar through Composio
('/connect googlecalendar'); the GMAIL_* OAuth env grant usually
isn't deployed there, so jobs that only knew GCalClient silently
skipped (cal-triage, cal-soon, leave-now, calendar monitors, the
in-call 'calendar_today' fast tool). Order:

  1. an active Composio 'googlecalendar' connection (read-only
     GOOGLECALENDAR_FIND_EVENT)
  2. the legacy GCalClient (GMAIL_* env creds)
  3. None — logged ONCE as 'calendar source unavailable', never a
     silent skip.

Everything here is read-only: no code path in this module can write
a calendar. Events are normalised to one shape — see _norm_event.
"""

import datetime
import logging
import os
import re

from .util import utcnow

log = logging.getLogger("pa.calsource")

# connections.provider names the calendar toolkit has been seen under
_PROVIDERS = ("googlecalendar", "google_calendar", "gcal")
_FIND_SLUGS = ("GOOGLECALENDAR_FIND_EVENT",)
_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MAX = 50

_warned = False


def _log_unavailable():
    global _warned
    if _warned:
        return
    _warned = True
    log.info("calendar source unavailable — no active Composio "
             "googlecalendar connection and no GMAIL_* OAuth grant")


# ---------------------------------------------------------------------------
# normalisation: every event becomes
#   {"id", "summary", "start", "end", "location", "all_day", "transparent"}
# start/end are ISO strings — timed events carry an explicit tz offset
# (so they parse tz-aware), all-day events stay 'YYYY-MM-DD'.

def _zone(name, s):
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name or getattr(s, "timezone", "") or "UTC")
    except Exception:
        return datetime.timezone.utc


def _norm_when(value, s):
    """One start/end field -> (iso str, all_day). Google shapes:
    {'dateTime':..,'timeZone':..} | {'date':..} | bare str/datetime."""
    tzname = ""
    if isinstance(value, dict):
        tzname = str(value.get("timeZone") or "")
        dt = value.get("dateTime")
        if not dt:
            d = value.get("date")
            return (str(d)[:10], True) if d else ("", False)
    else:
        dt = value
    if dt is None:
        return "", False
    raw = str(dt).strip()
    if _DATE_ONLY.match(raw):
        return raw, True
    try:
        parsed = datetime.datetime.fromisoformat(
            raw.replace("Z", "+00:00"))
    except ValueError:
        return raw[:40], False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_zone(tzname, s))
    return parsed.isoformat(), False


def _norm_event(raw, s):
    if not isinstance(raw, dict):
        return None
    start, s_all = _norm_when(raw.get("start"), s)
    end, e_all = _norm_when(raw.get("end"), s)
    if not start:
        return None
    etype = str(raw.get("eventType") or raw.get("event_type") or "")
    return {
        "id": str(raw.get("id") or raw.get("event_id") or ""),
        "summary": str(raw.get("summary") or raw.get("title")
                       or "")[:300],
        "start": start,
        "end": end,
        "location": str(raw.get("location") or "")[:300],
        "all_day": bool(s_all or e_all
                        or etype.lower() in ("allday", "all_day",
                                             "all-day")),
        "transparent": str(raw.get("transparency") or "").lower()
                       == "transparent",
    }


def _find_items(res):
    """First list of event-ish dicts inside a Composio tool result —
    nesting varies by toolkit version ('data.items', 'events', ...)."""
    KEYS = ("summary", "start", "title")

    def hit(v):
        return (isinstance(v, list) and v and all(
            isinstance(m, dict) for m in v)
            and any(k in m for m in v for k in KEYS))

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


def _normalise(events, s):
    return [e for e in (_norm_event(raw, s) for raw in events or [])
            if e is not None]


# ---------------------------------------------------------------------------
# sources

def _as_env(env):
    """callers pass os.environ, a plain dict, or (in tests) a fixture
    object holding one — normalise to a .get-able mapping."""
    if env is None:
        return os.environ
    if hasattr(env, "get"):
        return env
    return getattr(env, "env", None) or os.environ


def _composio_row(conn):
    """The active Composio calendar connection row, or None."""
    if conn is None:
        return None
    from . import connectflows
    for p in _PROVIDERS:
        try:
            row = connectflows.get(conn, p)
        except Exception:
            row = None
        if row and row["kind"] == "composio" \
                and row["status"] == "active":
            return row
    return None


def _pick_slug(conn, s, env, row) -> str:
    """The list-events slug the toolkit actually advertises, else the
    canonical name so execute() surfaces Composio's own error."""
    from . import connectflows
    try:
        names = {t["name"] for t in connectflows.list_tools(
            conn, s, env, row)}
    except Exception:
        names = set()
    for c in _FIND_SLUGS:
        if c in names:
            return c
    return _FIND_SLUGS[0]


def _via_composio(s, env, conn, args):
    """-> normalised events, or None when no active Composio calendar
    connection exists / the read failed (caller tries legacy next)."""
    if conn is None or not s.composio_key(env):
        return None
    row = _composio_row(conn)
    if row is None:
        return None
    from . import connectflows
    try:
        out = connectflows.call_tool(
            conn, s, env, row,
            _pick_slug(conn, s, env, row), args)
    except Exception as e:
        log.info("composio calendar read failed: %s", e)
        return None
    if not isinstance(out, dict) or out.get("error"):
        log.info("composio calendar read error: %s",
                 (out or {}).get("error") if isinstance(out, dict)
                 else type(out).__name__)
        return None
    res = out.get("result")
    if isinstance(res, dict) and (res.get("error")
                                  or res.get("successful") is False):
        log.info("composio calendar read error: %s",
                 str(res.get("error") or "unsuccessful")[:200])
        return None
    return _normalise(_find_items(res if res is not None else out), s)


def _via_legacy(s, env, conn, days_ahead, max_results, time_min):
    """-> normalised events via the GMAIL_* OAuth grant, or None."""
    from . import gcal
    try:
        cli = gcal.GCalClient(s, env, conn=conn)
    except gcal.GCalNotConfigured:
        return None
    try:
        evs = cli.list_events(
            days_ahead=days_ahead, max_results=max_results,
            time_min=time_min.isoformat(timespec="seconds")
            if time_min is not None else None)
    except Exception as e:
        log.info("calendar read failed: %s", e)
        return None
    return _normalise(evs, s)


def list_events(s, env, conn, days_ahead=1, max_results=25,
                time_min=None, time_max=None):
    """Normalised events overlapping [time_min, time_max), or None
    when no calendar source exists (logged once, not silently)."""
    env = _as_env(env)
    now = utcnow()
    tmin = time_min or now
    tmax = time_max or tmin + datetime.timedelta(
        days=max(1, min(int(days_ahead or 1), 60)))
    args = {"calendar_id": "primary",
            "timeMin": tmin.isoformat(timespec="seconds"),
            "timeMax": tmax.isoformat(timespec="seconds"),
            "max_results": max(1, min(int(max_results or 25), _MAX)),
            "single_events": True, "order_by": "startTime"}
    evs = _via_composio(s, env, conn, args)
    if evs is None:
        evs = _via_legacy(s, env, conn, days_ahead, max_results, tmin)
    if evs is None:
        _log_unavailable()
        return None
    return evs
