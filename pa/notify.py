"""final-2 N1: one notifier (PA_NOTIFY_V2).

notify() is the single place every outbound DM should pass through:
- urgent | high go out immediately (urgent callers still use the
  existing call path themselves — this layer never dials);
- normal | low during quiet hours hold into a morning batch that
  flushes on the first post-quiet notification;
- the same dedupe_key inside DEDUPE_SECONDS is dropped;
- more than PA_NOTIFY_PER_HOUR sends an hour hold the rest.

State lives in pa_settings (bounded) — no migration left in this
run, and none needed.
"""

import datetime
import hashlib
import logging
import time

from . import db as dbmod

log = logging.getLogger("pa.notify")

DEDUPE_SECONDS = 6 * 3600
_HELD_MAX = 30


def _quiet(bot) -> bool:
    try:
        from . import call as callmod
        return callmod.in_quiet_hours(
            getattr(bot.s, "quiet_hours", ""), bot.s.timezone)
    except Exception:
        return False


def _deduped(conn, key: str) -> bool:
    if not key:
        return False
    try:
        seen = float(dbmod.get_setting(conn, f"notify_d:{key}", "0")
                     or 0)
        return time.time() - seen < DEDUPE_SECONDS
    except ValueError:
        return False


def _mark_dedupe(conn, key: str):
    if key:
        dbmod.set_setting(conn, f"notify_d:{key}",
                          str(time.time()))


def _hour_count(conn) -> int:
    stamp = time.strftime("%Y%m%d%H", time.gmtime())
    try:
        return int(dbmod.get_setting(conn, f"notify_h:{stamp}", "0")
                   or 0)
    except ValueError:
        return 0


def _bump_hour(conn):
    stamp = time.strftime("%Y%m%d%H", time.gmtime())
    dbmod.set_setting(conn, f"notify_h:{stamp}",
                      str(_hour_count(conn) + 1))


def _held(conn) -> list:
    import json
    try:
        return json.loads(dbmod.get_setting(
            conn, "notify_held", "[]") or "[]")
    except Exception:
        return []


def _hold(conn, kind, text):
    held = _held(conn)
    held.append({"kind": kind, "text": text,
                 "at": datetime.datetime.now(
                     datetime.timezone.utc).isoformat()[:16]})
    dbmod.set_setting(conn, "notify_held",
                      __import__("json").dumps(held[-_HELD_MAX:]))


def _cap(bot) -> int:
    try:
        return max(1, int(getattr(bot.s, "notify_per_hour", 6) or 6))
    except (TypeError, ValueError):
        return 6


def _send_now(bot, text):
    from . import sysjobs
    dm = sysjobs.owner_dm(bot)
    if not dm:
        return ""
    bot.api.post_message(dm, text[:1900])
    if bot._voice is not None:
        try:
            # final-6: proactive voice obeys the same rule as replies —
            # voice_notes 'on' always, 'off' never, 'auto' when busy
            from . import busy as busymod
            if busymod.voice_should_send(bot):
                bot._voice.send_voice_note(dm, text)
        except Exception:
            pass
    return dm


def _phone_ping(bot, conn, priority: str, text: str) -> None:
    """A2 outbound: PA_NOTIFY_PHONE + a healthy phone node -> the same
    alert also reaches the phone's `notify` cap (and `vibrate` for
    urgent). Fire-and-forget — failures are logged, never block the
    Discord path. Held/deduped items never reach here, so quiet hours
    still mean quiet."""
    if not getattr(bot.s, "notify_phone", False):
        return
    try:
        from . import nodes as nodesmod
        out = nodesmod.submit_job(
            conn, bot.s, "notify", {"text": text[:500]},
            scope="owner-chat", unattended=True,
            tool_name="notify.phone", env=bot.env)
        if out.get("error"):
            log.info("phone notify skipped: %s",
                     out.get("reason") or out.get("error"))
        elif priority == "urgent":
            # 'vibrate' is sensitive+owner — it lands only when the
            # owner already has a standing allow rule; otherwise the
            # job stays refused, quietly.
            v = nodesmod.submit_job(
                conn, bot.s, "vibrate", {"pattern": "long"},
                scope="owner-chat", unattended=True,
                tool_name="notify.phone", env=bot.env)
            if v.get("error"):
                log.info("phone vibrate skipped: %s",
                         v.get("reason") or v.get("error"))
    except Exception as e:
        log.info("phone notify failed: %s", e)


def flush(conn, bot) -> int:
    """Deliver the held batch as one DM; returns count sent."""
    held = _held(conn)
    if not held:
        return 0
    dbmod.set_setting(conn, "notify_held", "[]")
    lines = [f"• {h['text'][:180]}" for h in held]
    _send_now(bot, f"📦 **{len(held)} held update"
                   f"{'s' if len(held) != 1 else ''}** "
                   "(from quiet hours)\n" + "\n".join(lines))
    return len(held)


def notify(bot, conn, kind: str, priority: str, text: str,
           dedupe_key: str = "") -> str:
    """-> 'sent' | 'held' | 'deduped' | 'no_dm'."""
    own = conn is None
    conn = conn or dbmod.connect(bot.db_path)
    try:
        if not dedupe_key:
            dedupe_key = hashlib.sha256(
                f"{kind}|{text[:200]}".encode()).hexdigest()[:16]
        if _deduped(conn, dedupe_key):
            return "deduped"
        # first notification after quiet hours flushes the batch
        quiet = _quiet(bot)
        if not quiet:
            flush(conn, bot)
        if priority == "urgent":
            _mark_dedupe(conn, dedupe_key)
            res = "sent" if _send_now(bot, text) else "no_dm"
            _phone_ping(bot, conn, priority, text)
            return res
        if quiet or _hour_count(conn) >= _cap(bot):
            _hold(conn, kind, text)
            return "held"
        _mark_dedupe(conn, dedupe_key)
        _bump_hour(conn)
        res = "sent" if _send_now(bot, text) else "no_dm"
        if priority == "high":
            _phone_ping(bot, conn, priority, text)
        return res
    finally:
        if own:
            conn.close()
