"""CLI entry: python -m pa <command>.

Commands:
  serve            run the WSGI app under waitress (worker + discord bot
                   in-process when configured)
  worker           run only the task worker + scheduler
  discord-bot      run only the Discord bot (gateway + notifier)
  create-owner     create/update the single owner account
  migrate          apply schema migrations
  backup           online SQLite backup to DATA_DIR/backups
  restore          restore the live db from a backup file
  export-backup    AES-256-GCM encrypted export to an owner-chosen path
  import-backup    decrypt an export and restore it
  self-update      report (default/--dry-run) or pull (--apply) the
                   upstream branch — same gate as /update in Discord
  eval             run the offline eval fixtures (--live, --ids a,b);
                   non-zero on any failure/regression
  dr-check         disaster-recovery drill: backup age, decryption,
                   test-restore, schema, env names, units, disk
                   (docs/DR.md); non-zero on red
  github-webhook   create the signed 'github' webhook endpoint, print
                   the payload URL + one-time signing secret
                   (docs/WEBHOOKS.md)
  watchdog         standalone supervisor process: probes /healthz,
                   restarts the systemd unit when pal stays sick,
                   one owner DM per outage window
                   (deploy/pa-watchdog.service)
  check-config     print non-secret config + bind check
  doctor           setup health report: what's configured, what's
                   missing, one-line fix hints (never prints secrets)
  provider-check   conformance test the configured LLM endpoint
  router-status    free-API router: each backend's config state,
                   cooldown and today's usage vs caps (no secrets)
  router-check     one tiny live call per configured router backend,
                   pass/fail + latency (never prints keys)
  cli-sidecar      <gemini|copilot> — serve an official-CLI session
                   provider on loopback for the router (bearer token)
  freebuff-sidecar serve the freebuff session provider on this machine
                   (owner hardware; remote PAs call it via
                   PA_FREEBUFF_REMOTE_URL/TOKEN — see
                   docs/FREEBUFF_CONNECTION.md)
  gmail-auth       one-time Gmail OAuth consent (read/draft/send +
                   calendar scopes); prints the env values to set on
                   the host
  computer-agent   serve this host's browser to a remote pal — loopback
                   or tailnet (100.64/10) bind only, PA_COMPUTER_TOKEN
                   auth (docs/COMPUTER.md)
  chat             <message> — one conversational agent turn on the
                   'cli' channel (same engine as Discord chat)
  schedules        list schedules
  schedule         <name> on|off|now — enable/disable/fire a schedule
  schedule-add     <name> <freq> <HH:MM> <assistant> <title>
                   (freq: daily|weekly|hourly; optional flags:
                   --tz ZONE --dow N --body TEXT --private)
  vault-scan       reindex the vault mirror now; prints stats
  tts-check        <text> — synthesize speech: reports which provider
                   answered + duration, writes the ogg to a temp file
                   (never prints keys)
  call-bench       [n] [live|pipeline] — first-audio p50/p90 per call
                   engine through N synthetic prompts (call_metrics
                   rows with engine='bench')
  call-diag        [seconds] — join the call voice channel and log the
                   DAVE/ssrc/decrypt state every 2s (the quick check
                   for "pal can't hear me"; needs the voice extras)
"""

import getpass
import logging
import os
import sys
import threading

from . import backup as backupmod, db as dbmod
from .config import ConfigError, is_loopback_host, is_tailnet_host, \
    load_settings
from .logsetup import setup_logging
from .passwords import hash_password
from .util import now_iso

log = logging.getLogger("pa.cli")


def _settings():
    try:
        return load_settings()
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        sys.exit(2)


def _conn(s):
    conn = dbmod.connect(s.db_path)
    dbmod.migrate(conn)
    return conn


def cmd_serve(s):
    s.check_bind(s.bind_host, s.port)
    conn = _conn(s)
    conn.close()
    from waitress import serve
    from .webapp import App
    from .worker import Worker
    from .discordbot import DiscordBot
    w = Worker(s.db_path, s)
    t = threading.Thread(target=w.run_forever, daemon=True,
                         name="pa-worker")
    t.start()
    bot = DiscordBot(s)
    bot.start()                      # no-op + clean log line if disabled
    log.info("serving on %s:%s (env=%s, worker embedded)",
             s.bind_host, s.port, s.env)
    serve(App(s, env=os.environ), host=s.bind_host, port=s.port,
          threads=8,
          max_request_header_size=16384,
          channel_timeout=30, cleanup_interval=10)


def cmd_worker(s):
    _conn(s).close()
    from .worker import Worker
    Worker(s.db_path, s).run_forever()


def cmd_discord_bot(s):
    _conn(s).close()
    from .discordbot import DiscordBot
    bot = DiscordBot(s)
    if not bot.start():
        print("discord disabled: set DISCORD_BOT_TOKEN (and "
              "DISCORD_OWNER_ID)", file=sys.stderr)
        sys.exit(2)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        bot.stop()


def cmd_create_owner(s):
    conn = _conn(s)
    try:
        username = input("owner username: ").strip()[:100]
        if not username:
            print("username required", file=sys.stderr)
            sys.exit(2)
        p1 = getpass.getpass("password: ")
        p2 = getpass.getpass("repeat password: ")
        if not p1 or p1 != p2:
            print("passwords empty or do not match", file=sys.stderr)
            sys.exit(2)
        if len(p1) < 10:
            print("password must be at least 10 characters",
                  file=sys.stderr)
            sys.exit(2)
        ph = hash_password(p1)
        conn.execute(
            "INSERT INTO users(username, password_hash, created_at)"
            " VALUES(?,?,?) ON CONFLICT(username) DO UPDATE SET"
            " password_hash=excluded.password_hash",
            (username, ph, now_iso()))
        conn.execute("DELETE FROM sessions")
        print(f"owner '{username}' saved (all sessions cleared)")
    finally:
        conn.close()


def cmd_migrate(s):
    conn = dbmod.connect(s.db_path)
    v = dbmod.migrate(conn)
    print(f"schema at version {v}")
    conn.close()


def cmd_backup(s, args):
    dest = args[0] if args else backupmod.default_backup_dest(s.data_dir)
    _conn(s).close()
    print(backupmod.backup(s.db_path, dest))


def cmd_restore(s, args):
    if not args:
        print("usage: python -m pa restore <backup.db>", file=sys.stderr)
        sys.exit(2)
    backupmod.restore(s.db_path, args[0])
    print("restored")


def cmd_export(s, args):
    dest = args[0] if args else os.environ.get("PA_BACKUP_DEST")
    if not dest:
        print("set PA_BACKUP_DEST or pass a destination path",
              file=sys.stderr)
        sys.exit(2)
    passphrase = os.environ.get("PA_BACKUP_PASSPHRASE") or \
        getpass.getpass("backup passphrase: ")
    print(backupmod.export_backup(s.db_path, dest, passphrase))


def cmd_import(s, args):
    if not args:
        print("usage: python -m pa import-backup <export-file>",
              file=sys.stderr)
        sys.exit(2)
    passphrase = os.environ.get("PA_BACKUP_PASSPHRASE") or \
        getpass.getpass("backup passphrase: ")
    backupmod.import_backup(s.db_path, args[0], passphrase)
    print("imported + restored")


def cmd_check_config(s):
    try:
        s.check_bind(s.bind_host, s.port)
        bind = "ok"
    except ConfigError as e:
        bind = f"REFUSED: {e}"
    print(f"env:            {s.env}")
    print(f"data_dir:       {s.data_dir}")
    print(f"bind:           {s.bind_host}:{s.port} -> {bind}")
    print(f"fly_topology:   {s.fly_topology}")
    print(f"timezone:       {s.timezone}")
    if s.llm_configured:
        host = s.provider_allowlist()[0][0]
        print(f"llm endpoint:   {host} model={s.llm_model}"
              f" streaming={s.llm_streaming} tools={s.llm_tools}")
    else:
        print("llm endpoint:   NOT SET (LLM_BASE_URL/LLM_MODEL)")
    print(f"llm_key_set:    {bool(os.environ.get('LLM_API_KEY'))}")
    if s.router_enabled:
        from .router import RouterProvider
        rp = RouterProvider(s, env=os.environ)
        n = sum(1 for b in rp.backends.values()
                if b.configured(os.environ, s))
        print(f"router:         enabled, {n} backend(s) configured")
    else:
        print("router:         disabled (PA_ROUTER_ENABLED)")
    print(f"search_enabled: {s.search_enabled}")
    print(f"tavily_key_set: {bool(os.environ.get('TAVILY_API_KEY'))}")
    print(f"discord:        enabled={s.discord_enabled}"
          f" token_set={bool(os.environ.get('DISCORD_BOT_TOKEN'))}"
          f" owner={'set' if s.discord_owner_id else 'UNSET'}"
          f" routes={len(s.discord_channels)}")
    from . import gmail as gmailmod
    print(f"gmail:          configured="
          f"{gmailmod.configured(os.environ)}")
    print(f"public_url:     {s.public_url or 'NOT SET (needed for /connect)'}")
    print(f"secrets_key:    {'set' if os.environ.get('PA_SECRETS_KEY') else 'NOT SET (needed for /connect)'}")
    cid, _ = s.google_client(os.environ)
    print(f"google_client:  {'set' if cid else 'not set (PA_GOOGLE_CLIENT_ID)'}")
    print(f"composio:       {'configured' if s.composio_key(os.environ) else 'not set'}")
    print(f"vault_path:     {s.vault_path or 'NOT SET'}")
    print(f"daily limits:   {s.daily_request_limit} req /"
          f" {s.daily_token_limit} tok")


def cmd_doctor(s):
    from . import doctor
    text, fails = doctor.report(doctor.run(s, os.environ))
    print(text)
    return 1 if fails else 0


def cmd_provider_check(s):
    from .provider_check import format_report, run_check
    rep = run_check(s, os.environ)
    print(format_report(rep))
    return 0 if rep["ok"] else 1


def _router(s):
    if not s.router_enabled:
        print("PA_ROUTER_ENABLED=true required", file=sys.stderr)
        sys.exit(2)
    from .router import RouterProvider
    return RouterProvider(s, env=os.environ)


def cmd_router_status(s):
    rp = _router(s)
    rows = rp.status_rows()
    print(f"{'backend':<15} {'kind':<8} {'configured':<22} "
          f"{'cooling':<26} {'today req/tok':<18} caps(rpm/rpd)")
    for r in rows:
        conf = "yes" if r["configured"] else \
            "needs " + ",".join(r["missing"] or ["?"])
        caps = r["caps"]
        print(f"{r['name']:<15} {r['kind']:<8} {conf:<22} "
              f"{(r['cooling'] or '-'):<26} "
              f"{r['requests']}/{r['tokens']:<10} "
              f"{caps.get('rpm', '-')}/{caps.get('rpd', '-')}")
    print("\nmodels (smart/fast):")
    for r in rows:
        print(f"  {r['name']:<15} {r['smart_model']} / {r['fast_model']}")


def cmd_router_check(s):
    rp = _router(s)
    worst = 0
    for r in rp.status_rows():
        name = r["name"]
        if not r["configured"]:
            print(f"[SKIP] {name}: needs config ({', '.join(r['missing'])})")
            continue
        ok, ms, detail = rp.check_backend(name)
        tag = "PASS" if ok else "FAIL"
        if ok is None:
            tag = "SKIP"
        if ok is False:
            worst = 1
        print(f"[{tag}] {name}: {ms}ms — {detail}")
    return worst


def cmd_cli_sidecar(s, args):
    """Serve an official-CLI session provider on loopback (bearer token,
    same wire protocol as the freebuff sidecar)."""
    kind = args[0] if args else ""
    presets = {
        "gemini": (s.gemini_cli_url, s.gemini_cli_token,
                   [s.gemini_cli_bin, "-p"],
                   "PA_GEMINI_CLI_TOKEN"),
        "copilot": (s.copilot_cli_url, s.copilot_cli_token,
                    [s.copilot_cli_bin, "-s", "--no-ask-user", "-p"],
                    "PA_COPILOT_CLI_TOKEN"),
    }
    if kind not in presets:
        print("usage: python -m pa cli-sidecar <gemini|copilot>",
              file=sys.stderr)
        return 2
    url, token, argv, token_env = presets[kind]
    if not token:
        print(f"{token_env} required — remote callers authenticate "
              "with the same token", file=sys.stderr)
        return 2
    import urllib.parse
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname or "127.0.0.1"
    port = parts.port or (443 if parts.scheme == "https" else 80)
    from .cli_session import CliSessionProvider
    from .freebuff_sidecar import serve
    provider = CliSessionProvider(argv)
    print(f"{kind}-cli sidecar on {host}:{port} (cli: {argv[0]})")
    serve(host, port, token, provider)
    return 0


def cmd_freebuff_sidecar(s):
    from .freebuff_session import FreebuffSessionProvider
    from .freebuff_sidecar import serve
    if not s.freebuff_enabled:
        print("PA_FREEBUFF_ENABLED=true required", file=sys.stderr)
        return 2
    token = s.freebuff_remote_token
    if not token:
        print("PA_FREEBUFF_REMOTE_TOKEN required — remote callers "
              "authenticate with the same token", file=sys.stderr)
        return 2
    print(f"freebuff sidecar on {s.freebuff_sidecar_host}:"
          f"{s.freebuff_sidecar_port}")
    serve(s.freebuff_sidecar_host, s.freebuff_sidecar_port, token,
          FreebuffSessionProvider(s))
    return 0


def cmd_computer_agent(s):
    """Serve this host's browser to a remote pal. Fails closed: the
    only legal binds are loopback or a 100.64/10 tailnet address —
    never a public VNIC."""
    from . import computer
    if not s.computer_enabled:
        print("PA_COMPUTER_ENABLED=true required", file=sys.stderr)
        return 2
    if not s.computer_token:
        print("PA_COMPUTER_TOKEN required — remote callers authenticate "
              "with the same token", file=sys.stderr)
        return 2
    host = s.computer_agent_bind
    if not (is_loopback_host(host) or is_tailnet_host(host)):
        print(f"refusing to bind {host!r}: the computer agent must "
              "listen on loopback or a tailnet (100.64.0.0/10) "
              "address — see docs/COMPUTER.md", file=sys.stderr)
        return 2
    service = computer.service_for(s, os.environ)
    print(f"computer agent on {host}:{s.computer_agent_port}")
    computer.serve_agent(host, s.computer_agent_port,
                         s.computer_token, service)
    return 0


def cmd_gmail_auth(s):
    from . import gmail as gmailmod
    cid = os.environ.get("GMAIL_CLIENT_ID") or \
        input("Gmail OAuth client id: ").strip()
    csec = os.environ.get("GMAIL_CLIENT_SECRET") or \
        getpass.getpass("Gmail OAuth client secret: ")
    if not cid or not csec:
        print("client id and secret are required", file=sys.stderr)
        sys.exit(2)
    try:
        data = gmailmod.run_auth_flow(s, cid, csec)
    except gmailmod.GmailError as e:
        print(f"gmail-auth failed: {e}", file=sys.stderr)
        sys.exit(1)
    print("\nSuccess. Set these on the service host "
          "(never commit them):\n")
    print(f"export GMAIL_CLIENT_ID={cid}")
    print("export GMAIL_CLIENT_SECRET=<the client secret you used>")
    print(f"export GMAIL_REFRESH_TOKEN={data['refresh_token']}")
    print("\nScopes requested: " + ", ".join(
        s.rsplit("/", 1)[-1] for s in gmailmod.GMAIL_AUTH_SCOPES))
    return 0


def cmd_schedules(s):
    from . import scheduler as sch
    conn = _conn(s)
    try:
        rows = sch.list_schedules(conn)
        if not rows:
            print("no schedules")
            return
        for r in rows:
            state = "ON " if r["enabled"] else "off"
            print(f"[{state}] {r['name']}: {r['freq']} "
                  f"{r['time_of_day']} {r['timezone']}"
                  f" (kind={r['kind']})")
            runs = conn.execute(
                "SELECT fire_key, status, task_id FROM schedule_runs"
                " WHERE schedule_id=? ORDER BY id DESC LIMIT 3",
                (r["id"],)).fetchall()
            for run in runs:
                print(f"      last: {run['fire_key']} -> {run['status']}"
                      f" task={run['task_id']}")
    finally:
        conn.close()


def cmd_schedule(s, args):
    from . import scheduler as sch
    if len(args) < 2 or args[1] not in ("on", "off", "now"):
        print("usage: python -m pa schedule <name> on|off|now",
              file=sys.stderr)
        sys.exit(2)
    conn = _conn(s)
    try:
        name, action = args[0], args[1]
        if action == "now":
            res = sch.fire_now(conn, name)
            print(res)
            return
        ok = sch.set_enabled(conn, name, action == "on")
        print("updated" if ok else f"no schedule named {name!r}")
    finally:
        conn.close()


def cmd_schedule_add(s, args):
    from . import scheduler as sch
    flags = [a for a in args if a.startswith("--")]
    pos = [a for a in args if not a.startswith("--")]
    if len(pos) < 5:
        print("usage: python -m pa schedule-add <name> "
              "<daily|weekly|hourly> <HH:MM> <assistant> <title> "
              "[--tz ZONE] [--dow N] [--body TEXT] [--private] [--on]",
              file=sys.stderr)
        sys.exit(2)

    def flag(name, default=None):
        for f in flags:
            if f.startswith(name + "="):
                return f.split("=", 1)[1]
        return default
    name, freq, tod, assistant, title = pos[:5]
    payload = {
        "assistant_name": assistant,
        "title": title,
        "body": flag("--body", title),
        "private": "--private" in flags,
    }
    dow = flag("--dow")
    conn = _conn(s)
    try:
        sid = sch.add_schedule(
            conn, name, freq, tod,
            flag("--tz", s.timezone), payload,
            day_of_week=int(dow) if dow is not None else None,
            enabled="--on" in flags)
        print(f"schedule #{sid} added ({'enabled' if '--on' in flags else 'disabled'})")
    finally:
        conn.close()


def cmd_chat(s, args):
    """One conversational turn on the 'cli' channel: pa chat <text>.

    Approvals are asked on stdin (y/N). Transcript goes to stdout so it
    suitable for a smoke-test log."""
    from . import chatagent
    text = " ".join(args).strip()
    if not text:
        print("usage: python -m pa chat <message>", file=sys.stderr)
        sys.exit(2)

    def cli_approval(name, args, aid):
        import json as _json
        print(f"\n[approval needed] {name} {_json.dumps(args)[:400]}")
        ans = input("approve? [y/N] ").strip().lower()
        from . import approvals, db as dbmod
        conn = dbmod.connect(s.db_path)
        try:
            approvals.decide(conn, aid, ans in ("y", "yes"))
        finally:
            conn.close()
        return "approved" if ans in ("y", "yes") else "denied"

    engine = chatagent.ChatEngine(s, s.db_path,
                                  request_approval=cli_approval)
    out = engine.run_turn("cli", text)
    for step in out["trace"]:
        if "tool" in step:
            print(f"[step] {step['tool']} "
                  f"{str(step['args'])[:200]}")
            print(f"       -> {str(step['result'])[:400]}")
        elif "malformed" in step:
            print(f"[step] malformed reply (used as answer)")
    print(f"\n{out['text']}")
    print(f"\n({out['steps']} tool steps, {out['elapsed_s']}s)")
    return 0


def cmd_vault_scan(s):
    from . import vault as vaultmod
    conn = _conn(s)
    try:
        stats = vaultmod.scan(conn, s)
        for k, v in sorted(stats.items()):
            print(f"{k}: {v}")
    except vaultmod.VaultNotConfigured as e:
        print(f"vault not configured: {e}", file=sys.stderr)
        sys.exit(2)
    finally:
        conn.close()


def cmd_tts_check(s, args):
    from . import voice as voicemod
    text = " ".join(args).strip() or "pal voice check"
    try:
        ogg, dur, _wave, provider = voicemod.synthesize(
            s, os.environ, text)
    except voicemod.VoiceError as e:
        print(f"tts-check FAILED: {e}", file=sys.stderr)
        return 1
    import tempfile
    import time as _t
    path = os.path.join(tempfile.gettempdir(),
                        f"pa-tts-check-{int(_t.time())}.ogg")
    with open(path, "wb") as f:
        f.write(ogg)
    print(f"provider: {provider}")
    print(f"duration: {dur:.2f}s")
    print(f"ogg:      {path} ({len(ogg)} bytes)")
    return 0


def cmd_call_diag(s, args):
    """Join the call voice channel and periodically log the DAVE
    session state, the ssrc->user map and the decrypt counters — the
    fast way to place a 'pal can't hear me' failure."""
    import json as _json
    from . import call as callmod
    token = s.discord_token(os.environ)
    if not token:
        print("DISCORD_BOT_TOKEN required", file=sys.stderr)
        return 2
    try:
        seconds = int(args[0]) if args else 30
    except ValueError:
        seconds = 30
    channel_id = s.call_channel_id
    if not channel_id and s.call_guild_id:
        from .discordbot import DiscordAPI
        api = DiscordAPI(s, token)
        try:
            for ch in api.guild_channels(s.call_guild_id) or []:
                if ch.get("type") == 2 and ch.get("name") == "pal-call":
                    channel_id = str(ch["id"])
                    break
        except Exception:
            pass
    if not channel_id:
        print("no call channel — set PA_CALL_CHANNEL_ID, or "
              "PA_CALL_GUILD_ID with a 'pal-call' channel "
              "(docs/VOICE.md)", file=sys.stderr)
        return 2
    try:
        rt = callmod.DiscordPyRuntime(token, s.discord_owner_id, s,
                                      os.environ)
    except ImportError as e:
        print(f"voice extras missing: {e} — "
              "pip install -r requirements-voice.txt", file=sys.stderr)
        return 2
    try:
        snap = rt.run_diag(channel_id=channel_id, seconds=seconds)
        print(_json.dumps(snap, indent=2, default=str))
    finally:
        rt.stop()
    return 0


def cmd_call_bench(s, args):
    """N synthetic prompts through Live + pipeline, printing
    first-audio/total p50/p90 per engine. Rows land in call_metrics
    with engine='bench'."""
    from . import callbench
    try:
        n = int(args[0]) if args else 5
    except ValueError:
        n = 5
    engines = ["live", "pipeline"]
    if len(args) > 1 and args[1] in engines:
        engines = [args[1]]
    callbench.run(s, os.environ, n=n, engines=engines)
    return 0


USAGE = __doc__


def main(argv=None):
    from . import envfile
    envfile.load()
    setup_logging()
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    cmd, args = argv[0], argv[1:]
    s = _settings()
    if cmd == "serve":
        cmd_serve(s)
    elif cmd == "worker":
        cmd_worker(s)
    elif cmd == "discord-bot":
        cmd_discord_bot(s)
    elif cmd == "create-owner":
        cmd_create_owner(s)
    elif cmd == "migrate":
        cmd_migrate(s)
    elif cmd == "backup":
        cmd_backup(s, args)
    elif cmd == "restore":
        cmd_restore(s, args)
    elif cmd == "export-backup":
        cmd_export(s, args)
    elif cmd == "import-backup":
        cmd_import(s, args)
    elif cmd == "check-config":
        cmd_check_config(s)
    elif cmd == "doctor":
        return cmd_doctor(s)
    elif cmd == "provider-check":
        return cmd_provider_check(s)
    elif cmd == "router-status":
        cmd_router_status(s)
    elif cmd == "router-check":
        return cmd_router_check(s)
    elif cmd == "cli-sidecar":
        return cmd_cli_sidecar(s, args)
    elif cmd == "freebuff-sidecar":
        return cmd_freebuff_sidecar(s)
    elif cmd == "computer-agent":
        return cmd_computer_agent(s)
    elif cmd == "gmail-auth":
        return cmd_gmail_auth(s)
    elif cmd == "schedules":
        cmd_schedules(s)
    elif cmd == "schedule":
        cmd_schedule(s, args)
    elif cmd == "schedule-add":
        cmd_schedule_add(s, args)
    elif cmd == "chat":
        return cmd_chat(s, args)
    elif cmd == "vault-scan":
        cmd_vault_scan(s)
    elif cmd == "tts-check":
        return cmd_tts_check(s, args)
    elif cmd == "call-diag":
        return cmd_call_diag(s, args)
    elif cmd == "call-bench":
        return cmd_call_bench(s, args)
    elif cmd == "self-update":
        return cmd_self_update(s, args)
    elif cmd == "eval":
        return cmd_eval(s, args)
    elif cmd == "dr-check":
        return cmd_dr_check(s)
    elif cmd == "github-webhook":
        return cmd_github_webhook(s)
    elif cmd == "watchdog":
        return cmd_watchdog(s)
    else:
        print(f"unknown command {cmd!r}\n{USAGE}", file=sys.stderr)
        return 2
    return 0


def cmd_github_webhook(s):
    """github-webhook — create the 'github' signed endpoint, print the
    public URL + the signing secret ONCE, and the GitHub UI steps
    (docs/WEBHOOKS.md). Rerunning rotates the secret."""
    from . import db as dbmod, webhooks
    conn = dbmod.connect(s.db_path)
    try:
        dbmod.migrate(conn)
        out = webhooks.create(conn, "github")
    finally:
        conn.close()
    base = (s.public_url or "https://<your-app>").rstrip("/")
    print("GitHub webhook endpoint created — save the secret, it is "
          "shown once:\n")
    print(f"  Payload URL : {base}{out['url_path']}")
    print(f"  Secret      : {out['secret']}")
    print("\nRepo → Settings → Webhooks → Add webhook:")
    print("  • Content type: application/json")
    print("  • Secret: the value above (GitHub sends "
          "X-Hub-Signature-256)")
    print("  • Events: workflow runs, check suites, pull requests, "
          "issue comments")
    print("\nCI/PR events post as owner-DM cards — no agent turn "
          "(PA_GITHUB_HOOK_AGENT=true reverts to the agent path).")
    return 0


def cmd_dr_check(s):
    """dr-check — disaster-recovery drill: backup age, decryption,
    test-restore, schema, env names, units, disk. Green/red table,
    non-zero on red (docs/DR.md)."""
    from . import drcheck
    out = drcheck.run(s)
    print(drcheck.table(out))
    return 0 if out["ok"] else 1


def cmd_eval(s, args):
    """eval [--live] [--ids a,b] — run the offline eval fixtures
    (docs/EVOLVE.md #9). Non-zero on any failure/regression."""
    from .evolve import eval as evalmod
    live = "--live" in args
    ids = None
    if "--ids" in args:
        ids = args[args.index("--ids") + 1].split(",")
    out = evalmod.run(s, ids=ids, live=live)
    for r in out["results"]:
        mark = "ok " if r["ok"] else "FAIL"
        print(f"{mark} {r['id']}: tools={r.get('tools')}"
              + (f" — {'; '.join(r['failures'])}"
                 if r["failures"] else ""))
    print(f"eval: {out['passed']}/{out['total']} passed "
          f"(score {out['score']:.2f})")
    return 0 if not out["regressions"] and \
        out["passed"] == out["total"] else 1


def cmd_watchdog(s):
    """watchdog — separate supervisor process (deploy/pa-watchdog
    .service): polls /healthz, restarts `pa` when it stays sick,
    one owner DM per outage window."""
    from . import watchdog
    return watchdog.loop(s)


def cmd_self_update(s, args):
    """self-update [--dry-run] [--apply]: report or pull the upstream
    branch. Bare invocation and --dry-run only REPORT (ls-remote; no
    fetch) — --apply does the fast-forward merge."""
    from . import selfupdate
    if "--apply" in args:
        out = selfupdate.apply(settings=s)
        print(out.get("message") or out)
        if out.get("ok"):
            print(selfupdate.describe(selfupdate.plan()))
            try:                       # re-pin evaluator manifest
                conn = dbmod.connect(s.db_path) \
                    if hasattr(dbmod, "connect") else dbmod.open_db(
                        s.db_path)
                from .evolve import evaluator
                evaluator.pin(conn)
                conn.close()
                print("evaluator manifest re-pinned")
            except Exception as e:   # never block a deploy on this
                log.warning("evaluator re-pin skipped: %s", e)
        return 0 if out.get("ok") else 1
    plan = selfupdate.plan()
    print(selfupdate.describe(plan))
    if plan.get("update_available"):
        print("run `python -m pa self-update --apply` to pull "
              "(or approve /update in Discord)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
