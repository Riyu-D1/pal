"""daily resource budgets — one ledger, pre-work checks.

Keyed by (day in Europe/London, scope, resource). Scopes:
'owner-chat', 'call', 'background', 'job:<name>', 'task:<id>'.
Resources: 'llm_tokens:<backend>', 'escalations', 'browser_min',
'node_sec:<node>', 'tts_chars:<provider>', 'stt_sec', 'live_min',
'composio_calls'.

Defaults (env-tunable PA_BUDGET_<RES>_<SCOPE>): background gets 30%
of each daily free quota; the call scope reserves 20% of TTS/STT/
Live; per-task caps come from job args. Owner chat is capped
generously — degradation there is a softer reply, never a dead end.

The ledger is the ONLY counter new callers should write; the older
voice_usage / composio_usage tables keep working for their existing
/status reads (the ledger mirrors TTS chars here).
"""
import os
import re
from datetime import datetime, timedelta, timezone

LON = timezone(timedelta(hours=1))   # Europe/London (BST); close enough
                                     # for a daily reset — the budget
                                     # boundary is advisory

# resource -> default whole-day cap (units = the resource's own)
DEFAULT_DAILY = {
    "llm_tokens:*": 1_000_000,
    "escalations": 40,
    "browser_min": 120,
    "node_sec:*": 7200,
    "tts_chars:*": 100_000,
    "stt_sec": 7200,
    "live_min": 120,
    "composio_calls": 900,
}
# scope -> fraction of the daily cap it may spend
SCOPE_SHARE = {
    "owner-chat": 1.0,
    "call": 0.2,          # calls reserve 20% of TTS/STT/Live
    "background": 0.3,    # background <= 30% of each daily quota
}


def _day(now=None) -> str:
    return (now or datetime.now(LON)).strftime("%Y-%m-%d")


def _norm_scope(scope: str) -> str:
    scope = str(scope or "owner-chat")
    if scope.startswith("job:") or scope.startswith("task:"):
        return "background"      # jobs/tasks share the background pot
    return scope if scope in SCOPE_SHARE else "background"


def _cap(conn, s, env, scope, resource, daily=None) -> int:
    """cap for (scope, resource): budget_limits row > PA_BUDGET_* env
    > DEFAULT_DAILY * scope share. job:/task: scopes inherit
    'background' unless a per-scope row exists."""
    rkey = resource.split(":", 1)[0] + ":*" \
        if ":" in resource else resource
    base = DEFAULT_DAILY.get(rkey, DEFAULT_DAILY.get(resource, 0))
    row = conn.execute(
        "SELECT cap FROM budget_limits WHERE scope=? AND resource=?",
        (scope, resource)).fetchone()
    if row:
        return int(row["cap"])
    env_cap = (env or {}).get(
        f"PA_BUDGET_{re.sub('[^A-Z0-9]', '_', resource.upper())}"
        f"_{re.sub('[^A-Z0-9]', '_', scope.upper())}")
    if env_cap and str(env_cap).strip().isdigit():
        return int(env_cap)
    if daily is not None:
        base = daily
    share = SCOPE_SHARE.get(scope, 0.3)
    return int(base * share)


def _row(conn, day, scope, resource):
    return conn.execute(
        "SELECT used, reserved FROM budget_ledger "
        "WHERE day=? AND scope=? AND resource=?",
        (day, scope, resource)).fetchone()


def _bucket_used(conn, day, scope, resource):
    """used+reserved for the resource across every scope that shares
    `scope`'s bucket — a job:* spend eats the same background pot."""
    bucket = _norm_scope(scope)
    rows = conn.execute(
        "SELECT scope, used, reserved FROM budget_ledger "
        "WHERE day=? AND resource=?", (day, resource)).fetchall()
    return sum(int(r["used"]) + int(r["reserved"]) for r in rows
               if _norm_scope(r["scope"]) == bucket)


def charge(conn, scope, resource, n, *, s=None, env=None,
           now=None) -> bool:
    """Add n units; False when it would exceed the cap (nothing is
    charged then — callers degrade instead)."""
    n = int(n or 0)
    if n <= 0:
        return True
    day, scope = _day(now), str(scope or "owner-chat")
    cap = _cap(conn, s, env, _norm_scope(scope), resource)
    used = _bucket_used(conn, day, scope, resource)
    if cap and used + n > cap:
        return False
    conn.execute(
        "INSERT INTO budget_ledger(day,scope,resource,used,updated_at)"
        " VALUES(?,?,?,?,?) ON CONFLICT(day,scope,resource) DO UPDATE"
        " SET used=used+excluded.used, updated_at=excluded.updated_at",
        (day, scope, resource, n, _now_iso()))
    return True


def reserve(conn, scope, resource, n, *, s=None, env=None,
            now=None) -> bool:
    """Hold n units for work in flight (LLM tokens before the usage
    arrives). False when the hold would exceed the cap."""
    n = int(n or 0)
    if n <= 0:
        return True
    day, scope = _day(now), str(scope or "owner-chat")
    cap = _cap(conn, s, env, _norm_scope(scope), resource)
    used = _bucket_used(conn, day, scope, resource)
    if cap and used + n > cap:
        return False
    conn.execute(
        "INSERT INTO budget_ledger(day,scope,resource,used,reserved,"
        " updated_at) VALUES(?,?,?,0,?,?) ON CONFLICT(day,scope,"
        " resource) DO UPDATE SET reserved=reserved+excluded.reserved,"
        " updated_at=excluded.updated_at",
        (day, scope, resource, n, _now_iso()))
    return True


def settle(conn, scope, resource, n, *, now=None):
    """Move n units from reserved to used (call with the ACTUAL cost;
    over-reserved units release via release())."""
    day, scope = _day(now), str(scope or "owner-chat")
    conn.execute(
        "UPDATE budget_ledger SET used=used+?, "
        "reserved=MAX(0, reserved-?), updated_at=? "
        "WHERE day=? AND scope=? AND resource=?",
        (int(n), int(n), _now_iso(), day, scope, resource))


def release(conn, scope, resource, n, *, now=None):
    day, scope = _day(now), str(scope or "owner-chat")
    conn.execute(
        "UPDATE budget_ledger SET reserved=MAX(0, reserved-?), "
        "updated_at=? WHERE day=? AND scope=? AND resource=?",
        (int(n), _now_iso(), day, scope, resource))


def remaining(conn, scope, resource, *, s=None, env=None,
              now=None) -> int:
    day, scope = _day(now), str(scope or "owner-chat")
    cap = _cap(conn, s, env, _norm_scope(scope), resource)
    if not cap:
        return 10 ** 9
    used = _bucket_used(conn, day, scope, resource)
    return max(0, cap - used)


def would_exceed(conn, scope, resource, n, *, s=None, env=None,
                 now=None) -> bool:
    return remaining(conn, scope, resource, s=s, env=env,
                     now=now) < int(n or 0)


def top_scopes(conn, n=3, *, now=None) -> list:
    """[(scope, resource, used+reserved)] busiest rows today — for
    /usage."""
    day = _day(now)
    return [dict(r) for r in conn.execute(
        "SELECT scope, resource, used, reserved FROM budget_ledger "
        "WHERE day=? ORDER BY used+reserved DESC LIMIT ?",
        (day, int(n))).fetchall()]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
