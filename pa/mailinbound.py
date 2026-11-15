"""A2: mail-inbound — pal's own Gmail as an ingest channel for the
owner (final-5, PA_MAIL_INBOUND).

Seeded DISABLED (migration 44, every 5 min); the owner turns it on
with '/jobs mail-inbound on' plus PA_MAIL_INBOUND=true.

Each tick polls **pal's own** Gmail — the 'gmail@pal' Composio
connection, a native 'google@pal' grant, or GMAIL_* env creds (never
the owner's google account). A message is accepted only when its
sender is on the allowlist (PA_OWNER_EMAIL + optional
PA_MAIL_INBOUND_SENDERS csv).
Everything else is dropped — fail closed, never treated as the owner.

An accepted message is enqueued as an *owner* chat_inbox turn (source
'mail') — the same drain as a Discord DM, replies land in Discord. A
short "got it" ack may go back from pal's account to the sender —
pal account -> owner channel, never to third parties, within the
job's budget.
"""

import logging

from . import connectflows, db as dbmod, gmail as gmailmod
from .inboxwatch import _addr, _norm_composio, _unwrap_messages
from .util import now_iso

log = logging.getLogger("pa.mailinbound")

_SOURCE = "mail"
_MAX_PER_TICK = 10


def _allowlist(bot) -> set:
    out = set()
    for raw in ((bot.s.owner_email or "") + "," +
                (bot.s.mail_inbound_senders or "")).split(","):
        a = raw.strip().lower()
        if a and "@" in a:
            out.add(a)
    return out


def _pal_account(bot, conn):
    """-> ('native', GmailClient) | ('composio', row) | None.

    Only pal's own mailbox counts — the same precedence as
    chattools' mail_to_owner: a 'gmail@pal' Composio connection,
    then a native 'google@pal' grant; GMAIL_* env creds are the
    service-level fallback (documented as pal's own). The owner's
    google account is NEVER polled here."""
    row = connectflows.get(conn, "gmail@pal")
    if row and row.get("status") == "active" \
            and row.get("kind") == "composio":
        return ("composio", row)
    grow = connectflows.get(conn, "google@pal")
    if grow and grow.get("status") == "active" \
            and grow.get("kind") == "google" and grow["token_blob"]:
        from . import secretsbox
        cid, csec = bot.s.google_client(bot.env)
        try:
            blob = secretsbox.open_blob(
                bot.env, f"pa-connection:{grow['provider']}",
                grow["token_blob"])
        except Exception:
            blob = {}
        rtok = blob.get("refresh_token")
        if cid and csec and rtok:
            return ("native", gmailmod.GmailClient(
                bot.s, bot.env, conn=conn,
                creds={"client_id": cid, "client_secret": csec,
                       "refresh_token": rtok}))
    env = bot.env
    if (env.get("GMAIL_CLIENT_ID") and env.get("GMAIL_CLIENT_SECRET")
            and env.get("GMAIL_REFRESH_TOKEN")) \
            or env.get("GMAIL_TOKEN_FILE"):
        try:
            return ("native", gmailmod.GmailClient(
                bot.s, env, conn=None))
        except gmailmod.GmailNotConfigured:
            return None
    return None


def _fetch(bot, conn, acct) -> list:
    kind, obj = acct
    if kind == "composio":
        res = connectflows.call_tool(
            conn, bot.s, bot.env, obj, "GMAIL_FETCH_EMAILS",
            {"query": "in:inbox is:unread", "max_results": _MAX_PER_TICK})
        return [_norm_composio(m) for m in _unwrap_messages(res)]
    return obj.list_messages(
        query="in:inbox is:unread", max_results=_MAX_PER_TICK)


def _seen(conn, mid: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM inbox_seen WHERE message_id=?",
        (mid,)).fetchone() is not None


def _mark(conn, m: dict, outcome: str):
    conn.execute(
        "INSERT OR IGNORE INTO inbox_seen(message_id, classification,"
        " internal_date, subject, frm, alerted, created_at)"
        " VALUES(?,?,?,?,?,0,?)",
        (str(m.get("id") or ""), f"mail-inbound:{outcome}",
         str(m.get("internal_date") or ""),
         (m.get("subject") or "")[:300],
         (m.get("from") or "")[:300], now_iso()))


def _body(bot, conn, acct, m: dict) -> str:
    """Best-effort fuller body; falls back to the snippet. Native
    fetch wraps it as untrusted text — the TURN still treats it as
    owner-authored (the allowlist already decided that), the fence is
    just so pasted instructions inside the body can't elevate."""
    if acct[0] == "native":
        try:
            full = acct[1].get_message(m["id"], meta_only=False)
            if full and full.get("body"):
                return full["body"]
        except Exception:
            pass
    return m.get("snippet") or m.get("body") or ""


def _enqueue_owner_turn(bot, conn, m: dict, body: str) -> None:
    """Insert the 'same drain as a Discord DM' row: channel = owner
    DM, source 'mail' -> _chat_worker. The prompt is owner-authored
    context, not an untrusted-webhook fence."""
    channel = ""
    try:
        from . import sysjobs
        channel = sysjobs.owner_dm(bot)
    except Exception:
        pass
    prompt = (
        "[mail from you — subject: "
        + (m.get("subject") or "(no subject)")[:160] + "]\n\n"
        + (body or "")[:6000]).strip()
    conn.execute(
        "INSERT INTO chat_inbox(channel_id, prompt, source, status,"
        " created_at) VALUES(?,?,'mail','queued',?)",
        (channel, prompt[:8000], now_iso()))


def _ack(bot, conn, acct, to_addr: str, subject: str) -> None:
    """Optional short 'got it' back to the *allowlisted* sender from
    pal's own account — never to anyone else. Failures are silent;
    the Discord reply is the real response."""
    if acct[0] != "native":
        return                       # composio send: out of scope
    try:
        from . import budget
        if not budget.charge(conn, "job:mail-inbound",
                             "composio_calls", 1, s=bot.s, env=bot.env):
            return
        acct[1].send_message(
            to_addr,
            f"re: {(subject or '')[:80]}",
            "got it — replying on Discord.")
    except Exception as e:
        log.info("mail-inbound ack failed: %s", e)


def run(bot, payload) -> str:
    conn = dbmod.connect(bot.db_path)
    try:
        if not getattr(bot.s, "mail_inbound", False):
            return "mail-inbound off"
        return _run(bot, conn)
    finally:
        conn.close()


def _run(bot, conn) -> str:
    senders = _allowlist(bot)
    if not senders:
        return "no allowlist — set PA_OWNER_EMAIL"
    acct = _pal_account(bot, conn)
    if acct is None:
        return ("no pal gmail — connect 'gmail' account:pal or set "
                "GMAIL_* env (docs/FEATURES.md)")
    try:
        msgs = _fetch(bot, conn, acct)
    except gmailmod.GmailError as e:
        return f"gmail poll failed: {e}"
    except Exception as e:
        return f"gmail poll failed: {e}"
    ok = dropped = 0
    for m in msgs[:_MAX_PER_TICK]:
        mid = str(m.get("id") or "")
        frm = _addr(m.get("from") or "")
        if not mid or _seen(conn, mid):
            continue
        if frm not in senders:
            _mark(conn, m, "dropped")
            dropped += 1
            continue
        _mark(conn, m, "accepted")
        _enqueue_owner_turn(bot, conn, m, _body(bot, conn, acct, m))
        _ack(bot, conn, acct, frm, m.get("subject") or "")
        ok += 1
    dbmod.audit(conn, "mail-inbound", "tick",
                f"{ok} accepted, {dropped} dropped")
    return f"{ok} accepted, {dropped} dropped of {len(msgs)}"
