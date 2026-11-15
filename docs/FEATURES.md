# Feature map

Where each capability lives in code + which doc covers it. Everything
is opt-in unless marked core — a bare install still boots, chats, and
schedules; extras show "needs config" until their env vars are set.

## Core (no config beyond Discord + a model)

| feature | code | doc |
|---|---|---|
| Discord bot: slash commands, DMs, chat, approval buttons | `pa/discordbot.py`, `pa/wsclient.py` | `docs/PARITY.md` |
| Chat agent loop (ReAct + tool calls) | `pa/chatagent.py`, `pa/chattools.py`, `pa/agent.py` | `docs/PARITY.md` |
| Durable task ledger + worker + retries | `pa/worker.py`, `pa/db.py` | `docs/ARCHITECTURE.md` |
| Scheduler (daily/weekly/interval jobs, all seeded disabled) | `pa/scheduler.py`, `pa/sysjobs.py` | `docs/JOBS.md` |
| SQLite schema + migrations | `pa/db.py` | `docs/ARCHITECTURE.md` |
| Policy floor — approval-gates owner-account writes | `pa/policy.py`, `pa/approvals.py` | `docs/SECURITY.md` |
| Untrusted-content fencing (email/web/SMS = data) | `pa/untrusted.py` | `docs/SECURITY.md` |
| Web dashboard + `/healthz` + approvals UI | `pa/webapp.py` | `docs/RUNBOOK.md` |
| Config/env parsing + bind + provider safety rails | `pa/config.py` | `docs/CONFIG.md` |
| `doctor` / `check-config` / `migrate` / `backup` / `eval` CLI | `pa/__main__.py`, `pa/doctor.py` | `docs/SETUP.md` |

## Opt-in integrations

| feature | env vars | code + doc |
|---|---|---|
| Gmail read/draft/send | `GMAIL_*` or `/connect google` | `pa/gmail.py` — `docs/INTEGRATIONS.md` |
| Google Calendar | same Google OAuth | `pa/gcal.py`, `pa/calsource.py` — `docs/INTEGRATIONS.md` |
| Connections library (`/connect <app>`) | `PA_SECRETS_KEY`, `PA_PUBLIC_URL`, `COMPOSIO_API_KEY` (optional) | `pa/connectflows.py`, `pa/oauth.py`, `pa/mcp.py`, `pa/composio.py` — `docs/CONNECTIONS.md` |
| Obsidian notes mirror | `VAULT_PATH`, `VAULT_GIT_SYNC` | `pa/vault.py` — `docs/INTEGRATIONS.md` |
| Web search | `TAVILY_API_KEY` or keyless DDG | `pa/search.py` |
| TTS / voice notes | `FISH_API_KEY`, `AZURE_SPEECH_*`, or Edge TTS | `pa/voice.py` — `docs/VOICE.md` |
| Voice-note transcription | `GROQ_WHISPER_BASE`/key or local whisper | `pa/voice.py` — `docs/VOICE.md` |
| Discord voice-channel calls | `PA_CALL_GUILD_ID` + `requirements-voice.txt` | `pa/call.py`, `pa/callbrain.py`, `pa/live.py` — `docs/VOICE.md` |
| Free-API router (Groq/Gemini/OpenRouter/CF/Pollinations failover) | `PA_ROUTER_ENABLED` + keys | `pa/router.py` — `docs/ROUTER.md` |
| CLI providers as model backends | `PA_FREEBUFF_*`, `PA_GEMINI_CLI_URL`, `PA_COPILOT_CLI_URL` | `pa/freebuff_session.py`, `pa/freebuff_sidecar.py`, `pa/cli_session.py` — `docs/FREEBUFF_CONNECTION.md` |
| Email triage / morning briefing / proactive jobs | `PA_PROACTIVE`, `PA_BRIEFING_*`, per-job toggles | `pa/inboxwatch.py`, `pa/triage.py`, `pa/briefing.py`, `pa/proactive.py` — `docs/JARVIS.md` |
| Memory OS, goals, owner facts | `PA_GOALS`, `PA_OWNER_*` | `pa/memory.py`, `pa/memblocks.py`, `pa/goals.py` — `docs/FEATURES.md` |
| pal's computer: persistent Chromium + `/desktop` | `PA_COMPUTER_*` | `pa/computer.py`, `pa/desktop.py` — `docs/COMPUTER.md` |
| Phone node (Android notifications/SMS/location) | `PA_NODES` + `PA_NODE_<NAME>_TOKEN`, `deploy/pa-node/` | `pa/nodes.py`, `pa/node_agent.py` — `docs/PHONE.md`, `docs/NODES.md` |
| Owner Mac over SSH | `MAC_SSH_HOST`/`MAC_SSH_USER`/`MAC_SSH_KEY` | `pa/mac.py` — `docs/NODES.md` |
| Research mode / workspace / sub-agents | `PA_RESEARCH`, `PA_SUBAGENTS`, `PA_WORKSPACE_MB` | `pa/research.py`, `pa/subagents.py`, `pa/workspace.py` — `docs/RESEARCH.md`, `docs/WORKSPACE.md`, `docs/SUBAGENTS.md` |
| Made-tools + skill extraction | `PA_MADETOOLS`, `PA_SKILLEXTRACT` | `pa/madetools.py`, `pa/skillextract.py` — `docs/TOOLMAKING.md` |
| Self-evolve loop (evals → versioned change → approval) | `PA_EVOLVE_*` (all off) | `pa/evolve/` — `docs/EVOLVE.md` |
| Backups + DR restore checks | `PA_BACKUP_*` | `pa/backup.py`, `pa/backupjob.py`, `pa/drcheck.py` — `docs/BACKUP.md`, `docs/DR.md` |
| Webhooks / external ingest | `PA_PUBLIC_URL`, `PA_GITHUB_HOOK_AGENT` | `pa/webhooks.py`, `pa/ghcards.py` — `docs/WEBHOOKS.md` |
| Telegram mirror | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | `pa/telegram.py` — `docs/PARITY.md` |

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
