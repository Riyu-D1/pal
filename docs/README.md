# Docs index

Start with the repo [`README.md`](../README.md). If you're an AI agent
setting pal up, follow [`AGENTS.md`](../AGENTS.md).

## Set up and run

| doc | what's in it |
|---|---|
| [SETUP.md](SETUP.md) | manual setup walkthrough + optional integrations table |
| [CONFIG.md](CONFIG.md) | every env var, with defaults (mirrors `.env.example`) |
| [DEPLOY.md](DEPLOY.md) | systemd, Docker/compose, tunnel, fly.io, upgrades |
| [RUNBOOK.md](RUNBOOK.md) | running it in the cloud, reverse proxy, day-2 ops |
| [TROUBLESHOOTING.md](TROUBLESHOOTING.md) | known failure modes and fixes |
| [BACKUP.md](BACKUP.md) · [DR.md](DR.md) | backups, restore, disaster-recovery playbooks |
| [SECURITY.md](SECURITY.md) | threat model, approval floor, fencing, bind rules |

## How it works

| doc | what's in it |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | components, task ledger, lifecycle |
| [FEATURES.md](FEATURES.md) | feature → code → doc map |
| [PARITY.md](PARITY.md) | chat/UX feature reference |
| [PROVIDERS.md](PROVIDERS.md) · [ROUTER.md](ROUTER.md) | model endpoints, free-API failover router |
| [FREEBUFF_CONNECTION.md](FREEBUFF_CONNECTION.md) | Freebuff CLI session provider design notes |

## Optional features (all off by default)

| doc | feature |
|---|---|
| [INTEGRATIONS.md](INTEGRATIONS.md) | Gmail, Calendar, Obsidian vault mirror |
| [CONNECTIONS.md](CONNECTIONS.md) | `/connect <app>` — OAuth/MCP/Composio connections |
| [JARVIS.md](JARVIS.md) | proactive: inbox watch, triage, morning briefing |
| [JOBS.md](JOBS.md) | recurring unattended agent jobs |
| [VOICE.md](VOICE.md) | voice notes in/out, Discord voice calls |
| [COMPUTER.md](COMPUTER.md) | pal's own Chromium + `/desktop` |
| [NODES.md](NODES.md) · [PHONE.md](PHONE.md) | remote nodes, Android/Termux phone node |
| [WORKSPACE.md](WORKSPACE.md) · [SUBAGENTS.md](SUBAGENTS.md) · [RESEARCH.md](RESEARCH.md) | file workspace, sub-agents, research reports |
| [TOOLMAKING.md](TOOLMAKING.md) | pal-made tools |
| [WEBHOOKS.md](WEBHOOKS.md) | signed inbound webhooks, GitHub CI cards |
| [EVOLVE.md](EVOLVE.md) | self-healing / self-improvement loop |
