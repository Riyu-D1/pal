"""Deep /healthz (docs/EVOLVE.md item 3).

Public and secret-free: short check codes only — never paths, env
values, or stack detail. `ok` requires: the DB answers a read AND a
tiny `health_beat` write, and RSS stays under PA_HEALTH_RSS_MB.
Optional checks (off unless asked for, so web-only contexts don't
false-fail): a fresh worker heartbeat (PA_HEALTH_EXPECT_WORKER) and
a live in-process Discord gateway (PA_HEALTH_DISCORD). The result is
cheap — no model calls — and canary-safe as the default surface.
"""

import logging
import resource
import sys
import time

from . import db as dbmod, version as vermod
from .util import now_iso, parse_iso, utcnow

log = logging.getLogger("pa.health")

WORKER_BEAT_KEY = "worker_beat"


def _env_flag(env, name, default=False) -> bool:
    return str((env or {}).get(name) or "").lower() in \
        ("1", "true", "yes", "on") or default


def _env_int(env, name, default) -> int:
    try:
        return int((env or {}).get(name) or default)
    except (TypeError, ValueError):
        return default


def rss_mb() -> float:
    try:
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        # ru_maxrss is bytes on macOS, kilobytes on Linux
        return rss / (1024 * 1024) if sys.platform == "darwin" \
            else rss / 1024
    except Exception:
        return 0.0


def beat(conn):
    """Workers stamp liveness here; the health check reads it."""
    try:
        dbmod.set_setting(conn, WORKER_BEAT_KEY, now_iso())
        conn.commit()
    except Exception:
        pass


def _beat_age(conn):
    try:
        raw = dbmod.get_setting(conn, WORKER_BEAT_KEY, "")
        if not raw:
            return None
        return (utcnow() - parse_iso(raw)).total_seconds()
    except Exception:
        return None


def check(conn, s, env=None) -> dict:
    """{ok, time, uptime_s, sha, checks, failed?} — one row per check;
    failed holds the short codes that make this a 503."""
    checks, failed = {}, []

    try:
        conn.execute("SELECT 1").fetchone()
        dbmod.set_setting(conn, "health_beat", now_iso())
        conn.commit()
        checks["db"] = "ok"
    except Exception:
        checks["db"] = "fail"
        failed.append("db")

    limit = _env_int(env, "PA_HEALTH_RSS_MB", 700)
    rss = rss_mb()
    checks["rss_mb"] = int(rss)
    if rss > limit:
        failed.append("rss")

    if _env_flag(env, "PA_HEALTH_EXPECT_WORKER"):
        max_age = _env_int(env, "PA_HEALTH_WORKER_MAX_SECONDS", 120)
        age = _beat_age(conn)
        checks["worker_s"] = None if age is None else int(age)
        if age is None or age > max_age:
            failed.append("worker")

    if _env_flag(env, "PA_HEALTH_DISCORD") and s.discord_enabled \
            and s.discord_token(env):
        from . import discordbot
        age = discordbot.gateway_age()
        checks["discord_s"] = None if age is None else int(age)
        if age is None or age > 180:
            failed.append("discord")

    out = {"ok": not failed, "time": now_iso(),
           "uptime_s": int(time.time() - vermod.STARTED_AT),
           "sha": vermod.git_sha(), "checks": checks}
    if failed:
        out["failed"] = failed
    return out
