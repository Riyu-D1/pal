"""Gmail adapter (official Gmail API + OAuth).

- Scopes requested by `pa gmail-auth`: gmail.readonly + gmail.compose +
  gmail.send + calendar.events. Read tools only need readonly; drafts
  and sends use the extra scopes. Sends are always behind an owner
  approval at the tool layer — this module just provides the mechanism.
- Credentials come from the environment:
      GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET, GMAIL_REFRESH_TOKEN
  or a JSON file GMAIL_TOKEN_FILE containing at least "refresh_token"
  (client_id/client_secret may live there too). The file MUST be mode
  0600 on a local disk — anything looser is refused, and it is never
  logged, printed or committed.
- `pa gmail-auth` runs the one-time consent flow on whatever machine the
  owner chooses and prints the env values to set on the host.
- Missing config is a first-class state: tools and jobs report
  "email_not_configured" with onboarding hints — never fake success.
- Mail content is UNTRUSTED: bodies are wrapped in explicit markers and
  the agent policy says never to obey instructions inside them.
"""

import base64
import datetime
import json
import os
import stat
import urllib.parse

from .config import GMAIL_AUTH_SCOPES, GMAIL_SCOPE
from .htmltext import html_to_text
from .netclient import APIClient, HTTPError, TransportError
from .util import json_loads_bounded, truncate_middle

MAX_LIST = 25
MAX_BODY_CHARS = 20000

_UNTRUSTED_OPEN = "<<<UNTRUSTED EMAIL {subject} >>>"
_UNTRUSTED_CLOSE = "<<<END UNTRUSTED EMAIL>>>"


class GmailNotConfigured(Exception):
    kind = "email_not_configured"


class GmailError(Exception):
    kind = "email_error"


def _read_token_file(path: str) -> dict:
    st = os.stat(path)
    if not stat.S_ISREG(st.st_mode):
        raise GmailNotConfigured("GMAIL_TOKEN_FILE is not a regular file")
    if st.st_mode & 0o077:
        raise GmailNotConfigured(
            "GMAIL_TOKEN_FILE permissions are too open; chmod 600 it")
    try:
        with open(path, "rb") as f:
            data = json.loads(f.read(64 * 1024).decode("utf-8"))
    except (OSError, ValueError):
        raise GmailNotConfigured("GMAIL_TOKEN_FILE is not valid JSON")
    if not isinstance(data, dict):
        raise GmailNotConfigured("GMAIL_TOKEN_FILE must be a JSON object")
    return data


def credentials(env, conn=None) -> dict:
    """{client_id, client_secret, refresh_token} from env, token file,
    or the 'google' connections row (`/connect google`). Missing pieces
    -> GmailNotConfigured with onboarding guidance."""
    data = {}
    tfile = (env.get("GMAIL_TOKEN_FILE") or "").strip()
    if tfile:
        data = _read_token_file(tfile)
    cid = (env.get("GMAIL_CLIENT_ID") or data.get("client_id") or "")
    csec = (env.get("GMAIL_CLIENT_SECRET") or data.get("client_secret")
            or "")
    rtok = (env.get("GMAIL_REFRESH_TOKEN") or data.get("refresh_token")
            or "")
    if cid and csec and rtok:
        return {"client_id": cid, "client_secret": csec,
                "refresh_token": rtok}
    if conn is not None:
        creds = _db_credentials(env, conn)
        if creds:
            return creds
    missing = [n for n, v in (("GMAIL_CLIENT_ID", cid),
                             ("GMAIL_CLIENT_SECRET", csec),
                             ("GMAIL_REFRESH_TOKEN", rtok)) if not v]
    raise GmailNotConfigured(
        "Gmail is not configured. Missing: " + ", ".join(missing) +
        ". Run `python -m pa gmail-auth` on any machine with a "
        "browser, or connect Google in Discord with /connect google.")


def _db_credentials(env, conn):
    """'/connect google' grant: client material from PA_GOOGLE_*/
    GMAIL_* env; refresh token sealed in the connections row."""
    from . import connectflows, secretsbox
    row = connectflows.get(conn, "google")
    if not row or row["status"] != "active" or not row["token_blob"]:
        return None
    try:
        blob = secretsbox.open_blob(env, "pa-connection:google",
                                    row["token_blob"])
    except Exception:
        return None
    cid = (env.get("PA_GOOGLE_CLIENT_ID") or
           env.get("GMAIL_CLIENT_ID") or "").strip()
    csec = (env.get("PA_GOOGLE_CLIENT_SECRET") or
            env.get("GMAIL_CLIENT_SECRET") or "").strip()
    rtok = blob.get("refresh_token")
    if not (cid and csec and rtok):
        return None
    return {"client_id": cid, "client_secret": csec,
            "refresh_token": rtok}


def configured(env, conn=None) -> bool:
    try:
        credentials(env, conn=conn)
        return True
    except GmailNotConfigured:
        return False


class GmailClient:
    """Minimal read-only client. One instance per call site is fine —
    the access token is cached for its lifetime only."""

    def __init__(self, settings, env=None, conn=None, creds=None):
        env = os.environ if env is None else env
        self.s = settings
        # `creds` bypasses env/db lookup — used to send as a labelled
        # account (e.g. 'google@pal', pal's own mailbox).
        self.creds = creds or credentials(env, conn=conn)  # raises N.C.
        self.api = APIClient(settings.gmail_api_base, timeout=20)
        self.token_endpoint = APIClient(settings.gmail_token_url,
                                        timeout=20)
        self._access_token = None
        self._token_expiry = None

    # -- oauth -----------------------------------------------------------
    def access_token(self) -> str:
        now = datetime.datetime.now(datetime.timezone.utc)
        if self._access_token and self._token_expiry and \
                now < self._token_expiry:
            return self._access_token
        body = {
            "client_id": self.creds["client_id"],
            "client_secret": self.creds["client_secret"],
            "refresh_token": self.creds["refresh_token"],
            "grant_type": "refresh_token",
        }
        try:
            _h, data = self.token_endpoint.post_form("", body)
        except HTTPError as e:
            if e.status in (400, 401, 403):
                raise GmailError(
                    "Gmail token refresh rejected (revoked grant? re-run "
                    "`python -m pa gmail-auth`)")
            raise GmailError(f"token endpoint HTTP {e.status}")
        except TransportError as e:
            raise GmailError(f"token endpoint unreachable: {e}")
        tok = data.get("access_token")
        if not tok:
            raise GmailError("token endpoint returned no access_token")
        self._access_token = tok
        self._token_expiry = now + datetime.timedelta(
            seconds=int(data.get("expires_in") or 3600) - 60)
        return tok

    def _get(self, path):
        try:
            return self.api.get_json(
                path, headers={
                    "Authorization": f"Bearer {self.access_token()}"})
        except HTTPError as e:
            if e.status in (401, 403):
                raise GmailError(f"gmail API auth failed (HTTP {e.status})")
            if e.status == 429:
                raise GmailError("gmail API rate limited (HTTP 429)")
            raise GmailError(f"gmail API HTTP {e.status}")
        except TransportError as e:
            raise GmailError(f"gmail API unreachable: {e}")

    def _post(self, path, obj):
        try:
            _h, data = self.api.post_json(
                path, obj, headers={
                    "Authorization": f"Bearer {self.access_token()}"})
            return data or {}
        except HTTPError as e:
            if e.status in (401, 403):
                raise GmailError(
                    f"gmail API auth failed (HTTP {e.status}) — re-run "
                    "`python -m pa gmail-auth` to grant send/compose "
                    "scopes")
            if e.status == 429:
                raise GmailError("gmail API rate limited (HTTP 429)")
            raise GmailError(f"gmail API HTTP {e.status}")
        except TransportError as e:
            raise GmailError(f"gmail API unreachable: {e}")

    # -- read-only endpoints ----------------------------------------------
    def list_messages(self, query: str = "", max_results: int = 10):
        """List messages + metadata. Returns [{id,subject,from,date,
        snippet}]. query uses Gmail search syntax, e.g. 'is:unread
        newer_than:1d'."""
        lim = max(1, min(int(max_results), MAX_LIST))
        q = urllib.parse.urlencode({
            "q": (query or "")[:500], "maxResults": lim})
        _h, data = self._get(f"/gmail/v1/users/me/messages?{q}")
        out = []
        for m in (data.get("messages") or [])[:lim]:
            meta = self.get_message(m["id"], meta_only=True)
            if meta:
                out.append(meta)
        return out

    def get_message(self, msg_id: str, meta_only: bool = False):
        """One message. meta_only -> headers+snippet; else body text too
        (wrapped as untrusted)."""
        if not msg_id or len(msg_id) > 64:
            return None
        fmt = "metadata" if meta_only else "full"
        path = (f"/gmail/v1/users/me/messages/{msg_id}?format={fmt}"
                "&metadataHeaders=Subject&metadataHeaders=From"
                "&metadataHeaders=Date")
        _h, data = self._get(path)
        headers = {}
        for h in ((data.get("payload") or {}).get("headers") or []):
            headers[h.get("name", "").lower()] = h.get("value") or ""
        msg = {
            "id": data.get("id"),
            "subject": (headers.get("subject") or "")[:300],
            "from": (headers.get("from") or "")[:300],
            "date": headers.get("date") or "",
            "internal_date": str(data.get("internalDate") or ""),
            "snippet": (data.get("snippet") or "")[:500],
            "labels": data.get("labelIds") or [],
        }
        if not meta_only:
            msg["body"] = _wrap(
                msg["subject"],
                truncate_middle(_body_text(data.get("payload") or {}),
                                MAX_BODY_CHARS))
        return msg

    # -- write endpoints (approval-gated at the tool layer) ---------------
    def create_draft(self, to: str, subject: str, body: str):
        """Create a Gmail draft (gmail.compose). Never sends."""
        data = self._post("/gmail/v1/users/me/drafts", {
            "message": {"raw": _mime_raw(to, subject, body)}})
        msg = data.get("message") or {}
        return {"draft_id": data.get("id"),
                "message_id": msg.get("id"), "to": to,
                "subject": subject}

    def send_message(self, to: str, subject: str, body: str):
        """Send a message now (gmail.send). Approval-gated upstream."""
        data = self._post("/gmail/v1/users/me/messages/send", {
            "raw": _mime_raw(to, subject, body)})
        return {"id": data.get("id"), "threadId": data.get("threadId"),
                "to": to, "subject": subject}


def _decode_part(part) -> str:
    data = ((part.get("body") or {}).get("data")) or ""
    try:
        return base64.urlsafe_b64decode(data + "==").decode(
            "utf-8", "replace")
    except Exception:
        return ""


def _body_text(payload) -> str:
    """Prefer text/plain; fall back to text/html converted to text."""
    mime = (payload.get("mimeType") or "").lower()
    if mime == "text/plain":
        return _decode_part(payload)
    html_parts = []
    for part in payload.get("parts") or []:
        pm = (part.get("mimeType") or "").lower()
        if pm == "text/plain":
            return _decode_part(part)
        if pm == "text/html":
            html_parts.append(_decode_part(part))
        elif pm.startswith("multipart/"):
            t = _body_text(part)
            if t:
                return t
    if mime == "text/html":
        html_parts.append(_decode_part(payload))
    if html_parts:
        return html_to_text(html_parts[0])
    return ""


def _wrap(subject: str, body: str) -> str:
    return (f"{_UNTRUSTED_OPEN.format(subject=subject[:200])}\n{body}\n"
            f"{_UNTRUSTED_CLOSE}")


def _mime_raw(to: str, subject: str, body: str) -> str:
    """Minimal RFC822 message -> base64url for the Gmail API."""
    def clean(v, n):
        return str(v or "").replace("\r", " ").replace("\n", " ")[:n]
    raw = (
        f"To: {clean(to, 500)}\r\n"
        f"Subject: {clean(subject, 300)}\r\n"
        "Content-Type: text/plain; charset=\"UTF-8\"\r\n"
        "\r\n" + str(body or "")[:100_000]).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


# -- one-time owner onboarding flow ----------------------------------------

def consent_url(client_id: str, redirect_uri: str,
                scopes=GMAIL_AUTH_SCOPES) -> str:
    qs = urllib.parse.urlencode({
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes),
        "access_type": "offline",
        "prompt": "consent",
    })
    return "https://accounts.google.com/o/oauth2/v2/auth?" + qs


def exchange_code(settings, client_id: str, client_secret: str,
                  code: str, redirect_uri: str) -> dict:
    client = APIClient(settings.gmail_token_url, timeout=20)
    try:
        _h, data = client.post_form("", {
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        })
    except (HTTPError, TransportError) as e:
        raise GmailError(f"code exchange failed: {e}")
    if not data.get("refresh_token"):
        raise GmailError(
            "no refresh_token returned (was access_type=offline honored? "
            "try revoking the app at myaccount.google.com/permissions "
            "and re-running)")
    return data


def run_auth_flow(settings, client_id: str, client_secret: str,
                  open_browser=True, timeout=180) -> dict:
    """Loopback installed-app consent flow. Prints the URL; the owner
    signs in; we catch the redirect on 127.0.0.1 and exchange the code."""
    import http.server
    import threading
    import webbrowser

    captured = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            qs = urllib.parse.parse_qs(
                urllib.parse.urlsplit(self.path).query)
            if "code" in qs:
                captured["code"] = qs["code"][0]
                body = (b"<h1>Done</h1><p>You can close this tab and "
                        b"return to the terminal.</p>")
            else:
                captured["error"] = qs.get("error", ["no code"])[0]
                body = b"<h1>Failed</h1><p>No code returned.</p>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    redirect_uri = f"http://127.0.0.1:{port}"
    url = consent_url(client_id, redirect_uri)
    print("Open this URL in your browser and approve the requested "
          "scopes (gmail read/draft/send + calendar):\n")
    print(f"  {url}\n")
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    print("Waiting for the consent redirect on 127.0.0.1 ...")
    srv.timeout = timeout
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    end = datetime.datetime.now() + datetime.timedelta(seconds=timeout)
    while "code" not in captured and "error" not in captured:
        if datetime.datetime.now() > end:
            srv.shutdown()
            raise GmailError("timed out waiting for the consent redirect")
        t.join(0.25)
    srv.shutdown()
    if "error" in captured:
        raise GmailError(f"consent failed: {captured['error']}")
    data = exchange_code(settings, client_id, client_secret,
                         captured["code"], redirect_uri)
    return data
