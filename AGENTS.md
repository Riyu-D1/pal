# AGENTS.md — set up pal for your human

You are an AI agent (Claude Code, Cursor, Codex, Devin, …) and the
human just handed you this repo and said **"set this up for me"**.
This file is your runbook. Follow it literally; it works.

## What pal is

A self-hosted personal assistant: a single Python 3.12 service
(`pa/`, stdlib + 4 pinned deps) that runs a Discord bot, a task
worker, a scheduler, and a small web dashboard — all in one process,
one SQLite volume. Discord is the interface. The human becomes "the
owner": the only Discord account allowed to talk to it.

## Non-negotiable safety rules for you

1. **Never print, echo, or commit secrets.** `.env` is gitignored;
   values go only into env or the env file. Never put a token in a
   command line other agents/logs can see — write it into `.env`.
2. **Every account write needs the human's approval click** — that's
   the product's design, not a limitation. Do not look for a bypass;
   there is deliberately none.
3. **Ask the human only for what you cannot do yourself.** You can't
   create their Discord bot, sign into their accounts, or pick their
   hosting provider. Everything else (code, config, venv, db, systemd,
   tunnel, tests) you can and should do.
4. pal is a **single-owner** app. Do not expose it to other users or
   make the web UI public without TLS + the documented topology
   (`docs/SECURITY.md`); keep `PA_BIND=127.0.0.1` behind a terminator.
5. Do not enable feature flags unless the human asked — everything
   risky is off by default and that's correct.

## What you must ask the human for (only these)

| ask | why | how they get it |
|---|---|---|
| `DISCORD_BOT_TOKEN` | the bot can't exist without it | discord.com/developers → New Application → Bot → Reset Token; also enable **Message Content intent** (chat needs it) |
| `DISCORD_OWNER_ID` | locks the bot to them | Discord → Settings → Advanced → Developer Mode ON → right-click their name → Copy User ID |
| one LLM endpoint | pal needs a brain | free options: `LLM_BASE_URL=https://api.groq.com/openai/v1` + a free `LLM_API_KEY` from console.groq.com + `LLM_MODEL` (e.g. `openai/gpt-oss-120b`); **or** `PA_ROUTER_ENABLED=true` + any one of `GROQ_API_KEY`/`GEMINI_API_KEY`/`OPENROUTER_API_KEY`/`CLOUDFLARE_*` |
| where it runs | hosting is their call | Oracle Always Free VM, any Linux box, Docker host, or their Mac/laptop — see `docs/DEPLOY.md` |
| `COMPOSIO_API_KEY` | optional — unlocks `/connect <app>` for ~1000 apps | composio.dev free Hobby key; skip if they don't want it |

Optional but useful: `PA_OWNER_EMAIL` (fixed-recipient email tool),
`PA_SECRETS_KEY` (needed for `/connect` — you can generate it yourself:
`python3 -c "import secrets;print(secrets.token_hex(32))"`),
`PA_PUBLIC_URL` (needed for OAuth callbacks — a free Cloudflare
quick-tunnel URL works; docs/CONNECTIONS.md).

## Setup steps — do these yourself

```sh
# 1. clone + enter the repo (or you're already in it)
# 2. one-command setup — creates .venv, installs deps, writes .env,
#    migrates the db, runs `pa doctor`:
DISCORD_BOT_TOKEN=… DISCORD_OWNER_ID=… \
LLM_BASE_URL=… LLM_MODEL=… LLM_API_KEY=… \
  scripts/setup.sh --non-interactive
#    (interactive `scripts/setup.sh` prompts for the same three values)

# 3. load the venv + .env into THIS shell — pal never reads .env by
#    itself (systemd uses EnvironmentFile=, compose uses env_file:):
. .venv/bin/activate && set -a && . ./.env && set +a

#    verify — all FAIL lines must be green:
python -m pa doctor

# 4. create the web-UI owner account (human picks username+password —
#    ask them to run it, or run it for them interactively):
python -m pa create-owner

# 5. boot + health check:
python -m pa serve &                    # or the systemd unit below
curl -sf http://127.0.0.1:8080/healthz  # -> {"ok":true,...}
```

Then tell the human: **DM the bot on Discord**. A dummy/typo'd token
fails *gracefully* (the bot retries; check-config still works) — an
invalid-token loop in logs means fix the token, not the code.

## Verify your work (in order)

| check | command | pass means |
|---|---|---|
| config sane | `python -m pa check-config` | no REFUSED lines; integrations show set/needs-config |
| doctor | `python -m pa doctor` | exit 0 — all required OK |
| tests | `python -m unittest discover -s tests` | `OK` at the end (~1500 tests) |
| evals | `python -m pa eval` | all fixtures pass |
| serve | `python -m pa serve` + `curl …/healthz` | `{"ok":true}` |
| scan | `python scripts/check_no_personal_data.py` | zero hits |

## Repo map

```
pa/            the whole app (stdlib-first Python, no framework)
  __main__.py  every CLI command: serve, doctor, migrate, chat, eval…
  config.py    ALL env parsing + bind/provider safety rules (single
               source of truth for env vars — docs/CONFIG.md mirrors it)
  discordbot.py Discord gateway bot + slash commands + chat turns
  chattools.py the tool catalogue the chat agent can call
  webapp.py    the small dashboard (login, approvals, integrations)
  db.py        connect() + versioned schema migrations
tests/         ~1500 unittest cases vs deterministic fakes — run all
evals/         offline eval fixtures for the agent loop (pa eval)
docs/          SETUP/CONFIG/DEPLOY/TROUBLESHOOTING/ARCHITECTURE + per-
               feature docs (CONNECTIONS, ROUTER, VOICE, COMPUTER…)
deploy/        systemd units, rebuild script, sidecar + node kits
scripts/       setup.sh (one-command), check_no_personal_data.py
.env.example   every variable, commented — copy to .env
docker-compose.yml + Dockerfile — one-container production path
```

## When something fails

`docs/TROUBLESHOOTING.md` covers the usual suspects (Discord's
100-char command limit, Groq quota → router fallback, macOS
`CERTIFICATE_VERIFY_FAILED` → `SSL_CERT_FILE=$(python -m certifi)`,
the `osascript`/Terminal permission dance if you run on a Mac).
Anything missing from an integration shows as "needs config" — check
`python -m pa doctor` first, then `docs/CONFIG.md` for the exact env
var. Never edit `pa/` to work around a config problem.

## Contributing changes

Follow the existing style: stdlib-first, no new deps without a strong
reason, one approval floor for owner-account writes, untrusted content
always fenced. Keep `python -m unittest discover -s tests` green and
`python scripts/check_no_personal_data.py` clean. See CONTRIBUTING.md.
