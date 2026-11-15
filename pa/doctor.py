"""`python -m pa doctor` — setup health report for agents and humans.

Prints one line per check: OK / WARN / FAIL plus a one-line fix hint.
Never prints secret values — only whether a variable is set. Exit 0
when every required check passes, 1 on any FAIL (warnings don't fail).

Required for a minimal working pal: deps importable, data dir
writable, schema migrated, a Discord token + owner id, and one model
endpoint (LLM_* or the free-API router). Everything else is optional
and reported as WARN so a fresh install reads cleanly.
"""

import os
import sys


def _have_module(name):
    try:
        __import__(name)
        return True
    except ImportError:
        return False


def run(s, env):
    rows = []  # (level, name, hint)

    def ok(name, hint=""):
        rows.append(("OK", name, hint))

    def warn(name, hint):
        rows.append(("WARN", name, hint))

    def fail(name, hint):
        rows.append(("FAIL", name, hint))

    # --- interpreter ------------------------------------------------
    v = sys.version_info
    if v >= (3, 11):
        ok("python", f"{v.major}.{v.minor}.{v.micro}")
    else:
        fail("python", f"{v.major}.{v.minor}.{v.micro} — pal needs "
                       ">= 3.11 (zoneinfo, tomllib-free stdlib only)")

    for mod, why in (("waitress", "required — pip install -r "
                                   "requirements.txt"),
                     ("cryptography", "required — encrypted backups"),
                     ("tzdata", "required — IANA zones on slim hosts"),
                     ("pypdf", "required — inbound PDF attachments")):
        if _have_module(mod):
            ok(f"dep:{mod}")
        else:
            fail(f"dep:{mod}", why)

    # --- core settings ----------------------------------------------
    if s.env == "production" or s.env in ("development", "test"):
        ok("env", s.env)
    else:
        fail("env", f"{s.env!r} — set PA_ENV=development|test|production")

    try:
        os.makedirs(s.data_dir, exist_ok=True)
        probe = os.path.join(s.data_dir, ".doctor-probe")
        with open(probe, "w") as fh:
            fh.write("x")
        os.unlink(probe)
        ok("data_dir", s.data_dir)
    except OSError as e:
        fail("data_dir", f"{s.data_dir} not writable: {e}")

    try:
        from . import db as dbmod
        conn = dbmod.connect(s.db_path)
        try:
            ver = dbmod.migrate(conn)
        finally:
            conn.close()
        ok("database", f"schema v{ver} at {s.db_path}")
    except Exception as e:  # noqa: BLE001 — doctor reports, never raises
        fail("database", f"{s.db_path}: {e}")

    try:
        s.check_bind(s.bind_host, s.port)
        ok("bind", f"{s.bind_host}:{s.port}")
    except Exception as e:  # noqa: BLE001
        fail("bind", str(e))

    # --- required integrations --------------------------------------
    if env.get("DISCORD_BOT_TOKEN"):
        ok("discord token")
    else:
        fail("discord token", "set DISCORD_BOT_TOKEN (Discord dev "
                              "portal → Bot → token)")

    if s.discord_owner_id:
        ok("discord owner", "DISCORD_OWNER_ID set")
    else:
        fail("discord owner", "set DISCORD_OWNER_ID to your Discord "
                              "user id (bot refuses everyone without it)")

    if s.llm_configured:
        ok("model endpoint",
           f"{s.llm_base_url} model={s.llm_model}")
    elif s.router_enabled:
        ok("model endpoint", "PA_ROUTER_ENABLED — free-API pool")
    else:
        fail("model endpoint",
             "set LLM_BASE_URL + LLM_MODEL + LLM_API_KEY, or "
             "PA_ROUTER_ENABLED=true + one free backend key (e.g. "
             "GROQ_API_KEY) — docs/SETUP.md")

    if env.get("LLM_API_KEY") or not s.llm_configured or s.router_enabled:
        if not s.llm_configured and not s.router_enabled:
            pass  # already failed above
        elif s.llm_configured and not env.get("LLM_API_KEY"):
            warn("llm key", "LLM_API_KEY unset — only OK for anonymous "
                            "endpoints (ollama, pollinations)")
        else:
            ok("llm key")

    # --- optional integrations (warn only) ---------------------------
    opt = [
        ("PA_SECRETS_KEY", "secrets key",
         "needed for /connect — generate: python3 -c "
         "\"import secrets;print(secrets.token_hex(32))\""),
        ("PA_PUBLIC_URL", "public url",
         "needed for /connect OAuth callbacks — a free Cloudflare "
         "Tunnel works (docs/CONNECTIONS.md)"),
        ("GMAIL_CLIENT_ID", "gmail oauth",
         "GMAIL_CLIENT_ID/SECRET/REFRESH_TOKEN, or /connect google"),
        ("VAULT_PATH", "vault mirror",
         "point VAULT_PATH at a synced Markdown dir for notes/brain"),
        ("COMPOSIO_API_KEY", "composio",
         "optional managed-auth fallback for /connect <app>"),
        ("PA_OWNER_EMAIL", "owner email",
         "needed for the email_owner tool (fixed-recipient sends)"),
    ]
    for var, name, hint in opt:
        if env.get(var):
            ok(name)
        else:
            warn(name, hint)

    if s.vault_path and not os.path.isdir(s.vault_path):
        warn("vault path", f"{s.vault_path} does not exist")

    owners = [n for n in ("PA_TIMEZONE", "PA_BRIEFING_CITY",
                          "PA_HOME_LAT")
              if env.get(n)]
    if owners:
        ok("owner identity", ", ".join(f"{n} set" for n in owners))
    else:
        warn("owner identity",
             "optional: PA_TIMEZONE, PA_BRIEFING_CITY/LAT/LON, "
             "PA_HOME_LAT/LON personalise pal; the owner's name is an "
             "owner_fact pal learns in chat (or the 'human' memory "
             "block) — never an env var")

    return rows


def report(rows):
    fails = sum(1 for lvl, *_ in rows if lvl == "FAIL")
    warns = sum(1 for lvl, *_ in rows if lvl == "WARN")
    width = max(len(name) for _l, name, _h in rows)
    out = []
    for lvl, name, hint in rows:
        mark = {"OK": " ok ", "WARN": "warn", "FAIL": "FAIL"}[lvl]
        line = f"[{mark}] {name.ljust(width)}"
        if hint:
            line += f"  {hint}"
        out.append(line)
    out.append("")
    if fails:
        out.append(f"{fails} required item(s) missing, {warns} "
                   "optional — fix the FAIL lines and re-run "
                   "`python -m pa doctor`.")
    else:
        out.append(f"all required checks pass ({warns} optional "
                   "unconfigured) — `python -m pa serve` to start.")
    return "\n".join(out), fails
