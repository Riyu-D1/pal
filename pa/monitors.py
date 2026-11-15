"""Standing monitors (docs/PARITY.md #8).

watch_add/watch_list/watch_set chat tools + a `monitor-tick` code-run
system job. A monitor is a small recurring check:

- `web`      — query is a URL; alert when the page's content hash
               changes (bounded guarded_fetch).
- `gmail`    — query is a Gmail search; alert when a NEWER message id
               appears (first tick only records state, no alert).
- `calendar` — "calendar soon": alert once per event starting within
               `query` minutes (default 15).

Alerts are owner DMs. `last_state` holds the previous check's
fingerprint so a monitor alerts only on CHANGE — it can never nag.

The `/watch` command name is taken by inbox rules, so the owner
command is `/monitor` / `/monitors`.
"""

import datetime
import hashlib
import json
import logging
import re

from . import db as dbmod
from .util import now_iso, parse_iso

log = logging.getLogger("pa.monitors")

KINDS = ("web", "gmail", "calendar")


def add_monitor(conn, kind: str, query: str,
                every_minutes: int = 15) -> dict:
    kind = (kind or "").strip().lower()
    if kind not in KINDS:
        return {"error": "bad_kind",
                "message": f"kind must be one of {', '.join(KINDS)}"}
    query = (query or "").strip()[:500]
    if kind in ("web", "gmail") and not query:
        return {"error": "bad_arguments",
                "message": f"{kind} monitors need a query"}
    if kind == "web" and not re.match(r"^https?://", query):
        return {"error": "bad_arguments",
                "message": "web monitor query must be a URL"}
    mins = max(5, min(int(every_minutes or 15), 24 * 60))
    cur = conn.execute(
        "INSERT INTO monitors(kind, query, every_minutes, last_state,"
        " last_run_at, enabled, created_at) VALUES(?,?,?,'',NULL,1,?)",
        (kind, query, mins, now_iso()))
    return {"id": cur.lastrowid, "kind": kind, "query": query,
            "every_minutes": mins}


def list_monitors(conn) -> list:
    return [dict(r) for r in conn.execute(
        "SELECT id, kind, query, every_minutes, enabled, last_run_at"
        " FROM monitors ORDER BY id").fetchall()]


def set_monitor(conn, monitor_id: int, enabled: bool) -> dict:
    cur = conn.execute(
        "UPDATE monitors SET enabled=? WHERE id=?",
        (1 if enabled else 0, int(monitor_id)))
    dbmod.audit(conn, "owner", "monitor_set",
                f"#{monitor_id} enabled={bool(enabled)}")
    return {"id": monitor_id, "updated": cur.rowcount > 0,
            "enabled": bool(enabled)}


def remove_monitor(conn, monitor_id: int) -> dict:
    cur = conn.execute("DELETE FROM monitors WHERE id=?",
                       (int(monitor_id),))
    dbmod.audit(conn, "owner", "monitor_remove", f"#{monitor_id}")
    return {"id": monitor_id, "removed": cur.rowcount > 0}


# -- tick --------------------------------------------------------------------

def _due(conn, now: datetime.datetime) -> list:
    rows = conn.execute(
        "SELECT * FROM monitors WHERE enabled=1").fetchall()
    out = []
    for r in rows:
        if not r["last_run_at"]:
            out.append(r)
            continue
        try:
            last = parse_iso(r["last_run_at"])
        except Exception:
            out.append(r)
            continue
        if (now - last).total_seconds() >= r["every_minutes"] * 60:
            out.append(r)
    return out


def _fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def _check_web(bot, mon):
    from . import egress
    res = egress.guarded_fetch(mon["query"], max_bytes=256_000,
                               timeout=10)
    text = res.get("text") or ""
    fp = _fingerprint(text[:200_000])
    if mon["last_state"] and fp != mon["last_state"]:
        return fp, (f"🔎 web monitor: **{mon['query']}** changed\n"
                    f"_{res.get('title') or 'page'}_")
    return fp, None


def _check_gmail(bot, mon, conn):
    from . import gmail as gmailmod
    cli = gmailmod.GmailClient(bot.s, bot.env, conn=conn)
    msgs = cli.list_messages(query=mon["query"], max_results=1)
    newest = msgs[0]["id"] if msgs else ""
    if mon["last_state"] and newest and newest != mon["last_state"]:
        subj = msgs[0].get("subject") or "(no subject)"
        return newest, (f"🔎 gmail monitor: new mail matching "
                        f"`{mon['query'][:80]}`\n**{_clip(subj)}**")
    return newest or mon["last_state"], None


def _check_calendar(bot, mon, conn):
    from . import calsource
    lead = int(mon["query"] or 15)
    lead = max(5, min(lead, 24 * 60))
    now = datetime.datetime.now(datetime.timezone.utc)
    events = calsource.list_events(
        bot.s, bot.env, conn, days_ahead=1, max_results=25)
    if events is None:
        return mon["last_state"], None
    seen = set(filter(None, (mon["last_state"] or "").split(",")))
    alerts, ids = [], []
    for ev in events:
        if ev.get("all_day"):
            continue
        eid = str(ev.get("id") or "")
        start = ev.get("start") or ""
        try:
            st = parse_iso(str(start))
        except Exception:
            continue
        if st.tzinfo is None:
            continue
        delta = (st - now).total_seconds()
        ids.append(eid)
        if 0 <= delta <= lead * 60 and eid not in seen:
            mins = int(delta // 60)
            alerts.append(f"🔔 **{_clip(ev.get('summary') or 'event')}**"
                          f" starts in {mins}m ({start[:16]})")
    return ",".join(ids[-50:]), ("\n".join(alerts) if alerts else None)


def _clip(s: str) -> str:
    s = str(s or "").replace("\n", " ")
    return s[:150] + ("…" if len(s) > 150 else "")


def run(bot, payload: dict) -> str:
    """monitor-tick sysjob handler: run every DUE monitor once, DM the
    owner on change. First tick just records state — no alert spam."""
    from . import sysjobs
    conn = bot._db()
    alerts, checked = [], 0
    try:
        now = datetime.datetime.now(datetime.timezone.utc)
        dm = sysjobs.owner_dm(bot)
        for mon in _due(conn, now):
            checked += 1
            try:
                if mon["kind"] == "web":
                    state, alert = _check_web(bot, mon)
                elif mon["kind"] == "gmail":
                    state, alert = _check_gmail(bot, mon, conn)
                else:
                    state, alert = _check_calendar(bot, mon, conn)
            except Exception as e:
                log.info("monitor #%s failed: %s", mon["id"], e)
                state, alert = mon["last_state"], None
            conn.execute(
                "UPDATE monitors SET last_state=?, last_run_at=?"
                " WHERE id=?",
                (state or "", now_iso(), mon["id"]))
            if alert and dm:
                try:
                    bot.api.post_message(dm, alert[:1900])
                    alerts.append(mon["id"])
                except Exception as e:
                    log.info("monitor alert DM failed: %s", e)
        return f"checked {checked}, alerted {len(alerts)}"
    finally:
        conn.close()
