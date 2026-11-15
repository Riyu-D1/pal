"""Credential vault + browse_fill_secret (docs/PARITY.md #14).

Login credentials live in the `credentials` table, each sealed with
the secretsbox AES-GCM key bound to aad 'cred:<domain>'. Listing
shows domains + labels only — values are NEVER rendered anywhere,
not in chat, not in /credentials, not in logs or audits.

`browse_fill_secret` is the ONLY read path: it domain-checks the
current browser tab against the stored credential's domain (suffix
match, so a phishing look-alike can't get it), then types the
username and password straight into the page via the driver's own
`type` action — bypassing browse_type's password-field refusal,
which exists precisely so ONLY this audited path can fill secrets.
The tool is always approval-gated and is excluded from allow rules.
"""

import re
import time

from . import secretsbox
from .util import now_iso

OTP_PREFIX = "otp:"                    # domain key for one-shot codes
OTP_TTL = 600                          # seconds a stored OTP stays live


def _aad(domain: str) -> str:
    return f"cred:{domain}"


def _norm(domain: str) -> str:
    d = (domain or "").strip().lower()
    d = re.sub(r"^https?://", "", d).split("/")[0]
    return d[:120]


def add(conn, env, domain: str, username: str, password: str,
        label: str = "") -> dict:
    d = _norm(domain)
    if not d or not username or not password:
        return {"error": "bad_arguments",
                "message": "need domain, username and password"}
    blob = secretsbox.seal(env, _aad(d),
                           {"u": username[:200], "p": password})
    conn.execute(
        "INSERT INTO credentials(domain, label, blob, created_at,"
        " updated_at) VALUES(?,?,?,?,?)"
        " ON CONFLICT(domain) DO UPDATE SET label=excluded.label,"
        " blob=excluded.blob, updated_at=excluded.updated_at",
        (d, (label or "")[:80], blob, now_iso(), now_iso()))
    from . import db as dbmod
    dbmod.audit(conn, "owner", "credential_add", d)  # domain only
    return {"saved": True, "domain": d}


def add_otp(conn, env, domain: str, code: str,
            ttl: int = OTP_TTL) -> dict:
    """Seal a one-time pass/2FA code under domain 'otp:<domain>' with
    an expiry baked into the blob. Read exactly once via read_otp
    (browse_fill_secret consumes it when a code field is on the page).
    """
    d = _norm(domain)
    code = (code or "").strip()
    if not d or not code:
        return {"error": "bad_arguments",
                "message": "need domain and code"}
    blob = secretsbox.seal(env, _aad(OTP_PREFIX + d),
                           {"c": code[:64],
                            "exp": int(time.time()) + int(ttl)})
    conn.execute(
        "INSERT INTO credentials(domain, label, blob, created_at,"
        " updated_at) VALUES(?,?,?,?,?)"
        " ON CONFLICT(domain) DO UPDATE SET label=excluded.label,"
        " blob=excluded.blob, updated_at=excluded.updated_at",
        (OTP_PREFIX + d, "one-time code", blob,
         now_iso(), now_iso()))
    from . import db as dbmod
    dbmod.audit(conn, "owner", "otp_add", d)  # domain only
    return {"saved": True, "domain": d}


def read_otp(conn, env, domain: str):
    """One-shot read of a stored code for `domain` (suffix-matched on
    the part after 'otp:'). The row is deleted on every call — read or
    expired — so a code can never be replayed. Returns the code or
    None."""
    d = _norm(domain)
    if not d:
        return None
    for r in conn.execute(
            "SELECT domain, blob FROM credentials").fetchall():
        stored = r["domain"]
        if not stored.startswith(OTP_PREFIX):
            continue
        base = stored[len(OTP_PREFIX):]
        if d != base and not d.endswith("." + base):
            continue
        conn.execute("DELETE FROM credentials WHERE domain=?",
                     (stored,))
        try:
            data = secretsbox.open_blob(env, _aad(stored), r["blob"])
        except Exception:
            return None
        if int(data.get("exp") or 0) < int(time.time()):
            return None                    # expired — dropped anyway
        return data.get("c")
    return None


def list_credentials(conn) -> list:
    return [dict(r) for r in conn.execute(
        "SELECT domain, label, updated_at FROM credentials"
        " ORDER BY domain").fetchall()]


def remove(conn, domain: str) -> bool:
    cur = conn.execute("DELETE FROM credentials WHERE domain=?",
                       (_norm(domain),))
    return cur.rowcount > 0


def _lookup(conn, env, host: str):
    """Credential whose stored domain suffix-matches the tab host.
    'example.com' covers 'app.example.com'; 'evil-example.com'
    does NOT match."""
    host = (host or "").lower()
    for r in conn.execute(
            "SELECT domain, blob FROM credentials").fetchall():
        d = r["domain"]
        if d.startswith(OTP_PREFIX):
            continue                     # OTP slots are read_otp-only
        if host == d or host.endswith("." + d):
            try:
                data = secretsbox.open_blob(env, _aad(d), r["blob"])
            except Exception:
                continue
            return {"domain": d, "username": data["u"],
                    "password": data["p"]}
    return None


_REF_RE = re.compile(r"^@(\w+)\s+(\S+)(.*)$")


def _find_fields(nodes: list):
    """(username_ref, password_ref) from snapshot lines."""
    user = pw = None
    for line in nodes:
        m = _REF_RE.match(line.strip())
        if not m or m.group(2) != "input":
            continue
        rest = m.group(3)
        if "type=password" in rest:
            pw = pw or "@" + m.group(1)
        elif user is None and (
                "type=email" in rest or "type=text" in rest
                or re.search(r'"[^"]*(user|login|email)[^"]*"',
                             rest, re.I)):
            user = "@" + m.group(1)
    return user, pw


_OTP_FIELD_RE = re.compile(
    r'"[^"]*(otp|one.?time|2fa|mfa|totp|verification|auth\w*\s*code|'
    r'security\s*code|passcode)[^"]*"', re.I)


def _find_otp_field(nodes: list, used: set):
    """Best-effort 'enter your code' input: a text-ish input whose
    label/name/placeholder mentions OTP-ish words, not already used."""
    for line in nodes:
        m = _REF_RE.match(line.strip())
        if not m or m.group(2) != "input":
            continue
        if "type=password" in m.group(3):
            continue
        ref = "@" + m.group(1)
        if ref in used:
            continue
        if _OTP_FIELD_RE.search(m.group(3)):
            return ref
    return None


def fill_secret(ctx, domain: str) -> dict:
    """Approval has ALREADY been granted by the caller (the tool is
    gated). Fills the matching credential into the current tab."""
    svc = ctx.computer
    if svc is None:
        from . import computer as computermod
        svc = computermod.service_for(ctx.s, ctx.env)
        ctx.computer = svc
    snap = svc.call("snapshot")
    if snap.get("error"):
        return snap
    url = snap.get("url") or ""
    host = re.sub(r"^https?://", "", url).split("/")[0].lower()
    cred = _lookup(ctx.conn, ctx.env, host if not domain else
                   _norm(domain))
    if cred is None:
        return {"error": "no_credential",
                "message": f"no saved credential for {host or domain!r}"}
    # the page must actually belong to the stored domain
    if host and host != cred["domain"] \
            and not host.endswith("." + cred["domain"]):
        return {"error": "domain_mismatch",
                "message": f"current tab is {host!r}, not "
                           f"{cred['domain']!r} — refusing to fill"}
    user_ref, pw_ref = _find_fields(snap.get("nodes") or [])
    if not pw_ref:
        return {"error": "no_password_field",
                "message": "no password field on this page"}
    steps = []
    if user_ref:
        svc.call("type", {"target": user_ref,
                          "text": cred["username"]})
        steps.append("username")
    svc.call("type", {"target": pw_ref, "text": cred["password"]})
    steps.append("password")
    # A stored one-time code is consumed here when the page shows a
    # code field — read_otp deletes the row either way.
    otp_ref = _find_otp_field(snap.get("nodes") or [],
                              {user_ref, pw_ref})
    if otp_ref:
        code = read_otp(ctx.conn, ctx.env, cred["domain"])
        if code:
            svc.call("type", {"target": otp_ref, "text": code})
            steps.append("otp")
    from . import db as dbmod
    dbmod.audit(ctx.conn, "owner", "browse_fill_secret",
                cred["domain"])
    return {"filled": True, "domain": cred["domain"],
            "fields": steps,
            "note": "credentials typed — nothing is echoed back; "
                    "submit is a separate gated step"}
