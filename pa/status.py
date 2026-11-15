"""/status — a one-message health card — and the health checks the
notifier loop turns into deduped owner DMs.

build() is read-mostly: the only live calls are the per-connection
probe (a token refresh / account GET / MCP handshake, each seconds-
bounded) — pass probe=None in tests to stay offline.
"""

import logging
import time

from . import composio as composiomod, connectflows, db as dbmod, \
    version, voice as voicemod
from .util import json_loads_bounded, utcnow

log = logging.getLogger("pa.status")


def _uptime() -> str:
    s = int(time.time() - version.STARTED_AT)
    h, s = divmod(s, 3600)
    m, _sec = divmod(s, 60)
    return f"{h}h {m}m" if h else f"{m}m"


def probe_connection(conn, s, env, row) -> str:
    """Cheap live check for one connection; never raises."""
    try:
        if row["status"] != "active":
            return f"row status {row['status']}"
        if row["kind"] == "composio":
            meta = json_loads_bounded(row.get("meta_json") or "{}")
            acc_id = (meta.get("connected_account_id")
                      or meta.get("connectedAccountId") or "")
            if not acc_id:
                return "no connected account id"
            cli = composiomod.ComposioClient(
                s, s.composio_key(env), timeout=8)
            acc = cli.account(acc_id)
            return f"account {acc['status'] or '?'}"
        if row["kind"] == "mcp":
            cli = connectflows.mcp_client(conn, s, env, row)
            cli.initialize()
            return "mcp handshake ok"
        if row["kind"] == "google" or row.get("token_blob"):
            connectflows.access_token(conn, s, env, row)
            return "token ok"
    except Exception as e:
        return f"probe failed: {str(e)[:140]}"
    return "no probe"


def _usage_today(conn, name: str) -> int:
    day = utcnow().strftime("%Y-%m-%d")
    row = conn.execute(
        "SELECT n FROM voice_usage WHERE name=? AND day=?",
        (name, day)).fetchone()
    return row["n"] if row else 0


def build(conn, s, env, probe=None) -> str:
    probe = probe_connection if probe is None else probe
    lines = []
    sha = version.git_sha() or "dev"
    lines.append(f"🩺 **pal {sha}** · up {_uptime()} · "
                 f"schema v{dbmod.current_version(conn)}")

    # connections + live probe
    rows = connectflows.list_all(conn)
    if rows:
        lines.append("**Connections**")
        for r in rows:
            acct = f"@{r['account']}" if r.get("account") else ""
            note = probe(conn, s, env, r) if probe else "not probed"
            lines.append(f"• {r['provider']}{acct} ({r['kind']}) "
                         f"{r['status']} — {note}")
    else:
        lines.append("**Connections:** none — /connect <app> to add")

    # router backends
    if s.router_enabled:
        try:
            from . import router as routermod
            rows = routermod.RouterProvider(s, env).status_rows()
            lines.append("**Router**")
            for r in rows:
                if r["cooling"]:
                    state = f"cooling {r['cooling'].split(' ')[0]}"
                elif r["configured"]:
                    state = "ok"
                else:
                    state = "no key"
                lines.append(
                    f"• {r['name']} {state} · {r['requests']} req "
                    f"today")
        except Exception as e:
            lines.append(f"**Router:** status failed ({str(e)[:80]})")
    else:
        lines.append("**Router:** off (PA_ROUTER_ENABLED)")

    # TTS/STT + today's usage vs rough caps
    lines.append("**Voice**")
    for name, st in voicemod.provider_states(s, env).items():
        n = _usage_today(conn, f"tts:{name}")
        if not st["configured"]:
            state = f"no key ({','.join(st['missing'])})"
        elif st["cooling_s"]:
            state = f"cooling {st['cooling_s']}s"
        else:
            state = "ok"
        left = voicemod.tts_chars_remaining(s, env, name)
        quota = f" · {left} chars left" if left is not None else ""
        lines.append(f"• tts {name}: {state} · {n} today{quota}")
    for name, st in voicemod.stt_states(s, env).items():
        n = _usage_today(conn, f"stt:{name}")
        state = "ok" if st["configured"] else "no key"
        lines.append(f"• stt {name}: {state} · {n} today")
    if s.composio_key(env):
        count = composiomod.usage_count(conn)
        lines.append(f"• composio calls this month: "
                     f"{count}/~{s.composio_monthly_limit}")

    # nodes (final-3): cached health only — /status must not block on
    # a device that's asleep
    try:
        from . import nodes as nodesmod
        node_lines = nodesmod.status_lines(conn, s, refresh=False)
    except Exception:
        node_lines = []
    if node_lines:
        lines.append("**Nodes**")
        lines += node_lines

    # jobs on/off + last result
    from . import scheduler as sch
    jobs = [r for r in sch.list_schedules(conn)
            if r["kind"] == "agent_job"]
    lines.append("**Jobs**")
    if not jobs:
        lines.append("• none")
    for r in jobs:
        state = "on" if r["enabled"] else "off"
        last = conn.execute(
            "SELECT status, reply, finished_at FROM chat_inbox"
            " WHERE source=? ORDER BY id DESC LIMIT 1",
            (f"job:{r['name']}",)).fetchone()
        tail = ""
        if last:
            tail = (f" — last {last['status']}"
                    f" {_clip(last['reply'] or '')[:80]}")
        lines.append(f"• {r['name']} [{state}]{tail}")

    # final-2 G3: activity feed counts (PA_ACTIVITY)
    if getattr(s, "activity_feed", False):
        try:
            from . import activity as actmod
            day = actmod.list_activity(conn, since_hours=24, limit=100)
            open_n = sum(1 for r in day if r["status"] == "open")
            needs = sum(1 for r in day
                        if r["kind"] == "needs_approval"
                        and r["status"] == "open")
            if day:
                tail = f" · {needs} need a yes" if needs else ""
                lines.append(
                    f"**Activity 24h:** {len(day)} rows"
                    f" ({open_n} open){tail} — /activity")
        except Exception:
            pass
    return "\n".join(lines)


def _clip(text: str) -> str:
    text = str(text or "")
    return text if len(text) <= 100 else text[:97] + "..."


# ---------------------------------------------------------------------------
# background failure alerts — (dedupe_key, text); clears let a key alert
# again once the condition is gone

def health_alerts(conn, s, env):
    """-> ([(key, text)], [clear_key])."""
    alerts, clears = [], []

    # a job that failed its last two runs in a row
    for r in conn.execute(
            "SELECT name FROM schedules WHERE kind='agent_job'"
            " AND enabled=1").fetchall():
        rows = conn.execute(
            "SELECT status FROM chat_inbox WHERE source=?"
            " ORDER BY id DESC LIMIT 2",
            (f"job:{r['name']}",)).fetchall()
        key = f"jobfail:{r['name']}"
        if len(rows) == 2 and \
                all(x["status"] == "failed" for x in rows):
            alerts.append((key, f"⚠️ job **{r['name']}** failed twice "
                                f"in a row — /status or the service "
                                "logs have the error"))
        elif rows and rows[0]["status"] == "done":
            clears.append(key)

    # a connection that needs re-auth
    for r in conn.execute(
            "SELECT provider, status FROM connections").fetchall():
        key = f"conn:{r['provider']}"
        if r["status"] in ("expired", "revoked", "error"):
            base = r["provider"].split("@")[0]
            alerts.append((key, f"⚠️ connection **{r['provider']}** "
                                f"is {r['status']} — /connect {base} "
                                "to re-auth"))
        elif r["status"] == "active":
            clears.append(key)

    # every configured TTS provider cooling at once
    day = utcnow().strftime("%Y-%m-%d")
    key = f"tts-down:{day}"
    try:
        states = voicemod.provider_states(s, env)
        live = [n for n, v in states.items() if v["configured"]]
        if live and all(states[n]["cooling_s"] > 0 for n in live):
            alerts.append((key, "⚠️ every configured TTS provider is "
                                "cooling down — voice notes will fail; "
                                "check keys/quotas"))
        else:
            clears.append(key)
    except Exception:
        pass
    return alerts, clears
