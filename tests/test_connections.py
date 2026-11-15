"""Connections library: fake OAuth-AS + MCP server end to end.

One fixture serves the protected-resource metadata, the authorization
server (metadata, dynamic registration, authorize->302, token), and the
MCP endpoint itself — the same origin, matching how Notion/Linear host
both on one service. A second fixture fakes the Composio API.
"""

import datetime
import http.server
import json
import threading
import unittest
import urllib.parse
from unittest import mock

from pa import chattools, composio as composiomod, connectflows, db
from pa import secretsbox, webapp
from tests.helpers import TmpEnv, WSGIClient

SECRETS = {"PA_SECRETS_KEY": "ab" * 32}


# ---------------------------------------------------------------------------
# fake OAuth authorization server + protected MCP endpoint (one origin)

class _OAuthMcpHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    @property
    def fx(self):
        return self.server.fx

    def _json(self, code, obj, headers=None):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _sse(self, code, events, headers=None):
        body = "".join(
            f"event: message\ndata: {json.dumps(e)}\n\n"
            for e in events).encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    # -- discovery ---------------------------------------------------------
    def do_GET(self):
        fx = self.fx
        p = urllib.parse.urlsplit(self.path)
        if p.path.startswith("/.well-known/oauth-protected-resource"):
            self._json(200, {
                "resource": fx.base + "/mcp",
                "authorization_servers": [fx.base],
                "scopes_supported": ["default"]})
            return
        if p.path.startswith("/.well-known/oauth-authorization-server"):
            self._json(200, fx.as_meta())
            return
        if p.path == "/authorize":
            q = urllib.parse.parse_qs(p.query)
            fx.authorize_calls.append(q)
            loc = (q["redirect_uri"][0] + "?code=fake-code-123&state="
                   + q["state"][0] + "&iss="
                   + urllib.parse.quote(fx.base))
            self.send_response(302)
            self.send_header("Location", loc)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self._json(404, {"error": "no page"})

    # -- register / token / mcp --------------------------------------------
    def do_POST(self):
        fx = self.fx
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n)
        if self.path == "/register":
            fx.registrations.append(json.loads(body or b"{}"))
            self._json(201, {"client_id": "fake-client-1"})
            return
        if self.path == "/token":
            form = {k: v[0] for k, v in
                    urllib.parse.parse_qs(body.decode()).items()}
            fx.token_forms.append(form)
            if form.get("grant_type") == "authorization_code":
                self._json(200, {"access_token": "at-1",
                                 "refresh_token": "rt-1",
                                 "expires_in": 3600,
                                 "token_type": "Bearer"})
            elif form.get("grant_type") == "refresh_token":
                self._json(200, {"access_token": "at-2",
                                 "refresh_token": "rt-2",
                                 "expires_in": 3600,
                                 "token_type": "Bearer"})
            else:
                self._json(400, {"error": "unsupported_grant_type"})
            return
        if self.path == "/mcp":
            if "authorization" not in self.headers:
                self._json(401, {"error": "unauthorized"}, {
                    "WWW-Authenticate":
                    'Bearer resource_metadata="' + fx.base
                    + '/.well-known/oauth-protected-resource/mcp"'})
                return
            req = json.loads(body or b"{}")
            fx.mcp_calls.append({
                "method": req.get("method"),
                "params": req.get("params"),
                "auth": self.headers.get("Authorization"),
                "session": self.headers.get("Mcp-Session-Id")})
            self._mcp(req)
            return
        self._json(404, {"error": "no page"})

    def _mcp(self, req):
        method = req.get("method")
        rid = req.get("id")
        if method == "initialize":
            self._json(200, {
                "jsonrpc": "2.0", "id": rid,
                "result": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "fake-mcp",
                                   "version": "0.1"}}},
                {"Mcp-Session-Id": "sess-1"})
            return
        if method == "notifications/initialized":
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if method == "tools/list":
            self._sse(200, [{
                "jsonrpc": "2.0", "id": rid,
                "result": {"tools": [
                    {"name": "read_thing",
                     "description": "read a thing",
                     "inputSchema": {"type": "object", "properties": {
                         "id": {"type": "string"}}},
                     "annotations": {"readOnlyHint": True}},
                    {"name": "write_thing",
                     "description": "write a thing",
                     "inputSchema": {"type": "object", "properties": {
                         "text": {"type": "string"}}}}]}}])
            return
        if method == "tools/call":
            name = (req.get("params") or {}).get("name")
            args = (req.get("params") or {}).get("arguments") or {}
            if name == "write_thing":
                self._sse(200, [{
                    "jsonrpc": "2.0", "id": rid,
                    "result": {"isError": True, "content": [
                        {"type": "text", "text": "denied"}]}}])
                return
            self._sse(200, [{
                "jsonrpc": "2.0", "id": rid,
                "result": {"content": [
                    {"type": "text",
                     "text": "thing:" + str(args.get("id"))}],
                    "structuredContent": {"echo": args}}}])
            return
        self._sse(200, [{"jsonrpc": "2.0", "id": rid,
                         "error": {"code": -32601,
                                   "message": "no such method"}}])


class FakeOAuthMcp:
    def __init__(self):
        self.authorize_calls = []
        self.registrations = []
        self.token_forms = []
        self.mcp_calls = []
        self.srv = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), _OAuthMcpHandler)
        self.srv.fx = self
        self.srv.daemon_threads = True
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever,
                         daemon=True).start()

    @property
    def base(self):
        return f"http://127.0.0.1:{self.port}"

    @property
    def mcp_url(self):
        return self.base + "/mcp"

    def as_meta(self):
        return {
            "issuer": self.base,
            "authorization_endpoint": self.base + "/authorize",
            "token_endpoint": self.base + "/token",
            "registration_endpoint": self.base + "/register",
            "grant_types_supported": ["authorization_code",
                                      "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none"]}

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


# ---------------------------------------------------------------------------
# fake Composio API v3.1

class _ComposioHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    @property
    def fx(self):
        return self.server.fx

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        p = urllib.parse.urlsplit(self.path)
        if p.path == "/api/v3.1/connected_accounts/ca-1":
            self._json(200, {"id": "ca-1",
                             "status": self.fx.status,
                             "toolkit": {"slug": "gmail"}})
            return
        if p.path == "/api/v3.1/tools":
            self.fx.tool_queries.append(
                {k: v for k, v in
                 urllib.parse.parse_qs(p.query).items()})
            self._json(200, {"items": FakeComposio.TOOLS})
            return
        if p.path == "/api/v3.1/toolkits":
            q = urllib.parse.parse_qs(p.query)
            self.fx.toolkit_queries.append(q)
            term = (q.get("search") or [""])[0].lower()
            items = [t for t in FakeComposio.TOOLKITS
                     if not term or term in t["slug"]
                     or term in t["name"].lower()]
            self._json(200, {"items": items})
            return
        self._json(404, {"error": "no page"})

    def do_POST(self):
        p = urllib.parse.urlsplit(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        self.fx.calls.append({"path": p.path, "body": body,
                              "key": self.headers.get("x-api-key")})
        if p.path == "/api/v3.1/tool_router/session":
            self._json(200, {"session_id": "sess-1"})
            return
        if p.path == "/api/v3.1/tool_router/session/sess-1/link":
            self._json(200, {
                "redirect_url":
                    "https://connect.composio.dev/link/ln_fake",
                "connected_account_id": "ca-1"})
            return
        if p.path.startswith("/api/v3.1/tools/execute/"):
            slug = urllib.parse.unquote(p.path.rsplit("/", 1)[-1])
            self._json(200, {"data": {"executed": slug,
                                      "args": body.get("arguments")},
                             "successful": True})
            return
        self._json(404, {"error": "no page"})


class FakeComposio:
    # reads, writes, a tag-flagged read and a multi-word-verb tool —
    # enough surface to exercise the read-only classifier
    TOOLS = [
        {"slug": "GMAIL_FETCH_EMAILS", "description": "fetch emails",
         "input_parameters": {"type": "object", "properties": {
            "query": {"type": "string"},
            "max_results": {"type": "integer"}}}},
        {"slug": "GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID",
         "description": "fetch one message by id",
         "input_parameters": {"type": "object",
            "required": ["message_id"],
            "properties": {"message_id": {"type": "string"}}}},
        {"slug": "GMAIL_GET_PROFILE", "description": "mailbox profile",
         "input_parameters": {"type": "object"}},
        {"slug": "GMAIL_SEND_EMAIL", "description": "send an email",
         "input_parameters": {"type": "object",
            "required": ["recipient_email", "subject"],
            "properties": {"recipient_email": {"type": "string"},
                           "subject": {"type": "string"}}}},
        {"slug": "GMAIL_CREATE_EMAIL_DRAFT",
         "description": "create a draft",
         "input_parameters": {"type": "object"}},
        {"slug": "GMAIL_DELETE_EMAIL", "description": "trash a message",
         "input_parameters": {"type": "object"}},
        {"slug": "GMAIL_BATCH_MODIFY", "description": "tag-only read",
         "tags": ["read"],
         "input_parameters": {"type": "object"}},
        {"slug": "GOOGLECALENDAR_FIND_EVENT",
         "description": "find calendar events",
         "input_parameters": {"type": "object",
            "required": ["calendar_id"],
            "properties": {"calendar_id": {"type": "string"}}}},
        {"slug": "GOOGLECALENDAR_CREATE_EVENT",
         "description": "create a calendar event",
         "input_parameters": {"type": "object"}},
    ]

    # a slice of the ~1000-app catalog — enough to prove /connect
    # autocomplete merges live results with the built-in servers
    TOOLKITS = [
        {"slug": "gmail", "name": "Gmail"},
        {"slug": "googlecalendar", "name": "Google Calendar"},
        {"slug": "trello", "name": "Trello"},
        {"slug": "airtable", "name": "Airtable"},
        {"slug": "spotify", "name": "Spotify"},
    ]

    def __init__(self):
        self.status = "INITIALIZING"
        self.calls = []
        self.tool_queries = []
        self.toolkit_queries = []
        self.srv = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), _ComposioHandler)
        self.srv.fx = self
        self.srv.daemon_threads = True
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever,
                         daemon=True).start()

    @property
    def base(self):
        return f"http://127.0.0.1:{self.port}/api/v3.1"

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


# ---------------------------------------------------------------------------

def _browser_get(url):
    """'Browser': GET that does not follow redirects. -> (status,
    headers)."""
    import http.client
    p = urllib.parse.urlsplit(url)
    c = http.client.HTTPConnection(p.hostname, p.port, timeout=10)
    c.request("GET", p.path + ("?" + p.query if p.query else ""))
    r = c.getresponse()
    r.read()
    out = (r.status, {k.lower(): v for k, v in r.getheaders()})
    c.close()
    return out


class Base(unittest.TestCase):
    def setUp(self):
        self.fx = FakeOAuthMcp()
        self.env = TmpEnv(
            PA_PUBLIC_URL="http://127.0.0.1:8080",
            **SECRETS)
        self.env.conn().close()

    def tearDown(self):
        self.fx.close()
        connectflows._TOOLS_CACHE.clear()

    # drive a full connect through the fake authorize endpoint
    def _connect_via_browser(self, app="url:" + "x"):
        conn = self.env.conn()
        try:
            res = connectflows.start_connect(
                conn, self.env.settings, self.env.env, app)
        finally:
            conn.close()
        self.assertTrue(res["url"], res)
        status, headers = _browser_get(res["url"])
        self.assertEqual(status, 302)
        q = urllib.parse.parse_qs(
            urllib.parse.urlsplit(headers["location"]).query)
        conn = self.env.conn()
        try:
            return connectflows.complete_callback(
                conn, self.env.settings, self.env.env, q), res
        finally:
            conn.close()


class TestSecretsBox(Base):
    def test_roundtrip_and_tamper(self):
        blob = secretsbox.seal(self.env.env, "aad", {"t": "secret"})
        self.assertTrue(blob.startswith("pasec1."))
        self.assertNotIn("secret", blob)
        self.assertEqual(
            secretsbox.open_blob(self.env.env, "aad", blob)["t"],
            "secret")
        # wrong aad -> fail
        with self.assertRaises(secretsbox.SecretsBoxError):
            secretsbox.open_blob(self.env.env, "other", blob)
        # wrong key -> fail
        with self.assertRaises(secretsbox.SecretsBoxError):
            secretsbox.open_blob(
                {"PA_SECRETS_KEY": "cd" * 32}, "aad", blob)

    def test_key_required_and_shape(self):
        with self.assertRaises(secretsbox.SecretsKeyMissing):
            secretsbox.load_key({})
        with self.assertRaises(secretsbox.SecretsBoxError):
            secretsbox.load_key({"PA_SECRETS_KEY": "short"})
        # connect refuses without the key (never writes plaintext)
        conn = self.env.conn()
        try:
            with self.assertRaises(secretsbox.SecretsKeyMissing):
                connectflows.start_connect(
                    conn, self.env.settings, {}, "notion")
        finally:
            conn.close()


class TestMcpConnect(Base):
    def _start(self):
        return self._connect_via_browser(
            f"url:{self.fx.mcp_url}")

    def test_full_flow_discovery_dcr_pkce_tools(self):
        (ok, title, _m, provider), res = self._start()
        self.assertTrue(ok, title)
        self.assertTrue(provider.startswith("url:"))
        # authorize URL carried PKCE + state + resource
        call = self.fx.authorize_calls[0]
        self.assertEqual(call["code_challenge_method"], ["S256"])
        self.assertEqual(call["response_type"], ["code"])
        self.assertIn("state", call)
        self.assertIn("resource", call)
        # DCR happened once and is cached for the same issuer+redirect
        self.assertEqual(len(self.fx.registrations), 1)
        form = self.fx.token_forms[0]
        self.assertEqual(form["grant_type"], "authorization_code")
        self.assertIn("code_verifier", form)       # PKCE
        # row is active, token sealed (not plaintext anywhere)
        conn = self.env.conn()
        try:
            row = connectflows.get(conn, provider)
            self.assertEqual(row["status"], "active")
            self.assertEqual(row["kind"], "mcp")
            self.assertTrue(row["token_blob"].startswith("pasec1."))
            self.assertNotIn("at-1", row["token_blob"])
            # tools list works with the sealed token
            tools = connectflows.list_tools(
                conn, self.env.settings, self.env.env, row)
            names = [t["name"] for t in tools]
            self.assertEqual(names, ["read_thing", "write_thing"])
            self.assertTrue(tools[0]["read_only"])
            self.assertFalse(tools[1]["read_only"])
            # session was minted + carried on later calls
            self.assertIn("sess-1", {c["session"]
                                     for c in self.fx.mcp_calls})
            # a tool call round-trips
            out = connectflows.call_tool(
                conn, self.env.settings, self.env.env, row,
                "read_thing", {"id": "42"})
            self.assertEqual(out["text"], "thing:42")
        finally:
            conn.close()

    def test_state_single_use_and_expired(self):
        conn = self.env.conn()
        res = connectflows.start_connect(
            conn, self.env.settings, self.env.env,
            f"url:{self.fx.mcp_url}")
        conn.close()
        q = urllib.parse.parse_qs(
            urllib.parse.urlsplit(res["url"]).query)
        state = q["state"][0]
        # replay with a code: first consumes, second refuses
        conn = self.env.conn()
        ok1, *_ = connectflows.complete_callback(
            conn, self.env.settings, self.env.env,
            {"state": [state], "code": ["fake-code-123"]})
        ok2, t2, m2, _ = connectflows.complete_callback(
            conn, self.env.settings, self.env.env,
            {"state": [state], "code": ["fake-code-123"]})
        self.assertTrue(ok1)
        self.assertFalse(ok2)
        self.assertIn("expired", (t2 + m2).lower())
        # expired row refuses
        res2 = connectflows.start_connect(
            conn, self.env.settings, self.env.env,
            f"url:{self.fx.mcp_url}")
        st2 = urllib.parse.parse_qs(
            urllib.parse.urlsplit(res2["url"]).query)["state"][0]
        conn.execute(
            "UPDATE oauth_states SET expires_at=? WHERE state=?",
            ("2000-01-01T00:00:00+00:00", st2))
        ok3, *_ = connectflows.complete_callback(
            conn, self.env.settings, self.env.env,
            {"state": [st2], "code": ["x"]})
        self.assertFalse(ok3)
        conn.close()

    def test_refresh_renews_sealed_token(self):
        (ok, _t, _m, provider), _ = self._start()
        self.assertTrue(ok)
        conn = self.env.conn()
        row = connectflows.get(conn, provider)
        # force expiry -> refresh grant
        conn.execute(
            "UPDATE connections SET expires_at=? WHERE provider=?",
            ("2000-01-01T00:00:00+00:00", provider))
        row = connectflows.get(conn, provider)
        tok = connectflows.access_token(
            conn, self.env.settings, self.env.env, row)
        self.assertEqual(tok, "at-2")
        grants = [f["grant_type"] for f in self.fx.token_forms]
        self.assertIn("refresh_token", grants)
        conn.close()

    def test_disconnect_deletes_tokens(self):
        (ok, _t, _m, provider), _ = self._start()
        self.assertTrue(ok)
        conn = self.env.conn()
        row = connectflows.disconnect(conn, self.env.env, provider)
        self.assertIsNotNone(row)
        self.assertIsNone(connectflows.get(conn, provider))
        conn.close()

    def test_find_connection_dotted_provider(self):
        (ok, _t, _m, provider), _ = self._start()
        self.assertTrue(ok)
        conn = self.env.conn()
        row, tool = connectflows.find_connection_for_tool(
            conn, provider + ".read_thing")
        self.assertIsNotNone(row)
        self.assertEqual(tool, "read_thing")
        row, tool = connectflows.find_connection_for_tool(
            conn, "nobody.x")
        self.assertIsNone(row)
        conn.close()

    def test_http_only_for_private_hosts(self):
        conn = self.env.conn()
        try:
            with self.assertRaises(connectflows.ConnectError):
                connectflows.start_connect(
                    conn, self.env.settings, self.env.env,
                    "url:http://169.254.1.1/mcp")
        finally:
            conn.close()


class TestChatToolsGating(Base):
    def _ctx(self, approve="approved"):
        conn = self.env.conn()
        self.addCleanup(conn.close)
        decisions = []

        def req(name, args, aid):
            decisions.append((name, aid))
            return approve

        ctx = chattools.ChatToolContext(
            conn, self.env.settings, None, "chan-1",
            env=self.env.env, request_approval=req)
        ctx._decisions = decisions
        return ctx

    def _connect(self):
        (ok, _t, _m, provider), _ = self._connect_via_browser(
            f"url:{self.fx.mcp_url}")
        self.assertTrue(ok)
        return provider

    def test_readonly_runs_free_write_is_gated(self):
        provider = self._connect()
        ctx = self._ctx(approve="approved")
        out = chattools.run(ctx, provider + ".read_thing", {"id": "7"})
        self.assertEqual(out.get("text"), "thing:7")
        self.assertEqual(ctx._decisions, [])          # no approval ask
        out = chattools.run(ctx, provider + ".write_thing",
                            {"text": "hi"})
        self.assertEqual(ctx._decisions[0][0],
                         provider + ".write_thing")    # gated

    def test_write_denied_never_calls_server(self):
        provider = self._connect()
        ctx = self._ctx(approve="denied")
        before = len([c for c in self.fx.mcp_calls
                      if c["method"] == "tools/call"])
        out = chattools.run(ctx, provider + ".write_thing",
                            {"text": "hi"})
        self.assertEqual(out["error"], "not_approved")
        after = len([c for c in self.fx.mcp_calls
                     if c["method"] == "tools/call"])
        self.assertEqual(before, after)

    def test_app_tools_listing(self):
        provider = self._connect()
        ctx = self._ctx()
        out = chattools.run(ctx, "app_tools", {"app": provider})
        self.assertEqual(out["count"], 2)
        self.assertTrue(any("read-only" in l for l in out["tools"]))
        out = chattools.run(ctx, "app_tools", {"app": "nope"})
        self.assertEqual(out["error"], "not_connected")

    def test_unknown_namespaced_tool(self):
        ctx = self._ctx()
        out = chattools.run(ctx, "zzz.not_a_tool", {})
        self.assertEqual(out["error"], "unknown_tool")


class TestErrorsUnchangedWithoutComposio(Base):
    """No own OAuth app AND no COMPOSIO_API_KEY -> the original
    ConnectError text is untouched."""

    def test_github_client_env_error(self):
        conn = self.env.conn()
        try:
            with self.assertRaises(connectflows.ConnectError) as cm:
                connectflows.start_connect(
                    conn, self.env.settings, self.env.env, "github")
            self.assertIn("PA_GITHUB_CLIENT_ID", str(cm.exception))
        finally:
            conn.close()

    def test_google_client_error(self):
        conn = self.env.conn()
        try:
            with self.assertRaises(connectflows.ConnectError) as cm:
                connectflows.start_connect(
                    conn, self.env.settings, self.env.env, "google")
            self.assertIn("needs your own OAuth client",
                          str(cm.exception))
        finally:
            conn.close()


class TestGoogleFlow(Base):
    def test_connect_google_then_gmail_reads_db(self):
        env = TmpEnv(
            PA_PUBLIC_URL="http://127.0.0.1:8080",
            PA_GOOGLE_CLIENT_ID="gcid",
            PA_GOOGLE_CLIENT_SECRET="gcsec",
            GOOGLE_AUTH_URL=self.fx.base + "/authorize",
            GMAIL_TOKEN_URL=self.fx.base + "/token",
            **SECRETS)
        conn = env.conn()
        res = connectflows.start_connect(
            conn, env.settings, env.env, "google")
        conn.close()
        self.assertIn("access_type=offline", res["url"])
        self.assertIn("gmail.readonly", res["url"])
        self.assertIn("calendar.events", res["url"])
        status, headers = _browser_get(res["url"])
        self.assertEqual(status, 302)
        q = urllib.parse.parse_qs(
            urllib.parse.urlsplit(headers["location"]).query)
        conn = env.conn()
        ok, *_ = connectflows.complete_callback(
            conn, env.settings, env.env, q)
        self.assertTrue(ok)
        # gmail.credentials falls through to the sealed db grant
        from pa import gmail as gmailmod
        creds = gmailmod.credentials(env.env, conn=conn)
        self.assertEqual(creds["client_id"], "gcid")
        self.assertEqual(creds["refresh_token"], "rt-1")
        conn.close()


class TestWebCallback(Base):
    def test_callback_page_and_notify(self):
        env = TmpEnv(
            PA_PUBLIC_URL="http://127.0.0.1:8080",
            DISCORD_OWNER_ID="", **SECRETS)
        app = webapp.App(env.settings, env=env.env)
        client = WSGIClient(app)
        conn = env.conn()
        res = connectflows.start_connect(
            conn, env.settings, env.env, f"url:{self.fx.mcp_url}")
        conn.close()
        state = urllib.parse.parse_qs(
            urllib.parse.urlsplit(res["url"]).query)["state"][0]
        status, _h, body = client.get(
            f"/oauth/callback?code=fake-code-123&state={state}")
        self.assertEqual(status, "200 OK")
        self.assertIn(b"close this tab", body)
        # replay -> 400 friendly error
        status, _h, body = client.get(
            f"/oauth/callback?code=fake-code-123&state={state}")
        self.assertEqual(status, "400 Bad Request")
        self.assertIn(b"expired", body.lower())
        # missing state -> 400
        status, _h, _b = client.get("/oauth/callback?state=bogus")
        self.assertEqual(status, "400 Bad Request")
        # provider error surfaces
        status, _h, body = client.get(
            "/oauth/callback?error=access_denied")
        self.assertEqual(status, "400 Bad Request")
        self.assertIn(b"access_denied", body)


class TestNoAuth(Base):
    def test_no_auth_connects_instantly(self):
        env = TmpEnv(**SECRETS)
        conn = env.conn()
        entry = {"url": self.fx.mcp_url.replace("/mcp", "/open"),
                 "label": "open", "auth": "none"}
        # the fake requires auth on /mcp only — add a no-auth path:
        # simplest: use a catalog override pointing at a tiny no-auth
        # endpoint the fixture does not serve; instead verify the
        # auth='none' path hits initialize. Our fixture 401s /mcp, so
        # serve an open endpoint by monkeypatching path.
        conn.close()
        # exercise the real path with a no-auth fixture path
        class Open(_OAuthMcpHandler):
            def do_POST(self):
                if self.path == "/open":
                    n = int(self.headers.get("Content-Length") or 0)
                    req = json.loads(self.rfile.read(n) or b"{}")
                    self._mcp(req)
                    return
                super().do_POST()

        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Open)
        srv.fx = self.fx
        srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        open_url = f"http://127.0.0.1:{srv.server_address[1]}/open"
        conn = env.conn()
        connectflows.MCP_CATALOG["fake-open"] = {
            "label": "Open", "url": open_url, "auth": "none"}
        try:
            res = connectflows.start_connect(
                conn, env.settings, env.env, "fake-open")
            self.assertEqual(res["url"], "")
            row = connectflows.get(conn, "fake-open")
            self.assertEqual(row["status"], "active")
            tools = connectflows.list_tools(
                conn, env.settings, env.env, row)
            self.assertEqual(len(tools), 2)
        finally:
            del connectflows.MCP_CATALOG["fake-open"]
            conn.close()


class TestComposio(Base):
    def setUp(self):
        super().setUp()
        self.cx = FakeComposio()
        self.addCleanup(self.cx.close)
        self.env = TmpEnv(
            PA_PUBLIC_URL="http://127.0.0.1:8080",
            COMPOSIO_API_KEY="ck-test",
            COMPOSIO_API_BASE=self.cx.base,
            **SECRETS)

    def test_link_poll_activate_execute_and_counter(self):
        conn = self.env.conn()
        res = connectflows.start_connect(
            conn, self.env.settings, self.env.env, "gmail")
        self.assertEqual(res["kind"], "composio")
        self.assertIn("connect.composio.dev", res["url"])
        # api key went to the fake
        self.assertEqual(self.cx.calls[0]["key"], "ck-test")
        # pending -> callback reports 'still finishing'; active -> ok
        self.cx.status = "ACTIVE"
        q = {"state": [res["state"]]}
        ok, title, _m, prov = connectflows.complete_callback(
            conn, self.env.settings, self.env.env, q)
        self.assertTrue(ok, title)
        row = connectflows.get(conn, "gmail")
        self.assertEqual(row["kind"], "composio")
        # tools + execute via the fake — the v3 filter is toolkit_slug
        tools = connectflows.list_tools(
            conn, self.env.settings, self.env.env, row)
        self.assertEqual(tools[0]["name"], "GMAIL_FETCH_EMAILS")
        self.assertIn({"toolkit_slug": ["gmail"], "limit": ["200"]},
                      self.cx.tool_queries)
        self.assertNotIn("toolkit", {
            k for q in self.cx.tool_queries for k in q}
            - {"toolkit_slug"})
        by_name = {t["name"]: t for t in tools}
        self.assertTrue(by_name["GMAIL_FETCH_EMAILS"]["read_only"])
        self.assertTrue(by_name["GMAIL_GET_PROFILE"]["read_only"])
        self.assertTrue(by_name["GMAIL_BATCH_MODIFY"]["read_only"])
        self.assertFalse(by_name["GMAIL_SEND_EMAIL"]["read_only"])
        self.assertFalse(
            by_name["GMAIL_CREATE_EMAIL_DRAFT"]["read_only"])
        self.assertFalse(by_name["GMAIL_DELETE_EMAIL"]["read_only"])
        self.assertTrue(
            by_name["GOOGLECALENDAR_FIND_EVENT"]["read_only"])
        self.assertFalse(
            by_name["GOOGLECALENDAR_CREATE_EVENT"]["read_only"])
        self.assertEqual(composiomod.usage_count(conn), 0)
        out = connectflows.call_tool(
            conn, self.env.settings, self.env.env, row,
            "GMAIL_FETCH_EMAILS", {"q": "x"})
        self.assertEqual(out["result"]["executed"],
                         "GMAIL_FETCH_EMAILS")
        self.assertEqual(out["result"]["args"], {"q": "x"})
        self.assertEqual(composiomod.usage_count(conn), 1)
        exec_calls = [c for c in self.cx.calls
                      if "execute" in c["path"]]
        self.assertEqual(
            exec_calls[0]["body"]["connected_account_id"], "ca-1")
        conn.close()

    def test_warn_threshold(self):
        s = self.env.settings
        self.assertFalse(composiomod.over_warn_threshold(s, 1))
        conn = self.env.conn()
        conn.execute(
            "INSERT INTO composio_usage(month, calls) VALUES(?,?)",
            (composiomod._month(), int(s.composio_monthly_limit * 0.8)))
        self.assertTrue(composiomod.over_warn_threshold(
            s, composiomod.usage_count(conn)))
        conn.close()

    def test_composio_read_free_write_gated_in_chat(self):
        conn = self.env.conn()
        connectflows.activate_composio(
            conn, "gmail", {"connected_account_id": "ca-1",
                            "toolkit": "gmail"})
        decisions = []
        ctx = chattools.ChatToolContext(
            conn, self.env.settings, None, "c",
            env=self.env.env,
            request_approval=lambda n, a, i: decisions.append(n)
            or "denied")
        # read-only verb -> runs free, no approval prompt at all
        out = chattools.run(ctx, "gmail.GMAIL_FETCH_EMAILS",
                            {"max_results": 1})
        self.assertEqual(out["result"]["executed"],
                         "GMAIL_FETCH_EMAILS")
        self.assertEqual(decisions, [])
        # write verb -> still gated
        out = chattools.run(ctx, "gmail.GMAIL_SEND_EMAIL",
                            {"recipient_email": "a@b.c",
                             "subject": "s", "body": "b"})
        self.assertEqual(out["error"], "not_approved")
        self.assertEqual(decisions, ["gmail.GMAIL_SEND_EMAIL"])
        conn.close()

    # -- fallbacks: no own OAuth app, but COMPOSIO_API_KEY is set -------

    def test_github_falls_back_to_composio(self):
        conn = self.env.conn()
        try:
            res = connectflows.start_connect(
                conn, self.env.settings, self.env.env, "github")
            self.assertEqual(res["kind"], "composio")
            self.assertEqual(res["provider"], "github")
            self.assertEqual(res["meta"]["toolkit"], "github")
            self.assertIn("connect.composio.dev", res["url"])
            link = [c for c in self.cx.calls
                    if c["path"].endswith("/link")]
            self.assertEqual(link[0]["body"]["toolkit"], "github")
            # completes through the usual composio callback path
            self.cx.status = "ACTIVE"
            ok, title, _m, prov = connectflows.complete_callback(
                conn, self.env.settings, self.env.env,
                {"state": [res["state"]]})
            self.assertTrue(ok, title)
            self.assertEqual(prov, "github")
            row = connectflows.get(conn, "github")
            self.assertEqual(row["kind"], "composio")
            self.assertEqual(row["status"], "active")
        finally:
            conn.close()

    def test_google_falls_back_to_composio_gmail(self):
        conn = self.env.conn()
        try:
            res = connectflows.start_connect(
                conn, self.env.settings, self.env.env, "google")
            self.assertEqual(res["kind"], "composio")
            self.assertEqual(res["provider"], "gmail")
            self.assertEqual(res["meta"]["toolkit"], "gmail")
            self.assertIn("googlecalendar", res["note"])
        finally:
            conn.close()

    def test_github_own_client_beats_composio(self):
        env = TmpEnv(
            PA_PUBLIC_URL="http://127.0.0.1:8080",
            PA_GITHUB_CLIENT_ID="ghid",
            PA_GITHUB_CLIENT_SECRET="ghsec",
            COMPOSIO_API_KEY="ck-test",
            COMPOSIO_API_BASE=self.cx.base,
            **SECRETS)
        conn = env.conn()
        try:
            # PA_GITHUB_* is set -> the self-hosted MCP path runs;
            # stub discovery so the test never leaves the sandbox.
            with mock.patch.object(
                    connectflows.oauth, "discover",
                    side_effect=connectflows.oauth.OAuthError("stub")):
                with self.assertRaises(connectflows.ConnectError) as cm:
                    connectflows.start_connect(
                        conn, env.settings, env.env, "github")
            self.assertIn("discovery failed", str(cm.exception))
            self.assertEqual(self.cx.calls, [])
        finally:
            conn.close()

    def test_google_own_client_beats_composio(self):
        env = TmpEnv(
            PA_PUBLIC_URL="http://127.0.0.1:8080",
            PA_GOOGLE_CLIENT_ID="gcid",
            PA_GOOGLE_CLIENT_SECRET="gcsec",
            COMPOSIO_API_KEY="ck-test",
            COMPOSIO_API_BASE=self.cx.base,
            GOOGLE_AUTH_URL=self.fx.base + "/authorize",
            GMAIL_TOKEN_URL=self.fx.base + "/token",
            **SECRETS)
        conn = env.conn()
        try:
            res = connectflows.start_connect(
                conn, env.settings, env.env, "google")
            self.assertEqual(res["kind"], "google")
            self.assertTrue(
                res["url"].startswith(self.fx.base), res["url"])
            self.assertEqual(self.cx.calls, [])
        finally:
            conn.close()

    def test_connect_autocomplete_offers_google_toolkits(self):
        from pa.discordbot import DiscordBot
        env = TmpEnv(
            PA_PUBLIC_URL="http://127.0.0.1:8080",
            DISCORD_BOT_TOKEN="t", DISCORD_OWNER_ID="owner-1",
            COMPOSIO_API_KEY="ck-test",
            COMPOSIO_API_BASE=self.cx.base,
            **SECRETS)
        bot = DiscordBot(env.settings, env=env.env, notifier=False)
        conn = env.conn()
        try:
            calls = []

            class FakeAPI:
                def interaction_autocomplete(self, iid, tok, choices):
                    calls.append(choices)

            bot.api = FakeAPI()
            bot._handle_autocomplete(conn, {
                "id": "i", "token": "t",
                "data": {"name": "connect",
                         "options": [{"focused": True,
                                      "value": "g"}]}})
            values = {c[1] if isinstance(c, (tuple, list))
                      else c.get("value", c) for c in calls[-1]}
            for name in ("gmail", "googlecalendar", "google",
                         "github"):
                self.assertIn(name, values)
        finally:
            conn.close()
            bot.stop()


    # -- Bug: browser lands on "link expired" after Composio success ---

    def _start_gmail_link(self):
        conn = self.env.conn()
        res = connectflows.start_connect(
            conn, self.env.settings, self.env.env, "gmail")
        conn.close()
        link = [c for c in self.cx.calls
                if c["path"].endswith("/link")][0]
        cb = link["body"]["callback_url"]
        return res, urllib.parse.parse_qs(
            urllib.parse.urlsplit(cb).query)

    def test_composio_callback_url_carries_state_and_app(self):
        res, q = self._start_gmail_link()
        self.assertEqual(q["state"], [res["state"]])
        self.assertEqual(q["app"], ["gmail"])

    def test_callback_after_poll_activation_shows_connected(self):
        # the Discord poller activates first and the state row is gone
        # by the time the browser redirect arrives — the page must
        # still say Connected, not "link expired"
        _res, q = self._start_gmail_link()
        conn = self.env.conn()
        connectflows.activate_composio(
            conn, "gmail", {"connected_account_id": "ca-1",
                            "toolkit": "gmail"})
        conn.execute("DELETE FROM oauth_states")
        ok, title, _m, prov = connectflows.complete_callback(
            conn, self.env.settings, self.env.env, q)
        self.assertTrue(ok, title)
        self.assertIn("connect", title.lower())
        self.assertEqual(prov, "")      # no duplicate Discord notify
        # and through the web page too
        app = webapp.App(self.env.settings, env=self.env.env)
        status, _h, body = WSGIClient(app).get(
            "/oauth/callback?state=" + q["state"][0] + "&app=gmail")
        self.assertEqual(status, "200 OK")
        self.assertIn(b"connected", body.lower())
        conn.close()

    def test_callback_refresh_still_shows_connected(self):
        res = connectflows.start_connect(
            self.env.conn(), self.env.settings, self.env.env, "gmail")
        self.cx.status = "ACTIVE"
        q = {"state": [res["state"]], "app": ["gmail"]}
        conn = self.env.conn()
        ok1, *_ = connectflows.complete_callback(
            conn, self.env.settings, self.env.env, q)
        ok2, title, _m, _p = connectflows.complete_callback(
            conn, self.env.settings, self.env.env, q)
        self.assertTrue(ok1)
        self.assertTrue(ok2, title)
        # but a genuinely-unused, never-connected link still 400s
        ok3, t3, _m, _p = connectflows.complete_callback(
            conn, self.env.settings, self.env.env,
            {"state": ["bogus"], "app": ["nope"]})
        self.assertFalse(ok3)
        self.assertIn("expired", t3.lower())
        conn.close()

    # -- Bug: built-in email_*/calendar_* route through Composio -------

    def _chat_ctx(self, approve="approved", decisions=None):
        conn = self.env.conn()
        self.addCleanup(conn.close)
        return chattools.ChatToolContext(
            conn, self.env.settings, None, "c", env=self.env.env,
            request_approval=(
                lambda n, a, i: (decisions.append(n), approve)[1]
                if decisions is not None else approve))

    def _activate_gmail(self):
        conn = self.env.conn()
        connectflows.activate_composio(
            conn, "gmail", {"connected_account_id": "ca-1",
                            "toolkit": "gmail",
                            "session_id": "sess-1"})
        conn.close()

    def _exec_calls(self, slug=""):
        return [c for c in self.cx.calls
                if c["path"].startswith("/api/v3.1/tools/execute/")
                and (not slug or c["path"].endswith("/" + slug))]

    def test_email_list_routes_to_composio(self):
        self._activate_gmail()
        ctx = self._chat_ctx()
        out = chattools.run(ctx, "email_list",
                            {"query": "is:unread", "max_results": 3})
        self.assertEqual(out["result"]["executed"],
                         "GMAIL_FETCH_EMAILS")
        call = self._exec_calls("GMAIL_FETCH_EMAILS")[0]
        args = call["body"]["arguments"]
        self.assertEqual(args["query"], "is:unread")
        self.assertEqual(args["max_results"], 3)

    def test_email_read_routes_to_composio(self):
        self._activate_gmail()
        ctx = self._chat_ctx()
        out = chattools.run(ctx, "email_read", {"id": "m-1"})
        self.assertEqual(out["result"]["executed"],
                         "GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID")
        self.assertEqual(
            self._exec_calls("GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID")
            [0]["body"]["arguments"]["message_id"], "m-1")

    def test_draft_email_routes_to_composio(self):
        self._activate_gmail()
        decisions = []
        ctx = self._chat_ctx(approve="approved", decisions=decisions)
        out = chattools.run(ctx, "draft_email",
                            {"to": "a@b.c", "subject": "s",
                             "body": "b"})
        self.assertEqual(out["result"]["executed"],
                         "GMAIL_CREATE_EMAIL_DRAFT")
        args = self._exec_calls("GMAIL_CREATE_EMAIL_DRAFT")[0][
            "body"]["arguments"]
        self.assertEqual(args["recipient_email"], "a@b.c")
        self.assertEqual(decisions, [])      # drafts stay ungated

    def test_send_email_via_composio_still_needs_approval(self):
        self._activate_gmail()
        ctx = self._chat_ctx(approve="denied", decisions=[])
        out = chattools.run(ctx, "send_email",
                            {"to": "a@b.c", "subject": "s",
                             "body": "b"})
        self.assertEqual(out["error"], "not_approved")
        self.assertEqual(self._exec_calls("GMAIL_SEND_EMAIL"), [])

    def test_calendar_list_routes_to_composio(self):
        conn = self.env.conn()
        connectflows.activate_composio(
            conn, "googlecalendar",
            {"connected_account_id": "ca-1",
             "toolkit": "googlecalendar"})
        conn.close()
        ctx = self._chat_ctx()
        out = chattools.run(ctx, "calendar_list", {"days_ahead": 3})
        self.assertEqual(out["result"]["executed"],
                         "GOOGLECALENDAR_FIND_EVENT")
        args = self._exec_calls("GOOGLECALENDAR_FIND_EVENT")[0][
            "body"]["arguments"]
        self.assertEqual(args["calendar_id"], "primary")
        self.assertIn("timeMin", args)

    def test_calendar_create_via_composio_needs_approval(self):
        conn = self.env.conn()
        connectflows.activate_composio(
            conn, "googlecalendar",
            {"connected_account_id": "ca-1",
             "toolkit": "googlecalendar"})
        conn.close()
        ctx = self._chat_ctx(approve="denied", decisions=[])
        out = chattools.run(ctx, "calendar_create",
                            {"summary": "s",
                             "start": "2026-11-12T10:00",
                             "end": "2026-11-12T11:00"})
        self.assertEqual(out["error"], "not_approved")
        self.assertEqual(
            self._exec_calls("GOOGLECALENDAR_CREATE_EVENT"), [])

    def test_not_configured_message_points_at_connection(self):
        ctx = self._chat_ctx()
        out = chattools.run(ctx, "email_list", {})
        self.assertEqual(out["error"], "email_not_configured")
        self.assertIn("gmail-auth", out["message"])
        # a present-but-inactive composio gmail row -> /connect hint,
        # not the native gmail-auth flow
        self._activate_gmail()
        conn = self.env.conn()
        conn.execute("UPDATE connections SET status='expired'"
                     " WHERE provider='gmail'")
        conn.close()
        ctx = self._chat_ctx()
        out = chattools.run(ctx, "email_list", {})
        self.assertEqual(out["error"], "email_not_configured")
        self.assertIn("/connect gmail", out["message"])
        self.assertNotIn("gmail-auth", out["message"])

    def test_app_tools_compact_and_filterable(self):
        self._activate_gmail()
        ctx = self._chat_ctx()
        out = chattools.run(ctx, "app_tools", {"app": "gmail"})
        self.assertTrue(all(len(l) < 400 for l in out["tools"]))
        out = chattools.run(
            ctx, "app_tools", {"app": "gmail", "query": "send"})
        self.assertEqual(out["count"], 1)
        self.assertIn("GMAIL_SEND_EMAIL", out["tools"][0])


class TestComposioReadOnlyUnit(unittest.TestCase):
    def test_verb_classification(self):
        ro = composiomod.tool_read_only
        self.assertTrue(ro("GMAIL_FETCH_EMAILS", {}))
        self.assertTrue(ro("GMAIL_GET_PROFILE", {}, toolkit="gmail"))
        self.assertTrue(ro("X_Y_SEARCH_ITEMS", {}, toolkit="x_y"))
        self.assertFalse(ro("GMAIL_SEND_EMAIL", {}))
        self.assertFalse(ro("GMAIL_CREATE_EMAIL_DRAFT", {}))
        self.assertFalse(ro("GMAIL_DELETE_EMAIL", {}))
        self.assertFalse(ro("NOVERB", {}))
        self.assertTrue(ro("A_B_C", {"annotations":
                                     {"readOnlyHint": True}}))
        self.assertTrue(ro("GMAIL_BATCH_MODIFY", {"tags": ["read"]}))
        self.assertFalse(ro("GMAIL_BATCH_MODIFY", {}))


class TestDiscordSurface(Base):
    """Command text + autocomplete without a live gateway."""

    def test_execute_and_autocomplete(self):
        from pa.discordbot import DiscordBot
        env = TmpEnv(
            PA_PUBLIC_URL="http://127.0.0.1:8080",
            DISCORD_BOT_TOKEN="t", DISCORD_OWNER_ID="owner-1",
            **SECRETS)
        env.conn().close()
        bot = DiscordBot(env.settings, env=env.env, notifier=False)
        conn = env.conn()
        try:
            # unknown app -> helpful catalog text
            reply = bot.execute(conn, ["connect", "bogus"])
            self.assertIn("unknown app", reply)
            self.assertIn("notion", reply)
            # usage + empty list paths
            self.assertIn("usage:", bot.execute(conn, ["connect"]))
            self.assertIn("Nothing connected",
                          bot.execute(conn, ["connections"]))
            self.assertIn("not connected",
                          bot.execute(conn, ["disconnect", "x"]))
            # autocomplete: catalog match (substring search now —
            # the same query also goes to Composio live)
            calls = []

            class FakeAPI:
                def interaction_autocomplete(self, iid, tok, choices):
                    calls.append(choices)

            bot.api = FakeAPI()
            bot._handle_autocomplete(conn, {
                "id": "i", "token": "t",
                "data": {"name": "connect",
                         "options": [{"focused": True, "value": "not"}]}})
            values = [c[1] if isinstance(c, (tuple, list))
                      else c.get("value", c) for c in calls[-1]]
            self.assertEqual(values, ["notion"])
            bot._handle_autocomplete(conn, {
                "id": "i", "token": "t",
                "data": {"name": "disconnect",
                         "options": [{"focused": True, "value": ""}]}})
            self.assertEqual(calls[-1], [])
        finally:
            conn.close()
            bot.stop()


if __name__ == "__main__":
    unittest.main()
