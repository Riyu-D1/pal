"""one-time, expiring tokens for the HTTPS secret-capture form
plus a short-lived "capture pending" flag for the Discord modal/DM
flow.

Both live in ``pa_settings`` (no schema migration needed): each token
row is ``secform:<token>`` -> {"kind": "password"|"otp", "at": iso};
the pending flag is ``cap_pending`` -> {"kind": ..., "at": iso}.

Tokens are single-use: ``consume`` deletes the row whether or not the
value was still valid, so a replayed POST can never re-seal. The
pending flag just lets the bot recognise "the owner is pasting the
secret right now" for ~10 minutes after they asked for a capture.

Nothing in this module stores or returns a secret value.
"""

import json
import secrets
from datetime import datetime, timedelta, timezone

from . import db as dbmod

TTL_SECONDS = 600                      # ~10 minutes
_PREFIX = "secform:"
_PENDING_KEY = "cap_pending"
KINDS = ("password", "otp")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def _parse(ts: str) -> datetime:
    try:
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S").replace(
            tzinfo=timezone.utc)
    except Exception:
        return datetime.fromtimestamp(0, timezone.utc)


def _read_rec(conn, key: str):
    row = conn.execute(
        "SELECT value FROM pa_settings WHERE key=?", (key,)
    ).fetchone()
    if not row:
        return None
    try:
        rec = json.loads(row["value"])
    except Exception:
        return None
    if not isinstance(rec, dict) or rec.get("kind") not in KINDS:
        return None
    return rec


def _fresh(rec: dict) -> bool:
    return _parse(str(rec.get("at") or "")) > \
        _now() - timedelta(seconds=TTL_SECONDS)


# -- one-time form tokens --------------------------------------------------

def issue(conn, kind: str = "password") -> str:
    """Mint an unguessable single-use token for the /secret/<token>
    form. ``kind`` is 'password' or 'otp' and decides which fields the
    form renders and which vault slot the POST seals into."""
    if kind not in KINDS:
        kind = "password"
    tok = secrets.token_urlsafe(24)
    dbmod.set_setting(
        conn, _PREFIX + tok,
        json.dumps({"kind": kind, "at": _iso(_now())}))
    return tok


def _drop(conn, token: str) -> None:
    conn.execute("DELETE FROM pa_settings WHERE key=?",
                 (_PREFIX + token,))


def peek(conn, token: str):
    """Validate a token without consuming it (GET renders the form).
    Expired rows are deleted; returns the record or None."""
    rec = _read_rec(conn, _PREFIX + str(token or ""))
    if rec is None:
        return None
    if not _fresh(rec):
        _drop(conn, token)
        return None
    return rec


def consume(conn, token: str):
    """Validate and burn a token (POST). The row is deleted even when
    it was stale, so a second POST always fails."""
    rec = _read_rec(conn, _PREFIX + str(token or ""))
    _drop(conn, token)
    if rec is None or not _fresh(rec):
        return None
    return rec


# -- pending-capture flag --------------------------------------------------

def mark_pending(conn, kind: str = "password") -> None:
    """Arm 'the owner is about to send/paste a secret' for TTL seconds.
    Set when /credentials add opens the modal or /credentials form
    issues a link, so a pasted DM can be sealed + deleted instead of
    journaled."""
    if kind not in KINDS:
        kind = "password"
    dbmod.set_setting(conn, _PENDING_KEY,
                      json.dumps({"kind": kind, "at": _iso(_now())}))


def pending(conn):
    """Return the pending kind ('password'/'otp') or None. A stale
    flag is cleared."""
    rec = _read_rec(conn, _PENDING_KEY)
    if rec is None:
        return None
    if not _fresh(rec):
        conn.execute("DELETE FROM pa_settings WHERE key=?",
                     (_PENDING_KEY,))
        return None
    return rec["kind"]


def clear_pending(conn) -> None:
    conn.execute("DELETE FROM pa_settings WHERE key=?",
                 (_PENDING_KEY,))


# -- URL helper --------------------------------------------------------------

def url(settings, token: str) -> str:
    """Public link for the form — only over https (or a loopback dev
    base). Empty string when there is no safe public URL."""
    base = (getattr(settings, "public_url", "") or "").rstrip("/")
    if base.startswith("https://") or base.startswith(
            ("http://127.0.0.1", "http://localhost", "http://[::1]")):
        return f"{base}/secret/{token}"
    return ""
