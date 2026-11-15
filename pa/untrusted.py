"""Central untrusted-data fencing (docs/PARITY.md #12).

Every byte that came from outside — web pages, search snippets,
connected-app payloads, PDF text — is DATA, not instructions.
`untrusted(source, text)` wraps it in an explicit fence the system
prompt already teaches the model to never obey.

final-1 P4: fencing also TAINTS the active tool context. The turn's
ctx is bound around each tool call; any fenced payload that flows
through marks ctx.tainted (+ the first source seen), and
pa/policy.decide upgrades every subsequent non-read action in that
turn to an owner approval — 'requested after reading <source>'.

Source-specific fences that predate this module (UNTRUSTED EMAIL,
NOTE, VISION CAPTION, WEBHOOK PAYLOAD) keep their own markers; this
helper is for everything that had none.
"""

import json
import threading

OPEN = "<<<UNTRUSTED {source}>>>"
CLOSE = "<<<END UNTRUSTED {source}>>>"

_tls = threading.local()


def bind(ctx):
    """chattools.run wraps each tool call in this so fenced results
    know which turn they landed in."""
    _tls.ctx = ctx


def unbind():
    _tls.ctx = None


def _taint(source: str):
    ctx = getattr(_tls, "ctx", None)
    if ctx is None or not hasattr(ctx, "tainted"):
        return
    ctx.tainted = True
    if not getattr(ctx, "taint_source", ""):
        ctx.taint_source = str(source or "untrusted data")[:80].lower()


def untrusted(source: str, text: str) -> str:
    src = "".join(c for c in str(source or "DATA") if c.isalnum()
                  or c in " ._-")[:60].upper() or "DATA"
    _taint(src)
    return (f"{OPEN.format(source=src)}\n{text}"
            f"\n{CLOSE.format(source=src)}")


# keys whose VALUES are remote-supplied text worth fencing at any
# length ('text' deliberately excluded — short scalar results like
# {"text": "thing:7"} are structure, not content)
_FENCE_KEYS = {"snippet", "content", "body", "excerpt",
               "caption", "subject", "title_text"}


def fence_result(source: str, out):
    """Fence untrusted text inside a tool result, preserving the
    dict/list shape so callers and tests still work. Short scalar
    strings (enum-ish fields like 'executed', ids) stay raw —
    they're structure, not content."""
    if isinstance(out, str):
        _taint(source)
        return untrusted(source, out)
    if isinstance(out, dict):
        return {k: (untrusted(source, v) if isinstance(v, str)
                    and (k in _FENCE_KEYS or len(v) > 160)
                    else v if isinstance(v, str)
                    else fence_result(source, v))
                for k, v in out.items()}
    if isinstance(out, list):
        return [fence_result(source, v) for v in out]
    return out


def fence_json(source: str, out) -> str:
    """Whole result as one fenced JSON string — for payloads whose
    shape the model doesn't need structured."""
    return untrusted(source, json.dumps(out, default=str)[:8000])
