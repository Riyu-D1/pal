"""Durable, restart-safe scheduler.

- Schedules live in SQLite (`schedules`); every fired occurrence is one
  row in `schedule_runs` keyed UNIQUE(schedule_id, fire_key). A crash
  between "should fire" and "task created" is impossible — both happen
  inside one BEGIN IMMEDIATE transaction — and a restart can never
  duplicate a run: the second insert is ignored.
- Timezone per schedule (default Europe/London via PA_TIMEZONE) using
  IANA zones, so Europe/London DST transitions move the UTC fire instant
  correctly. Nonexistent local times (spring-forward) map to the instant
  the zoneinfo rules give; ambiguous times use the first occurrence.
- Frequencies: 'daily' at time_of_day, 'weekly' on day_of_week at
  time_of_day, 'monthly' on day_of_month (clamped to month end) at
  time_of_day, 'hourly' at the minute part of time_of_day, 'interval'
  every day_of_month minutes (day_of_month is reused as the interval —
  e.g. 15 means :00/:15/:30/:45).
- Missed occurrences fire once on catch-up (the latest due slot), never
  in a storm.
- Everything ships DISABLED; a schedule does nothing until the owner
  enables it and its assistant exists.
"""

import calendar
import datetime
import json
import logging

from .util import json_loads_bounded, new_id, now_iso, utcnow

log = logging.getLogger("pa.scheduler")

FREQS = ("daily", "weekly", "monthly", "hourly", "once", "interval")
KINDS = ("create_task", "chat_prompt", "agent_job")


def _zone(name: str):
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        raise RuntimeError("zoneinfo unavailable (install tzdata)")
    try:
        return ZoneInfo(name)
    except Exception:
        raise ValueError(f"unknown timezone {name!r}")


def parse_time_of_day(value: str):
    """'HH:MM' -> (hour, minute). Raises ValueError."""
    parts = (value or "").strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"time_of_day must be HH:MM, got {value!r}")
    hh, mm = int(parts[0]), int(parts[1])
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        raise ValueError(f"time_of_day out of range: {value!r}")
    return hh, mm


def latest_occurrence(sched, now_utc=None):
    """fire_key ('YYYY-MM-DDTHH:MM' local) of the most recent scheduled
    occurrence at or before now_utc, or None when nothing is due."""
    now_utc = now_utc or utcnow()
    tz = _zone(sched["timezone"])
    if sched["freq"] == "once":
        # time_of_day carries the full 'YYYY-MM-DDTHH:MM' local key
        key = sched["time_of_day"]
        if occurrence_instant(sched, key) <= now_utc:
            return key
        return None
    hh, mm = parse_time_of_day(sched["time_of_day"])
    local_now = now_utc.astimezone(tz)
    freq = sched["freq"]

    if freq == "daily":
        cand = local_now.date()
        inst = datetime.datetime(cand.year, cand.month, cand.day,
                                 hh, mm, tzinfo=tz)
        if inst.astimezone(datetime.timezone.utc) > now_utc:
            cand = cand - datetime.timedelta(days=1)
    elif freq == "weekly":
        dow = sched["day_of_week"]
        if dow is None:
            raise ValueError("weekly schedule needs day_of_week")
        back = (local_now.weekday() - int(dow)) % 7
        cand = local_now.date() - datetime.timedelta(days=back)
        inst = datetime.datetime(cand.year, cand.month, cand.day,
                                 hh, mm, tzinfo=tz)
        if inst.astimezone(datetime.timezone.utc) > now_utc:
            cand = cand - datetime.timedelta(days=7)
    elif freq == "monthly":
        dom = sched["day_of_month"]
        if dom is None:
            raise ValueError("monthly schedule needs day_of_month")
        cand = _clamp_day(local_now.year, local_now.month, int(dom))
        inst = datetime.datetime(cand.year, cand.month, cand.day,
                                 hh, mm, tzinfo=tz)
        if inst.astimezone(datetime.timezone.utc) > now_utc:
            y, m = local_now.year, local_now.month - 1
            if m == 0:
                y, m = y - 1, 12
            cand = _clamp_day(y, m, int(dom))
    elif freq == "hourly":
        cand = local_now.date()
        inst = datetime.datetime(cand.year, cand.month, cand.day,
                                 local_now.hour, mm, tzinfo=tz)
        if inst.astimezone(datetime.timezone.utc) > now_utc:
            inst = inst - datetime.timedelta(hours=1)
            cand = inst.astimezone(tz).date()
            return f"{cand.isoformat()}T{inst.astimezone(tz).hour:02d}:{mm:02d}"
        return f"{cand.isoformat()}T{local_now.hour:02d}:{mm:02d}"
    elif freq == "interval":
        # day_of_month carries the interval in minutes; the fire key is
        # the floored slot inside the local day (15 -> :00/:15/:30/:45).
        mins = int(sched["day_of_month"] or 0)
        if mins <= 0:
            raise ValueError("interval schedule needs interval minutes")
        total = local_now.hour * 60 + local_now.minute
        slot = (total // mins) * mins
        return (f"{local_now.date().isoformat()}T"
                f"{slot // 60:02d}:{slot % 60:02d}")
    else:
        raise ValueError(f"unknown freq {freq!r}")
    return f"{cand.isoformat()}T{hh:02d}:{mm:02d}"


def _clamp_day(year: int, month: int, day: int) -> datetime.date:
    """day_of_month clamped to the month's real last day (31 -> Feb 28/
    29)."""
    last = calendar.monthrange(year, month)[1]
    return datetime.date(year, month, max(1, min(day, last)))


def occurrence_instant(sched, fire_key: str) -> datetime.datetime:
    """The UTC instant a fire_key refers to (for display/tests)."""
    tz = _zone(sched["timezone"])
    d, t = fire_key.split("T")
    y, m, dd = (int(x) for x in d.split("-"))
    hh, mm = parse_time_of_day(t)
    return datetime.datetime(y, m, dd, hh, mm, tzinfo=tz).astimezone(
        datetime.timezone.utc)


# ---------------------------------------------------------------------------

def list_schedules(conn):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM schedules ORDER BY name").fetchall()]


def get_schedule(conn, name: str):
    return conn.execute("SELECT * FROM schedules WHERE name=?",
                        (name,)).fetchone()


def _validate(name, freq, time_of_day, timezone, payload,
              day_of_week, day_of_month, kind):
    if freq not in FREQS:
        raise ValueError(f"freq must be one of {FREQS}")
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")
    if freq == "once":
        # time_of_day holds the full local 'YYYY-MM-DDTHH:MM' fire key
        d, _, t = (time_of_day or "").partition("T")
        if len(d.split("-")) != 3:
            raise ValueError("once needs time_of_day 'YYYY-MM-DDTHH:MM'")
        parse_time_of_day(t)
    else:
        parse_time_of_day(time_of_day)
    _zone(timezone)  # validates
    if freq == "interval":
        if day_of_month is None or not 5 <= int(day_of_month) <= 1440:
            raise ValueError("interval needs an interval of 5-1440 "
                             "minutes (stored in day_of_month)")
    if freq == "weekly" and day_of_week is None:
        raise ValueError("weekly needs day_of_week (0=Mon..6=Sun)")
    if freq == "monthly":
        if day_of_month is None:
            raise ValueError("monthly needs day_of_month (1-31; "
                             "clamped to month end)")
        if not 1 <= int(day_of_month) <= 31:
            raise ValueError("day_of_month must be 1-31")
    if kind == "chat_prompt":
        for k in ("channel_id", "prompt"):
            if not payload.get(k):
                raise ValueError(f"payload needs {k}")
    elif kind == "agent_job":
        # channel_id is optional — the job worker falls back to the
        # owner chat channel / DM when it is empty
        if not payload.get("prompt"):
            raise ValueError("payload needs prompt")
    else:
        for k in ("assistant_name", "title", "body"):
            if not payload.get(k):
                raise ValueError(f"payload needs {k}")


def add_schedule(conn, name, freq, time_of_day, timezone, payload,
                 day_of_week=None, day_of_month=None, enabled=False,
                 kind="create_task"):
    _validate(name, freq, time_of_day, timezone, payload,
              day_of_week, day_of_month, kind)
    now = now_iso()
    cur = conn.execute(
        "INSERT INTO schedules(name, kind, freq, time_of_day, day_of_week,"
        " day_of_month, timezone, enabled, payload_json, created_at,"
        " updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (name, kind, freq, time_of_day, day_of_week, day_of_month,
         timezone, int(bool(enabled)), json.dumps(payload), now, now))
    return cur.lastrowid


def update_schedule(conn, name, freq, time_of_day, timezone, payload,
                    day_of_week=None, day_of_month=None, enabled=None):
    """Edit an existing schedule in place (chat 'edit my job' path).
    `enabled=None` leaves the current flag alone. Returns True when the
    schedule existed."""
    row = get_schedule(conn, name)
    if row is None:
        return False
    _validate(name, freq, time_of_day, timezone, payload,
              day_of_week, day_of_month, row["kind"])
    conn.execute(
        "UPDATE schedules SET freq=?, time_of_day=?, day_of_week=?,"
        " day_of_month=?, timezone=?, payload_json=?, enabled=?,"
        " updated_at=? WHERE name=?",
        (freq, time_of_day, day_of_week, day_of_month, timezone,
         json.dumps(payload),
         row["enabled"] if enabled is None else int(bool(enabled)),
         now_iso(), name))
    return True


def set_enabled(conn, name: str, enabled: bool) -> bool:
    cur = conn.execute(
        "UPDATE schedules SET enabled=?, updated_at=? WHERE name=?",
        (int(bool(enabled)), now_iso(), name))
    return cur.rowcount > 0


def delete_schedule(conn, name: str) -> bool:
    """Remove a schedule entirely (used by /reminders cancel)."""
    cur = conn.execute("DELETE FROM schedules WHERE name=?", (name,))
    return cur.rowcount > 0


def _fire(conn, sched, fire_key: str, status: str, now):
    """Create the run row + the task in one transaction. Caller holds no
    transaction; this function manages its own. Returns (run_inserted,
    task_id, detail)."""
    payload = json_loads_bounded(sched["payload_json"])
    assistant = None
    if sched["kind"] == "create_task":
        assistant = conn.execute(
            "SELECT id FROM assistants WHERE name=?",
            (str(payload.get("assistant_name") or ""),)).fetchone()
    conn.execute("BEGIN IMMEDIATE")
    try:
        cur = conn.execute(
            "INSERT OR IGNORE INTO schedule_runs(schedule_id, fire_key,"
            " status, created_at) VALUES(?,?,?,?)",
            (sched["id"], fire_key, status, now_iso()))
        if cur.rowcount == 0:
            conn.execute("COMMIT")
            return False, None, "already fired"
        if sched["kind"] in ("chat_prompt", "agent_job"):
            # chat_prompt -> reminder text; agent_job -> a full
            # unattended agent turn (the Discord bot drains chat_inbox
            # and routes job:* rows through the unattended policy)
            source = f"schedule:{sched['name']}"
            if sched["kind"] == "agent_job":
                source = f"job:{sched['name']}"
            prompt = str(payload.get("prompt") or "")[:8000]
            conn.execute(
                "INSERT INTO chat_inbox(channel_id, prompt, source,"
                " status, created_at) VALUES(?,?,?,'queued',?)",
                (str(payload.get("channel_id") or ""), prompt, source,
                 now_iso()))
            conn.execute("COMMIT")
            return True, None, ("chat prompt queued" if source.startswith(
                "schedule") else "job queued")
        if assistant is None:
            conn.execute(
                "UPDATE schedule_runs SET status='skipped', detail=?"
                " WHERE schedule_id=? AND fire_key=?",
                (f"no assistant named {payload.get('assistant_name')!r}",
                 sched["id"], fire_key))
            conn.execute("COMMIT")
            return True, None, "assistant missing (skipped)"
        dl_seconds = int(payload.get("deadline_seconds") or 900)
        deadline = (now + datetime.timedelta(seconds=dl_seconds)
                    ).isoformat(timespec="milliseconds")
        cur = conn.execute(
            "INSERT INTO tasks(assistant_id, title, body, private, state,"
            " deadline_at, created_at, updated_at)"
            " VALUES(?,?,?,?,'queued',?,?,?)",
            (assistant["id"], str(payload.get("title") or "scheduled task"
                                            )[:200],
             str(payload.get("body") or "")[:16000],
             int(bool(payload.get("private"))),
             deadline, now_iso(), now_iso()))
        task_id = cur.lastrowid
        conn.execute(
            "UPDATE schedule_runs SET task_id=? WHERE schedule_id=?"
            " AND fire_key=?",
            (task_id, sched["id"], fire_key))
        conn.execute("COMMIT")
        return True, task_id, "task queued"
    except Exception:
        conn.execute("ROLLBACK")
        raise


def tick(conn, settings, now_utc=None) -> list:
    """Fire every enabled schedule whose latest occurrence is due.

    Returns a list of outcome dicts. Safe to call every worker poll."""
    now_utc = now_utc or utcnow()
    out = []
    for sched in conn.execute(
            "SELECT * FROM schedules WHERE enabled=1").fetchall():
        try:
            key = latest_occurrence(sched, now_utc)
        except Exception as e:
            log.warning("schedule %s: %s", sched["name"], e)
            out.append({"schedule": sched["name"], "fired": False,
                        "detail": str(e)})
            continue
        if key is None:
            continue
        fired, task_id, detail = _fire(conn, sched, key, "fired", now_utc)
        if fired:
            log.info("schedule %s fired %s -> %s",
                     sched["name"], key, detail)
            out.append({"schedule": sched["name"], "fired": True,
                        "fire_key": key, "task_id": task_id,
                        "detail": detail})
    return out


def fire_now(conn, name: str):
    """Manual fire (owner CLI). Uses a unique manual fire_key so it can
    never clash with a scheduled occurrence."""
    sched = get_schedule(conn, name)
    if sched is None:
        raise ValueError(f"no schedule named {name!r}")
    # unique per call: each manual fire is a distinct owner intent
    key = "manual-" + now_iso() + "-" + new_id()[:8]
    fired, task_id, detail = _fire(conn, sched, key, "manual", utcnow())
    return {"fired": fired, "task_id": task_id, "detail": detail}
