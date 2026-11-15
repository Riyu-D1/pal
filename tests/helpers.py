"""Shared test fixtures.

- TmpEnv: a temp DATA_DIR + test settings.
- ProviderFixture: a localhost OpenAI-compatible endpoint with scripted
  responses; records every request body.
- WebFixture: a localhost web server with pages, redirects, oversized and
  gzip-bomb bodies and a slow-loris endpoint.
- WSGIClient: drives the app in-process (cookies, CSRF, origin control).
- Fake resolver/dialer pair for egress tests: DNS answers are scripted,
  and the dialer maps the vetted (public) IP onto the local fixture while
  recording exactly which IP was dialled.
"""

import gzip
import http.server
import io
import json
import os
import socket
import tempfile
import threading
import time
import urllib.parse

from pa import db as dbmod
from pa.config import load_settings


class TmpEnv:
    def __init__(self, **overrides):
        self.dir = tempfile.mkdtemp(prefix="pa-test-")
        env = {
            "PA_ENV": "test",
            "PA_DATA_DIR": self.dir,
            "PA_LEASE_TTL_SECONDS": "2.0",
            "PA_HEARTBEAT_SECONDS": "0.2",
            "PA_WORKER_POLL_SECONDS": "0.05",
            "PA_TPM_LIMIT": "8000",
            "PA_RPM_LIMIT": "30",
        }
        env.update({k: str(v) for k, v in overrides.items()})
        self.env = env
        self.settings = load_settings(env)

    @property
    def db_path(self):
        return self.settings.db_path

    def conn(self):
        c = dbmod.connect(self.db_path)
        dbmod.migrate(c)
        return c

    def add_assistant(self, name="a1", tools=None, instructions="",
                      host="localhost", model=None):
        conn = self.conn()
        tools = tools if tools is not None else [
            "web_search", "fetch_url", "memory_search", "propose_memory"]
        cur = conn.execute(
            "INSERT INTO assistants(name, instructions, provider_host,"
            " provider_model, tools, created_at) VALUES(?,?,?,?,?,?)",
            (name, instructions, host, model or self.settings.dev_model,
             json.dumps(tools), "2026-01-01T00:00:00Z"))
        conn.close()
        return cur.lastrowid

    def add_owner(self, username="owner", password="correct horse 99"):
        from pa.passwords import hash_password
        conn = self.conn()
        conn.execute(
            "INSERT INTO users(username, password_hash, created_at)"
            " VALUES(?,?,?)", (username, hash_password(password),
                              "2026-01-01T00:00:00Z"))
        conn.close()
        return username, password


# ---------------------------------------------------------------------------

class _FixtureHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, code, obj, extra_headers=None):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _raw(self, code, payload, extra_headers=None):
        body = payload if isinstance(payload, bytes) else \
            str(payload).encode()
        self.send_response(code)
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        if "Content-Type" not in (extra_headers or {}):
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        fx = self.server.fixture
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n)
        fx.auths.append(self.headers.get("Authorization"))
        if self.path == "/v1/chat/completions":
            fx.calls.append(json.loads(body or b"{}"))
            item = fx.next_response()
            self._provider_reply(item)
            return
        if self.path == "/search":
            fx.calls.append(json.loads(body or b"{}"))
            item = fx.next_response("search")
            self._provider_reply(item)
            return
        if self.path.startswith("/gmail/"):
            fx.calls.append({"method": "POST", "path": self.path,
                             "headers": dict(self.headers),
                             "body": json.loads(body or b"{}")})
            item = fx.next_response("gmail")
            self._provider_reply(item)
            return
        # generic POST capture (discord REST, token endpoints, ...)
        try:
            parsed = json.loads(body or b"{}")
        except ValueError:
            parsed = body          # e.g. form-encoded token requests
        fx.calls.append({"method": "POST", "path": self.path,
                         "headers": dict(self.headers),
                         "body": parsed})
        item = fx.next_response("post")
        self._provider_reply(item)

    def do_PUT(self):
        fx = self.server.fixture
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n)
        try:
            parsed = json.loads(body or b"{}")
        except ValueError:
            parsed = body
        fx.calls.append({"method": "PUT", "path": self.path,
                         "headers": dict(self.headers), "body": parsed})
        self._provider_reply(fx.next_response("post"))

    def do_PATCH(self):
        fx = self.server.fixture
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n)
        try:
            parsed = json.loads(body or b"{}")
        except ValueError:
            parsed = body
        fx.calls.append({"method": "PATCH", "path": self.path,
                         "headers": dict(self.headers), "body": parsed})
        self._provider_reply(fx.next_response("post"))

    def do_DELETE(self):
        fx = self.server.fixture
        fx.calls.append({"method": "DELETE", "path": self.path,
                         "headers": dict(self.headers), "body": None})
        self._provider_reply(fx.next_response("post"))

    def _provider_reply(self, item):
        if callable(item):
            item(self)
            return
        if isinstance(item, int):
            self._json(item, {"error": {"code": item}})
            return
        if isinstance(item, tuple):
            code, headers, payload = item
            if isinstance(payload, (bytes, str)):
                self._raw(code, payload, headers)
            else:
                self._json(code, payload, headers)
            return
        self._json(200, item)

    def do_GET(self):
        fx = self.server.fixture
        fx.auths.append(self.headers.get("Authorization"))
        if self.path.startswith("/v1/models"):
            if fx.models is None:
                self._json(404, {"error": "no models endpoint"})
                return
            self._json(200, {"object": "list",
                             "data": [{"id": m, "object": "model"}
                                      for m in fx.models]})
            return
        if self.path.startswith("/gmail/"):
            fx.calls.append({"method": "GET", "path": self.path,
                             "headers": dict(self.headers), "body": None})
            item = fx.next_response("gmail")
            self._provider_reply(item)
            return
        h = fx.pages.get(self.path)
        if h is None:
            self._json(404, {"error": "no page"})
            return
        if callable(h):
            h(self)
            return
        code, headers, body = h
        self.send_response(code)
        for k, v in headers.items():
            self.send_header(k, v)
        if not isinstance(body, SlowBody):
            self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        if isinstance(body, SlowBody):
            body.write(self.wfile)
        else:
            self.wfile.write(body)


class SlowBody:
    """Trickles bytes past the deadline - a slow-loris fixture."""
    def __init__(self, chunks=None, delay=0.35, count=40):
        self.chunks = chunks or [b"x" * 64]
        self.delay = delay
        self.count = count

    def write(self, wfile):
        for i in range(self.count):
            for c in self.chunks:
                try:
                    wfile.write(c)
                    wfile.flush()
                except OSError:
                    return
            time.sleep(self.delay)


class Fixture:
    """Combined fake provider (/v1/chat/completions, /search) and web
    server (scripted GET pages) on one localhost port."""
    def __init__(self):
        self.calls = []
        self.auths = []          # Authorization header per request
        self.models = ["fixture-model"]
        self._queue = {}
        self.pages = {}
        self.srv = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), _FixtureHandler)
        self.srv.fixture = self
        self.srv.daemon_threads = True
        self.port = self.srv.server_address[1]
        self._t = threading.Thread(target=self.srv.serve_forever,
                                   daemon=True)
        self._t.start()

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.port}/v1"

    @property
    def web_base(self):
        return f"http://127.0.0.1:{self.port}"

    def next_response(self, key="chat"):
        q = self._queue.setdefault(key, [])
        if not q:
            return {"choices": [{"message": {
                "role": "assistant", "content": "done"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5}}
        return q.pop(0)

    def push_chat(self, item):
        self._queue.setdefault("chat", []).append(item)

    def push_search(self, item):
        self._queue.setdefault("search", []).append(item)

    def push_gmail(self, item):
        self._queue.setdefault("gmail", []).append(item)

    def push_post(self, item):
        self._queue.setdefault("post", []).append(item)

    def chat_text(self, text, usage=None):
        return {"choices": [{"message": {"role": "assistant",
                                        "content": text}}],
                "usage": usage or {"prompt_tokens": 10,
                                   "completion_tokens": 5}}

    def chat_tool(self, name, args, call_id="call_1", usage=None):
        return {"choices": [{"message": {
            "role": "assistant", "content": None,
            "tool_calls": [{"id": call_id, "type": "function",
                            "function": {"name": name,
                                         "arguments": json.dumps(args)}}]}}],
            "usage": usage or {"prompt_tokens": 10, "completion_tokens": 5}}

    def page(self, path, body=b"<html><title>t</title><p>hello</p></html>",
             content_type="text/html", code=200, extra=None):
        headers = {"Content-Type": content_type}
        headers.update(extra or {})
        self.pages[path] = (code, headers, body)

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


# ---------------------------------------------------------------------------

class WSGIClient:
    def __init__(self, app):
        self.app = app
        self.cookies = {}

    def _environ(self, method, path, body=b"", ctype="", headers=None):
        parts = urllib.parse.urlsplit(path)
        env = {
            "REQUEST_METHOD": method,
            "PATH_INFO": parts.path,
            "QUERY_STRING": parts.query,
            "SERVER_NAME": "testserver",
            "SERVER_PORT": "80",
            "HTTP_HOST": "testserver",
            "REMOTE_ADDR": "10.9.9.9",
            "wsgi.input": io.BytesIO(body),
            "CONTENT_LENGTH": str(len(body)),
            "CONTENT_TYPE": ctype,
        }
        if self.cookies:
            env["HTTP_COOKIE"] = "; ".join(
                f"{k}={v}" for k, v in self.cookies.items())
        for k, v in (headers or {}).items():
            env["HTTP_" + k.upper().replace("-", "_")] = v
        return env

    def request(self, method, path, data=None, headers=None, ctype=None):
        body = b""
        if data is not None:
            if isinstance(data, bytes):
                body = data
            else:
                body = urllib.parse.urlencode(data).encode()
            ctype = ctype or "application/x-www-form-urlencoded"
        captured = {}
        def start_response(status, resp_headers):
            captured["status"] = status
            captured["headers"] = resp_headers
        chunks = self.app(self._environ(method, path, body,
                                        ctype or "", headers),
                          start_response)
        payload = b"".join(chunks)
        for k, v in captured["headers"]:
            if k.lower() == "set-cookie":
                first = v.split(";")[0]
                name, _, val = first.partition("=")
                if "Max-Age=0" in v:
                    self.cookies.pop(name, None)
                else:
                    self.cookies[name] = val
        return captured["status"], dict(
            (k.lower(), v) for k, v in captured["headers"]), payload

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def post(self, path, data=None, **kw):
        return self.request("POST", path, data=data, **kw)

    def login(self, username, password):
        return self.post("/login", {"username": username,
                                    "password": password})

    def csrf(self, app):
        name = app.cookie_name
        sid = self.cookies.get(name)
        conn = app.conn()
        row = conn.execute("SELECT csrf FROM sessions WHERE id=?",
                           (sid,)).fetchone()
        conn.close()
        return row["csrf"] if row else None


# ---------------------------------------------------------------------------

class FakeResolver:
    """Scripted DNS answers for egress tests."""
    def __init__(self, mapping):
        self.mapping = mapping
        self.queries = []

    def __call__(self, host, port):
        self.queries.append(host)
        ips = self.mapping.get(host)
        if ips is None:
            raise socket.gaierror("no such host")
        return [(socket.AF_INET6 if ":" in ip else socket.AF_INET,
                 socket.SOCK_STREAM, socket.IPPROTO_TCP, "",
                 (ip, port)) for ip in ips]


class FakeDialer:
    """Maps the vetted IP onto a real local fixture; records calls."""
    def __init__(self, real_host, real_port):
        self.real = (real_host, real_port)
        self.dialed = []

    def __call__(self, ip, port, timeout):
        self.dialed.append((ip, port))
        return socket.create_connection(self.real, timeout=timeout)


def wait_for(pred, timeout=8.0, interval=0.05):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(interval)
    return False


# ---------------------------------------------------------------------------
# Fake Discord gateway (scripted ws:// server sharing the frame codec)

import base64 as _b64
import hashlib as _sha1
import re as _re

from pa.wsclient import (OP_CLOSE, OP_PING, OP_PONG, OP_TEXT, WSClosed,
                         _GUIDE, encode_frame, read_frame)


class GWConn:
    """One accepted fake-gateway connection."""
    def __init__(self, sock, gw):
        self.sock = sock
        self.f = sock.makefile("rb")
        self.gw = gw
        self.received = []

    def send(self, obj):
        self.sock.sendall(encode_frame(
            OP_TEXT, json.dumps(obj).encode(), mask=False))

    def recv(self, timeout=None):
        if timeout is not None:
            self.sock.settimeout(timeout)
        while True:
            fin, op, payload = read_frame(self.f, expect_masked=True)
            if op == OP_PING:
                self.sock.sendall(encode_frame(OP_PONG, payload,
                                               mask=False))
                continue
            if op == OP_PONG:
                continue
            if op == OP_CLOSE:
                raise WSClosed("client closed")
            obj = json.loads(payload)
            self.received.append(obj)
            self.gw.received.append(obj)
            return obj

    def drop(self):
        try:
            self.sock.close()
        except OSError:
            pass

    def close(self, code=1000):
        """A protocol-level close frame with a real close code (e.g.
        Discord's 4004 'authentication failed')."""
        import struct
        try:
            self.sock.sendall(encode_frame(
                OP_CLOSE, struct.pack("!H", code), mask=False))
        except OSError:
            pass
        self.drop()


class FakeGateway:
    """Scripted Discord gateway. `scripts` is a list of callables, one
    per accepted connection (last one repeats for extra connections)."""
    def __init__(self):
        self.scripts = []
        self.received = []
        self.connections = 0
        self._lsock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._lsock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._lsock.bind(("127.0.0.1", 0))
        self._lsock.listen(5)
        self.port = self._lsock.getsockname()[1]
        self._t = threading.Thread(target=self._accept, daemon=True)
        self._t.start()

    @property
    def url(self):
        return f"ws://127.0.0.1:{self.port}/"

    def _accept(self):
        while True:
            try:
                sock, _ = self._lsock.accept()
            except OSError:
                return
            idx = self.connections
            self.connections += 1
            try:
                self._handshake(sock)
                if self.scripts:
                    # per docstring: the last script repeats for extra
                    # connections (a connect can also race the test's
                    # scripts.append — the rerun is deduped downstream)
                    self.scripts[min(idx, len(self.scripts) - 1)](
                        GWConn(sock, self))
                else:
                    GWConn(sock, self).sock.close()
            except (OSError, WSClosed):
                pass
            try:
                sock.close()
            except OSError:
                pass

    def _handshake(self, sock):
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = sock.recv(4096)
            if not chunk:
                raise OSError("closed during handshake")
            data += chunk
        m = _re.search(rb"Sec-WebSocket-Key: ([^\r\n]+)", data)
        if not m:
            raise OSError("no ws key")
        accept = _b64.b64encode(
            _sha1.sha1(m.group(1).strip() + _GUIDE).digest())
        sock.sendall(
            b"HTTP/1.1 101 Switching Protocols\r\n"
            b"Upgrade: websocket\r\nConnection: Upgrade\r\n"
            b"Sec-WebSocket-Accept: " + accept + b"\r\n\r\n")

    def close(self):
        try:
            self._lsock.close()
        except OSError:
            pass
