# Runbook

How to run this service in the cloud. **Nothing in this repo deploys
anything** — the owner must approve host, region and spend first. This
guide is host-generic: any single VM / container host with one persistent
volume and outbound internet works.

## Topology

- **One container**, one volume. `python -m pa serve` runs the WSGI app
  with the task worker, scheduler and Discord bot embedded — no second
  process to manage.
- State lives entirely under `PA_DATA_DIR` (SQLite WAL + backups). One
  volume, one instance — do not run two replicas against the same volume.
- Bind loopback and put a TLS terminator on the same host (Caddy shown
  below). A non-loopback bind is refused at startup unless the documented
  Fly topology is detected.

## Processes / commands

```sh
python -m pa serve            # web + worker + scheduler + discord bot
python -m pa worker           # standalone worker+scheduler (alternative split)
python -m pa discord-bot      # standalone bot (alternative split)
python -m pa create-owner     # owner account (interactive)
python -m pa check-config     # non-secret config + integration status
python -m pa provider-check   # endpoint conformance test
python -m pa gmail-auth       # one-time OAuth consent -> env values
python -m pa schedules        # list jobs
python -m pa schedule daily-email-summary on    # enable once gmail is configured
python -m pa chat <message>   # one conversational turn on the 'cli' channel
python -m pa vault-scan       # reindex the vault mirror now
python -m pa backup / restore / export-backup / import-backup
```

## Required env (production)

See `.env.example` for every name. Minimum to boot:

| var | notes |
|---|---|
| `PA_ENV=production` | Secure cookie + bind checks |
| `PA_DATA_DIR` | persistent volume mount (e.g. `/data`) |
| `PA_BIND` / `PA_PORT` | `127.0.0.1`/`8080` behind a same-host terminator |
| `PA_TIMEZONE` | IANA zone for schedules (default `Europe/London`) |
| `LLM_BASE_URL` + `LLM_MODEL` (+ `LLM_API_KEY`) | the model endpoint |

Integrations (all optional, all fail closed / "needs config" when absent):
`DISCORD_BOT_TOKEN`, `DISCORD_OWNER_ID`, `DISCORD_CHANNELS`,
`DISCORD_CHAT_CHANNEL_ID`, `GMAIL_CLIENT_ID`/`GMAIL_CLIENT_SECRET`/
`GMAIL_REFRESH_TOKEN` (or `GMAIL_TOKEN_FILE`), `VAULT_PATH`,
`VAULT_GIT_SYNC`, `SEARCH_ENABLED` + `TAVILY_API_KEY`,
`PA_FREEBUFF_REMOTE_URL` + `PA_FREEBUFF_REMOTE_TOKEN`,
`MAC_SSH_HOST` + `MAC_SSH_USER` + `MAC_SSH_KEY`.

Connections library (`/connect <app>` in Discord — see
`docs/CONNECTIONS.md`): `PA_SECRETS_KEY` (required — `python3 -c
"import secrets; print(secrets.token_hex(32))"`), `PA_PUBLIC_URL`
(public https base; a free Cloudflare Tunnel works — exact
`cloudflared` commands are in the doc), `PA_GOOGLE_CLIENT_*` for
`/connect google`, `PA_GITHUB_CLIENT_*` for `/connect github`,
`COMPOSIO_API_KEY` (+ `PA_COMPOSIO_MONTHLY_LIMIT`) for the managed
fallback. Migration 7 adds `connections`, `oauth_states`,
`oauth_clients`, `composio_usage` — encrypted token blobs only; losing
`PA_SECRETS_KEY` means reconnecting every app, so keep it in the host
secret store.

Conversational chat tuning (all have sane defaults):
`PA_CHAT_ASSISTANT` (default `pal`), `PA_CHAT_MAX_STEPS` (8),
`PA_CHAT_TURN_SECONDS` (300), `PA_CHAT_HISTORY_TURNS` (8),
`PA_CHAT_WORKING_SECONDS` (8), `PA_CHAT_APPROVAL_WAIT` (3600),
`PA_CHAT_SANDBOX`, `PA_SHELL_TIMEOUT`, `PA_SHELL_OUTPUT_CAP`,
`MAC_SSH_TIMEOUT`, `DDG_SEARCH_BASE`, `GCAL_API_BASE`.

Secrets come from the host's secret store/env only — never files in the
repo, never the database, never flags that would land in `ps`.

## Generic container deploy

```sh
# on the host
cp .env.example .env && $EDITOR .env
docker compose up -d                # builds the image, mounts pa_data volume
docker compose logs -f pa
curl -sf http://127.0.0.1:8080/healthz    # -> {"ok":true}
docker compose exec pa python -m pa check-config
docker compose exec pa python -m pa create-owner
```

`docker-compose.yml` binds `127.0.0.1:8080` only. Terminate TLS on the
same host, e.g. Caddy:

```
assist.example.com {
    reverse_proxy 127.0.0.1:8080
}
```

The Discord bot needs no inbound ports at all — it dials the gateway
outbound. The web UI is optional for daily use (approvals can be handled
in Discord) but keep it reachable for setup.

### Vault mirror

Mount a synced Markdown directory, e.g.
`- /srv/vault-mirror:/vault` and `VAULT_PATH=/vault`. Any sync method
works: official Obsidian Sync headless (only if the owner already has it),
Syncthing, or a `git pull` cron. The task tools only read it; the **chat**
agent can `vault_append`/`vault_write` (overwrite needs an approval
click), so mount read-write if you want note-taking. With
`VAULT_GIT_SYNC=true` and the mirror inside a git repo, every accepted
write is committed and pushed automatically.

## One-time onboarding order

1. Deploy with `LLM_*` set → `provider-check` passes.
2. `create-owner`, log into the UI, create the `email` assistant
   (tools: `email_list`, `email_read`, `vault_search`, `vault_read`).
3. Discord portal → bot token, invite to the server; set
   `DISCORD_BOT_TOKEN`, `DISCORD_OWNER_ID`, `DISCORD_CHANNELS`.
4. `python -m pa gmail-auth` on a machine with a browser → set the three
   `GMAIL_*` vars on the host (scopes: `gmail.readonly`, `gmail.compose`,
   `gmail.send`, `calendar.events` — read works without consent for the
   extras; send/draft/create still need approval clicks).
5. Sync the vault to the host → `VAULT_PATH` → `vault-scan`.
6. `python -m pa schedule daily-email-summary on` — the shipped job
   stays disabled until now.
7. Backups: schedule `export-backup` off-box (see `docs/BACKUP.md`).

## Operations

- **Health** — `GET /healthz` (also wired as the container HEALTHCHECK).
- **Logs** — structured, metadata only; a scrub filter redacts token/key
  shapes. Never bodies, never secrets.
- **Quota** — UI shows live daily usage vs. configured ceilings;
  `quota_exceeded` failures include the reset time.
- **Backups** — `python -m pa backup` (same-volume snapshot) and
  `export-backup` (encrypted, owner-chosen destination = the DR path).
- **Restore drill** — backup → wipe → restore → verify memories/owner
  facts intact. Do it once before trusting the service.
- **Upgrades** — `git pull` (or rebuild image) + `python -m pa migrate`;
  migrations are forward-only and idempotent.

## Upgrade the host

With a systemd user service (see `docs/DEPLOY.md`) pulling straight from
this repo:

```sh
git pull
pip install -r requirements.txt
python -m pa migrate
systemctl --user restart pa
```

Memory budget is **1 GB RAM + 2 GB swap** — that's why the chat agent
keeps prompts compact and there is deliberately no headless browser
dependency anywhere (web fetch is raw HTTP + HTML-to-text). If the
`pa-freebuff-sidecar` service is also installed on this host, restart it
too: `systemctl --user restart pa-freebuff-sidecar`.

**Free-API router** — migration 6 adds `router_usage_minutes`,
`router_usage_days`, `router_cooldowns` (usage + cooldown persistence).
Nothing changes until `PA_ROUTER_ENABLED=true` is in the service env and
an assistant is pinned to host `router` (SQL in `docs/ROUTER.md`). After
the restart, `python -m pa router-status` should show at least one
backend `yes`; `router-check` makes one tiny live call per configured
backend. Optional `pa-gemini-sidecar` / `pa-copilot-sidecar` user
services (units in `deploy/cli-sidecars/`) run the official CLIs on
loopback — they are independent of the `pa` service's restart cycle but
share the same env file convention (`~/.pa-sidecar/env`).

- **When Postgres** — more than one app instance, or writers on several
  hosts. Until then SQLite + WAL is correct and simpler.

## Failure states you will see

`quota_exceeded` (with reset time) · `provider_auth` · `search_disabled` ·
`fetch_blocked` · `deadline` · `cancelled` · integrations absent →
`needs config` on the dashboard, never fake success.
