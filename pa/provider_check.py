"""`pa provider-check` — conformance test against the configured endpoint.

Three checks against the owner-pinned LLM_* endpoint:

1. models    - GET /models (skipped-but-ok when the endpoint doesn't
               expose a list)
2. chat      - one plain-text completion
3. tool_call - one controlled read-only tool-call round trip: the model
               is asked to call echo_check, we reply with a fixed tool
               result, and require a final text answer.

Read-only by construction: the only "tool" is echo_check, executed
locally — the endpoint never triggers any system action. Nothing here
prints or logs the API key.
"""

import json

from .provider import (ChatProvider, ProviderBadResponse, ProviderError,
                       provider_for)

_ECHO_TOOL = {
    "type": "function",
    "function": {
        "name": "echo_check",
        "description": "Read-only conformance probe. Echoes back the "
                       "fixed string 'pong'. Call it exactly once.",
        "parameters": {"type": "object", "properties": {}},
    },
}


def _report():
    return {"checks": [], "ok": True}


def _add(rep, name, ok, detail=""):
    rep["checks"].append({"name": name, "ok": bool(ok), "detail": detail})
    if not ok:
        rep["ok"] = False


def _empty_detail(provider, max_tokens, fallback):
    """Empty content on HTTP 200 + finish_reason=length means a reasoning
    model spent the whole completion budget thinking — say so."""
    if getattr(provider, "last_finish_reason", None) == "length":
        return (f"{fallback}: finish_reason=length (reasoning used the "
                f"whole {max_tokens}-token budget)")
    return fallback


def run_check(settings, env=None, provider=None, max_tokens=512) -> dict:
    """Run all checks; returns a dict report (never raises for endpoint
    failures — those become failed checks)."""
    import os
    env = os.environ if env is None else env
    rep = _report()
    if provider is None:
        host, model = settings.default_provider_pair()
        if not host:
            _add(rep, "configured", False,
                 "no provider configured — set LLM_BASE_URL and LLM_MODEL")
            return rep
        try:
            provider = provider_for(settings, host, model)
        except ProviderError as e:
            _add(rep, "configured", False, str(e))
            return rep
    if getattr(provider, "kind", None) == "agent_session":
        _add(rep, "configured", True,
             "agent-session provider (freebuff-cli) — not an "
             "OpenAI-compatible endpoint")
        for name, ok, detail in provider.check_installation():
            _add(rep, name, ok, detail)
        # Opt-in only: a live probe spends one owner session. Used to
        # re-verify TUI markers after a CLI update (see
        # docs/FREEBUFF_CONNECTION.md).
        if getattr(provider, "live_check", False):
            try:
                out = provider.run(
                    "Reply with the single word: ok",
                    deadline_seconds=120)
                _add(rep, "session", True,
                     f"live probe ok in {out['meta'].get('duration_s')}s "
                     f"(spent one owner session)")
            except ProviderError as e:
                _add(rep, "session", False, f"live probe failed: {e}")
        else:
            _add(rep, "session", True,
                 "live session check skipped — would spend an owner "
                 "session; set PA_FREEBUFF_LIVE_CHECK=true to probe")
        return rep

    _add(rep, "configured", True,
         f"endpoint={provider.base_url} model={provider.model}"
         f" streaming={provider.streaming} tools={provider.tools_enabled}")

    # 1. models list -----------------------------------------------------
    try:
        models = provider.list_models()
        if models is None:
            _add(rep, "models", True, "endpoint does not expose /models "
                                     "(skipped)")
        else:
            have = provider.model in models
            shown = ", ".join(models[:10]) + (
                ", ..." if len(models) > 10 else "")
            _add(rep, "models", True,
                 f"{len(models)} model(s) listed: {shown}"
                 + ("" if have else f"; NOTE: {provider.model!r} not in list"))
    except ProviderError as e:
        _add(rep, "models", False, f"/models failed: {e}")

    # 2. plain chat ------------------------------------------------------
    try:
        msg, usage = provider.chat(
            [{"role": "user",
              "content": "Reply with exactly the word: ok"}],
            max_completion_tokens=max_tokens)
        text = (msg.get("content") or "").strip()
        _add(rep, "chat", bool(text),
             f"completion returned ({len(text)} chars)" if text else
             _empty_detail(provider, max_tokens, "empty completion"))
    except ProviderError as e:
        _add(rep, "chat", False, str(e))
        return rep  # no point attempting tool-calls if chat fails

    # 3. controlled read-only tool-call round trip -----------------------
    if not provider.tools_enabled:
        _add(rep, "tool_call", True,
             "LLM_TOOLS=off — tool calling disabled by config (skipped)")
        return rep
    try:
        convo = [{"role": "user",
                  "content": "Call the echo_check tool now. Do not "
                             "answer in prose."}]
        msg, _u = provider.chat(convo, tools=[_ECHO_TOOL],
                                max_completion_tokens=max_tokens)
        calls = msg.get("tool_calls") or []
        if not calls or calls[0]["function"]["name"] != "echo_check":
            _add(rep, "tool_call", False,
                 "model did not request echo_check "
                 f"(content={msg.get('content')!r})")
            return rep
        convo.append({"role": "assistant", "content": msg.get("content"),
                      "tool_calls": calls})
        convo.append({"role": "tool", "tool_call_id": calls[0]["id"],
                      "name": "echo_check", "content": json.dumps(
                          {"result": "pong"})})
        msg2, _u2 = provider.chat(convo, tools=[_ECHO_TOOL],
                                  max_completion_tokens=max_tokens)
        _add(rep, "tool_call", bool(msg2.get("content")),
             "round trip completed" if msg2.get("content") else
             _empty_detail(provider, max_tokens,
                           "no final answer after tool result"))
    except ProviderError as e:
        _add(rep, "tool_call", False, str(e))
    return rep


def format_report(rep: dict) -> str:
    lines = ["provider-check"]
    for c in rep["checks"]:
        mark = "PASS" if c["ok"] else "FAIL"
        lines.append(f"  [{mark}] {c['name']}: {c['detail']}")
    lines.append(f"overall: {'PASS' if rep['ok'] else 'FAIL'}")
    return "\n".join(lines)
