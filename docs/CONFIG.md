# Configuration reference

Every setting is environment-driven — nothing is stored in the repo.
Secrets come only from env or the host's secret store; **never commit
`.env`** (it's in `.gitignore` already).

Copy `.env.example` → `.env` and fill in, or run `scripts/setup.sh`
which writes a minimal `.env` for you. `python -m pa doctor` reports
what's configured and what's missing; `python -m pa check-config`
prints the resolved non-secret config. This file lists every variable
the code reads — `.env.example` has the same list with fuller comments.

## The minimal set

A working pal needs only:

| var | why |
|---|---|
| `DISCORD_BOT_TOKEN` | the bot's token (Discord dev portal → Bot) |
| `DISCORD_OWNER_ID` | your Discord user id — the only account allowed in |
| `LLM_BASE_URL` + `LLM_MODEL` + `LLM_API_KEY` | one OpenAI-compatible endpoint — a free Groq key works |

Everything else is **opt-in** and shows "needs config" until set —
nothing fakes success. The owner's *name* is an `owner_fact` pal learns
in chat (or the `human` memory block) — deliberately not an env var.
Identity vars that DO exist: `DISCORD_OWNER_ID`, `PA_OWNER_EMAIL`,
`PA_TIMEZONE`, `PA_BRIEFING_CITY`/`PA_BRIEFING_LAT`/`PA_BRIEFING_LON`,
`PA_HOME_LAT`/`PA_HOME_LON`.

Defaults below are the values in `.env.example`; `*(unset)*` means the
feature stays off until you set it.


## core

| var | default | what it does |
|---|---|---|
| `PA_ENV` | `production` | development \| test \| production |
| `PA_DATA_DIR` | `/data` | Where SQLite and backups live (mount a volume here in production) |
| `PA_BIND` | `127.0.0.1` | compose publishes to host loopback only), or keep loopback behind a TLS terminator like Caddy. |
| `PA_PORT` | `8080` |  |
| `PA_CONTAINER_BIND` | *(unset)* | only inside Docker/compose deployments |
| `PA_TIMEZONE` | `Europe/London` | Scheduler default timezone (IANA name; per-schedule override exists) |

## Inference provider (owner-supplied OpenAI-compatible endpoint)

| var | default | what it does |
|---|---|---|
| `LLM_BASE_URL` **required** | *(unset)* | Example: Groq  -> LLM_BASE_URL=https://api.groq.com/openai/v1 Example: local -> LLM_BASE_URL=http://127.0.0.1:11434/v1 (no key needed) |
| `LLM_MODEL` **required** | *(unset)* |  |
| `LLM_API_KEY` **required** | *(unset)* |  |
| `LLM_STREAMING` | `false` | SSE streaming on/off |
| `LLM_TOOLS` | `true` | set false for endpoints without tool calling |

## Discord interface

| var | default | what it does |
|---|---|---|
| `DISCORD_ENABLED` | `true` | Gateway bot. Disabled cleanly while the token is unset. |
| `DISCORD_BOT_TOKEN` **required** | *(unset)* |  |
| `DISCORD_OWNER_ID` **required** | *(unset)* | The ONE user allowed to drive the bot (fail closed for everyone else; when unset, everyone is refused). |
| `DISCORD_CHANNELS` | *(unset)* | "1234567890", "default": "1234567891"} |
| `DISCORD_CHAT_CHANNEL_ID` | *(unset)* | Optional guild channel where plain owner messages are treated as chat (DMs and @mentions always count as chat): |

## Conversational chat agent

| var | default | what it does |
|---|---|---|
| `PA_CHAT_ASSISTANT` | *(unset)* | Assistant (by name) whose provider pin drives chat. The freebuff-cli pin gives the owner-LLM agent session; any OpenAI-compatible pin works |
| `PA_CHAT_MAX_STEPS` | *(unset)* | ReAct tool steps per turn |
| `PA_CHAT_TURN_SECONDS` | *(unset)* | overall turn deadline |
| `PA_CHAT_HISTORY_TURNS` | *(unset)* | kept turns; older ones are summarised |
| `PA_CHAT_WORKING_SECONDS` | *(unset)* | delay before the "working on it" note |
| `PA_CHAT_PROGRESS` | *(unset)* | live turn progress: 👀 reaction + one |
| `PA_CHAT_APPROVAL_WAIT` | *(unset)* | how long a turn waits on a button click |
| `PA_ATTACH_MAX_BYTES` | *(unset)* | inbound image/PDF download cap |
| `PA_PDF_MAX_PAGES` | *(unset)* | pypdf extraction caps for DM'd PDFs |
| `PA_PDF_MAX_CHARS` | *(unset)* |  |
| `PA_VISION` | *(unset)* | image captions for DM'd images + the |
| `PA_VISION_ORDER` | *(unset)* | backend key exists (GROQ_API_KEY / GEMINI_API_KEY / CLOUDFLARE_*) |
| `PA_VISION_MODEL_GROQ` | *(unset)* | override the built-in candidate list |
| `PA_VISION_MODEL_GEMINI` | *(unset)* |  |
| `PA_VISION_MODEL_CLOUDFLARE` | *(unset)* |  |
| `PA_VISION_MAX_BYTES` | *(unset)* | images over this get no caption |
| `PA_VIDEO` | *(unset)* | video_look: sample+caption frames |
| `PA_VIDEO_MAX_MB` | *(unset)* | (needs ffmpeg + a vision backend) |
| `PA_MEMORY_SAVE_APPROVAL` | *(unset)* | also gate per-assistant memory_save |
| `PA_MEMORY_AUTO_PROPOSE` | *(unset)* | end-of-turn fast pass may PROPOSE up to |
| `PA_MEMORY_PROPOSE_MAX` | *(unset)* | owner_facts as approval cards (never |
| `PA_CHAT_SANDBOX` | *(unset)* | shell sandbox dir (default <data>/chat-sandbox) |
| `PA_SHELL_TIMEOUT` | *(unset)* | PA_SHELL_OUTPUT_CAP=8000 |

## Freebuff agent-session provider

| var | default | what it does |
|---|---|---|
| `PA_FREEBUFF_ENABLED` | *(unset)* | Official CLI on this machine (chat/task brains drive it via tmux PTY). |
| `PA_FREEBUFF_SIDECAR_HOST` | *(unset)* | PA_FREEBUFF_SIDECAR_PORT=8777 |
| `PA_FREEBUFF_REMOTE_URL` | *(unset)* | On the REMOTE PA, point at the sidecar (shared bearer token): |
| `PA_FREEBUFF_REMOTE_TOKEN` | *(unset)* |  |
| `PA_FREEBUFF_ASSUME_READY` | *(unset)* | PA_FREEBUFF_LIVE_CHECK=false |

## Free-API router ("mini Freebuff")

| var | default | what it does |
|---|---|---|
| `PA_ROUTER_ENABLED` | *(unset)* | docs/ROUTER.md). Empty keys are fine — that backend just reads "needs config" and is never called. |
| `PA_ROUTER_ORDER` | *(unset)* | CSV backend names, both tiers |
| `PA_ROUTER_SMART_ORDER` | *(unset)* | smart = tool/agent steps |
| `PA_ROUTER_FAST_ORDER` | *(unset)* | fast  = plain chat / summaries |
| `PA_ROUTER_SMART_MODELS` | *(unset)* | CSV name=model overrides |
| `PA_ROUTER_FAST_MODELS` | *(unset)* |  |
| `PA_ROUTER_CALL_SECONDS` | *(unset)* | whole-call deadline |
| `PA_ROUTER_BACKEND_SECONDS` | *(unset)* | per-attempt cap |
| `PA_ROUTER_ESCALATE` | *(unset)* | 'think harder' tier (docs/EVOLVE.md #15) |
| `PA_ROUTER_ESCALATE_ORDER` | *(unset)* | CSV; defaults to the documented order |
| `PA_ROUTER_ESCALATE_RPD` | *(unset)* | escalate calls/day cap |
| `GROQ_API_KEY` | *(unset)* | API keys (only the ones you sign up for): |
| `GEMINI_API_KEY` | *(unset)* |  |
| `OPENROUTER_API_KEY` | *(unset)* |  |
| `CLOUDFLARE_ACCOUNT_ID` | *(unset)* |  |
| `CLOUDFLARE_API_TOKEN` | *(unset)* |  |
| `POLLINATIONS_API_KEY` | *(unset)* | optional — pollinations works keyless |
| `CEREBRAS_API_KEY` | *(unset)* | opt-in only: payment-gated trial, see ROUTER.md |
| `MISTRAL_API_KEY` | *(unset)* | opt-in only: no free API tier as of 2026-11 |
| `PA_GEMINI_CLI_TOKEN` | *(unset)* | Official-CLI session sidecars (loopback + bearer; the token is a shared secret YOU choose for router<->sidecar auth, not a provider key): |
| `PA_GEMINI_CLI_URL` | *(unset)* |  |
| `PA_GEMINI_CLI_BIN` | *(unset)* | Antigravity CLI (or 'gemini' w/ Code Assist) |
| `PA_COPILOT_CLI_TOKEN` | *(unset)* |  |
| `PA_COPILOT_CLI_URL` | *(unset)* |  |
| `PA_COPILOT_CLI_BIN` | *(unset)* |  |

## Gmail (read + draft + send)

| var | default | what it does |
|---|---|---|
| `GMAIL_CLIENT_ID` | *(unset)* | Requests gmail.readonly + gmail.compose + gmail.send + calendar.events; sending still requires an explicit approval click. |
| `GMAIL_CLIENT_SECRET` | *(unset)* |  |
| `GMAIL_REFRESH_TOKEN` | *(unset)* |  |
| `GMAIL_TOKEN_FILE` | *(unset)* | Or put the token JSON in a chmod-600 file instead: |
| `GCAL_API_BASE` | *(unset)* | calendar API override |

## Connections library (/connect <app> in Discord)

| var | default | what it does |
|---|---|---|
| `PA_SECRETS_KEY` | *(unset)* | python3 -c "import secrets; print(secrets.token_hex(32))" Never committed, never logged; losing it means reconnecting every app. |
| `PA_PUBLIC_URL` | *(unset)* | <PA_PUBLIC_URL>/oauth/callback — add exactly that as the redirect URI on OAuth apps you register yourself. |
| `PA_GOOGLE_CLIENT_ID` | *(unset)* | Google OAuth client for `/connect google` (Gmail+Calendar). Falls back to the GMAIL_CLIENT_* pair above when unset — same client. |
| `PA_GOOGLE_CLIENT_SECRET` | *(unset)* |  |
| `PA_GITHUB_CLIENT_ID` | *(unset)* | GitHub's auth server has no dynamic client registration — register an OAuth App by hand (redirect URI = the callback above) for /connect git |
| `PA_GITHUB_CLIENT_SECRET` | *(unset)* |  |
| `COMPOSIO_API_KEY` | *(unset)* | Free Hobby platform tier ~= 20k managed-app calls/month; a local counter warns you in Discord at 80%. Override the assumed cap if pricing ch |
| `PA_COMPOSIO_MONTHLY_LIMIT` | *(unset)* |  |
| `PA_MCP_TIMEOUT` | *(unset)* | per-request seconds for MCP/OAuth calls |
| `PA_OWNER_EMAIL` | *(unset)* | REQUIRED for the email_owner chat tool (the only send that needs no approval — the recipient can only ever be this address). |

## Voice: TTS + voice notes + Discord calls (docs/VOICE.md)

| var | default | what it does |
|---|---|---|
| `PA_VOICE_NOTES` | *(unset)* | default before the first /voice |
| `PA_TTS_ORDER` | *(unset)* | gemini,fish,azure,cloudflare); a provider with no key is skipped, an erroring one cools down and the next is tried. |
| `PA_TTS_GEMINI_MODEL` | *(unset)* | PA_TTS_GEMINI_VOICE=Kore |
| `FISH_API_KEY` | *(unset)* | 2) Fish Audio — free s2.1-pro-free until 2026-11-30 (docs.fish.audio). |
| `PA_TTS_FISH_VOICE` | *(unset)* | optional reference-id for a cloned voice |
| `AZURE_SPEECH_KEY` | *(unset)* | 3) Azure Speech — F0 free tier (needs BOTH key + region). |
| `PA_TTS_AZURE_VOICE` | *(unset)* |  |
| `CLOUDFLARE_AI_BASE` | *(unset)* | 4) Cloudflare Workers AI @cf/deepgram/aura-2-en — reuses the CLOUDFLARE_* pair above. PA_TTS_CF_SPEAKER=luna |
| `PA_FFMPEG` | *(unset)* |  |
| `GROQ_WHISPER_BASE` | *(unset)* | whisper-large-v3-turbo (reuses GROQ_API_KEY), Cloudflare @cf/openai/whisper-large-v3-turbo fallback. Reply = text + voice note. |
| `PA_WHISPER_MODEL` | *(unset)* |  |
| `PA_CF_WHISPER_MODEL` | *(unset)* |  |
| `PA_CALL_GUILD_ID` | *(unset)* | guild pal calls in (required for calls) |
| `PA_CALL_CHANNEL_ID` | *(unset)* | voice channel; unset -> find/create 'pal-call' |
| `PA_CALL_MAX_PER_HOUR` | *(unset)* | PA_CALL_JOIN_SECONDS=120 |
| `PA_CALL_LISTEN_SECONDS` | *(unset)* | PA_CALL_MAX_SECONDS=600 |
| `PA_QUIET_HOURS` | *(unset)* | local HH-HH; only urgent calls ring inside |
| `PA_CALL_METRICS` | *(unset)* | per-turn timing rows (/call stats) |
| `PA_CALL_AUDIO` | *(unset)* | inproc = gapless in-process playback |
| `PA_CALL_TAIL_MS` | *(unset)* | PA_CALL_TAIL_SHORT_MS=700 |
| `PA_CALL_ENGINE` | *(unset)* | auto = Gemini Live first, pipeline |
| `PA_LIVE_MODEL` | *(unset)* | fallback; live = Live only; pipeline = STT->LLM->TTS only |
| `PA_LIVE_VOICE` | *(unset)* | Live prebuilt voice |
| `PA_LIVE_BUDGET` | *(unset)* | reserve+charge live_min under call scope |
| `PA_WATCH_MAX_CLASSIFY` | *(unset)* | watcher model calls per 15-min run |
| `PA_WATCH_MAX_CALLS_PER_DAY` | *(unset)* | watcher-triggered calls/day cap |
| `PA_WATCH_IMPORTANT_BATCH_SECONDS` | *(unset)* |  |
| `PA_OPEN_METEO_BASE` | *(unset)* |  |
| `PA_BRIEFING_CITY` | *(unset)* | PA_BRIEFING_LAT=51.5074  PA_BRIEFING_LON=-0.1278 |
| `PA_IMPORT_MAX_BYTES` | *(unset)* | DM attachment cap for chat-history imports |

## Obsidian vault mirror

| var | default | what it does |
|---|---|---|
| `VAULT_PATH` | *(unset)* | notes (overwrite needs approval); optionally git-sync the mirror after each write. |
| `VAULT_MAX_FILE_BYTES` | *(unset)* | VAULT_MAX_FILES=5000  VAULT_NOTE_CAP=12000 |
| `VAULT_GIT_SYNC` | *(unset)* | true -> git add/commit/push after each write |

## Owner Mac over SSH (Tailscale)

| var | default | what it does |
|---|---|---|
| `MAC_SSH_HOST` | *(unset)* | Lets the `mac` chat tool run commands on the owner's Mac. EVERY mac command requires approval. Unreachable -> "the Mac is offline". |
| `MAC_SSH_USER` | *(unset)* |  |
| `MAC_SSH_KEY` | *(unset)* |  |
| `MAC_SSH_TIMEOUT` | *(unset)* |  |

## Web search

| var | default | what it does |
|---|---|---|
| `SEARCH_ENABLED` | `false` | Tavily when both are set; otherwise chat falls back to keyless DuckDuckGo HTML (rate-limited, best-effort). Off for tasks when unset. |
| `TAVILY_API_KEY` | *(unset)* |  |
| `DDG_SEARCH_BASE` | *(unset)* |  |

## Development-only provider fallback (never in production)

| var | default | what it does |
|---|---|---|
| `PA_OLLAMA_BASE_URL` | *(unset)* |  |
| `PA_DEV_MODEL` | *(unset)* |  |

## Quota pacing (tune to your endpoint's limits)

| var | default | what it does |
|---|---|---|
| `PA_MAX_INPUT_TOKENS` | *(unset)* |  |
| `PA_MAX_COMPLETION_TOKENS` | *(unset)* |  |
| `PA_TPM_LIMIT` | *(unset)* |  |
| `PA_RPM_LIMIT` | *(unset)* |  |
| `PA_DAILY_REQUEST_LIMIT` | *(unset)* |  |
| `PA_DAILY_TOKEN_LIMIT` | *(unset)* |  |
| `PA_RATE_MAX_RETRIES` | *(unset)* |  |

## Backups

| var | default | what it does |
|---|---|---|
| `PA_BACKUP_DEST` | *(unset)* |  |
| `PA_BACKUP_PASSPHRASE` | *(unset)* | interactive prompt) |
| `PA_BACKUP_KEEP` | *(unset)* | scheduled-backup: keep newest N exports |

## Telegram mirror (docs/PARITY.md #26) — off by default

| var | default | what it does |
|---|---|---|
| `PA_TELEGRAM` | *(unset)* | mirror + answer the owner's bot chat |
| `TELEGRAM_BOT_TOKEN` | *(unset)* |  |
| `TELEGRAM_CHAT_ID` | *(unset)* | the owner's own chat id with the bot |

## pal's computer: persistent Chromium + /desktop (docs/COMPUTER.md)

| var | default | what it does |
|---|---|---|
| `PA_COMPUTER_ENABLED` | *(unset)* | Off by default. Best on a second Always Free Ampere A1 VM reached over Tailscale; same-VM fallback is on-demand + cgroup-capped. |
| `PA_COMPUTER_PROFILE` | *(unset)* | default <data>/browser-profile, chmod 700 |
| `PA_COMPUTER_MAX_TABS` | *(unset)* | hard tab cap |
| `PA_COMPUTER_IDLE_SECONDS` | *(unset)* | idle-exit; profile stays on disk |
| `PA_COMPUTER_MEMORY_MB` | *(unset)* | self-check recycle; cgroup is the real cap |
| `PA_COMPUTER_LOCK_WAIT` | *(unset)* | seconds a 2nd caller waits on the lock |
| `PA_COMPUTER_URL` | *(unset)* | e.g. http://100.64.1.5:8788 |
| `PA_COMPUTER_TOKEN` | *(unset)* | shared bearer secret YOU choose |
| `PA_COMPUTER_AGENT_BIND` | *(unset)* | On the computer host (`python -m pa computer-agent`) — loopback or 100.64/10 bind only, never 0.0.0.0: |
| `PA_COMPUTER_AGENT_PORT` | *(unset)* |  |
| `PA_COMPUTER_DOWNLOAD_MAX` | *(unset)* | download cap, into the chat sandbox |
| `PA_COMPUTER_BROWSER_BIN` | *(unset)* | chromium path override (else PATH/cache) |
| `PA_DESKTOP_MINUTES` | *(unset)* | /desktop TTL; hard cap 120 |
| `PA_DESKTOP_PORT` | *(unset)* | noVNC port on the tailnet bind |
| `PA_JINA_READER` | *(unset)* | read_url's r.jina.ai fallback; default |
| `JINA_API_KEY` | *(unset)* | optional — raises jina's ~20 req/min cap |

## pal evolve: self-improvement loop (docs/EVOLVE.md) — all off

| var | default | what it does |
|---|---|---|
| `PA_EVOLVE` | *(unset)* | master switch for detect/reflect/etc |
| `PA_EVOLVE_AUTO_APPLY` | *(unset)* | Track A: auto-apply LOW-risk items |
| `PA_EVOLVE_SELF_HEAL` | *(unset)* | queue approval-gated fix code_tasks |
| `PA_EVOLVE_PLAN_CALLS` | *(unset)* | max planner model calls per day |
| `PA_EVOLVE_REFLECT_MAX` | *(unset)* | max lesson/skill items per night |
| `PA_EVOLVE_DETECT_THRESHOLD` | *(unset)* | fingerprint hits in 24h -> an item |
| `PA_EVOLVE_DETECT_MAX_ITEMS` | *(unset)* | new items opened per detect run |
| `PA_EVOLVE_HEALTH_SECONDS` | *(unset)* | canary health window after deploy |
| `PA_EVOLVE_ARCHIVE` | *(unset)* | versioned improvement archive (docs/EVOLVE.md) |
| `PA_EVOLVE_PRACTICE` | *(unset)* | nightly owner-approved self-practice |
| `PA_BACKUP_OFFSITE` | *(unset)* | 'gdrive@pal' copies each encrypted backup to pal's own Drive |

## deep /healthz (docs/EVOLVE.md item 3)

| var | default | what it does |
|---|---|---|
| `PA_HEALTH_RSS_MB` | *(unset)* | RSS ceiling for ok=true |
| `PA_HEALTH_EXPECT_WORKER` | *(unset)* | also require a fresh worker beat |
| `PA_HEALTH_WORKER_MAX_SECONDS` | *(unset)* | beat staleness limit |
| `PA_HEALTH_DISCORD` | *(unset)* | also require a live in-process |
| `PA_CALL_STREAM_BRAIN` | *(unset)* | api backends only, 4s per attempt). PA_CALL_STREAM_BRAIN=off restores the full chat-agent turn; PA_CALL_LLM_SECONDS tunes the per-backend ca |
| `PA_ROUTER_CALL_ORDER` | *(unset)* |  |
| `PA_ROUTER_CALL_MODELS` | *(unset)* |  |
| `PA_CALL_LLM_SECONDS` | *(unset)* |  |
| `PA_TTS_CHARS_DAY` | *(unset)* | is skipped before the call locks a voice. PA_TTS_CHARS_DAY sets one global cap; PA_TTS_CHARS_<PROVIDER>_DAY overrides per provider. |
| `PA_TTS_CHARS_AZURE_DAY` | *(unset)* |  |
| `PA_TTS_CHARS_CLOUDFLARE_DAY` | *(unset)* |  |
| `PA_TTS_CHARS_GEMINI_DAY` | *(unset)* |  |
| `PA_BROWSER_OWNER_DOMAINS` | *(unset)* | is pal's (unknown domains fail closed to owner). Connections get owner_kind via the /connect 'whose account?' button. |
| `PA_POLICY_ENGINE` | *(unset)* | (default) — writes/sends/deletes/spends/ |
| `PA_POLICY_ENGINE` | *(unset)* | them. 'off' restores the legacy per-tool gates (emergency only). PA_POLICY_VAULT_UNATTENDED keeps vault writes unattended-free. |
| `PA_POLICY_VAULT_UNATTENDED` | *(unset)* |  |
| `PA_TOOL_TOPK` | *(unset)* | L1 lean tool catalogue: prune the ReAct tool list to a core set + top-k keyword matches per turn; tools_more lists the rest. |

## goals, proactive read-only work, memory OS, triage

| var | default | what it does |
|---|---|---|
| `PA_GOALS` | *(unset)* | standing goals: goal_* tools + /goals |
| `PA_PROACTIVE` | *(unset)* | proactive-tick job (hourly, quiet-aware) |
| `PA_PROACTIVE_GOAL_TOKENS` | *(unset)* | per-goal token cap per tick |
| `PA_ACTIVITY` | *(unset)* | local activity feed + /activity |
| `PA_ACTIVITY_GREETING` | *(unset)* | "while you were away" DM preamble |
| `PA_AUTOMATION` | *(unset)* | automation_propose tool (monitor/schedule/webhook) |
| `PA_BRIEFING_GOALS` | *(unset)* | Goals section in the morning briefing |
| `PA_MEMORY_BLOCKS` | *(unset)* | pinned persona/human/goals/world blocks |
| `PA_SLEEPTIME` | *(unset)* | nightly memory curation pass |
| `PA_RECALL` | *(unset)* | one FTS5 index across memory/vault/journal |
| `PA_KG` | *(unset)* | personal knowledge graph over the vault |
| `PA_TRIAGE` | *(unset)* | inbox triage labels + local reply drafts |
| `PA_CAL_TRIAGE` | *(unset)* | 30-min meeting prep + conflict cards |
| `PA_CAL_SOON` | *(unset)* | cheap "meeting in 15m/5m" ping via notify() |
| `PA_CAL_SOON_LEADS` | *(unset)* | lead minutes for the meeting-soon ping |
| `PA_LEAVE_NOW` | *(unset)* | TfL "leave in 10m / leave now" for events |
| `PA_HOME_LAT` | *(unset)* | leave-from origin (default: briefing city) |
| `PA_LEAVE_NOW_USE_PHONE_LOC` | *(unset)* |  |
| `TFL_APP_KEY` | *(unset)* | optional; keyless TfL works at low volume |
| `PA_GITHUB_HOOK_AGENT` | *(unset)* | github/ci webhook payloads run an agent |
| `PA_COURSEWORK` | *(unset)* | Classroom/Docs reads + hourly deadline |
| `PA_NOTION_INDEX` | *(unset)* | Notion -> recall/KG index, read-only, |
| `PA_SECRET_CAPTURE` | *(unset)* | credentials via private modal/one-time |
| `PA_NOTIFY_PHONE` | *(unset)* | high/urgent notify() also pings a healthy |
| `PA_MAIL_INBOUND` | *(unset)* | poll pal's own Gmail for owner messages |
| `PA_OWNER_EMAIL` | *(unset)* | the owner's From: for the allowlist |
| `PA_MAIL_INBOUND_SENDERS` | *(unset)* | extra allowed senders, csv |
| `PA_NOTIFY_V2` | *(unset)* | priority/quiet/batched notifications |
| `PA_NOTIFY_PER_HOUR` | *(unset)* | DM cap per hour (held -> morning batch) |
| `PA_PROJECTS` | *(unset)* | goal-linked multi-day projects |
| `PA_NODES` | *(unset)* | loopback/tailnet/private, https otherwise, no credentials). Each node needs a shared bearer in PA_NODE_<NAME>_TOKEN — never commit it. |
| `PA_NODE_PHONE_TOKEN` | *(unset)* |  |
| `PA_NODE_CONNECT_SECONDS` | *(unset)* |  |
| `PA_NODE_HEALTH_SECONDS` | *(unset)* |  |
| `PA_NODE_SMS_READ_UNATTENDED` | *(unset)* | not read the owner's SMS inbox unless this is explicitly on — attended chat reads are always fine. |
| `PA_WORKSPACE` | *(unset)* | file_write keeps its attach-only behaviour; on, file_list/read/ version/delete appear and file_write also persists. |
| `PA_WORKSPACE_MB` | *(unset)* |  |
| `PA_WORKSPACE_PROJECT` | *(unset)* |  |
| `PA_MADETOOLS` | *(unset)* | small pure-Python tools under <PA_DATA_DIR>/tools/. Off by default — tool_make/tool_run/tool_list stay hidden. |
| `PA_INTEGRATIONS` | *(unset)* | API clients (K1 tools + a 'made' connection row; keys sealed in the vault, injected at run time, never in code). Needs PA_MADETOOLS=1. |
| `PA_SUBAGENTS` | *(unset)* | the worker. Off by default — delegate falls back to a queued inbox turn. PA_SUBAGENT_MAX caps live children. |
| `PA_SUBAGENT_MAX` | *(unset)* |  |
| `PA_RESEARCH` | *(unset)* | a cited-report sub-agent (needs PA_SUBAGENTS) and writes the result to the workspace. Off by default. |
| `PA_SKILLEXTRACT` | *(unset)* | skill_save for the owner; also marks the top-3 relevant skills in the system prompt. Off by default. |
