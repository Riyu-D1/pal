"""Allowlisted HTTPS client for provider/search/integration endpoints.

This is deliberately separate from the web-fetch egress path: it speaks
only to hosts on the provider/search/integration allowlists (the
owner-configured LLM_BASE_URL, api.tavily.com, Discord/Gmail bases, plus
the dev Ollama base URL outside production), uses short timeouts, never
follows redirects silently, and never sends cookies.
"""

import http.client
import json
import ssl
import time
import urllib.parse


class HTTPError(Exception):
    def __init__(self, status: int, headers: dict, body: bytes):
        super().__init__(f"HTTP {status}")
        self.status = status
        self.headers = headers
        self.body = body


class TransportError(Exception):
    pass


class APIClient:
    """Minimal POST/GET client pinned to one allowlisted base URL."""

    def __init__(self, base_url: str, timeout: float = 30.0,
                 tls_context: ssl.SSLContext = None):
        self.base_url = base_url
        parts = urllib.parse.urlsplit(base_url)
        self.scheme = parts.scheme
        self.host = parts.hostname or ""
        self.port = parts.port or (443 if self.scheme == "https" else 80)
        self.base_path = parts.path.rstrip("/")
        self.timeout = timeout
        self._tls = tls_context

    @staticmethod
    def default_tls() -> ssl.SSLContext:
        return ssl.create_default_context()

    def request(self, method: str, path: str, body=None, headers=None,
                timeout=None):
        url_path = self.base_path + path
        data = None
        hdrs = {
            "Host": self.host if self.port in (80, 443)
                    else f"{self.host}:{self.port}",
            "User-Agent": "pa-assistant/1.0",
            "Accept": "application/json",
            "Connection": "close",
        }
        if headers:
            hdrs.update(headers)
        if body is not None:
            if isinstance(body, bytes):
                data = body
            else:
                data = json.dumps(body).encode("utf-8")
                hdrs["Content-Type"] = "application/json"
            hdrs["Content-Length"] = str(len(data))

        t = timeout or self.timeout
        deadline = time.monotonic() + t
        try:
            if self.scheme == "https":
                conn = http.client.HTTPSConnection(
                    self.host, self.port, timeout=t,
                    context=self._tls or self.default_tls())
            elif self.scheme == "http":
                conn = http.client.HTTPConnection(
                    self.host, self.port, timeout=t)
            else:
                raise TransportError(f"unsupported scheme {self.scheme!r}")
            conn.request(method, url_path, body=data, headers=hdrs)
            resp = conn.getresponse()
            # `timeout` bounds the WHOLE request, not each recv: a host
            # trickling a byte every few seconds to stay alive must not
            # stretch a request past its budget (observed: a queueing
            # free endpoint held one call ~47 minutes). Read the body in
            # slices and check the absolute deadline between them.
            payload = bytearray()
            cap = 8 * 1024 * 1024
            while len(payload) < cap:
                remain = deadline - time.monotonic()
                if remain <= 0:
                    conn.close()
                    raise TransportError("request deadline exceeded")
                if conn.sock:
                    conn.sock.settimeout(min(remain, 20.0))
                chunk = resp.read1(min(65536, cap - len(payload)))
                if not chunk:
                    break
                payload += chunk
            payload = bytes(payload)
            resp_headers = {k.lower(): v for k, v in resp.getheaders()}
            status = resp.status
            conn.close()
        except (OSError, http.client.HTTPException) as e:
            raise TransportError(str(e))
        if status in (301, 302, 303, 307, 308):
            raise TransportError(
                f"unexpected redirect from provider endpoint ({status})")
        if status >= 400:
            raise HTTPError(status, resp_headers, payload)
        return resp_headers, payload

    def request_stream(self, method: str, path: str, body=None,
                       headers=None, timeout=None):
        """POST and yield body chunks as they arrive (SSE streaming).
        The absolute deadline applies across the whole stream the same
        way request() bounds total time — a trickling host cannot
        stretch it."""
        url_path = self.base_path + path
        data = None
        hdrs = {
            "Host": self.host if self.port in (80, 443)
                    else f"{self.host}:{self.port}",
            "User-Agent": "pa-assistant/1.0",
            "Accept": "text/event-stream",
            "Connection": "close",
        }
        if headers:
            hdrs.update(headers)
        if body is not None:
            if isinstance(body, bytes):
                data = body
            else:
                data = json.dumps(body).encode("utf-8")
                hdrs["Content-Type"] = "application/json"
            hdrs["Content-Length"] = str(len(data))
        t = timeout or self.timeout
        deadline = time.monotonic() + t
        if self.scheme == "https":
            conn = http.client.HTTPSConnection(
                self.host, self.port, timeout=t,
                context=self._tls or self.default_tls())
        elif self.scheme == "http":
            conn = http.client.HTTPConnection(
                self.host, self.port, timeout=t)
        else:
            raise TransportError(f"unsupported scheme {self.scheme!r}")
        try:
            conn.request(method, url_path, body=data, headers=hdrs)
            resp = conn.getresponse()
            resp_headers = {k.lower(): v for k, v in resp.getheaders()}
            if resp.status >= 400:
                payload = resp.read(1024 * 1024)
                conn.close()
                raise HTTPError(resp.status, resp_headers, payload)
            while True:
                remain = deadline - time.monotonic()
                if remain <= 0:
                    conn.close()
                    raise TransportError("stream deadline exceeded")
                if conn.sock:
                    conn.sock.settimeout(min(remain, 20.0))
                chunk = resp.read1(65536)
                if not chunk:
                    break
                yield chunk
        except (OSError, http.client.HTTPException) as e:
            conn.close()
            raise TransportError(str(e))
        conn.close()

    def post_json(self, path: str, body, headers=None, timeout=None):
        h, payload = self.request("POST", path=path, body=body,
                                  headers=headers, timeout=timeout)
        # e.g. Discord interaction callbacks answer 204 No Content
        if not payload:
            return h, None
        return h, json.loads(payload.decode("utf-8"))

    def post_form(self, path: str, fields: dict, headers=None,
                  timeout=None):
        hdrs = {"Content-Type": "application/x-www-form-urlencoded"}
        hdrs.update(headers or {})
        h, payload = self.request(
            "POST", path=path,
            body=urllib.parse.urlencode(fields).encode("utf-8"),
            headers=hdrs, timeout=timeout)
        return h, json.loads(payload.decode("utf-8"))

    def get_json(self, path: str, headers=None, timeout=None):
        h, payload = self.request("GET", path, headers=headers,
                                  timeout=timeout)
        return h, json.loads(payload.decode("utf-8"))
