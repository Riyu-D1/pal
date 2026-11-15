# JARVIS — proactive pal

The pieces that make pal proactive instead of reactive. Everything here
is **off until you turn it on**; commands are Discord slash commands
(plain `pa <cmd>` text works in DM too).

```
/jobs inbox-watch on        # urgent inbox watcher (every 15 min)
/jobs morning-briefing on   # 07:30 Europe/London daily briefing
/watch vip boss@work.com    # teach the watcher
/reminders                  # list + cancel reminders
/briefing now               # briefing on demand
/reset                      # clear this channel's chat history
/status                     # live health card
```

A second proactive layer (see `docs/FEATURES.md`) adds
standing goals (`/goals`), a read-only `proactive-tick`, the local
activity feed (`/activity`), inbox `/triage`, calendar prep cards,
pinned memory blocks + sleeptime, cross-source recall and a vault
knowledge graph. All flag-gated (`PA_GOALS`, `PA_PROACTIVE`,
`PA_ACTIVITY`, `PA_TRIAGE`, `PA_CAL_TRIAGE`, `PA_MEMORY_BLOCKS`,
`PA_SLEEPTIME`, `PA_RECALL`, `PA_KG`, `PA_NOTIFY_V2`,
`PA_PROJECTS`) and all off by default.

## 1. Urgent inbox watcher — `inbox-watch`

Seeded **disabled**, interval every 15 minutes. Each run:

1. Fetches owner Gmail messages newer than a persisted cursor
   (`inbox_watch_cursor` in `pa_settings`; first run only primes the
   cursor — it does **not** alert on the backlog).
2. Skips anything already in `inbox_seen` (dedupe, no double alerts).
3. Applies `/watch` rules by sender address/domain:
   - `vip` → straight to urgent, no model call.
   - `mute` → dropped, never classifies or alerts.
4. Classifies the rest with the cheapest router model, one short
   prompt, capped at `PA_WATCH_MAX_CLASSIFY` (20) per run:
   - **critical** — call even inside quiet hours (security incident,
     "call me NOW", house on fire).
   - **urgent** — needs a real person today: bank/school/security
     alerts, deadlines, people waiting on you.
   - **important** — worth knowing, not worth ringing for.
   - **ignore** — newsletters, promos, receipts, bots.
   Heuristic fallback when no model answers (matches
   security/bank/school keywords → urgent; known bulk senders → ignore).

Delivery:

| verdict | quiet hours? | what happens |
|---|---|---|
| critical | either | `call_owner` — rings through quiet hours |
| urgent | outside | `call_owner` |
| urgent | inside (23–07) | voice note + DM, no ring |
| important | — | batched DM (+voice) at most every `PA_WATCH_IMPORTANT_BATCH_SECONDS` (3600) |
| ignore | — | nothing |

Safety: calls/day capped at `PA_WATCH_MAX_CALLS_PER_DAY` (5) *and*
the existing per-hour cap; if a call can't go through it falls back to
DM + voice note. The cursor advances even on partial failure so a bad
message can't re-alert forever.

```
/watch                     # list rules
/watch vip alice@x.com     # always urgent
/watch vip @school.org     # whole domain
/watch mute @linkedin.com  # never alert
/watch mute -alice@x.com   # remove a rule ('-' prefix on any sub)
```

## 2. Morning briefing — `morning-briefing`

Seeded **disabled**, daily 07:30 Europe/London. Collects, in order:

- today's Google Calendar events (Composio `googlecalendar`),
- overnight **important** email (subjects/senders the watcher logged
  in `inbox_seen`, plus a fresh Gmail unread check),
- vault notes whose name/front-matter contains today's date or a
  `due:`/`todo` marker found by a quick vault scan,
- London weather from Open-Meteo (no key): now + today's high/low and
  rain chance. Location is `PA_BRIEFING_CITY/LAT/LON` — default London.

Goes out as one text summary **plus** one voice note capped at
~90 seconds of speech (~250 words). `/briefing now` runs it on demand
in the invoking channel.

## 3. Reminders

Plain language in chat — the `remind_me` tool parses times in
Europe/London (DST-aware):

```
"remind me at 5 to call mum"        # today/tomorrow at 17:00
"remind me in 20 minutes ..."       # relative
"remind me every Monday 9am ..."    # recurring
"remind me tomorrow 8:30 urgent ..."# urgent -> may call
```

One-off reminders store a `schedule:` inbox row that delivers a DM +
voice note when it fires; **urgent** ones also `call_owner` (quiet
hours still apply — only critical rings inside them). Recurring ones
become normal `reminder` schedules in the scheduler.

```
/reminders            # list upcoming (one-off + recurring)
/reminders cancel 3   # cancel by id from the list
```

## 4. `/reset` + self-trust

`/reset` exports this channel's `chat_messages` to
`chat_history_backups`, then deletes them — pal stops trusting its own
stale refusals ("I can't check email") from before a connection
existed.

Two things also re-arm pal's belief in its tools without a reset:

- any connection change (connect, disconnect, token refresh failure)
  bumps `caps_updated_at` in `pa_settings`;
- a deploy whose git sha differs from `deploy_sha` bumps it too.

When the last assistant message predates that stamp, the system prompt
injects a short note — *"capabilities changed since your last reply;
re-check `app_tools` instead of repeating an old 'I can't'"*.

## 5. `/status` + failure alerts

`/status` (no id) renders a live card; `/status <id>` still shows one
task. The card shows:

- version (git sha), uptime, schema version
- each connection + a cheap live probe (token refresh, Composio
  account GET, or MCP handshake)
- router backends: configured/cooling + today's requests & tokens
- TTS/STT providers: configured or missing key or cooling-down, plus
  today's per-provider usage from `voice_usage`
- Composio monthly calls vs cap
- every job: on/off + last run result

The notifier loop also checks once a minute and **DMs the owner once**
per firing condition (deduped in `discord_events`, auto re-arms when
the condition clears): an enabled job failed its last two runs, a
connection went `expired`/`revoked`, or every configured TTS provider
is cooling at once.

## 6. ChatGPT / other history imports

Request the export: ChatGPT → **Settings → Data Controls → Export** →
wait for the email → download the zip. Then **DM the zip to pal**.
Any `.zip`/`.json`/`.md`/`.txt` attachment in DM (up to
`PA_IMPORT_MAX_BYTES`, 60 MB) routes to the importer:

- a zip (or bare JSON) containing `conversations.json` →
  `pa/importers/chatgpt.py`: one Markdown note per conversation
  (title, date, cleaned transcript) under `Imports/ChatGPT/<month>-<slug>`,
  plus `_digest-<YYYY-MM>` monthly index notes;
- anything else → `pa/importers/generic.py`: each `.md`/`.txt` file
  becomes a note under `Imports/Files/`; a JSON file shaped like
  `[{title, messages:[{role,text}]}]` splits per item — that's the
  drop-in shape for other assistant exports later.

Both record every external id in `imported_items`, so re-sending the
same export is a no-op (reply says `N already had`). Writes go through
`vault.write_notes` — one reindex + one `git push` (`VAULT_GIT_SYNC`).

The same importer shape accepts any JSON dump in that form if one
exists.

## Env vars

| var | default | what |
|---|---|---|
| `PA_WATCH_MAX_CLASSIFY` | 20 | max model classifications per watcher run |
| `PA_WATCH_MAX_CALLS_PER_DAY` | 5 | watcher-triggered calls/day |
| `PA_WATCH_IMPORTANT_BATCH_SECONDS` | 3600 | min gap between "important" batches |
| `PA_QUIET_HOURS` | `23-07` | existing — calls inside need critical/urgent |
| `PA_OPEN_METEO_BASE` | `https://api.open-meteo.com` | weather endpoint |
| `PA_BRIEFING_CITY` | `London` | label on the briefing |
| `PA_BRIEFING_LAT` / `PA_BRIEFING_LON` | `51.5074` / `-0.1278` | weather coords |
| `PA_IMPORT_MAX_BYTES` | `62914560` | DM attachment cap for imports |
| `VAULT_GIT_SYNC` | `false` | existing — git push after vault writes |

## Deploy

```sh
# on the host running pal:
cd <repo> && git pull
python -m pa migrate        # schema v10: interval freq + new tables +
                            # seeds inbox-watch / morning-briefing OFF
systemctl --user restart pa    # or your process manager
# then, when ready:
/jobs inbox-watch on
/jobs morning-briefing on
```

Migration 10 is additive — new tables (`chat_history_backups`,
`watch_rules`, `inbox_seen`, `imported_items`, `voice_usage`) and two
**disabled** schedule rows; nothing existing is touched.
