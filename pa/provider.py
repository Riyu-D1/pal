"""Provider access: one owner-pinned OpenAI-compatible chat endpoint.

Policy (hard rules):
- The endpoint is configured ONLY server-side via LLM_BASE_URL / LLM_MODEL
  / LLM_API_KEY. An assistant is pinned to one (host, model) pair from
  the allowlist. Nothing user-, model- or web-controllable can redirect
  the endpoint, and the API key is attached only to requests to the
  configured base URL (no redirects are ever followed).
- Loopback/private endpoints are allowed when the owner sets them; plain
  http to a public host is rejected at config load.
- No automatic cross-provider fallback. A key being present never
  enables a provider.
- 401/403 -> ProviderAuthError (task fails, no fallback).
- 429 -> ProviderRateLimit carrying the reset time from retry-after or
  x-ratelimit-* headers.
- Network/5xx failures -> ProviderUnavailable (task fails visibly; the
  worker may retry the call only before any dependent tool action).
- LLM_STREAMING toggles SSE streaming; LLM_TOOLS toggles whether tool
  schemas are sent at all (off for endpoints without tool support).
"""

import datetime
import json
import re
import urllib.parse

from .netclient import APIClient, HTTPError, TransportError
from .util import json_loads_bounded


class ProviderError(Exception):
    kind = "provider_error"
    retryable = False


class ProviderNotConfigured(ProviderError):
    kind = "provider_not_configured"


class ProviderAuthError(ProviderError):
    kind = "provider_auth"


class ProviderRateLimit(ProviderError):
    kind = "quota_exceeded"

    def __init__(self, message: str, reset_at: str = None, retry_after: float = None):
        super().__init__(message)
        self.reset_at = reset_at
        self.retry_after = retry_after


class ProviderUnavailable(ProviderError):
    kind = "provider_unavailable"
    retryable = True


class ProviderBadResponse(ProviderError):
    kind = "provider_bad_response"


_DURATION_RE = re.compile(r"^(?:(\d+(?:\.\d+)?)ms|(\d+(?:\.\d+)?)s|"
                          r"(?:(\d+))m(?:(\d+(?:\.\d+)?)s)?)$")


def parse_reset_seconds(value: str):
    """Parse rate-limit reset values ('15ms', '2.5s', '1m30s') to seconds."""
    if value is None:
        return None
    v = value.strip()
    m = _DURATION_RE.match(v)
    if not m:
        return None
    if m.group(1):
        return float(m.group(1)) / 1000.0
    if m.group(2):
        return float(m.group(2))
    minutes = float(m.group(3) or 0)
    return minutes * 60 + float(m.group(4) or 0)


def reset_at_iso(headers: dict, now=None) -> str:
    """Best-effort absolute reset time from rate-limit headers."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    candidates = [
        headers.get("x-ratelimit-reset-tokens"),
        headers.get("x-ratelimit-reset-requests"),
        headers.get("retry-after"),
    ]
    for c in candidates:
        if c is None:
            continue
        secs = parse_reset_seconds(c)
        if secs is None:
            try:
                secs = float(c)
            except ValueError:
                continue
        return (now + datetime.timedelta(seconds=secs)).isoformat(
            timespec="seconds")
    return (now + datetime.timedelta(seconds=60)).isoformat(timespec="seconds")


class ChatProvider:
    """OpenAI-compatible chat-completions client for an allowlisted pair."""

    def __init__(self, base_url: str, model: str, api_key: str = None,
                 timeout: float = 60.0, streaming: bool = False,
                 tools_enabled: bool = True,
                 completions_path: str = "/chat/completions"):
        self.base_url = base_url
        self.model = model
        self.api_key = api_key
        self.streaming = streaming
        self.tools_enabled = tools_enabled
        self.completions_path = completions_path
        self.client = APIClient(base_url, timeout=timeout)
        self.host = urllib.parse.urlsplit(base_url).hostname or ""
        self.last_headers: dict = {}
        self.last_finish_reason = None

    @property
    def configured(self) -> bool:
        # Endpoints with no key (e.g. a local anonymous endpoint) are
        # "configured" by definition; a set-but-empty key is not.
        return True if self.api_key is None else bool(self.api_key)

    def _headers(self):
        # The key is ONLY ever attached to requests made by this client,
        # which is pinned to the configured base_url. Redirects are never
        # followed (APIClient raises on 3xx), so it cannot leak elsewhere.
        return {"Authorization": f"Bearer {self.api_key}"} \
            if self.api_key else {}

    def list_models(self):
        """GET /models if the endpoint exposes it. Returns [ids] or None
        when the endpoint doesn't implement the route (404/405)."""
        try:
            _h, data = self.client.get_json("/models",
                                            headers=self._headers())
        except HTTPError as e:
            if e.status in (404, 405):
                return None
            self._raise_for(e)
        except TransportError as e:
            raise ProviderUnavailable(str(e))
        try:
            return [m["id"] for m in data.get("data") or [] if m.get("id")]
        except (AttributeError, TypeError):
            raise ProviderBadResponse("malformed /models response")

    def chat(self, messages, tools=None, max_completion_tokens=1024,
             timeout=None, tier=None):
        """One chat completion. Returns (message_dict, usage_dict).

        message_dict: {"role":"assistant","content":str|None,
                       "tool_calls":[{id,function{name,arguments}}]}
        `tier` is a RouterProvider hint ('fast'|'smart'); single-endpoint
        providers accept and ignore it for interface parity.
        """
        if not self.configured:
            raise ProviderNotConfigured(
                f"no API key configured for {self.host}")
        body = {
            "model": self.model,
            "messages": messages,
            "max_completion_tokens": max_completion_tokens,
        }
        if tools and self.tools_enabled:
            body["tools"] = tools
            body["tool_choice"] = "auto"
            body["parallel_tool_calls"] = False
        if self.streaming:
            body["stream"] = True
        try:
            if self.streaming:
                h, data = self._chat_stream(body, timeout)
            else:
                h, data = self.client.post_json(
                    self.completions_path, body, headers=self._headers(),
                    timeout=timeout)
            self.last_headers = h
        except HTTPError as e:
            self.last_headers = e.headers
            self._raise_for(e)
        except TransportError as e:
            raise ProviderUnavailable(str(e))

        try:
            choice = data["choices"][0]
            message = choice["message"]
            usage = data.get("usage") or {}
        except (KeyError, IndexError, TypeError):
            raise ProviderBadResponse("malformed provider response")
        self.last_finish_reason = choice.get("finish_reason")
        return _normalize_message(message), usage

    def _raise_for(self, e: HTTPError):
        if e.status in (401, 403):
            raise ProviderAuthError(
                f"provider authentication failed (HTTP {e.status})")
        if e.status == 429:
            ra = e.headers.get("retry-after")
            try:
                retry_after = float(ra) if ra else None
            except ValueError:
                retry_after = parse_reset_seconds(ra)
            raise ProviderRateLimit(
                "provider rate limit (HTTP 429)",
                reset_at=reset_at_iso(e.headers),
                retry_after=retry_after)
        if e.status >= 500:
            raise ProviderUnavailable(f"provider HTTP {e.status}")
        raise ProviderBadResponse(
            f"provider HTTP {e.status}: {e.body[:200]!r}")

    def chat_iter(self, messages, max_completion_tokens=1024,
                  timeout=None):
        """Incremental streaming: yields content-delta strings as SSE
        chunks arrive — the call brain cuts the first clause
        mid-stream.
        After the generator ends, self.last_usage/last_message hold the
        assembled result."""
        if not self.configured:
            raise ProviderNotConfigured(
                f"no API key configured for {self.host}")
        body = {"model": self.model, "messages": messages,
                "max_completion_tokens": max_completion_tokens,
                "stream": True}
        chunks = self.client.request_stream(
            "POST", self.completions_path, body=body,
            headers=self._headers(), timeout=timeout)
        self.last_usage = {}
        text_parts = []
        for delta in _iter_sse(chunks):
            if isinstance(delta, dict):
                self.last_usage = delta.get("usage") or {}
                continue
            text_parts.append(delta)
            yield delta
        self.last_message = {"role": "assistant",
                             "content": "".join(text_parts) or None,
                             "tool_calls": []}

    # -- streaming (SSE accumulate) --------------------------------------
    def _chat_stream(self, body, timeout):
        """POST with stream=true and fold the SSE chunks into one
        response-shaped dict. The whole stream is still bounded by the
        client read cap, so this stays memory-safe."""
        h, payload = self.client.request(
            "POST", self.completions_path, body=body,
            headers=self._headers(), timeout=timeout)
        text = payload.decode("utf-8", "replace")
        return h, _fold_sse(text)


def _iter_sse(chunks):
    """Incrementally parse `data:` SSE lines from raw byte chunks.
    Yields str content deltas; the last yielded item may be a
    {"usage": ...} dict so callers can record token counts."""
    buf = b""
    usage = {}
    saw_data = False
    for chunk in chunks:
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            line = line.strip()
            if not line.startswith(b"data:"):
                continue
            saw_data = True
            data = line[5:].strip()
            if data == b"[DONE]":
                break
            try:
                ev = json_loads_bounded(data.decode("utf-8", "replace"),
                                        max_len=2_000_000)
            except ValueError:
                continue
            if ev.get("usage"):
                usage = ev["usage"]
            for ch in ev.get("choices") or []:
                delta = ch.get("delta") or {}
                if delta.get("content"):
                    yield delta["content"]
    if not saw_data:
        raise ProviderBadResponse("streaming response had no data events")
    if usage:
        yield {"usage": usage}


def _fold_sse(text: str) -> dict:
    """Fold OpenAI-style `data:` SSE chunks into a non-streaming response."""
    content_parts = []
    tool_deltas = {}          # index -> {id, name, args[]}
    usage = {}
    finish = None
    saw_data = False
    for line in text.split("\n"):
        line = line.strip()
        if not line.startswith("data:"):
            continue
        saw_data = True
        data = line[5:].strip()
        if data == "[DONE]":
            break
        try:
            chunk = json_loads_bounded(data, max_len=2_000_000)
        except ValueError:
            continue
        if chunk.get("usage"):
            usage = chunk["usage"]
        for ch in chunk.get("choices") or []:
            delta = ch.get("delta") or {}
            if delta.get("content"):
                content_parts.append(delta["content"])
            for tc in delta.get("tool_calls") or []:
                idx = int(tc.get("index") or 0)
                slot = tool_deltas.setdefault(
                    idx, {"id": "", "name": "", "args": []})
                if tc.get("id"):
                    slot["id"] = tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    slot["name"] = fn["name"]
                if fn.get("arguments"):
                    slot["args"].append(fn["arguments"])
            if ch.get("finish_reason"):
                finish = ch["finish_reason"]
    if not saw_data:
        raise ProviderBadResponse("streaming response had no data events")
    tool_calls = [{
        "id": s["id"], "type": "function",
        "function": {"name": s["name"], "arguments": "".join(s["args"])}
    } for _i, s in sorted(tool_deltas.items())]
    return {
        "choices": [{
            "message": {
                "role": "assistant",
                "content": "".join(content_parts) or None,
                "tool_calls": tool_calls,
            },
            "finish_reason": finish or "stop",
        }],
        "usage": usage,
    }


def _normalize_message(message: dict) -> dict:
    tool_calls = []
    for tc in message.get("tool_calls") or []:
        fn = tc.get("function") or {}
        tool_calls.append({
            "id": tc.get("id") or "",
            "type": "function",
            "function": {
                "name": fn.get("name") or "",
                "arguments": fn.get("arguments") or "{}",
            },
        })
    return {
        "role": "assistant",
        "content": message.get("content"),
        "tool_calls": tool_calls,
    }


def provider_for(settings, host: str, model: str):
    """Build a client for an allowlisted (host, model) pair.

    Returns a ChatProvider for OpenAI-compatible endpoints, a
    FreebuffSessionProvider for the freebuff-cli sentinel (an agent-
    session provider — a different interface, not a chat endpoint), or a
    RouterProvider for the 'router' sentinel."""
    import os
    from .config import ConfigError, ROUTER_HOST
    from .freebuff_session import FREEBUFF_HOST, freebuff_provider_for
    try:
        base_url, key_env = settings.resolve_provider(host, model)
    except ConfigError as e:
        raise ProviderNotConfigured(str(e))
    if (host or "").strip().lower() == ROUTER_HOST:
        from .router import RouterProvider
        return RouterProvider(settings, env=os.environ)
    if (host or "").strip().lower() == FREEBUFF_HOST:
        return freebuff_provider_for(settings)
    api_key = os.environ.get(key_env) if key_env else None
    return ChatProvider(base_url, model, api_key=api_key,
                        streaming=settings.llm_streaming,
                        tools_enabled=settings.llm_tools,
                        completions_path=settings.llm_completions_path)
