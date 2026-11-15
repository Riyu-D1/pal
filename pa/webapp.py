"""WSGI web app: auth, CSRF, routing, HTML UI and a small JSON API.

Security surface:
- every path requires a session except /healthz and /login;
- POSTs need a valid CSRF token AND a matching Origin header when the
  browser sends one;
- sessions: DB-backed, rotated at login, idle+absolute expiry;
- cookie: production "pa_session" (Secure;HttpOnly;SameSite=Strict,
  host-only). Development uses a DIFFERENT name "pa_session_dev" with
  Secure omitted - and production can never take the relaxed path;
- login throttled per account (durable), per client IP, and globally so
  forged client-IP headers cannot buy extra attempts;
- request bodies capped; headers already bounded by waitress.
"""

import datetime
import html
import logging
import json
import os
import re
import threading
import time
import urllib.parse

from . import approvals, db as dbmod, memory as memmod, worker as workmod
from .passwords import verify_password
from .tools import TOOL_DEFS
from .util import (json_dumps, json_loads_bounded, new_id, now_iso,
                   parse_iso, utcnow)

SESSION_COOKIE_PROD = "pa_session"
SESSION_COOKIE_DEV = "pa_session_dev"
MAX_FIELD = 8000

ERROR_TEXT = {
    "quota_exceeded": "The model quota is exhausted.",
    "provider_auth": "The model provider rejected our credentials.",
    "provider_not_configured": "No inference endpoint is configured "
                               "(LLM_BASE_URL/LLM_MODEL).",
    "provider_error": "The model provider failed.",
    "search_disabled": "Web search is disabled (no key / not opted in).",
    "fetch_blocked": "A fetch was blocked by the egress policy.",
    "deadline": "The task ran out of time.",
    "cancelled": "The task was cancelled.",
    "over_budget": "The task needed more tokens than the per-request cap.",
    "internal": "Something went wrong on our side.",
}


def esc(s) -> str:
    return html.escape(str(s if s is not None else ""), quote=True)


class Request:
    def __init__(self, environ, settings):
        self.environ = environ
        self.method = environ.get("REQUEST_METHOD", "GET").upper()
        self.path = environ.get("PATH_INFO", "/") or "/"
        self.query = urllib.parse.parse_qs(
            environ.get("QUERY_STRING", ""), keep_blank_values=True)
        self.settings = settings
        self._body = None
        self.session = None
        self.user = None

    def body(self) -> bytes:
        if self._body is None:
            try:
                n = int(self.environ.get("CONTENT_LENGTH") or 0)
            except ValueError:
                n = 0
            if n > self.settings.max_body_bytes:
                raise BodyTooLarge()
            stream = self.environ["wsgi.input"]
            self._body = stream.read(n) if n > 0 else b""
        return self._body

    def form(self) -> dict:
        ctype = (self.environ.get("CONTENT_TYPE") or "").split(";")[0] \
            .strip().lower()
        if ctype not in ("application/x-www-form-urlencoded", ""):
            raise UnsupportedMedia()
        raw = self.body()
        if len(raw) > self.settings.max_body_bytes:
            raise BodyTooLarge()
        parsed = urllib.parse.parse_qs(
            raw.decode("utf-8", "replace"), keep_blank_values=True,
            max_num_fields=50)
        return {k: [v[:MAX_FIELD] for v in vs] for k, vs in parsed.items()}

    def header(self, name):
        return self.environ.get("HTTP_" + name.upper().replace("-", "_"))

    @property
    def host(self):
        return self.environ.get("HTTP_HOST") or environ_host(self.environ)


class BodyTooLarge(Exception):
    pass


class UnsupportedMedia(Exception):
    pass


def environ_host(environ):
    return environ.get("SERVER_NAME", "localhost")


class Response:
    def __init__(self, body=b"", status="200 OK",
                 content_type="text/html; charset=utf-8", headers=None):
        self.body = body.encode("utf-8") if isinstance(body, str) else body
        self.status = status
        self.headers = [("Content-Type", content_type)]
        if headers:
            for k, v in headers:
                _check_header(k, v)
                self.headers.append((k, v))
        self.headers.append(("Content-Length", str(len(self.body))))


def _check_header(name: str, value: str):
    if not re.fullmatch(r"[!-~]+", name):
        raise ValueError("bad header name")
    if re.search(r"[\r\n\x00-\x1f\x7f]", value):
        raise ValueError("bad header value")


def redirect(location: str, extra_headers=None) -> Response:
    headers = [("Location", location)]
    if extra_headers:
        headers += extra_headers
    return Response(b"", "303 See Other", "text/plain", headers)


def json_response(obj, status="200 OK") -> Response:
    return Response(json_dumps(obj).encode(), status,
                    "application/json; charset=utf-8")


# ---------------------------------------------------------------------------

SEC_HEADERS = [
    ("Content-Security-Policy",
     "default-src 'self'; style-src 'self'; script-src 'self'; "
     "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
     "base-uri 'none'"),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "same-origin"),
    ("X-Frame-Options", "DENY"),
    ("Cache-Control", "no-store"),
]


class App:
    def __init__(self, settings, static_dir=None, env=None):
        self.s = settings
        self.env = os.environ if env is None else env
        self.db_path = settings.db_path
        self._static = {}
        sd = static_dir or os.path.join(os.path.dirname(__file__), "static")
        if os.path.isdir(sd):
            for fn in os.listdir(sd):
                if re.fullmatch(r"[A-Za-z0-9_.-]+", fn):
                    with open(os.path.join(sd, fn), "rb") as f:
                        self._static[fn] = f.read()
        self._login_global = []          # global throttle bucket (ts list)
        self._login_per_ip = {}
        self._lock = threading.Lock()

    # -- db --------------------------------------------------------------------
    def conn(self):
        return dbmod.connect(self.db_path)

    # -- sessions ----------------------------------------------------------------
    @property
    def cookie_name(self):
        return SESSION_COOKIE_PROD if self.s.is_production \
            else SESSION_COOKIE_DEV

    def _cookie_flags(self):
        flags = [f"{self.cookie_name}=", "Path=/", "HttpOnly",
                 "SameSite=Strict"]
        if self.s.is_production:
            flags.append("Secure")
        return flags

    def _set_cookie(self, sid: str) -> str:
        parts = self._cookie_flags()
        parts[0] = f"{self.cookie_name}={sid}"
        return "; ".join(parts)

    def _clear_cookie(self) -> str:
        parts = self._cookie_flags()
        parts[0] = f"{self.cookie_name}=; Max-Age=0"
        return "; ".join(parts)

    def _get_cookie(self, req: Request):
        raw = req.environ.get("HTTP_COOKIE") or ""
        for part in raw.split(";"):
            k, _, v = part.strip().partition("=")
            if k == self.cookie_name:
                return v.strip()
        return None

    def _session(self, req: Request):
        sid = self._get_cookie(req)
        if not sid:
            return None
        conn = self.conn()
        try:
            s = conn.execute(
                "SELECT s.*, u.username FROM sessions s JOIN users u ON"
                " u.id=s.user_id WHERE s.id=?", (sid,)).fetchone()
            if not s:
                return None
            now = utcnow()
            if parse_iso(s["expires_at"]) <= now:
                conn.execute("DELETE FROM sessions WHERE id=?", (sid,))
                return None
            idle_deadline = parse_iso(s["last_seen_at"]) + \
                datetime.timedelta(seconds=self.s.session_idle_seconds)
            if idle_deadline <= now:
                conn.execute("DELETE FROM sessions WHERE id=?", (sid,))
                return None
            conn.execute(
                "UPDATE sessions SET last_seen_at=? WHERE id=?",
                (now_iso(), sid))
            return dict(s)
        finally:
            conn.close()

    def _new_session(self, conn, user_id):
        sid = new_id(24)
        now = utcnow()
        exp = now + datetime.timedelta(
            seconds=self.s.session_absolute_seconds)
        conn.execute(
            "INSERT INTO sessions(id, user_id, csrf, created_at,"
            " last_seen_at, expires_at) VALUES(?,?,?,?,?,?)",
            (sid, user_id, new_id(16), now_iso(), now_iso(),
             exp.isoformat(timespec="milliseconds")))
        return sid

    # -- security checks ----------------------------------------------------------
    def _check_origin(self, req: Request) -> bool:
        origin = req.header("Origin")
        if not origin:
            ref = req.header("Referer")
            if not ref:
                return True  # non-browser client; CSRF token still required
            try:
                origin = urllib.parse.urlsplit(ref).netloc
            except ValueError:
                return False
            return origin.lower() == req.host.split(",")[0].strip().lower()
        try:
            netloc = urllib.parse.urlsplit(origin).netloc
        except ValueError:
            return False
        return netloc.lower() == req.host.split(",")[0].strip().lower()

    def _check_csrf(self, req: Request, form: dict) -> bool:
        tok = (form.get("csrf") or [""])[0]
        return bool(tok) and req.session and \
            tok == req.session.get("csrf")

    def _throttle_ok(self, username: str, client_ip: str, conn) -> bool:
        now = utcnow()
        window_start = (now - datetime.timedelta(
            seconds=self.s.login_window_seconds)).isoformat(
            timespec="milliseconds")
        fails = conn.execute(
            "SELECT COUNT(*) c FROM login_attempts WHERE username=? AND"
            " ok=0 AND at>?", (username, window_start)).fetchone()["c"]
        if fails >= self.s.login_max_failures:
            return False
        with self._lock:
            cutoff = time.time() - self.s.login_window_seconds
            g = [t for t in self._login_global if t > cutoff]
            self._login_global = g
            if len(g) >= self.s.login_max_failures * 6:
                return False
            ip = [t for t in self._login_per_ip.get(client_ip, [])
                  if t > cutoff]
            self._login_per_ip[client_ip] = ip
            if len(ip) >= self.s.login_max_failures * 2:
                return False
        return True

    def _record_login_attempt(self, username: str, ok: bool, client_ip: str,
                              conn):
        conn.execute(
            "INSERT INTO login_attempts(username, ok, at) VALUES(?,?,?)",
            (username, int(ok), now_iso()))
        with self._lock:
            self._login_global.append(time.time())
            self._login_per_ip.setdefault(client_ip, []).append(time.time())

    # -- response helpers -----------------------------------------------------------
    def _page(self, title, body_html, req=None, banner=None,
              banner_kind="error") -> str:
        from .views import layout
        return layout(title, body_html, req, banner, banner_kind, self.s)

    # -- entry point ------------------------------------------------------------------
    def __call__(self, environ, start_response):
        req = Request(environ, self.s)
        try:
            resp = self._dispatch(req)
        except BodyTooLarge:
            resp = Response("request too large", "413 Content Too Large",
                            "text/plain")
        except UnsupportedMedia:
            resp = Response("unsupported media type",
                            "415 Unsupported Media Type", "text/plain")
        except Exception:
            import logging, traceback
            logging.getLogger("pa.web").error(
                "request error: %s", traceback.format_exc(limit=8))
            resp = Response("internal error", "500 Internal Server Error",
                            "text/plain")
        headers = resp.headers + SEC_HEADERS
        start_response(resp.status, headers)
        return [resp.body]

    def _dispatch(self, req: Request) -> Response:
        path = req.path
        if req.method == "GET" and path == "/healthz":
            return self._healthz()
        if path == "/login":
            return self._login(req)
        if req.method == "GET" and path == "/oauth/callback":
            return self._oauth_callback(req)
        if req.method == "GET" and path.startswith("/static/"):
            return self._static_file(path)
        if req.method == "POST" and path.startswith("/hooks/"):
            return self._webhook(req)
        if req.method == "GET" and path.startswith("/pages/"):
            return self._public_page(req)
        if path == "/secret" or path.startswith("/secret/"):
            return self._secret_form(req)
        # everything below requires auth
        req.session = self._session(req)
        if req.session is None:
            if req.method == "GET":
                return redirect("/login")
            return json_response({"error": "unauthenticated"}, "401 Unauthorized")
        if req.method == "POST":
            form = req.form()
            if not self._check_csrf(req, form):
                return Response("forbidden: missing or bad CSRF token",
                                "403 Forbidden", "text/plain")
            if not self._check_origin(req):
                return Response("forbidden: origin check failed",
                                "403 Forbidden", "text/plain")
            req._form = form
        return self._route(req)

    def _healthz(self) -> Response:
        """Deep public health check (docs/EVOLVE.md item 3) — short
        codes only, 503 with {"ok": false, "failed": [...]} on
        failure. No secrets, no DATA_DIR paths."""
        from . import health
        conn = self.conn()
        try:
            body = health.check(conn, self.s, env=self.env)
        finally:
            conn.close()
        return json_response(
            body, "200 OK" if body["ok"] else "503 Service Unavailable")

    def _public_page(self, req: Request) -> Response:
        """GET /pages/<slug> — public shareable page; the unguessable
        slug is the credential (docs/PARITY.md #25)."""
        from . import pages
        slug = req.path[len("/pages/"):]
        if not pages.valid_slug(slug):
            return Response("not found", "404 Not Found", "text/plain")
        conn = self.conn()
        try:
            row = pages.get(conn, slug)
        finally:
            conn.close()
        if row is None:
            return Response("not found", "404 Not Found", "text/plain")
        return Response(pages.render(row["markdown"], row["title"]),
                        "200 OK", "text/html; charset=utf-8")

    def _webhook(self, req: Request) -> Response:
        """POST /hooks/<name> — public but HMAC-signed (docs/PARITY.md
        #9). No session; the signature IS the credential."""
        from . import webhooks
        name = req.path[len("/hooks/"):]
        if not re.fullmatch(r"[a-z0-9_-]{1,40}", name):
            return json_response({"error": "not found"},
                                 "404 Not Found")
        conn = self.conn()
        try:
            ep = conn.execute(
                "SELECT * FROM webhook_endpoints WHERE name=? AND"
                " enabled=1", (name,)).fetchone()
            if ep is None:
                return json_response({"error": "not found"},
                                     "404 Not Found")
            body = req.body()
            sig = (req.environ.get("HTTP_X_PA_SIGNATURE")
                   or req.environ.get("HTTP_X_HUB_SIGNATURE_256")
                   or "")
            if not webhooks.verify(ep["secret"], body, sig):
                dbmod.audit(conn, "webhook", "bad_signature", name)
                return json_response({"error": "bad signature"},
                                     "401 Unauthorized")
            # A6: a 'github'/'ci' hook renders CI-ish events as owner
            # DM cards — no agent turn unless the owner opted in.
            if name in ("github", "ci") and not getattr(
                    self.s, "github_hook_agent", False):
                from . import ghcards
                ev = ghcards.detect(
                    req.header("X-GitHub-Event"), body)
                card = ghcards.render(
                    ev, json.loads(body[:webhooks.BODY_CAP]
                                   .decode("utf-8", "replace")
                                   or "{}")) if ev else ""
                if card:
                    ghcards.enqueue_card(conn, ep, card)
                    return json_response({"card": True})
                conn.execute(
                    "UPDATE webhook_endpoints SET last_seen_at=?"
                    " WHERE name=?", (now_iso(), name))
                return json_response({"ignored": True})
            webhooks.enqueue(conn, ep, body)
            return json_response({"queued": True})
        finally:
            conn.close()

    # -- A1 secret-capture form ------------------------------------------------
    _SEC_HEADERS = [
        ("Cache-Control", "no-store"),
        ("X-Content-Type-Options", "nosniff"),
    ]

    def _secret_https_ok(self, req: Request) -> bool:
        """The capture form is HTTPS-only. The tunnel terminates TLS,
        so accept the request when the public base is https, the proxy
        says https, the WSGI scheme is https, or the peer is loopback
        (local dev/test)."""
        env = req.environ
        if env.get("wsgi.url_scheme") == "https":
            return True
        xfp = (env.get("HTTP_X_FORWARDED_PROTO") or "").split(",")
        if xfp and xfp[0].strip() == "https":
            return True
        pub = (getattr(self.s, "public_url", "") or "")
        if pub.startswith("https://"):
            return True
        return (env.get("REMOTE_ADDR") or "") in (
            "127.0.0.1", "::1", "localhost")

    @staticmethod
    def _secret_page(inner: str) -> Response:
        html = ("<!doctype html><title>pal — save a secret</title>"
                "<meta name=viewport content='width=device-width,"
                "initial-scale=1'><meta name=referrer content=never>"
                "<body style='font-family:system-ui;max-width:26em;"
                "margin:3em auto;padding:0 1em'>" + inner + "</body>")
        return Response(html, "200 OK", "text/html; charset=utf-8",
                        headers=App._SEC_HEADERS)

    def _secret_form(self, req: Request) -> Response:
        """GET /secret/<token> renders a one-time form; POST /secret
        (token in the body, never the URL) seals to the vault and
        burns the token. Public like /hooks and /pages — the
        unguessable expiring token is the credential. Nothing about
        the secret is logged or rendered back."""
        if not getattr(self.s, "secret_capture", False):
            return Response("not found", "404 Not Found",
                            "text/plain")
        if not self._secret_https_ok(req):
            return Response("secret capture requires HTTPS",
                            "403 Forbidden", "text/plain",
                            headers=self._SEC_HEADERS)
        from . import secretform
        conn = self.conn()
        try:
            if req.method == "GET" and req.path.startswith("/secret/"):
                token = req.path[len("/secret/"):]
                rec = secretform.peek(conn, token)
                if rec is None:
                    logging.getLogger("pa.web").info(
                        "secret form expired/used get")
                    return self._secret_page(
                        "<h1>Link expired</h1><p>This link is gone — "
                        "single-use links last ~10 minutes. Ask pal "
                        "for a new one with <code>/credentials form"
                        "</code>.</p>")
                otp = rec["kind"] == "otp"
                fields = (
                    "<label>Domain <input name=domain required "
                    "autocomplete=off></label><br><br>"
                    + ("" if otp else
                       "<label>Username <input name=username "
                       "autocomplete=off></label><br><br>")
                    + ("<label>" + ("Code" if otp else "Password") +
                       " <input name=secret type=password required "
                       "autocomplete=off></label><br><br>")
                    + ("" if otp else
                       "<label>Label (optional) <input name=label "
                       "autocomplete=off></label><br><br>"))
                return self._secret_page(
                    "<h1>" +
                    ("Save a one-time code" if otp else "Save a login")
                    + "</h1><form method=post action=/secret>"
                    + "<input type=hidden name=token value="
                    + esc(token) + ">" + fields
                    + "<button type=submit>Save</button></form>"
                    + "<p><small>single use · expires ~10 min · "
                      "pal can never show it back</small></p>")
            if req.method == "POST" and req.path == "/secret":
                form = req.form()
                token = (form.get("token") or [""])[0]
                rec = secretform.consume(conn, token)
                if rec is None:
                    logging.getLogger("pa.web").info(
                        "secret form expired/used post")
                    return self._secret_page(
                        "<h1>Link expired</h1><p>Nothing was saved — "
                        "the link was already used or too old. Ask "
                        "pal for a fresh one.</p>")
                domain = (form.get("domain") or [""])[0]
                secret = (form.get("secret") or [""])[0]
                from . import credvault
                if rec["kind"] == "otp":
                    out = credvault.add_otp(
                        conn, self.env, domain, secret)
                else:
                    out = credvault.add(
                        conn, self.env, domain,
                        (form.get("username") or [""])[0],
                        secret,
                        (form.get("label") or [""])[0])
                if "error" in out:
                    return self._secret_page(
                        "<h1>Not saved</h1><p>" + esc(out["message"])
                        + " — ask pal for a fresh link.</p>")
                dbmod.audit(conn, "web", "secret_form_used",
                            out["domain"])
                logging.getLogger("pa.web").info("secret form used")
                return self._secret_page(
                    "<h1>Saved</h1><p>Sealed for <b>" +
                    esc(out["domain"]) +
                    "</b> — pal can fill it but never show it back. "
                    "You can close this tab.</p>")
            return Response("not found", "404 Not Found",
                            "text/plain")
        finally:
            conn.close()

    def _route(self, req: Request) -> Response:
        m = re.fullmatch(r"/", req.path)
        if req.method == "GET" and m:
            return self._dashboard(req)
        m = re.fullmatch(r"/assistants", req.path)
        if m and req.method == "POST":
            return self._assistant_create(req)
        m = re.fullmatch(r"/assistants/new", req.path)
        if m and req.method == "GET":
            return self._assistant_new_form(req)
        m = re.fullmatch(r"/assistants/(\d+)", req.path)
        if m and req.method == "GET":
            return self._assistant_detail(req, int(m.group(1)))
        m = re.fullmatch(r"/tasks", req.path)
        if m and req.method == "POST":
            return self._task_create(req)
        m = re.fullmatch(r"/tasks/(\d+)", req.path)
        if m and req.method == "GET":
            return self._task_detail(req, int(m.group(1)))
        m = re.fullmatch(r"/tasks/(\d+)/(cancel|retry)", req.path)
        if m and req.method == "POST":
            return (self._task_cancel(req, int(m.group(1))) if m.group(2)
                    == "cancel" else self._task_retry(req, int(m.group(1))))
        m = re.fullmatch(r"/api/tasks/(\d+)", req.path)
        if m and req.method == "GET":
            return self._api_task(req, int(m.group(1)))
        m = re.fullmatch(r"/api/status", req.path)
        if m and req.method == "GET":
            return self._api_status(req)
        m = re.fullmatch(r"/approvals", req.path)
        if m and req.method == "GET":
            return self._approvals(req)
        m = re.fullmatch(r"/approvals/([0-9a-f]+)/(approve|deny)", req.path)
        if m and req.method == "POST":
            return self._approval_decide(req, m.group(1),
                                         m.group(2) == "approve")
        m = re.fullmatch(r"/memories", req.path)
        if m and req.method == "GET":
            return self._memories(req)
        m = re.fullmatch(r"/memories/(\d+)/(edit|delete)", req.path)
        if m and req.method == "POST":
            return self._memory_mutation(req, int(m.group(1)), m.group(2))
        m = re.fullmatch(r"/settings", req.path)
        if m and req.method == "GET":
            return self._settings(req)
        if m and req.method == "POST":
            return self._settings_save(req)
        m = re.fullmatch(r"/owner-facts", req.path)
        if m and req.method == "GET":
            return self._owner_facts(req)
        if m and req.method == "POST":
            return self._owner_fact_save(req)
        m = re.fullmatch(r"/owner-facts/(\d+)/delete", req.path)
        if m and req.method == "POST":
            return self._owner_fact_delete(req, int(m.group(1)))
        m = re.fullmatch(r"/logout", req.path)
        if m and req.method == "POST":
            return self._logout(req)
        return Response(self._page("Not found",
                                   "<p>That page does not exist.</p>", req),
                        "404 Not Found")

    # -- handlers ------------------------------------------------------------------
    def _oauth_callback(self, req: Request):
        """Public OAuth redirect target — the only auth is the
        single-use `state` the provider echoes back (10 min expiry)."""
        from . import connectflows
        conn = self.conn()
        try:
            ok, title, msg, provider = connectflows.complete_callback(
                conn, self.s, self.env, req.query)
        finally:
            conn.close()
        if ok:
            self._notify_connected(provider)
        body = (f"<h1>{esc(title)}</h1><p>{esc(msg)}</p>")
        return Response(self._page(title, body, req),
                        "200 OK" if ok else "400 Bad Request")

    def _notify_connected(self, provider):
        """pal confirms in Discord: DM the owner once a connection is
        active. Best-effort — the row is the source of truth."""
        try:
            if not (self.s.discord_enabled and
                    self.s.discord_owner_id and provider):
                return
            token = self.s.discord_token(self.env)
            if not token:
                return
            from .discordbot import DiscordAPI
            api = DiscordAPI(self.s, token)
            dm = api.open_dm(self.s.discord_owner_id)
            if dm:
                api.post_message(
                    dm, f"✅ **{provider}** connected — its tools are "
                        "available in chat now.")
        except Exception:
            import logging
            logging.getLogger("pa.web").warning(
                "connection notify failed", exc_info=True)

    def _static_file(self, path):
        name = path.rsplit("/", 1)[-1]
        data = self._static.get(name)
        if data is None:
            return Response("not found", "404 Not Found", "text/plain")
        ctype = ("text/css" if name.endswith(".css") else
                 "application/javascript" if name.endswith(".js") else
                 "application/octet-stream")
        return Response(data, "200 OK", ctype)

    # --- auth ---
    def _login(self, req: Request):
        conn = self.conn()
        try:
            if req.method == "GET":
                body = f"""
                <h1>Sign in</h1>
                <form method="post" action="/login" class="card">
                  <label for="username">Username</label>
                  <input id="username" name="username" type="text"
                         autocomplete="username" required>
                  <label for="password">Password</label>
                  <input id="password" name="password" type="password"
                         autocomplete="current-password" required>
                  <button type="submit">Sign in</button>
                </form>"""
                return Response(self._page("Sign in", body))
            form = req.form()
            if not self._check_origin(req):
                return Response("forbidden: origin check failed",
                                "403 Forbidden", "text/plain")
            username = (form.get("username") or [""])[0][:100]
            password = (form.get("password") or [""])[0][:500]
            client_ip = self.s.trusted_client_ip(req.environ)
            if not self._throttle_ok(username, client_ip, conn):
                body = """
                <h1>Sign in</h1>
                <div class="banner error" role="alert">Too many attempts.
                Wait a few minutes and try again.</div>
                <p><a href="/login">Back to sign in</a></p>"""
                return Response(self._page("Sign in", body),
                                "429 Too Many Requests")
            row = conn.execute(
                "SELECT * FROM users WHERE username=?",
                (username,)).fetchone()
            ok = bool(row) and verify_password(password, row["password_hash"])
            self._record_login_attempt(username, ok, client_ip, conn)
            if not ok:
                body = """
                <h1>Sign in</h1>
                <div class="banner error" role="alert">Wrong username or
                password.</div>
                <p><a href="/login">Back to sign in</a></p>"""
                return Response(self._page("Sign in", body), "401 Unauthorized")
            sid = self._new_session(conn, row["id"])
            return redirect("/", [("Set-Cookie", self._set_cookie(sid))])
        finally:
            conn.close()

    def _logout(self, req: Request):
        conn = self.conn()
        try:
            conn.execute("DELETE FROM sessions WHERE id=?",
                         (req.session["id"],))
        finally:
            conn.close()
        return redirect("/login", [("Set-Cookie", self._clear_cookie())])

    # --- dashboard ---
    def _dashboard(self, req: Request):
        conn = self.conn()
        try:
            assistants = conn.execute(
                "SELECT a.*, (SELECT COUNT(*) FROM tasks t WHERE"
                " t.assistant_id=a.id) ntasks FROM assistants a"
                " ORDER BY a.id").fetchall()
            tasks = conn.execute(
                "SELECT t.*, a.name aname FROM tasks t JOIN assistants a"
                " ON a.id=t.assistant_id ORDER BY t.id DESC LIMIT 12"
            ).fetchall()
            u = dbmod.usage_today(conn, self._today())
            pend = len(approvals.list_pending(conn))
        finally:
            conn.close()
        pct = min(100, int(100 * u["requests"] / max(1, self.s.daily_request_limit)))
        tpct = min(100, int(100 * (u["tokens_in"] + u["tokens_out"]) /
                          max(1, self.s.daily_token_limit)))
        body = f"""
        <h1>Assistants</h1>
        <div class="card usage">
          <h2>Today's provider usage</h2>
          <p><strong>{u['requests']}</strong> / {self.s.daily_request_limit}
             requests &middot;
             <strong>{u['tokens_in'] + u['tokens_out']}</strong> /
             {self.s.daily_token_limit} tokens</p>
          <div class="meter"><div style="width:{pct}%"></div></div>
          <div class="meter"><div style="width:{tpct}%"></div></div>
          <p class="muted">Quota resets at 00:00 UTC.</p>
        </div>""" + self._integrations_html(conn) + f"""
        <p><a class="btn" href="/approvals">Approvals
           ({pend} pending)</a>
           <a class="btn" href="/memories">All memories</a>
           <a class="btn" href="/owner-facts">Owner facts</a></p>
        """
        if not assistants:
            body += """
            <div class="empty"><p>No assistants yet.</p>
            <p><a class="btn" href="/assistants/new">Create one</a></p></div>"""
        else:
            body += '<ul class="cards">'
            for a in assistants:
                body += f"""
                <li class="card"><h2><a href="/assistants/{a['id']}">
                  {esc(a['name'])}</a></h2>
                  <p class="muted">{esc(a['provider_model'])} &middot;
                     {a['ntasks']} task(s)</p></li>"""
            body += '</ul><p><a class="btn" href="/assistants/new">' \
                    'New assistant</a></p>'
        if tasks:
            body += "<h2>Recent tasks</h2><ul class='tasks'>"
            for t in tasks:
                body += (f"<li><a href='/tasks/{t['id']}'>#{t['id']} "
                         f"{esc(t['title'])}</a> "
                         f"<span class='state {esc(t['state'])}'>"
                         f"{esc(t['state'])}</span> "
                         f"<span class='muted'>{esc(t['aname'])}</span></li>")
            body += "</ul>"
        return Response(self._page("Dashboard", body, req))

    def _integrations_html(self, conn):
        """Needs-config status panel: every integration reports its real
        state — never fake success."""
        from . import gmail as gmailmod
        rows = []
        if self.s.llm_configured:
            al = self.s.provider_allowlist()
            rows.append(("Provider",
                         f"{esc(al[0][0])} &middot; {esc(self.s.llm_model)}"
                         f" &middot; streaming={'on' if self.s.llm_streaming else 'off'}"
                         f" tools={'on' if self.s.llm_tools else 'off'}"
                         + (" (dev fallback)" if not self.s.is_production
                            and al[0][0] == "localhost" else "")))
        else:
            rows.append(("Provider",
                         "<strong>needs config</strong>: set LLM_BASE_URL"
                         " + LLM_MODEL (+ LLM_API_KEY if required)"))
        if self.s.discord_enabled:
            tok = bool(self.s.discord_token(os.environ))
            if not tok:
                state = "disabled — no DISCORD_BOT_TOKEN"
            elif not self.s.discord_owner_id:
                state = "token set; <strong>DISCORD_OWNER_ID missing — " \
                        "refusing everyone</strong>"
            else:
                state = (f"on — {len(self.s.discord_channels)} channel "
                         f"route(s)")
            rows.append(("Discord", state))
        else:
            rows.append(("Discord", "disabled (DISCORD_ENABLED=false)"))
        rows.append(("Gmail", "configured (read-only)"
                     if gmailmod.configured(os.environ) else
                     "needs config — run <code>pa gmail-auth</code>"))
        if self.s.vault_path:
            n = conn.execute("SELECT COUNT(*) c FROM vault_notes"
                             ).fetchone()["c"]
            rows.append(("Vault", f"{esc(self.s.vault_path)} — {n} note(s)"
                                  " indexed"))
        else:
            rows.append(("Vault", "needs config — set VAULT_PATH to a "
                                  "synced mirror"))
        try:
            from . import scheduler as sch
            scheds = sch.list_schedules(conn)
            txt = ", ".join(
                f"{esc(r['name'])}({'on' if r['enabled'] else 'off'})"
                for r in scheds) or "none"
        except Exception:
            txt = "unavailable"
        rows.append(("Schedules", txt))
        items = "".join(f"<tr><th>{k}</th><td>{v}</td></tr>"
                        for k, v in rows)
        return (f'<div class="card usage"><h2>Integrations</h2>'
                f'<table class="kv">{items}</table></div>')

    # --- assistants ---
    def _assistant_new_form(self, req: Request):
        allowed_tools = sorted(t for t in TOOL_DEFS if t != "finish")
        toolboxes = "".join(
            f'<label class="check"><input type="checkbox" name="tool"'
            f' value="{esc(n)}"> {esc(n)}</label>'
            for n in allowed_tools)
        pairs = self.s.provider_allowlist()
        options = "".join(
            f'<option value="{esc(host)}|{esc(m)}">{esc(m)}'
            f' ({esc(host)})</option>'
            for host, _url, models, _key in pairs for m in models)
        if not options:
            options = ""
        provider_field = (
            f'<select id="provider" name="provider">{options}</select>'
            if options else
            '<div class="banner error" role="alert">No provider '
            'configured — set LLM_BASE_URL and LLM_MODEL first.</div>')
        body = f"""
        <h1>New assistant</h1>
        <form method="post" action="/assistants" class="card">
          {self._csrf(req)}
          <label for="name">Name</label>
          <input id="name" name="name" required maxlength="60">
          <label for="instructions">Instructions</label>
          <textarea id="instructions" name="instructions" rows="6"
                    maxlength="8000"></textarea>
          <label for="provider">Provider / model</label>
          {provider_field}
          <fieldset><legend>Allowed tools</legend>{toolboxes}</fieldset>
          <button type="submit">Create assistant</button>
        </form>"""
        return Response(self._page("New assistant", body, req))

    def _assistant_create(self, req: Request):
        f = req._form
        name = (f.get("name") or [""])[0].strip()[:60]
        instructions = (f.get("instructions") or [""])[0]
        tools = [t for t in f.get("tool", [])
                 if t in TOOL_DEFS and t != "finish"]
        pair = (f.get("provider") or [""])[0]
        host, _, model = pair.partition("|")
        if not name:
            return Response(self._page(
                "New assistant", "<div class='banner error' role='alert'>"
                "A name is required.</div>", req), "400 Bad Request")
        try:
            self.s.resolve_provider(host, model)
        except Exception:
            return Response("provider/model not on allowlist",
                            "400 Bad Request", "text/plain")
        conn = self.conn()
        try:
            conn.execute(
                "INSERT INTO assistants(name, instructions, provider_host,"
                " provider_model, tools, created_at) VALUES(?,?,?,?,?,?)",
                (name, instructions, host, model,
                 json.dumps(tools), now_iso()))
            dbmod.audit(conn, "owner", "assistant_create", name)
            aid = conn.execute("SELECT last_insert_rowid() i").fetchone()["i"]
        finally:
            conn.close()
        return redirect(f"/assistants/{aid}")

    def _assistant_detail(self, req: Request, aid: int):
        conn = self.conn()
        try:
            a = conn.execute("SELECT * FROM assistants WHERE id=?",
                             (aid,)).fetchone()
            if not a:
                return Response("not found", "404 Not Found", "text/plain")
            mems = memmod.list_memories(conn, aid)
            tasks = conn.execute(
                "SELECT * FROM tasks WHERE assistant_id=? ORDER BY id DESC"
                " LIMIT 15", (aid,)).fetchall()
        finally:
            conn.close()
        tools = ", ".join(json.loads(a["tools"] or "[]")) or "none"
        body = f"""
        <h1>{esc(a['name'])}</h1>
        <div class="card"><p class="muted">{esc(a['provider_host'])} &middot;
          {esc(a['provider_model'])} &middot; tools: {esc(tools)}</p>
          <p>{esc(a['instructions'])}</p></div>
        <h2>New task</h2>
        <form method="post" action="/tasks" class="card">
          {self._csrf(req)}
          <input type="hidden" name="assistant_id" value="{a['id']}">
          <label for="title">Title</label>
          <input id="title" name="title" required maxlength="200">
          <label for="body">What should {esc(a['name'])} do?</label>
          <textarea id="body" name="body" rows="5" required
                    maxlength="12000"></textarea>
          <label class="check"><input type="checkbox" name="private">
            Private task (searches need your approval first)</label>
          <label for="deadline">Deadline (minutes)</label>
          <input id="deadline" name="deadline" type="number" min="1"
                 max="60" value="10">
          <button type="submit">Start task</button>
        </form>
        <h2>Memories</h2>"""
        if not mems:
            body += '<div class="empty">No memories saved yet.</div>'
        else:
            body += "<ul>"
            for m in mems:
                body += (f"<li>{esc(m['content'][:300])} "
                         f"<span class='muted'>#{m['id']}</span></li>")
            body += "</ul>"
        body += "<h2>Tasks</h2>"
        if not tasks:
            body += '<div class="empty">No tasks yet.</div>'
        else:
            body += "<ul class='tasks'>"
            for t in tasks:
                body += (f"<li><a href='/tasks/{t['id']}'>#{t['id']} "
                         f"{esc(t['title'])}</a> <span class='state "
                         f"{esc(t['state'])}'>{esc(t['state'])}</span></li>")
            body += "</ul>"
        return Response(self._page(a["name"], body, req))

    # --- tasks ---
    def _task_create(self, req: Request):
        f = req._form
        try:
            aid = int((f.get("assistant_id") or ["0"])[0])
        except ValueError:
            aid = 0
        title = (f.get("title") or [""])[0].strip()[:200]
        body = (f.get("body") or [""])[0].strip()[:16000]
        private = bool(f.get("private"))
        try:
            deadline_min = min(60, max(1, int(
                (f.get("deadline") or ["10"])[0])))
        except ValueError:
            deadline_min = 10
        conn = self.conn()
        try:
            a = conn.execute("SELECT id FROM assistants WHERE id=?",
                             (aid,)).fetchone()
            if not a or not title or not body:
                return Response("bad task", "400 Bad Request", "text/plain")
            tid = workmod.create_task(
                conn, aid, title, body, private,
                deadline_seconds=deadline_min * 60)
            dbmod.audit(conn, "owner", "task_create", f"#{tid} {title}")
        finally:
            conn.close()
        return redirect(f"/tasks/{tid}")

    def _task_detail(self, req: Request, tid: int):
        conn = self.conn()
        try:
            t = conn.execute(
                "SELECT t.*, a.name aname FROM tasks t JOIN assistants a ON"
                " a.id=t.assistant_id WHERE t.id=?", (tid,)).fetchone()
            if not t:
                return Response("not found", "404 Not Found", "text/plain")
            steps = conn.execute(
                "SELECT seq, kind, tool_name, status, result_json,"
                " tokens_in, tokens_out, created_at FROM steps"
                " WHERE task_id=? ORDER BY seq", (tid,)).fetchall()
        finally:
            conn.close()
        body = self._task_html(req, t, steps)
        return Response(self._page(f"Task #{tid}", body, req))

    def _task_html(self, req, t, steps):
        err = ""
        if t["error_kind"]:
            detail = esc(t["error_detail"] or "")
            reset = f" Quota resets at {esc(t['reset_at'])}." \
                if t["reset_at"] else ""
            kind = esc(ERROR_TEXT.get(t["error_kind"], t["error_kind"]))
            err = (f'<div class="banner error" role="alert">{kind}'
                   f'{reset}<br><span class="muted">{detail}</span></div>')
        actions = ""
        if t["state"] in ("queued", "running", "waiting_approval"):
            actions += f"""
            <form method="post" action="/tasks/{t['id']}/cancel"
                  class="inline">{self._csrf(req)}
              <button type="submit" class="danger">Cancel task</button></form>"""
        if t["state"] in ("failed", "cancelled"):
            actions += f"""
            <form method="post" action="/tasks/{t['id']}/retry"
                  class="inline">{self._csrf(req)}
              <button type="submit">Retry task</button></form>"""
        priv = " &middot; private" if t["private"] else ""
        body = f"""
        <h1>Task #{t['id']}: {esc(t['title'])}</h1>
        <p><span class="state {esc(t['state'])}" id="task-state">
           {esc(t['state'])}</span>
           <span class="muted">{esc(t['aname'])}{priv} &middot;
           deadline {esc(t['deadline_at'][:19])}</span></p>
        {err}
        <div class="card"><h2>Request</h2><p>{esc(t['body'])}</p></div>
        <div class="actions">{actions}</div>
        <h2>Progress</h2>
        <ul class="steps" id="steps" data-task="{t['id']}"
            data-state="{esc(t['state'])}">"""
        if not steps:
            body += '<li class="empty" id="no-steps">No steps yet.</li>'
        for s in steps:
            body += self._step_html(s)
        body += "</ul>"
        result = self._result_html(t)
        body += result
        body += ('<p class="muted" id="live-note" hidden>'
                 'Updating every 2 seconds&hellip;</p>')
        return body

    def _step_html(self, s):
        label = s["tool_name"] or ("model" if s["kind"] == "model_call"
                                   else s["kind"])
        detail = ""
        if s["result_json"]:
            try:
                r = json_loads_bounded(s["result_json"])
                if s["kind"] == "tool_call":
                    detail = esc(json.dumps(r)[:400])
                else:
                    c = r.get("message", {})
                    detail = esc((c.get("content") or "")[:200])
            except Exception:
                detail = ""
        tok = ""
        if s["tokens_in"]:
            tok = (f" <span class='muted'>in:{s['tokens_in']}"
                   f" out:{s['tokens_out']}</span>")
        return (f"<li class='step {esc(s['status'])}'>"
                f"<span class='seq'>{s['seq']}</span> "
                f"<strong>{esc(label)}</strong> "
                f"<span class='state'>{esc(s['status'])}</span>{tok}"
                f"<div class='detail'>{detail}</div></li>")

    def _result_html(self, t):
        if not t["result_json"]:
            return ""
        try:
            r = json_loads_bounded(t["result_json"])
        except Exception:
            return ""
        out = "<h2>Result</h2>"
        if "text" in r:
            out += f"<div class='card'><p>{esc(r['text'])}</p></div>"
            return out
        claims = r.get("claims") or []
        sources = {s["id"]: s for s in r.get("sources") or []}
        out += "<div class='card'><ul class='claims'>"
        for c in claims:
            sids = c.get("source_ids") or []
            badge = "" if sids else \
                ' <span class="unsupported">unsupported</span>'
            links = " ".join(
                f'<a href="{esc(sources[s]["url"])}" rel="noopener">'
                f'[{s}]</a>' for s in sids if s in sources)
            out += f"<li>{esc(c['text'])} {links}{badge}</li>"
        out += "</ul></div>"
        if sources:
            out += "<h3>Sources</h3><ol class='sources'>"
            for sid, s in sources.items():
                out += (f"<li value='{sid}'><a href='{esc(s['url'])}' "
                        f"rel='noopener'>{esc(s['title'] or s['url'])}</a>"
                        f"<div class='muted'>{esc(s['fetched_at'])}"
                        f"</div><blockquote>{esc((s['excerpt'] or '')[:300])}"
                        f"</blockquote></li>")
            out += "</ol>"
        return out

    # --- API ---
    def _api_task(self, req: Request, tid: int):
        conn = self.conn()
        try:
            t = conn.execute("SELECT * FROM tasks WHERE id=?",
                             (tid,)).fetchone()
            if not t:
                return json_response({"error": "not found"},
                                     "404 Not Found")
            steps = [dict(r) for r in conn.execute(
                "SELECT seq, kind, tool_name, status, tokens_in,"
                " tokens_out, created_at FROM steps WHERE task_id=?"
                " ORDER BY seq", (tid,))]
            u = dbmod.usage_today(conn, self._today())
        finally:
            conn.close()
        return json_response({
            "id": t["id"], "state": t["state"],
            "error_kind": t["error_kind"],
            "error_text": ERROR_TEXT.get(t["error_kind"] or "", ""),
            "reset_at": t["reset_at"],
            "result": json_loads_bounded(t["result_json"])
            if t["result_json"] else None,
            "steps": steps,
            "usage": u,
            "limits": {"requests": self.s.daily_request_limit,
                       "tokens": self.s.daily_token_limit},
        })

    def _api_status(self, req: Request):
        conn = self.conn()
        try:
            u = dbmod.usage_today(conn, self._today())
            pend = len(approvals.list_pending(conn))
        finally:
            conn.close()
        return json_response({
            "usage": u,
            "limits": {"requests": self.s.daily_request_limit,
                       "tokens": self.s.daily_token_limit},
            "search_enabled": self.s.search_enabled,
            "pending_approvals": pend,
        })

    # --- approvals ---
    def _approvals(self, req: Request):
        conn = self.conn()
        try:
            pend = approvals.list_pending(conn)
        finally:
            conn.close()
        body = "<h1>Approvals</h1>"
        if not pend:
            body += '<div class="empty">Nothing is waiting for review.' \
                    '</div>'
        for a in pend:
            payload = json_loads_bounded(a["payload_json"])
            body += f'<div class="card approval" data-kind="{esc(a["kind"])}">'
            body += (f"<h2>{esc(a['kind'])}</h2>"
                     f"<p class='muted'>task {a['task_id']} "
                     f"&middot; {esc(a['created_at'][:19])}</p>")
            if a["kind"] == "search_query":
                body += (f"<p>The assistant wants to run this web search:"
                         f"</p><p><code>{esc(payload.get('query'))}</code></p>")
            elif a["kind"] == "owner_fact":
                body += (f"<p>Set owner fact "
                         f"<code>{esc(payload.get('key'))}</code>:</p>")
                val = esc(payload.get("value") or "")
                body += (f"<form method='post' action='/approvals/{a['id']}"
                         f"/approve' class='inline-edit'>{self._csrf(req)}"
                         f"<input name='value' value=\"{val}\" "
                         f"maxlength='2000'><button type='submit'>Approve "
                         f"&amp; apply</button></form>")
            else:
                body += (f"<p>Save memory for "
                         f"<b>{esc(payload.get('assistant_name'))}</b>:</p>"
                         f"<form method='post' action='/approvals/{a['id']}"
                         f"/approve' class='inline-edit'>{self._csrf(req)}"
                         f"<textarea name='content' rows='3' maxlength='4000'>"
                         f"{esc(payload.get('content'))}</textarea>"
                         f"<button type='submit'>Approve &amp; save</button>"
                         f"</form>")
            body += (f"<form method='post' action='/approvals/{a['id']}/deny'"
                     f" class='inline'>{self._csrf(req)}"
                     f"<button type='submit' class='danger'>Deny</button>"
                     f"</form></div>")
        return Response(self._page("Approvals", body, req))

    def _approval_decide(self, req: Request, aid: str, approve: bool):
        conn = self.conn()
        try:
            ap = approvals.get(conn, aid)
            if not ap:
                return Response("not found", "404 Not Found", "text/plain")
            if approve:
                payload = json_loads_bounded(ap["payload_json"])
                f = req._form
                if ap["kind"] == "memory_write" and f.get("content"):
                    payload["content"] = f["content"][0][:4000]
                if ap["kind"] == "owner_fact" and f.get("value"):
                    payload["value"] = f["value"][0][:2000]
                approvals.decide(conn, aid, True,
                                 edited_payload=payload)
                approvals.apply_if_new(conn, aid)
                if ap["kind"] == "search_query":
                    workmod.resume_waiting(conn, ap)
                dbmod.audit(conn, "owner", "approval_approve",
                            f"{ap['kind']} {aid[:8]}")
            else:
                approvals.decide(conn, aid, False)
                if ap["kind"] == "search_query":
                    workmod.resume_waiting(conn, ap)
                dbmod.audit(conn, "owner", "approval_deny",
                            f"{ap['kind']} {aid[:8]}")
        finally:
            conn.close()
        return redirect("/approvals")

    # --- memories ---
    def _memories(self, req: Request):
        conn = self.conn()
        try:
            q = (req.query.get("q") or [""])[0].strip()
            aid = req.query.get("assistant_id") or [""]
            aid = int(aid[0]) if aid[0].isdigit() else None
            if q and aid is not None:
                mems = memmod.search_memories(conn, aid, q, limit=50)
            elif q:
                mems = [m for m in memmod.list_memories(conn)
                        if q.lower() in m["content"].lower()]
            else:
                mems = memmod.list_memories(conn, aid)
        finally:
            conn.close()
        body = f"""
        <h1>Memories</h1>
        <form method="get" action="/memories" class="card search">
          <label for="q">Search</label>
          <input id="q" name="q" value="{esc(q)}">
          <button type="submit">Search</button>
        </form>"""
        if not mems:
            body += '<div class="empty">No memories found.</div>'
        else:
            body += "<ul class='mems'>"
            for m in mems:
                body += f"""
                <li class="card">
                  <p>{esc(m['content'])}</p>
                  <p class="muted">#{m['id']} &middot;
                    {esc(m['assistant_name'])} &middot;
                    {esc(m['updated_at'][:19])}</p>
                  <form method="post"
                        action="/memories/{m['id']}/edit"
                        class="inline-edit">{self._csrf(req)}
                    <input type="hidden" name="assistant_id"
                           value="{m['assistant_id']}">
                    <input name="content" maxlength="4000"
                           value="{esc(m['content'][:200])}">
                    <button type="submit">Edit</button></form>
                  <form method="post"
                        action="/memories/{m['id']}/delete"
                        class="inline">{self._csrf(req)}
                    <button type="submit" class="danger">Delete</button>
                  </form>
                </li>"""
            body += "</ul>"
        return Response(self._page("Memories", body, req))

    def _memory_mutation(self, req: Request, mid: int, op: str):
        f = req._form
        try:
            aid = int((f.get("assistant_id") or ["0"])[0])
        except ValueError:
            aid = 0
        conn = self.conn()
        try:
            if op == "edit":
                content = (f.get("content") or [""])[0][:4000]
                memmod.update_memory(conn, mid, aid, content)
                dbmod.audit(conn, "owner", "memory_edit", f"#{mid}")
            else:
                memmod.delete_memory(conn, mid, aid)
                dbmod.audit(conn, "owner", "memory_delete", f"#{mid}")
        finally:
            conn.close()
        return redirect("/memories")

    # --- owner facts ---
    _SETTINGS_KEYS = {
        "voice_notes": "Voice notes (Discord replies spoken back)",
        "chat_progress": "Live turn progress (status line + reactions)",
    }

    def _settings(self, req: Request):
        """/settings — the owner-editable pa_settings toggles. Only
        keys in _SETTINGS_KEYS are writable; everything else is shown
        read-only (never secrets — those live in env only)."""
        conn = self.conn()
        try:
            rows = conn.execute(
                "SELECT key, value FROM pa_settings ORDER BY key"
            ).fetchall()
        finally:
            conn.close()
        vals = {r["key"]: r["value"] for r in rows}

        def sel(key):
            cur = vals.get(key, "")
            choices = [("", "default (env)"), ("on", "on"),
                       ("off", "off")]
            if key == "voice_notes":
                choices.append(
                    ("auto", "auto — only while you're busy"))
            opts = "".join(
                f'<option value="{v}"'
                f'{" selected" if cur == v else ""}>{l}</option>'
                for v, l in choices)
            return f'<select name="{key}">{opts}</select>'

        body = "<h1>Settings</h1><p class='muted'>Saved to pa_settings" \
            " — overrides apply immediately, env stays the default.</p>" \
            f'<form method="post" action="/settings" class="card">' \
            f"{self._csrf(req)}"
        for key, label in self._SETTINGS_KEYS.items():
            body += f"<label>{esc(label)}</label>{sel(key)}"
        body += '<button type="submit">Save</button></form>'
        others = [r for r in rows if r["key"] not in self._SETTINGS_KEYS]
        if others:
            body += "<h2>Internal keys (read-only)</h2><ul class='mems'>"
            for r in others:
                body += (f"<li class='card'><strong>"
                         f"{esc(r['key'])}</strong>: "
                         f"{esc(str(r['value'])[:120])}</li>")
            body += "</ul>"
        return Response(self._page("Settings", body, req))

    def _settings_save(self, req: Request):
        f = req._form
        conn = self.conn()
        try:
            for key in self._SETTINGS_KEYS:
                v = (f.get(key) or [""])[0].strip().lower()
                if v in ("on", "off", "") or \
                        (key == "voice_notes" and v == "auto"):
                    dbmod.set_setting(conn, key, v)
            dbmod.audit(conn, "owner", "settings_save",
                        ",".join(sorted(self._SETTINGS_KEYS)))
        finally:
            conn.close()
        return redirect("/settings")

    def _owner_facts(self, req: Request):
        conn = self.conn()
        try:
            facts = memmod.list_owner_facts(conn)
        finally:
            conn.close()
        body = f"""
        <h1>Owner facts</h1>
        <p class="muted">Shared with every assistant. Changes made here are
        yours and apply immediately through the same one-time effect path
        used for approvals.</p>
        <form method="post" action="/owner-facts" class="card">
          {self._csrf(req)}
          <label for="key">Key</label>
          <input id="key" name="key" required maxlength="100">
          <label for="value">Value</label>
          <input id="value" name="value" required maxlength="2000">
          <button type="submit">Save fact</button>
        </form>"""
        if not facts:
            body += '<div class="empty">No owner facts yet.</div>'
        else:
            body += "<ul class='mems'>"
            for fact in facts:
                body += f"""
                <li class="card">
                  <p><strong>{esc(fact['key'])}</strong>:
                     {esc(fact['value'])}</p>
                  <form method="post"
                        action="/owner-facts/{fact['id']}/delete"
                        class="inline">{self._csrf(req)}
                    <button type="submit" class="danger">Delete</button>
                  </form>
                </li>"""
            body += "</ul>"
        return Response(self._page("Owner facts", body, req))

    def _owner_fact_save(self, req: Request):
        f = req._form
        key = (f.get("key") or [""])[0].strip()[:100]
        value = (f.get("value") or [""])[0].strip()[:2000]
        conn = self.conn()
        try:
            if key:
                # owner-initiated: create + approve + apply in one path,
                # keyed so a double-submit cannot apply twice
                payload = {"key": key, "value": value,
                           "assistant_name": "owner-ui"}
                aid = "ui-" + new_id(12)
                approvals.create_approval(conn, "owner_fact", payload,
                                          approval_id=aid)
                approvals.decide(conn, aid, True)
                approvals.apply_if_new(conn, aid)
                dbmod.audit(conn, "owner", "owner_fact_save", key)
        finally:
            conn.close()
        return redirect("/owner-facts")

    def _owner_fact_delete(self, req: Request, fid: int):
        conn = self.conn()
        try:
            row = conn.execute("SELECT key FROM owner_facts WHERE id=?",
                               (fid,)).fetchone()
            if row:
                payload = {"key": row["key"], "delete": True,
                           "assistant_name": "owner-ui"}
                aid = "ui-" + new_id(12)
                approvals.create_approval(conn, "owner_fact", payload,
                                          approval_id=aid)
                approvals.decide(conn, aid, True)
                approvals.apply_if_new(conn, aid)
                dbmod.audit(conn, "owner", "owner_fact_delete", row["key"])
        finally:
            conn.close()
        return redirect("/owner-facts")

    # -- misc ------------------------------------------------------------------
    def _csrf(self, req: Request) -> str:
        tok = req.session["csrf"] if req.session else ""
        return f'<input type="hidden" name="csrf" value="{esc(tok)}">'

    @staticmethod
    def _today() -> str:
        return datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y-%m-%d")
