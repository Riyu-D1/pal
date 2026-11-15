"""pa watchdog (docs/EVOLVE.md #14): a SEPARATE tiny process (its own
systemd unit, deploy/pa-watchdog.service) that pokes /healthz on
loopback and restarts pal when it stays sick. One restart + one
owner DM per outage window; the incident write is best-effort.

Nothing here runs inside pal — `python -m pa watchdog` is the entry.
"""

import json
import logging
import time
import urllib.request

log = logging.getLogger("pa.watchdog")


def probe_health(port: int) -> bool:
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/healthz",
                timeout=5) as r:
            return json.loads(r.read()).get("ok") is True
    except Exception:
        return False


def systemd_argv(scope: str = "user", unit: str = "pa") -> list:
    """`systemctl [--user] restart <unit>` for the configured scope."""
    argv = ["systemctl"]
    if scope == "user":
        argv.append("--user")
    return argv + ["restart", unit]


def systemd_restart(runner, scope: str = "user",
                    unit: str = "pa") -> bool:
    try:
        p = runner(systemd_argv(scope, unit),
                   capture_output=True, text=True, timeout=60)
        return p.returncode == 0
    except Exception:
        return False


def record_incident(db_path: str, text: str):
    """Best-effort: DB may be the thing that's down — just log."""
    try:
        from . import db as dbmod
        from .evolve import incidents
        conn = dbmod.connect(db_path)
        try:
            incidents.record(conn, "health", "health_fail",
                             summary=text[:200])
        finally:
            conn.close()
    except Exception:
        log.info("watchdog incident write failed", exc_info=True)


def run_once(sick_seconds: float, fail_after: float, probe,
             restart) -> str:
    """One evaluation step. probe() -> bool. Returns 'ok' |
    'sick' (still under the threshold) | 'restarted' | 'failed'."""
    if probe():
        return "ok"
    if sick_seconds < fail_after:
        return "sick"
    return "restarted" if restart() else "failed"


def loop(settings, env=None, probe=None, restart=None, notify=None,
         interval=15.0, fail_after=60.0, runner=None,
         max_loops=None) -> int:
    """Forever: probe every `interval`s; after `fail_after`s of
    continuous sickness restart once and DM once per window."""
    import os
    import subprocess
    env = os.environ if env is None else env
    runner = runner or subprocess.run
    probe = probe or (lambda: probe_health(settings.port))
    restart = restart or (lambda: systemd_restart(
        runner, getattr(settings, "systemd_scope", "user"),
        getattr(settings, "systemd_unit", "pa")))

    def _notify(text):
        record_incident(settings.db_path, text)
        if notify:
            notify(text)

    sick_since, alerted = None, False
    loops = 0
    while True:
        sick = 0 if sick_since is None else \
            time.monotonic() - sick_since
        out = run_once(sick, fail_after, probe, restart)
        if out == "ok":
            sick_since, alerted = None, False
        elif out == "sick":
            if sick_since is None:
                sick_since = time.monotonic()
        else:                            # restarted or failed
            if not alerted:
                alerted = True
                _notify(
                    "🐕 watchdog " +
                    ("restarted pal" if out == "restarted" else
                     "could not restart pal") +
                    f" after {int(sick)}s unhealthy")
            sick_since = None           # reset the outage window
        loops += 1
        if max_loops is not None and loops >= max_loops:
            return 0
        time.sleep(interval)
