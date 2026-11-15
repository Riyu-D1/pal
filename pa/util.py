"""Small shared helpers: UTC time, ids, bounded JSON parsing."""

import datetime
import json
import secrets

MAX_JSON_DEPTH = 24


def utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def now_iso() -> str:
    return utcnow().isoformat(timespec="milliseconds")


def parse_iso(s: str) -> datetime.datetime:
    return datetime.datetime.fromisoformat(s)


def new_id(nbytes: int = 16) -> str:
    return secrets.token_hex(nbytes)


def short_id() -> str:
    return secrets.token_hex(8)


def json_dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _check_depth(node, depth: int, max_depth: int) -> None:
    if depth > max_depth:
        raise ValueError("json depth limit exceeded")
    if isinstance(node, dict):
        for v in node.values():
            _check_depth(v, depth + 1, max_depth)
    elif isinstance(node, list):
        for v in node:
            _check_depth(v, depth + 1, max_depth)


def json_loads_bounded(text: str, max_depth: int = MAX_JSON_DEPTH,
                       max_len: int = 1_000_000):
    """Parse JSON with hard limits on size and nesting depth."""
    if text is None:
        raise ValueError("empty json")
    if len(text) > max_len:
        raise ValueError("json too large")
    obj = json.loads(text)
    _check_depth(obj, 0, max_depth)
    return obj


def estimate_tokens(text: str) -> int:
    """Rough token estimate for mixed English text/JSON (~4 chars/token).

    Deliberately rounds up slightly; we reconcile against provider-reported
    usage after every call and store both.
    """
    return (len(text) + 3) // 4 + 4


def truncate_middle(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    keep = max(0, limit - 40)
    half = keep // 2
    return text[:half] + "\n...[truncated]...\n" + text[-half:]
