"""Freebuff sidecar — run the agent-session provider on the owner's
machine so a remote PA never calls freebuff from a datacenter IP.

This is the legitimate answer to the service's cloud-IP gate: the
session genuinely originates from the owner's home machine running the
official CLI (driven by the normal FreebuffSessionProvider) — routing,
not spoofing or evasion. A cloud PA reaches it over a private
tailnet/SSH tunnel only.

Wire protocol (shared bearer token required on every request):

    GET  /healthz -> {"ok": true, "cli": "<installed version>"}
    POST /run     {"prompt": str, "deadline_seconds": float}
                  -> 200 {"text": str, "reasoning": str|null,
                          "meta": {...}}
                  -> 4xx/5xx {"error": {"kind": str, "message": str,
                             "reset_at": str|null,
                             "retry_after": float|null}}

`pa freebuff-sidecar` serves it. `PA_FREEBUFF_REMOTE_URL` +
`PA_FREEBUFF_REMOTE_TOKEN` on the remote PA make the `freebuff-cli`
assistant pin resolve to the remote provider below instead of a local
tmux session. The remote URL passes the same validation as LLM_BASE_URL:
https anywhere, plain http only to loopback/private (incl. tailnet).
"""

import http.server
import json
import logging
import socketserver
import time
import urllib.parse

from .netclient import APIClient, HTTPError, TransportError
from .provider import (ProviderAuthError, ProviderError,
                       ProviderNotConfigured, ProviderRateLimit,
                       ProviderUnavailable)
from .util import json_loads_bounded, truncate_middle

log = logging.getLogger("pa.freebuff.sidecar")

MAX_BODY = 96 * 1024
MAX_RESPONSE_TEXT = 256 * 1024
TOKEN_HEADER = "x-pa-freebuff-token"


# ---------------------------------------------------------------- server

def _error_payload(e):
    body = {"kind": getattr(e, "kind", "provider_error"),
            "message": str(e)[:500]}
    if isinstance(e, ProviderRateLimit):
        body["reset_at"] = e.reset_at
        body["retry_after"] = e.retry_after
    return body


def make_handler(provider_run, token, version_probe=None):
    """Request handler bound to a provider.run callable.

    `provider_run(prompt, deadline_seconds, check_cancel)` — injectable
    so tests never touch a real CLI. `version_probe()` optionally
    reports the installed CLI version for /healthz.
    """
    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            log.debug("sidecar: " + fmt, *args)

        def _reply(self, status, obj):
            body = json.dumps(obj).encode("utf-8")[:MAX_RESPONSE_TEXT]
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self):
            return self.headers.get(TOKEN_HEADER) == token

        def do_GET(self):
            if not self._authorized():
                return self._reply(401, {"error": {
                    "kind": "provider_auth", "message": "bad token"}})
            if urllib.parse.urlsplit(self.path).path != "/healthz":
                return self._reply(404, {"error": {"kind": "not_found",
                                                 "message": "unknown path"}})
            self._reply(200, {"ok": True,
                              "cli": version_probe() if version_probe
                              else None})

        def do_POST(self):
            if not self._authorized():
                return self._reply(401, {"error": {
                    "kind": "provider_auth", "message": "bad token"}})
            if urllib.parse.urlsplit(self.path).path != "/run":
                return self._reply(404, {"error": {"kind": "not_found",
                                                 "message": "unknown path"}})
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if not 0 < length <= MAX_BODY:
                return self._reply(400, {"error": {
                    "kind": "bad_request",
                    "message": "missing or oversized body"}})
            try:
                req = json_loads_bounded(
                    self.rfile.read(length).decode("utf-8", "replace"),
                    max_len=MAX_BODY)
                prompt = req["prompt"]
                if not isinstance(prompt, str) or not prompt.strip():
                    raise ValueError("prompt must be a non-empty string")
                deadline = float(req.get("deadline_seconds") or 0) or None
            except (ValueError, KeyError, TypeError):
                return self._reply(400, {"error": {
                    "kind": "bad_request", "message": "malformed request"}})
            try:
                out = provider_run(prompt, deadline, None)
            except ProviderError as e:
                status = (429 if isinstance(e, ProviderRateLimit)
                          else 502)
                return self._reply(status, {"error": _error_payload(e)})
            except Exception as e:
                log.exception("sidecar run failed")
                return self._reply(500, {"error": {
                    "kind": "provider_error",
                    "message": str(e)[:500]}})
            self._reply(200, {
                "text": out["text"], "reasoning": out.get("reasoning"),
                "meta": out.get("meta") or {}})

    return Handler


class _Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def serve(host, port, token, provider):
    """Blocking sidecar server. `provider` is a FreebuffSessionProvider."""
    handler = make_handler(provider.run, token,
                           provider._installed_version)
    srv = _Server((host, port), handler)
    log.info("freebuff sidecar listening on %s:%d", host, port)
    try:
        srv.serve_forever()
    finally:
        srv.server_close()


# ---------------------------------------------------------------- client

class SidecarSessionClient:
    """Agent-session provider that delegates to a session sidecar.

    Same contract as FreebuffSessionProvider (kind='agent_session'):
    prompt in -> session text out. Used for the freebuff home sidecar
    (the official CLI runs on owner hardware, so the service sees the
    owner's own residential IP — no spoofing anywhere) and for the
    router's official-CLI sidecars (gemini-cli / copilot-cli, which
    serve the same wire protocol on loopback).
    """

    kind = "agent_session"
    tools_enabled = False
    streaming = False
    last_headers: dict = {}

    def __init__(self, remote_url, token, session_timeout=300.0):
        self.client = APIClient(remote_url, timeout=30.0)
        self.token = token
        self.session_timeout = session_timeout
        self.remote_url = remote_url

    def _headers(self):
        # token only ever goes to the owner-pinned sidecar URL — same
        # rule as LLM_API_KEY (never follows redirects)
        return {TOKEN_HEADER: self.token}

    def check_installation(self):
        try:
            _, body = self.client.get_json("/healthz",
                                           headers=self._headers(),
                                           timeout=10)
            cli = body.get("cli") if isinstance(body, dict) else None
            return [("sidecar", True,
                     f"{self.remote_url} reachable (cli {cli or '?'})")]
        except (HTTPError, TransportError, ValueError) as e:
            return [("sidecar", False,
                     f"{self.remote_url} unreachable: {e}")]

    def run(self, prompt, deadline_seconds=None, check_cancel=None):
        timeout = min(deadline_seconds or self.session_timeout,
                      self.session_timeout)
        if check_cancel:
            check_cancel()
        try:
            _, body = self.client.post_json(
                "/run",
                {"prompt": truncate_middle(prompt or "", 96 * 1024),
                 "deadline_seconds": timeout},
                headers=self._headers(),
                # read deadline = session budget + slack for the wire
                timeout=timeout + 30.0)
        except HTTPError as e:
            raise self._map_error(e)
        except (TransportError, ValueError) as e:
            raise ProviderUnavailable(
                f"freebuff sidecar unreachable: {e}")
        if not isinstance(body, dict) or not isinstance(
                body.get("text"), str):
            raise ProviderUnavailable(
                "freebuff sidecar returned a malformed response")
        return {"text": body["text"],
                "reasoning": body.get("reasoning"),
                "meta": {**(body.get("meta") or {}),
                         "via": "sidecar", "remote": self.client.host}}

    def _map_error(self, e: HTTPError):
        try:
            err = json_loads_bounded(
                e.body.decode("utf-8", "replace"))["error"]
        except Exception:
            err = {"kind": "provider_error",
                   "message": f"HTTP {e.status}"}
        kind, msg = err.get("kind", ""), err.get("message", "")[:300]
        if e.status in (401, 403) or kind == "provider_auth":
            return ProviderAuthError(
                f"freebuff sidecar rejected the token: {msg}")
        if e.status == 429 or kind == "quota_exceeded":
            return ProviderRateLimit(
                f"freebuff sidecar: {msg}",
                reset_at=err.get("reset_at"),
                retry_after=err.get("retry_after"))
        if kind == "provider_not_configured":
            return ProviderNotConfigured(f"freebuff sidecar: {msg}")
        if kind == "provider_unavailable":
            return ProviderUnavailable(f"freebuff sidecar: {msg}")
        return ProviderError(f"freebuff sidecar: {msg}")


# Backwards-compatible name: the freebuff remote provider is the
# generic session-sidecar client with a freebuff URL+token.
FreebuffRemoteProvider = SidecarSessionClient
