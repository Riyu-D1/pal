"""A5: "leave now" alerts — TfL journey planner + calendar locations
(final-5, PA_LEAVE_NOW, default off).

Runs inside the 'cal-triage' tick: for each upcoming event that has a
location we plan a journey with the free TfL Journey Planner API and
compute a leave-by time. Two pings per event ride notify()
kind='leave-now': "leave in 10m" and "leave now" — deduped
per event+kind, held in quiet hours like any 'high' alert.

Origin: PA_HOME_LAT/PA_HOME_LON (falling back to the briefing
lat/lon), or a recent phone-node 'location' fix ONLY when
PA_LEAVE_NOW_USE_PHONE_LOC is on and a fix younger than
_PHONE_LOC_FRESH_S exists in node_jobs — pal never dispatches a
location request from this path; the cap stays owner-gated and the
phone only volunteers fixes it already produced.

TFL_APP_KEY is sent as app_key when set and never logged. On any TfL
error / no journey we skip quietly (the plain cal-soon ping still
covers 'starts soon') — a made-up travel time is worse than none.
"""

import datetime
import json
import logging
import urllib.parse

from . import db as dbmod
from .netclient import APIClient, HTTPError, TransportError
from .util import now_iso

log = logging.getLogger("pa.leavenow")

_HORIZON_MIN = 240            # don't plan journeys for far-off events
_SOON_LEAD_S = 10 * 60        # "leave in 10m" window before leave-by
_PHONE_LOC_FRESH_S = 30 * 60  # a stored fix older than this is stale
_CACHE = "leave_j:"           # pa_settings key prefix (event_id -> min)


def _latlon(bot, conn):
    """Origin string for TfL: 'lat,lon' or '' when we have nothing.

    Phone fix first ONLY under the flag and only when fresh — read
    from node_jobs results; nothing is dispatched from here."""
    if getattr(bot.s, "leave_now_phone_loc", False):
        fix = _phone_fix(conn)
        if fix:
            return fix
    lat = (getattr(bot.s, "home_lat", "") or "").strip()
    lon = (getattr(bot.s, "home_lon", "") or "").strip()
    if lat and lon:
        return f"{lat},{lon}"
    try:
        return f"{bot.s.briefing_lat},{bot.s.briefing_lon}"
    except Exception:
        return ""


def _phone_fix(conn) -> str:
    """Newest completed 'location' node job under _PHONE_LOC_FRESH_S
    -> 'lat,lon' or ''. Reads existing rows only — never polls."""
    try:
        row = conn.execute(
            "SELECT result_json, created_at FROM node_jobs"
            " WHERE cap='location' AND status='done'"
            " AND result_json IS NOT NULL"
            " ORDER BY id DESC LIMIT 1").fetchone()
    except Exception:
        return ""
    if not row:
        return ""
    try:
        age = (datetime.datetime.now(datetime.timezone.utc)
               - datetime.datetime.fromisoformat(
                   row["created_at"].replace("Z", "+00:00"))
               ).total_seconds()
        if age > _PHONE_LOC_FRESH_S:
            return ""
        res = json.loads(row["result_json"])
        lat = res.get("lat", res.get("latitude"))
        lon = res.get("lon", res.get("longitude"))
        if lat is None or lon is None:
            return ""
        return f"{float(lat)},{float(lon)}"
    except Exception:
        return ""


def journey_minutes(bot, origin: str, dest: str) -> int:
    """Fastest current itinerary duration in whole minutes, or 0.

    A short app_key goes as a query param only — it is never logged
    (the exception path carries no URL)."""
    if not origin or not dest:
        return 0
    app_key = (bot.env.get("TFL_APP_KEY") or "").strip()
    q = ("?" + urllib.parse.urlencode({"app_key": app_key})
         if app_key else "")
    path = ("/Journey/JourneyResults/"
            + urllib.parse.quote(origin, safe=",")
            + "/to/" + urllib.parse.quote(dest[:160], safe="") + q)
    try:
        _h, data = APIClient(bot.s.tfl_base, timeout=15).get_json(path)
    except (HTTPError, TransportError, ValueError) as e:
        log.info("tfl journey failed (%s)", type(e).__name__)
        return 0
    durations = [int(j["duration"]) for j in (data.get("journeys")
                                              or [])
                 if isinstance(j.get("duration"), (int, float))]
    return min(durations) if durations else 0


def _travel_minutes(bot, conn, event_id: str, origin: str,
                    dest: str) -> int:
    """Cached per event+origin bucket — the same tick replans at most
    once per event (the cache key pins the origin so a new home or a
    fresh phone fix replans)."""
    key = f"{_CACHE}{event_id}:{origin}"
    try:
        v = int(float(dbmod.get_setting(conn, key, "") or 0))
        if v:
            return v
    except ValueError:
        pass
    mins = journey_minutes(bot, origin, dest)
    if mins:
        dbmod.set_setting(conn, key, str(mins))
    return mins


def alerts(bot, conn, evs, now) -> int:
    """Fire 'leave in 10m' / 'leave now' through notify(); returns how
    many were sent/held. Safe no-op when PA_LEAVE_NOW is off."""
    if not getattr(bot.s, "leave_now", False):
        return 0
    from . import activity, notify
    origin = _latlon(bot, conn)
    if not origin:
        return 0
    fired = 0
    for ev in evs:
        start = _parse(ev.get("start"))
        loc = (ev.get("location") or "").strip()
        if not start or start.tzinfo is None or not loc \
                or ev.get("all_day"):
            continue
        away = (start - now).total_seconds()
        if away < 0 or away > _HORIZON_MIN * 60:
            continue
        mins = _travel_minutes(bot, conn, ev["id"], origin, loc)
        if not mins:
            continue                       # no journey -> skip quietly
        leave_by = start - datetime.timedelta(minutes=mins)
        til_leave = (leave_by - now).total_seconds()
        if til_leave <= 0:
            kind, text = "now", "leave now"
        elif til_leave <= _SOON_LEAD_S:
            kind, text = "in10", f"leave in ~{int(til_leave / 60) or 1}m"
        else:
            continue
        msg = (f"🚇 **{text}** for **{ev['summary'] or '(untitled)'}**"
               f"\n  {ev['start']} @ {loc}"
               f" — ~{mins}m journey, leave by "
               f"{leave_by.isoformat()[:16]}")
        out = notify.notify(bot, conn, "leave-now", "high", msg,
                            dedupe_key=f"leave:{ev['id']}:{kind}")
        if out in ("sent", "held"):
            activity.record(conn, "noticed",
                            f"{text}: {ev['summary'][:70]}",
                            body=msg, source="leave-now",
                            ref={"event_id": ev["id"], "kind": kind,
                                 "journey_min": mins})
            fired += 1
    return fired


def _parse(dt):
    try:
        return datetime.datetime.fromisoformat(
            str(dt).replace("Z", "+00:00"))
    except ValueError:
        return None
