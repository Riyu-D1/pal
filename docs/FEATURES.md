# Feature map

Where each capability lives in code + which doc covers it. Everything
is opt-in unless marked core — a bare install still boots, chats, and
schedules; extras show "needs config" until their env vars are set.

## Core (no config beyond Discord + a model)

| feature | code | doc |
|---|---|---|
| Discord bot: slash commands, DMs, chat, approval buttons | `pa/discordbot.py`, `pa/discordapi.py`, `pa/commands*.py` | `docs/PARITY.md` |
| Chat agent loop (ReAct + tool calls) | `pa/chatbrain.py`, `pa/chattools.py`, `pa/toolloop.py` | `docs/PARITY.md` |
| Durable task ledger + worker + retries | `pa/ledger.py`, `pa/worker.py`, `pa/executor.py` | `docs/ARCHITECTURE.md` |
| Scheduler (daily/weekly/interval jobs, all seeded disabled) | `pa/scheduler.py`, `pa/jobs.py` | `docs/JOBS.md` |
| SQLite schema + migrations | `pa/db.py`, `pa/migrations.py` | `docs/ARCHITECTURE.md` |
| Policy floor — approval-gates owner-account writes | `pa/policy.py`, `pa/approvals.py` | `docs/SECURITY.md` |
| Untrusted-content fencing (email/web/SMS = data) | `pa/fencing.py` | `docs/SECURITY.md` |
| Web dashboard + `/healthz` + approvals UI | `pa/webapp.py` | `docs/RUNBOOK.md` |
| Config/env parsing + bind + provider safety rails | `pa/config.py` | `docs/CONFIG.md` |
| `doctor` / `check-config` / `migrate` / `backup` / `eval` CLI | `pa/__main__.py`, `pa/doctor.py` | `docs/SETUP.md` |

## Opt-in integrations

| feature | env vars | code + doc |
|---|---|---|
| Gmail read/draft/send | `GMAIL_*` or `/connect google` | `pa/gmail*.py` — `docs/INTEGRATIONS.md` |
| Google Calendar | same Google OAuth | `pa/gcal*.py` — `docs/INTEGRATIONS.md` |
| Connections library (`/connect <app>`) | `PA_SECRETS_KEY`, `PA_PUBLIC_URL`, `COMPOSIO_API_KEY`, `PA_MCP_URL` | `pa/connections*.py`, `pa/mcp*.py` — `docs/CONNECTIONS.md` |
| Obsidian notes mirror | `VAULT_PATH`, `PA_VAULT_MODE` | `pa/vault*.py` — `docs/INTEGRATIONS.md` |
| Web search | `TAVILY_API_KEY` or keyless DDG | `pa/search.py` |
| TTS / voice notes | `FISH_API_KEY`, `AZURE_SPEECH_*`, or Edge TTS | `pa/tts*.py` — `docs/VOICE.md` |
| Voice-note transcription | `GROQ_WHISPER_BASE`/key or local whisper | `pa/stt*.py` — `docs/VOICE.md` |
| Discord voice-channel calls | `PA_DISCORD_VOICE_CALLS` + PyNaCl | `pa/voicecall*.py` — `docs/VOICE.md` |
| Free-API router (Groq/Gemini/OpenRouter/CF/Pollinations failover) | `PA_ROUTER_ENABLED` + keys | `pa/router*.py`, `pa/backends*.py` — `docs/ROUTER.md` |
| CLI providers as model backends | `PA_FREEBUFF_CLI`, `PA_GEMINI_CLI`, `PA_COPILOT_CLI` | `pa/freebuff*.py` — `docs/FREEBUFF_CONNECTION.md` |
| Email triage / morning briefing / proactive jobs | `PA_PROACTIVE`, `PA_BRIEFING_*`, per-job toggles | `pa/jobs.py`, `pa/proactive*.py` — `docs/JARVIS.md` |
| Memory OS, goals, owner facts | `PA_GOALS`, `PA_OWNER_*` | `pa/memory*.py`, `pa/goals*.py` — `docs/FEATURES.md` |
| pal's computer: persistent Chromium + `/desktop` | `PA_COMPUTER_*` | `pa/computer*.py` — `docs/COMPUTER.md` |
| Phone node (Android notifications/SMS/location) | `PA_NODE_TOKEN` + `deploy/node` | `pa/phonenode*.py` — `docs/PHONE.md`, `docs/NODES.md` |
| Owner Mac over SSH | `PA_MAC_SSH*` | `pa/macnode*.py` — `docs/NODES.md` |
| Research mode / workspace / sub-agents | `PA_RESEARCH`, `PA_SUBAGENTS`, `PA_WORKSPACE_MB` | `pa/research*.py`, `pa/subagent*.py` — `docs/RESEARCH.md`, `docs/WORKSPACE.md`, `docs/SUBAGENTS.md` |
| Made-tools + skill extraction | `PA_TOOLMAKING`, `PA_SKILLS` | `pa/toolmaking*.py` — `docs/TOOLMAKING.md` |
| Self-evolve loop (evals → versioned change → approval) | `PA_EVOLVE_*` (all off) | `pa/evolve*.py` — `docs/EVOLVE.md` |
| Backups + DR restore checks | `PA_BACKUP_*` | `pa/backup*.py` — `docs/BACKUP.md`, `docs/DR.md` |
| Webhooks / external ingest | `PA_WEBHOOKS`, `PA_PUBLIC_URL` | `pa/webhooks*.py` — `docs/WEBHOOKS.md` |
| Telegram mirror | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | `pa/telegram*.py` — `docs/PARITY.md` |

## Conventions worth knowing

- **`PA_*` env vars** are pal's own; provider vars keep the provider's
  name (`GROQ_API_KEY`, `COMPOSIO_API_KEY`). `docs/CONFIG.md` lists all.
- **Owner identity** = `DISCORD_OWNER_ID` + `PA_OWNER_EMAIL` +
  `PA_TIMEZONE` + `PA_HOME_LAT/LON` + `PA_BRIEFING_*`. The owner's
  *name* lives in `owner_facts` (learned in chat) — no env var.
- **Feature flags** default off; enabling one never weakens the policy
  floor or untrusted fencing.
- **Tests** fake every integration (Discord gateway, Gmail, model
  endpoints); nothing needs credentials to run green.
