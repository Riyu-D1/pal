# Agentic jobs — recurring unattended agent turns

Beyond daily/weekly/hourly reminders (`remind_me` — posts a chat
message), pal can run a **full agent turn on a schedule**: connected
apps, the vault, and web tools, with nobody watching. You create,
edit, list, and disable them in plain chat; `/jobs` lists and toggles
them from Discord.

```
"every Sunday 6pm go through my emails and update my brain"
"monthly 1st 09:00 do a tidy-up of the vault index"
/jobs                          # list
/jobs weekly-brain-update on   # enable a job
/jobs weekly-brain-update off  # disable it
```

In chat the tools are `job_add` / `job_list` / `job_set`; naming an
existing job updates it in place. `when` accepts `daily HH:MM`,
`weekly <dow> HH:MM`, `monthly <day> HH:MM` (day clamps to month end —
the 31st fires on the 28th/29th in February), or `hourly :MM`.
Timezones are per-schedule IANA zones (`PA_TIMEZONE` default), so DST
moves the UTC instant, not the local time.

**System jobs** — `inbox-watch` and `morning-briefing` (seeded
disabled by migration 10, see docs/JARVIS.md) run fixed code paths in
`pa/sysjobs.py`, not agent turns; their names are reserved —
`job_add` refuses them. The scheduler also accepts
`every N minutes` (interval frequency, 5–1440, stored as minutes in
`day_of_month`) which is what `inbox-watch` uses for its 15-minute
cadence.

## What an unattended run may do

| action | unattended? |
|---|---|
| read-only tools (search, fetch, email_list/read, app `readOnlyHint` tools) | yes |
| `vault_write` / `vault_append` — the vault is git-versioned; every write commits + pushes (`VAULT_GIT_SYNC`) | yes |
| `email_owner` — recipient is fixed to `PA_OWNER_EMAIL` | yes |
| `call_owner(message, urgent)` — rings the owner in a voice channel; quiet-hours + per-hour cap apply | yes |
| `send_voice_note(text)` — posts a TTS voice note | yes |
| everything else gated (`send_email`, delete/modify tools, shell writes, `mac`) | **queued** — a `job_action` approval is created and the turn moves on |

Held actions arrive as Discord messages with Approve/Reject buttons
(the `approve <id>` text command works too). Approving **executes the
tool once** and posts the result; rejecting skips it. There is no
paused turn to resume — the job finished when it was queued.

Each run posts a short summary (`job <name>: <text>`) to the owner's
Discord channel — the chat channel the job was created in, or the
owner DM for jobs with no channel. When voice-note replies are on
(`PA_VOICE_NOTES` or `/voice on`), the summary is also posted as a
voice note.

## Voice + calls from jobs

Two ways a job can reach the owner by voice — see docs/VOICE.md for
setup:

- `send_voice_note(text)` — a TTS voice note in the job's channel.
  Good for ordinary "here's what happened" summaries.
- `call_owner(message, urgent=true)` — pal pings the owner, joins a
  private voice channel, waits ~2 min for them, speaks the alert, and
  (when voice-recv is working) holds a short two-way conversation.
  For **genuinely urgent** findings only — a same-day deadline, a
  security alert, a failed payment. `urgent=true` rings through quiet
  hours (`PA_QUIET_HOURS`, default 23-07); non-urgent calls are
  refused inside them and all calls are capped at
  `PA_CALL_MAX_PER_HOUR` (3).

Prompt guidance for jobs that watch for urgent things (the seeded
`daily-email-summary` body and `weekly-brain-update` prompt carry it
via schema migration 9): *"If something genuinely needs me right now,
call_owner with a one-line spoken alert; otherwise lead the summary
with 'URGENT:'."*

## Shipped example: `weekly-brain-update`

Seeded by schema migration 8, **disabled** by default — enable with
`/jobs weekly-brain-update on` after Gmail and the vault are set up.
Weekly, Sundays 18:00 Europe/London:

1. Reads the owner's Gmail for the last 7 days (`newer_than:7d`).
2. Best-effort: if a ChatGPT data-export email or zip link shows up,
   fetch it and note what it contained. **There is no ChatGPT history
   API** — the data export (Settings → Data Controls → Export) is the
   only legitimate source; anything else would be invented.
3. Writes a dated note under `pal/weekly/<date>.md`; appends to
   existing notes only where clearly relevant (never rewrites one).
   Vault writes git-push automatically.
4. `email_owner`s a digest of what it wrote and what needs attention.

Note on other "export" sources: only ChatGPT's official export is
handled; other assistants' exports are not implemented or claimed
anywhere.

## Deploy steps (the `pa` systemd user service)

```sh
# 1. deploy the code (your normal pull path), then migrate + restart
cd <repo> && git pull   # on the host
python -m pa migrate           # applies schema v8: connections.account,
                             # schedules.day_of_month, seeds the
                             # weekly-brain-update job DISABLED
                             # (service restart below also runs it —
                             #  this step is belt-and-braces)

# 2. env — add to your service env file (never commit these):
#    PA_OWNER_EMAIL=you@example.com       # required by email_owner
#    COMPOSIO_API_KEY=...                 # if not already set
#    PA_SECRETS_KEY=...                   # required for /connect
#    PA_PUBLIC_URL=...                    # required for /connect
#    VAULT_PATH=/srv/vault                # your vault mirror path
#    VAULT_GIT_SYNC=true                  # optional

# 3. restart the service
systemctl --user restart pa

# 4. connect pal's mailbox (new Google account you create), then verify
#    /connect gmail account:pal
#    /connections            # shows gmail (owner) + gmail@pal
# 5. turn the example job on when you're ready
#    /jobs weekly-brain-update on
```

The migration is additive (`ALTER TABLE … ADD COLUMN`, one seeded
disabled row) — existing connections keep working as the default
`owner` account and existing schedules are untouched.
