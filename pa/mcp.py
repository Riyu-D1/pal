"""Remote MCP client: Streamable HTTP transport + 401 detection.

Speaks JSON-RPC over one POST endpoint:
  initialize -> (capture Mcp-Session-Id) -> notifications/initialized
  -> tools/list / tools/call.

Both response shapes are handled — a single `application/json` body or
an `text/event-stream` SSE stream whose `data:` lines carry the
JSON-RPC messages. A stale session (HTTP 404 on a request that carried
a session id) triggers one re-initialize + retry. A 401 raises
McpAuthRequired carrying the WWW-Authenticate header so the OAuth layer
can run discovery from the resource_metadata hint.
"""

import urllib.parse

from .netclient import APIClient, HTTPError, TransportError
from .util import json_dumps, json_loads_bounded

PROTOCOL_VERSION = "2025-06-18"


class McpError(Exception):
    pass


class McpAuthRequired(McpError):
    def __init__(self, www_authenticate: str):
        super().__init__("server requires OAuth sign-in")
        self.www_authenticate = www_authenticate or ""


def _sse_messages(body: bytes):
    """Yield JSON-RPC payloads from an SSE body (data: lines)."""
    for block in body.decode("utf-8", "replace").split("\n\n"):
        data = []
        for line in block.split("\n"):
            if line.startswith("data:"):
                data.append(line[5:].strip())
        if data:
            try:
                yield json_loads_bounded("\n".join(data))
            except ValueError:
                continue


class McpClient:
    """One MCP endpoint. `token` is a bearer access token (may be '')."""

    def __init__(self, url: str, token: str = "", timeout: float = 20.0):
        self.url = url
        self.token = token
        self.timeout = timeout
        parts = urllib.parse.urlsplit(url)
        self._client = APIClient(
            f"{parts.scheme}://{parts.netloc}", timeout=timeout)
        self._path = parts.path + ("?" + parts.query if parts.query else "")
        self.session_id = ""
        self.protocol_version = PROTOCOL_VERSION
        self.server_info = {}
        self._id = 0

    # -- transport ---------------------------------------------------------
    def _headers(self):
        h = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "MCP-Protocol-Version": self.protocol_version,
        }
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def _post(self, obj):
        try:
            return self._client.request(
                "POST", self._path, body=json_dumps(obj).encode("utf-8"),
                headers=self._headers(), timeout=self.timeout)
        except HTTPError as e:
            if e.status == 401:
                raise McpAuthRequired(
                    e.headers.get("www-authenticate") or "")
            if e.status == 404 and self.session_id:
                raise StaleSession()
            raise McpError(f"MCP endpoint HTTP {e.status}")
        except TransportError as e:
            raise McpError(f"MCP endpoint unreachable: {e}")

    def _rpc(self, method: str, params=None):
        self._id += 1
        rid = self._id
        req = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            req["params"] = params
        try:
            headers, payload = self._post(req)
        except StaleSession:
            self.session_id = ""
            self.initialize()
            headers, payload = self._post(req)
        return self._extract(headers, payload, rid)

    def _extract(self, headers, payload: bytes, rid: int):
        ctype = (headers.get("content-type") or "").lower()
        msgs = []
        if "text/event-stream" in ctype:
            msgs = list(_sse_messages(payload))
        elif payload.strip():
            msgs = [json_loads_bounded(
                payload.decode("utf-8", "replace"))]
        answer = None
        for m in msgs:
            if isinstance(m, dict) and m.get("id") == rid:
                answer = m
        if answer is None:
            # tolerate servers that answer in-stream without an id match
            for m in reversed(msgs):
                if isinstance(m, dict) and ("result" in m or "error" in m):
                    answer = m
                    break
        if answer is None:
            raise McpError("no JSON-RPC response in server reply")
        if "error" in answer:
            err = answer["error"] or {}
            raise McpError(
                f"JSON-RPC error {err.get('code')}: "
                f"{str(err.get('message'))[:200]}")
        return answer.get("result") or {}

    # -- protocol ------------------------------------------------------------
    def initialize(self):
        """Raw handshake — the one call where the RESPONSE headers
        matter (the server may mint an Mcp-Session-Id on it)."""
        req = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
               "params": {
                   "protocolVersion": PROTOCOL_VERSION,
                   "capabilities": {},
                   "clientInfo": {"name": "pa", "version": "1.0"}}}
        headers, payload = self._post(req)
        result = self._extract(headers, payload, 1)
        self._id = max(self._id, 1)
        self.session_id = headers.get("mcp-session-id") or ""
        self.server_info = result.get("serverInfo") or {}
        self.protocol_version = result.get("protocolVersion") or \
            PROTOCOL_VERSION
        self._send_initialized()
        return result

    def _send_initialized(self):
        self._post({"jsonrpc": "2.0",
                    "method": "notifications/initialized"})

    def tools_list(self) -> list:
        if not self.server_info:
            self.initialize()
        result = self._rpc("tools/list")
        tools = result.get("tools") or []
        return tools if isinstance(tools, list) else []

    def call_tool(self, name: str, arguments: dict):
        if not self.server_info:
            self.initialize()
        return self._rpc("tools/call", {
            "name": name, "arguments": arguments or {}})


class StaleSession(Exception):
    pass
