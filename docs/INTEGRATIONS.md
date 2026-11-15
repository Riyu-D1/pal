# Integrations

All integrations are optional and degrade to explicit **needs config**
states — never fake success. Everything missing is listed on the owner
dashboard and by `python -m pa check-config`.

## Discord (main interface)

`pa.discordbot` — a gateway bot implemented on the stdlib WebSocket client
(`pa.wsclient`); no discord library.

- **Env**: `DISCORD_ENABLED` (default on; cleanly idle while the token is
  unset), `DISCORD_BOT_TOKEN`, `DISCORD_OWNER_ID`, `DISCORD_CHANNELS`
  (JSON `{assistant-name-or-id: channel-id}`).
- **Owner allowlist**: exactly one Discord user id may drive the bot.
  Everyone else gets an ephemeral refusal (interactions) or silence
  (messages). With `DISCORD_OWNER_ID` unset the bot refuses *everyone*.
- **Commands** (slash commands in guilds, or DM text):

  | command | effect |
  |---|---|
  | `task <assistant> <title> [| body]` | queue a task |
  | `tasks` | recent tasks with states |
  | `status <id>` | task detail + summary |
  | `cancel <id>` | cancel a running/queued task |
  | `retry <id>` | requeue a finished/failed task |
  | `approvals` | list pending approvals |
  | `approve <id8>` / `reject <id8>` | decide an approval |
  | `help` | command list |

- **Delivery**: finished tasks post to the assistant's routed channel
  (`DISCORD_CHANNELS`); pending approvals DM the owner. Each notification
  is sent at most once (durable dedup table).
- **Reconnect**: resumes with `session_id`+sequence when Discord allows
  it, re-identifies otherwise. Every dispatched event is recorded in
  `discord_events` before handling, so gateway redelivery never
  double-acts or double-replies.
- **Writes**: approvals decided over Discord still go through the same
  `approvals.decide` + `apply_if_new` path as the UI — preview first,
  owner-only, idempotent.
- Bot setup: Discord developer portal → application → bot token → invite
  with `applications.commands` + `bot` scopes. **Conversational chat
  needs the privileged Message Content intent** enabled in the portal
  (slash commands + DMs alone do not).

## Gmail (read-only)

`pa.gmail` — official Gmail HTTP API via `pa.netclient`, OAuth refresh
token. Reads use `gmail.readonly`; `draft_email`/`send_email` need the
`gmail.compose`/`gmail.send` scopes granted by `pa gmail-auth` (or
`/connect google`), and sending always requires an approval click.

- There is no delete, archive or label path anywhere in the codebase.
- **Env**: `GMAIL_CLIENT_ID`, `GMAIL_CLIENT_SECRET`,
  `GMAIL_REFRESH_TOKEN`; or `GMAIL_TOKEN_FILE` pointing at a chmod-600
  JSON (looser permissions are refused).
- **Onboarding**: `python -m pa gmail-auth` runs a one-time localhost
  consent flow on any machine with a browser and prints the env values.
- **Tools**: `email_list` (query + metadata list), `email_read` (full
  body, plain-text extracted). Both wrap results in
  `<<<UNTRUSTED EMAIL ...>>>` markers.
- The access token is refreshed in-memory only and never written to the
  database or logs.

## Obsidian vault (read-only)

`pa.vault` — indexes a synced Markdown mirror under `VAULT_PATH`.

- The mirror must land on the host by some external means — official
  Obsidian Sync headless **only if the owner already has it** (nothing in
  this repo assumes or sets it up), Syncthing, git cron, etc. Mount it
  read-only (`:ro` in compose).
- Walk skips: symlinks, escapes outside the root, dotdirs/dotfiles, files
  with secret-looking names (`*.env`, `*secret*`, `*credential*`…),
  non-`.md`, oversize files (`VAULT_MAX_FILE_BYTES`), beyond
  `VAULT_MAX_FILES` total.
- **Tools**: `vault_search` (LIKE-matched snippets) and `vault_read`
  (capped full text). Both wrap output in `<<<UNTRUSTED NOTE ...>>>`.
- Incremental re-indexing: content-hash dedup; changed files re-indexed,
  deleted files removed. `pa vault-scan` runs it on demand; reads lazily
  scan when stale.
- Note text is data for retrieval — never commands (see SECURITY prompt
  policy).

## Scheduler

`pa.scheduler` — durable timezone-aware jobs driven by the worker tick.

- Tables: `schedules` (definition + enabled flag) and `schedule_runs`
  (`schedule_id + fire_key` unique → one run per occurrence, forever).
- Occurrence math is done in the job's IANA timezone (`zoneinfo`);
  `PA_TIMEZONE` is the default (`Europe/London`). DST transitions are
  unit-tested both directions.
- A tick computes the latest due occurrence and fires it once; downtime
  catches up to the *latest* missed slot only — no stampede of missed runs.
- Manual trigger: `python -m pa schedule <name> now` — always allowed for
  the owner, recorded as a `manual` run, never collides.
- Shipped job `daily-email-summary` (07:30 local, assistant `email`) is
  seeded **disabled**; enable it with
  `python -m pa schedule daily-email-summary on` after Gmail + Discord
  are configured.
- Add jobs with `pa schedule-add <name> <daily|weekly|hourly> <HH:MM>
  <assistant> <title> [--tz ZONE] [--dow N] [--body TEXT] [--on]`.
