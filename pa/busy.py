"""is the owner busy? — the voice_notes 'auto' trigger.

voice_notes gained a third mode: 'on' speaks every reply, 'off'
never, and 'auto' (the new default when the setting is unset)
attaches a voice note only while the owner plausibly can't read:

  1. their latest message was a voice message / audio attachment —
     reply in kind for ~VOICE_S afterwards (busy_voice_at);
  2. they said so — "i'm busy", "driving", "can't read", "busy for an
     hour" sets busy_until in pa_settings (default 60m, simple
     durations parsed); "not busy" / "i'm free" / "back" clears it;
  3. the calendar (pa.calsource) has an event running right now —
     not all-day, not transparent/free — polled at most every
     CAL_POLL_S with the 'busy until <end>' answer cached between.

All proactive voice (notify, dm_and_voice, reminders, briefing, job
summaries) goes through voice_should_send() so 'off' really means
text-only. Detection is best-effort: it never raises into a chat
turn and fails closed to text.
"""

import datetime
import logging
import re
import time

from . import db as dbmod

log = logging.getLogger("pa.busy")

VOICE_S = 10 * 60           # a recent voice message keeps you "busy"
CAL_POLL_S = 5 * 60         # min seconds between calendar reads
BUSY_DEFAULT_MIN = 60
BUSY_MAX_MIN = 8 * 60

_K_VOICE_AT = "busy_voice_at"
_K_BUSY_UNTIL = "busy_until"
_K_CAL_UNTIL = "busy_cal_until"
_K_CAL_CHECK = "busy_cal_checked"

_BUSY_RE = re.compile(
    r"\bi'?m busy\b|\bi am busy\b"
    r"|\bbusy (?:right now|today|tonight|"
    r"this (?:morning|afternoon|evening))\b"
    r"|\bbusy (?:for|in|til+|until)\b"
    r"|\b(?:i'?m |i am )?driving\b"
    r"|\bcan'?t (?:read|talk|type|text)\b"
    r"|\bcannot (?:read|talk|type|text)\b"
    r"|\bin a (?:meeting|call)\b"
    r"|\bon (?:a|the) (?:call|phone)\b"
    r"|\bhands?[ -]?full\b|\bat the wheel\b", re.I)

_FREE_RE = re.compile(
    r"\b(?:i'?m|i am) (?:not busy|free|back)\b"
    r"|\bnot busy\b|\bfree now\b|\bno longer busy\b|\bdone driving\b"
    r"|\bcan (?:read|talk|type|text)\b"
    r"|\bout of (?:the )?(?:meeting|call)\b|\bback now\b", re.I)

# bare one-word clears the regex shouldn't have to spell out
_FREE_WORDS = {"back", "free", "im back", "i'm back", "im free",
               "i'm free"}

_DUR_RE = re.compile(
    r"(?:for|in|til+|until)\s+(?:the\s+next\s+|next\s+)?"
    r"(an?|one|two|three|four|five|\d+(?:\.\d+)?)\s*"
    r"(hours?|hrs?|hr|h|minutes?|mins?|min|m)\b", re.I)
_HALF_HOUR = re.compile(r"\bhalf an? hour\b", re.I)
_WORD = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3,
         "four": 4, "five": 5}


def _duration_minutes(text: str) -> int:
    """'busy for an hour' -> 60; 'busy for 30 minutes' -> 30.
    Unknown/absent durations get the default hour."""
    if _HALF_HOUR.search(text):
        return 30
    m = _DUR_RE.search(text)
    if not m:
        return BUSY_DEFAULT_MIN
    n = _WORD.get(m.group(1).lower())
    if n is None:
        try:
            n = float(m.group(1))
        except ValueError:
            return BUSY_DEFAULT_MIN
    mins = n * (60 if m.group(2).lower().startswith("h") else 1)
    return max(5, min(int(mins), BUSY_MAX_MIN))


def _ts(conn, key) -> float:
    try:
        return float(dbmod.get_setting(conn, key, "") or 0)
    except (ValueError, TypeError):
        return 0.0


# ---------------------------------------------------------------------------
# triggers 1 + 2 — set by the Discord message route

def note_voice(conn) -> None:
    """The owner's latest message was a voice/audio message."""
    try:
        dbmod.set_setting(conn, _K_VOICE_AT, f"{time.time():.0f}")
    except Exception:
        pass


def note_text(conn, text: str) -> None:
    """Observe one owner-typed message; update the NL busy flag.
    Best-effort — never raises, never blocks a chat turn."""
    t = (text or "").strip().lower()
    if not t:
        return
    try:
        if _FREE_RE.search(t) or t.rstrip(".! ") in _FREE_WORDS:
            dbmod.set_setting(conn, _K_BUSY_UNTIL, "")
            dbmod.set_setting(conn, _K_VOICE_AT, "")
            return
        if _BUSY_RE.search(t):
            until = time.time() + _duration_minutes(t) * 60
            dbmod.set_setting(conn, _K_BUSY_UNTIL, f"{until:.0f}")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# trigger 3 — a calendar event running now

def _parse(dt):
    try:
        return datetime.datetime.fromisoformat(
            str(dt).replace("Z", "+00:00"))
    except ValueError:
        return None


def _running_event_end(evs, now):
    """Latest end among events live at `now` — not all-day, not free."""
    end = None
    for ev in evs:
        if ev.get("all_day") or ev.get("transparent"):
            continue
        s, e = _parse(ev.get("start")), _parse(ev.get("end"))
        if s is None or s.tzinfo is None:
            continue
        e = e or s
        if e.tzinfo is None:
            e = e.replace(tzinfo=s.tzinfo)
        if s <= now <= e and (end is None or e > end):
            end = e
    return end


def _calendar_busy(bot, conn, now) -> bool:
    """A non-all-day opaque event is running right now. Polls the
    calendar source at most every CAL_POLL_S and caches 'busy until
    <end>' in between — no source means not-busy, quietly."""
    if _ts(conn, _K_CAL_UNTIL) > now:
        return True
    checked = _ts(conn, _K_CAL_CHECK)
    if checked and now - checked < CAL_POLL_S:
        return False
    dbmod.set_setting(conn, _K_CAL_CHECK, f"{now:.0f}")
    try:
        from . import calsource
        nowdt = datetime.datetime.now(datetime.timezone.utc)
        evs = calsource.list_events(
            bot.s, bot.env, conn,
            time_min=nowdt - datetime.timedelta(hours=3),
            time_max=nowdt + datetime.timedelta(hours=8),
            max_results=10)
    except Exception as e:
        log.info("busy calendar read failed: %s", e)
        return False
    end = _running_event_end(evs or [], nowdt)
    if end is None:
        return False
    dbmod.set_setting(conn, _K_CAL_UNTIL, f"{end.timestamp():.0f}")
    return True


def is_busy(bot, conn=None) -> bool:
    """Any trigger -> True. Fail-closed to text (False) on errors."""
    own = conn is None
    try:
        if own:
            conn = dbmod.connect(bot.db_path)
        now = time.time()
        if _ts(conn, _K_VOICE_AT) > now - VOICE_S:
            return True
        if _ts(conn, _K_BUSY_UNTIL) > now:
            return True
        return _calendar_busy(bot, conn, now)
    except Exception:
        return False
    finally:
        if own and conn is not None:
            try:
                conn.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# the voice_notes mode + the single send-gate

def mode(bot) -> str:
    """pa_settings voice_notes -> 'on' | 'off' | 'auto' (default).
    Unset falls back to PA_VOICE_NOTES: true -> 'on', else 'auto'."""
    v = ""
    try:
        conn = dbmod.connect(bot.db_path)
        try:
            v = (dbmod.get_setting(conn, "voice_notes", "") or "")
        finally:
            conn.close()
    except Exception:
        pass
    v = v.strip().lower()
    if v in ("on", "off", "auto"):
        return v
    return "on" if getattr(bot.s, "voice_notes_default", False) \
        else "auto"


def voice_should_send(bot) -> bool:
    """The one gate every voice-note send goes through:
    'on' -> always, 'off' -> never, 'auto' -> owner busy only."""
    m = mode(bot)
    if m == "on":
        return True
    if m == "off":
        return False
    return is_busy(bot)
