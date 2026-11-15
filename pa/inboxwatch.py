"""inbox-watch — the urgent-mail watcher (docs/JARVIS.md).

Seeded DISABLED; the owner turns it on with '/jobs inbox-watch on'.
Every 15 minutes it:

1. pulls new owner-Gmail messages (native creds preferred; else the
   'gmail' Composio connection) using a persisted cursor + the
   inbox_seen dedupe table — a message never alerts twice;
2. classifies each one: /watch vip/mute rules first, then one cheap
   model call (rules below), heuristic fallback when the model is down;
3. acts:
   - critical: calls the owner (rings even in quiet hours);
   - urgent: calls, except quiet hours -> voice note + DM instead;
   - important: queued into a digest DM (+voice note) at most hourly;
   - ignore: nothing.
   Calls cap out at PA_WATCH_MAX_CALLS_PER_DAY; classifies per run at
   PA_WATCH_MAX_CLASSIFY.

Classification rules (also what the model is asked):
  CRITICAL  — money/security could be lost in hours: fraud alert,
              account breach, failed payment, family emergency.
  URGENT    — a real person needs a reply/action TODAY.
  IMPORTANT — worth a note within hours (real replies, school/work,
              changed appointments).
  IGNORE    — newsletters, promos, receipts, no-reply notifications.
"""

import json
import logging
import re
import time

from . import call as callmod, connectflows, db as dbmod, \
    gmail as gmailmod, sysjobs
from .util import json_loads_bounded, now_iso

log = logging.getLogger("pa.inboxwatch")

_CLASSES = ("critical", "urgent", "important", "ignore")

_CLASSIFY_PROMPT = """You are triaging email for a busy person's assistant.
Reply with EXACTLY ONE WORD — CRITICAL, URGENT, IMPORTANT or IGNORE.

- CRITICAL: money/security could be lost within hours — bank fraud,
  account breach, failed payment, family emergency.
- URGENT: a real person needs a reply or action TODAY.
- IMPORTANT: worth a short heads-up within hours — replies from real
  people, school/work items, changed appointments.
- IGNORE: newsletters, promos, receipts, no-reply notifications.

From: {sender}
Subject: {subject}
Snippet: {snippet}
"""

_NOISE_RE = re.compile(
    r"unsubscribe|newsletter|noreply|no[-_.]?reply|do[-_.]?not[-_.]?reply"
    r"|mailchimp|substack|promo|marketing|notifications?@",
    re.IGNORECASE)


class WatchError(Exception):
    pass


# ---------------------------------------------------------------- fetch

def _unwrap_messages(res) -> list:
    """Find the list of {id,...} dicts inside a Composio tool result —
    the response shape has moved between versions, so search for the
    first list of dicts that all carry an 'id'."""
    def looks(v):
        return (isinstance(v, list) and v and
                all(isinstance(m, dict) and m.get("id") for m in v))

    def walk(v, depth=0):
        if depth > 6:
            return []
        if looks(v):
            return v
        if isinstance(v, dict):
            for child in v.values():
                found = walk(child, depth + 1)
                if found:
                    return found
        return []
    return walk(res)


def _norm_composio(m: dict) -> dict:
    frm = m.get("from") or m.get("sender") or ""
    if isinstance(frm, dict):
        frm = frm.get("email") or frm.get("name") or ""
    labels = m.get("labelIds") or m.get("labels") or []
    return {"id": str(m.get("id") or ""),
            "from": str(frm),
            "subject": str(m.get("subject") or m.get("subjectLine") or ""),
            "snippet": str(m.get("snippet") or m.get("preview") or
                           m.get("messageText") or "")[:400],
            "internal_date": str(m.get("internalDate") or ""),
            "labels": labels}


def _fetch_new(bot, conn) -> tuple:
    """-> (msgs, mode). mode 'primed' means this run only planted the
    cursor (first ever run alerts on nothing)."""
    s, env = bot.s, bot.env
    cursor = dbmod.get_setting(conn, "inbox_watch_cursor", "")
    try:
        cli = gmailmod.GmailClient(s, env, conn=conn)
    except gmailmod.GmailNotConfigured:
        cli = None
    if cli is not None:
        if cursor:
            # gmail 'after:' takes unix seconds; dedupe table guards the
            # boundary either way
            q = f"in:inbox after:{int(int(cursor) / 1000)}"
            return cli.list_messages(query=q, max_results=20), "cursor"
        # first run: remember the newest message, alert on nothing
        msgs = cli.list_messages(query="in:inbox", max_results=1)
        stamp = "0"
        for m in msgs:
            _mark_seen(conn, m, "")
            stamp = max(stamp, str(m.get("internal_date") or "0"))
        dbmod.set_setting(conn, "inbox_watch_cursor", stamp or "1")
        return [], "primed"
    row = connectflows.get(conn, "gmail")
    if row and row.get("status") == "active" \
            and row.get("kind") == "composio":
        res = connectflows.call_tool(
            conn, s, env, row, "GMAIL_FETCH_EMAILS",
            {"query": "in:inbox newer_than:30m", "max_results": 20})
        return [_norm_composio(m) for m in _unwrap_messages(res)], "ids"
    raise WatchError(
        "no Gmail for the owner — connect 'gmail' (Composio) or give "
        "pal its own Gmail creds (see docs/JARVIS.md)")


# ---------------------------------------------------------------- rules

def _addr(from_hdr: str) -> str:
    m = re.search(r"<([^>]+)>", from_hdr or "")
    return (m.group(1) if m else (from_hdr or "")).strip().lower()


def _rule_for(conn, from_hdr: str):
    """/watch vip|mute patterns: 'a@b.c' = exact address; '@b.c' or
    'b.c' = whole domain. Mute wins over vip."""
    addr, dom = _addr(from_hdr), _addr(from_hdr).split("@")[-1]
    rules = conn.execute(
        "SELECT pattern, action FROM watch_rules").fetchall()
    for want in ("mute", "vip"):
        for r in rules:
            if r["action"] != want:
                continue
            p = (r["pattern"] or "").lower()
            if "@" in p and not p.startswith("@"):
                hit = addr == p
            else:
                hit = dom == p.lstrip("@")
            if hit:
                return want
    return None


# ------------------------------------------------------------ classify

def _heuristic(msg: dict) -> str:
    """No-model fallback: newsletters/no-reply -> ignore; everything
    else lands in the hourly 'important' batch. Nothing calls."""
    hay = f"{msg.get('from','')} {msg.get('subject','')} " \
        f"{msg.get('snippet','')}"
    return "ignore" if _NOISE_RE.search(hay) else "important"


def classify(bot, conn, msg: dict) -> str:
    rule = _rule_for(conn, msg.get("from", ""))
    if rule:
        return "urgent" if rule == "vip" else "ignore"
    prompt = _CLASSIFY_PROMPT.format(
        sender=(msg.get("from") or "")[:200],
        subject=(msg.get("subject") or "")[:200],
        snippet=(msg.get("snippet") or "")[:400])
    word = sysjobs.model_text(bot, conn, prompt, budget=20.0,
                              tier="fast").strip().lower()
    word = re.sub(r"[^a-z]", "", word)
    if word in _CLASSES:
        return word
    return _heuristic(msg)


# ------------------------------------------------------------------ act

def _mark_seen(conn, m: dict, cls: str):
    conn.execute(
        "INSERT OR IGNORE INTO inbox_seen(message_id, classification,"
        " internal_date, subject, frm, alerted, created_at)"
        " VALUES(?,?,?,?,?,0,?)",
        (str(m.get("id") or ""), cls,
         str(m.get("internal_date") or ""),
         (m.get("subject") or "")[:300], (m.get("from") or "")[:300],
         now_iso()))


def _calls_today(conn) -> int:
    day = time.strftime("%Y-%m-%d", time.gmtime())
    v = dbmod.get_setting(conn, f"inbox_watch_calls:{day}", "0")
    try:
        return int(v)
    except ValueError:
        return 0


def _bump_calls(conn):
    day = time.strftime("%Y-%m-%d", time.gmtime())
    dbmod.set_setting(conn, f"inbox_watch_calls:{day}",
                      str(_calls_today(conn) + 1))


def _pending(conn) -> list:
    raw = dbmod.get_setting(conn, "inbox_watch_pending", "[]")
    v = json_loads_bounded(raw)
    return v if isinstance(v, list) else []


def _save_pending(conn, items: list):
    dbmod.set_setting(conn, "inbox_watch_pending",
                      json.dumps(items[-30:]))


def _call_owner(bot, conn, cls: str, msg: dict, quiet: bool) -> str:
    """Returns 'called' | 'dm' — never raises."""
    subj = (msg.get("subject") or "(no subject)")[:120]
    frm = (msg.get("from") or "?")[:120]
    spoken = (f"{'Critical' if cls == 'critical' else 'Urgent'} email. "
              f"{subj}. From {frm}.")
    can_call = _calls_today(conn) < bot.s.watch_max_calls_per_day
    if can_call and (not quiet or cls == "critical"):
        res = bot._calls.call(spoken, urgent=(cls == "critical"),
                              source="inbox-watch")
        if res.get("error"):
            log.info("inbox-watch call failed (%s); falling back to DM",
                     res.get("detail") or res.get("error"))
        else:
            _bump_calls(conn)
            return "called"
    tag = "🚨" if cls == "critical" else "📧"
    extra = " (quiet hours — not ringing)" if quiet else \
        " (call cap reached — DM only)" if not can_call else ""
    sysjobs.dm_and_voice(
        bot, f"{tag} **{cls.upper()} email**{extra}\n"
        f"**{subj}**\nfrom {frm}")
    return "dm"


def _flush_batch(bot, conn, force: bool = False) -> int:
    pending = _pending(conn)
    if not pending:
        return 0
    try:
        last = float(dbmod.get_setting(conn, "inbox_watch_last_batch",
                                       "0") or 0)
    except ValueError:
        last = 0.0
    if not force and time.time() - last < bot.s.watch_batch_seconds:
        return 0
    lines = "\n".join(
        f"• {(m.get('subject') or '(no subject)')[:90]} — "
        f"{(m.get('from') or '?')[:60]}" for m in pending)
    sysjobs.dm_and_voice(
        bot, f"📬 **{len(pending)} important email"
        f"{'s' if len(pending) != 1 else ''}**\n{lines}")
    _save_pending(conn, [])
    dbmod.set_setting(conn, "inbox_watch_last_batch", str(time.time()))
    return len(pending)


# ------------------------------------------------------------------ run

def run(bot, payload) -> str:
    conn = dbmod.connect(bot.db_path)
    try:
        return _run(bot, conn)
    finally:
        conn.close()


def _run(bot, conn) -> str:
    msgs, mode = _fetch_new(bot, conn)
    if mode == "primed":
        return "primed the gmail cursor — alerts start next run"

    quiet = callmod.in_quiet_hours(bot.s.quiet_hours, bot.s.timezone)
    counts = {c: 0 for c in _CLASSES}
    actions = []
    classified = 0
    max_stamp = dbmod.get_setting(conn, "inbox_watch_cursor", "0")

    for m in msgs:
        mid = str(m.get("id") or "")
        if not mid:
            continue
        cur = conn.execute(
            "INSERT OR IGNORE INTO inbox_seen(message_id,"
            " classification, internal_date, subject, frm, alerted,"
            " created_at) VALUES(?, '', ?, ?, ?, 0, ?)",
            (mid, str(m.get("internal_date") or ""),
             (m.get("subject") or "")[:300], (m.get("from") or "")[:300],
             now_iso()))
        if cur.rowcount == 0:
            continue                          # already seen/alerted
        if str(m.get("internal_date") or "0") > max_stamp:
            max_stamp = str(m.get("internal_date"))
        if classified >= bot.s.watch_max_classify:
            cls = _heuristic(m)               # cap: never calls
        else:
            cls = classify(bot, conn, m)
            classified += 1
        counts[cls] += 1
        conn.execute(
            "UPDATE inbox_seen SET classification=? WHERE message_id=?",
            (cls, mid))
        # pal-DB triage bucket + local reply draft
        if getattr(bot.s, "triage", False):
            try:
                from . import triage as triagemod
                triagemod.on_new_mail(bot, conn, m, mid)
            except Exception:
                log.exception("triage failed for %s", mid)
        if cls in ("critical", "urgent"):
            how = _call_owner(bot, conn, cls, m, quiet)
            conn.execute(
                "UPDATE inbox_seen SET alerted=1 WHERE message_id=?",
                (mid,))
            actions.append(f"{cls}:{how} "
                           f"{(m.get('subject') or '')[:60]}")
        elif cls == "important":
            p = _pending(conn)
            p.append({"id": mid, "from": m.get("from", ""),
                      "subject": m.get("subject", "")})
            _save_pending(conn, p)
    if mode == "cursor" and max_stamp:
        dbmod.set_setting(conn, "inbox_watch_cursor", max_stamp)
    flushed = _flush_batch(bot, conn)

    parts = [f"{counts[c]} {c}" for c in _CLASSES if counts[c]]
    out = (f"{len(msgs)} fetched, {classified} classified"
           + (": " + ", ".join(parts) if parts else "")
           + (f"; batch sent ({flushed})" if flushed else ""))
    if actions:
        out += " | " + "; ".join(actions[:6])
    return out
