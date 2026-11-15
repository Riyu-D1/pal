# Architecture

Single-process Python service. One owner, one worker, one Discord bot,
one SQLite file.

```
                ┌────────────────────────── waitress (WSGI) ──┐
 browser ──────►│  pa.webapp  routes + sessions + CSRF        │
                └───────┬─────────────────────────────────────┘
                        │ sqlite (WAL)
        ┌───────────────┼───────────────────────────────────┐
        ▼               ▼                                   ▼
  pa.webapp      pa.worker (thread)              pa.discordbot (threads)
  reads state    claim→run→ledger                gateway ws + REST notifier
                     │      pa.scheduler (tick)  owner-allowlisted commands
                     ▼                           pa.backup (CLI)
              pa.agent.AgentRunner ── pa.provider (owner-pinned endpoint)
                     │               ── pa.egress   (guarded web fetch)
                     │               ── pa.search   (Tavily, opt-in)
                     ▼               ── pa.gmail    (read-only API)
              pa.tools.ToolContext ── pa.vault    (read-only mirror index)
                                     ── pa.memory / pa.approvals
```

## Modules (`pa/`)

| module | role |
|---|---|
| `config.py` | env parsing, `LLM_*` validation, `(host, model)` allowlist, bind-safety check |
| `db.py` | connect (WAL, FK, busy-timeout), versioned migrations v1→v4 |
| `passwords.py` | `hashlib.scrypt` hash/verify |
| `provider.py` | OpenAI-compatible client; streaming toggle; tools toggle; `/models`; error taxonomy; `x-ratelimit-*` capture |
| `provider_check.py` | `pa provider-check` conformance probe (models/chat/tool-call) |
| `pacer.py` | token-aware per-minute + per-day buckets, DB-persisted `usage_days` |
| `egress.py` | guarded fetch: DNS vetting, vetted-IP dial, SNI, redirect re-vetting, size/time/content-type bounds, bounded decompression |
| `netclient.py` | separate allowlisted client for provider/search/integration hosts |
| `search.py` | Tavily opt-in service + query privacy screening |
| `scheduler.py` | durable TZ-aware jobs; `schedule_runs` ledger = no duplicates |
| `vault.py` | read-only Obsidian-mirror index/search; escape+secret skips |
| `gmail.py` | read-only Gmail API (`gmail.readonly`), OAuth refresh, onboarding flow |
| `wsclient.py` | stdlib WebSocket client (frame codec, masking, TLS) |
| `discordbot.py` | gateway bot: identify/resume, owner allowlist, dedup, commands, notifier, voice-note route |
| `voice.py` | TTS provider chain (gemini→fish→azure→cloudflare) → ffmpeg OGG/Opus 48k mono + waveform; Whisper transcription (groq→cloudflare); attachment download; `VoiceServices` for chat tools |
| `call.py` | `CallManager` (quiet hours, per-hour cap, call flow + channel create) driving `DiscordPyRuntime` — discord.py/`davey`/`voice-recv` do the voice gateway, UDP, Opus, DAVE E2EE on a second gateway session |
| `memory.py` | per-assistant memories + shared owner facts, FTS5 |
| `approvals.py` | approval items; `decide` is the only writer of state changes |
| `agent.py` | transcript rebuild from ledger, step persistence, replay, repair |
| `worker.py` | claim/lease/heartbeat/cancel/retry/resume loop + scheduler tick |
| `tools.py` | `fetch_url`, `web_search`, `memory_search`, `propose_memory`, `email_list`, `email_read`, `vault_search`, `vault_read`, `finish` |
| `citations.py` | claim→source validation, unsupported labelling |
| `backup.py` | online backup, AES-GCM encrypted export/import, restore |
| `webapp.py` | WSGI app: routes, sessions, CSRF/Origin, throttle, integrations panel |
| `views.py`, `static/` | server-rendered UI, 375px-safe CSS |
| `logsetup.py` | logging with secret scrub filter |

## Ledger model

`tasks` — one row per submitted task: `queued → running ⇄ waiting_approval
→ succeeded | failed | cancelled`.

`attempts` — each run of a task: lease owner (worker id) + lease expiry,
`active | abandoned | done | cancelled`. Claiming is one `BEGIN IMMEDIATE`
transaction: pick a `queued` task, or a `running` task whose *only* attempt
has an **expired** lease; mark that attempt abandoned; insert a new attempt;
flip the task to `running`. Terminal tasks are never claimed.

`steps` — ordered per task (`seq`): `model_call` and `tool_call` rows carry a
`request_hash` and the stored result. On resume a `done` step is replayed
from its row — never re-executed, never duplicated. A step left `running`
(crash mid-tool) is `aborted`; a *read-only* unfinished step may be re-run,
an uncertain external effect fails closed.

`effects` — `effect_key = approval_id`, unique. Applying an approval is one
transaction: insert effect → if the insert created the row, apply the memory
change → mark the approval applied. Retries converge on the same key, so an
approval can never be applied twice (tested under a simulated crash before
and after commit, and concurrent apply).

## Worker lifecycle

`Worker.run_once()` claims one task, spawns a **heartbeat thread with its
own DB connection** that renews the lease every ~30 s while the attempt is
active and still owned by this worker. If renewal fails (task reclaimed),
the heartbeat sets a flag and the attempt stops writing. Cancel is a flag
checked between steps; network calls use short timeouts. On cancel: attempt
+ task → cancelled, pending approvals voided, completed steps untouched.

## Agent loop

`AgentRunner` rebuilds the model transcript **only** from completed ledger
steps (plus the task's `task_messages`), so a crash mid-turn never replays a
partial turn. Orphaned assistant `tool_calls` left by a crash are repaired:
read-only/idempotent tools are re-driven; uncertain external effects abort
the attempt. Serial tool calls only (one call per turn, by policy).

Tools are deliberately boring: `fetch_url` (guarded), `web_search`
(privacy-screened, opt-in), `memory_search` (assistant-scoped + owner
facts), `propose_memory` (creates an approval — the only write path),
`finish` (claims + sources validated before accept).

## Data & bounds

SQLite in WAL mode behind versioned migrations (`schema_migrations`).
Bounded: turns, wall-clock deadline, output size, retries, request body,
header size, JSON depth, response size, decompression, fetch time. Files
only ever under `DATA_DIR` via `realpath` + `O_NOFOLLOW`.

## Scaling boundary

Correct for one owner on one machine. Move to Postgres when: more than one
app machine, or writers on several hosts (stated in `docs/RUNBOOK.md`).
