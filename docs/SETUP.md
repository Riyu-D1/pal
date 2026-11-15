# Setup — manual walkthrough

One-command path: `scripts/setup.sh` does steps 1–4 below for you.
This doc is the same thing, step by step, plus every optional
integration. If you're an AI agent, use `AGENTS.md` instead — it's the
same instructions in agent form.

## 1. Prerequisites

- **Python 3.11+** (3.12 recommended). macOS: `brew install python@3.12`;
  Debian/Ubuntu: `apt install python3.12 python3.12-venv`.
- A **Discord bot token** + your **Discord user id** (see below).
- One **LLM endpoint** — free Groq key is the easiest start.
- Outbound HTTPS only; no inbound ports needed (Discord uses a
  gateway socket; the web UI can stay on loopback).

### Getting the Discord bits

1. discord.com/developers → **New Application** → name it anything.
2. **Bot** tab → **Reset Token** → copy it → that's `DISCORD_BOT_TOKEN`.
3. Same page → enable **Message Content intent** (required for chat).
4. **OAuth2 → URL Generator** → scope `bot`, permissions
   Send Messages / Use Slash Commands / Attach Files → open the URL,
   add the bot to a server you own.
5. Discord app → Settings → Advanced → **Developer Mode** on →
   right-click your own name → **Copy User ID** → `DISCORD_OWNER_ID`.

## 2. Install

```sh
git clone <this repo> && cd pal
python3.12 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt        # 4 pinned deps, nothing else
```

## 3. Configure

```sh
cp .env.example .env     # then edit — three required values:
```

| var | value |
|---|---|
| `DISCORD_BOT_TOKEN` | the token from above |
| `DISCORD_OWNER_ID` | your user id from above |
| `LLM_BASE_URL` / `LLM_MODEL` / `LLM_API_KEY` | e.g. Groq: `https://api.groq.com/openai/v1` / `openai/gpt-oss-120b` / your key |

Or use the free-backend router instead of `LLM_*`:
`PA_ROUTER_ENABLED=true` + `GROQ_API_KEY=...` (or Gemini/OpenRouter/
Cloudflare/Pollinations). `docs/ROUTER.md` has the full matrix.

Sensible extras, all optional: `PA_SECRETS_KEY`
(`openssl rand -hex 32`, needed for `/connect`), `PA_PUBLIC_URL`
(tunnel URL for OAuth callbacks), `PA_OWNER_EMAIL`, `VAULT_PATH`,
`TAVILY_API_KEY`. Full list: `docs/CONFIG.md`.

## 4. Initialise + verify

```sh
set -a; . ./.env; set +a
python -m pa migrate        # creates ./data/pa.db, runs all migrations
python -m pa doctor         # required checks must all pass
python -m pa create-owner   # pick web-UI username + password
python -m pa serve &        # web + worker + scheduler + discord bot
curl http://127.0.0.1:8080/healthz    # {"ok":true,...}
```

DM the bot — it's ready. The web UI at `http://127.0.0.1:8080`
shows the dashboard, integrations panel, and pending approvals.

## 5. Optional integrations (all off until configured)

| thing | env vars | doc |
|---|---|---|
| Gmail read/draft/send | `GMAIL_CLIENT_ID/SECRET/REFRESH_TOKEN` (GCP OAuth desktop client) or `/connect google` | `docs/INTEGRATIONS.md` |
| Google Calendar | same Google OAuth (add calendar scope) | `docs/INTEGRATIONS.md` |
| Connections library (`/connect <app>`) | `PA_SECRETS_KEY` + `PA_PUBLIC_URL` + `COMPOSIO_API_KEY` and/or `PA_MCP_URL` | `docs/CONNECTIONS.md` |
| Notes mirror (Obsidian) | `VAULT_PATH`, `PA_VAULT_MODE` | `docs/INTEGRATIONS.md` |
| Web search | `TAVILY_API_KEY` (keyless DDG fallback exists) | `docs/CONFIG.md` |
| Voice notes TTS | `FISH_API_KEY` or Edge TTS (free, no key) | `docs/VOICE.md` |
| Discord voice calls | `PA_DISCORD_VOICE_CALLS=true` + PyNaCl | `docs/VOICE.md` |
| Free-API router | `PA_ROUTER_ENABLED` + backend keys | `docs/ROUTER.md` |
| pal's computer (Chromium) | `PA_COMPUTER_*` | `docs/COMPUTER.md` |
| Phone node | `PA_NODE_TOKEN`, deploy/node kit | `docs/PHONE.md` |
| Self-evolve loop | `PA_EVOLVE_*` (all off) | `docs/EVOLVE.md` |
| Owner email | `PA_OWNER_EMAIL` | `docs/CONFIG.md` |
| Home/weather context | `PA_HOME_LAT/LON`, `PA_BRIEFING_CITY` | `docs/CONFIG.md` |

## 6. Production

See `docs/DEPLOY.md` — systemd units, Docker/compose, Cloudflare
tunnel for OAuth callbacks, backups (`python -m pa backup` cron).
