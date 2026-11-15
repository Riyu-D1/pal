# Feature reference — the chat/UX surface

The numbered items below are the conversational + safety features
pal gained on top of the base platform (Discord gateway, tasks,
scheduler, Gmail, vault). Everything risky or always-on is off by
default behind an env flag or a seeded-disabled job.

## 1. Live progress — reactions + editable status line

**Flag:** `PA_CHAT_PROGRESS` — default **on** (low-risk UX; set `false`
for the old typing-only behaviour).

- `DiscordAPI` gains `create_message` (returns the message id),
  `edit_message` (PATCH), `delete_message`, `add_reaction`, and
  `delete_reaction` (emoji is URL-encoded on the path).
- `pa/progress.py` (`LiveProgress`) drives one turn: 👀 on the owner's
  message, **one** status message ("on it — working…") is posted and
  edited (PATCH, throttled to ~1.2s) as each tool starts
  ("on it — searching the web…"), then deleted when the reply lands —
  the reaction flips to ✅ (or ⚠️ on a broken turn). Typing keeps
  firing in the background as a fallback signal.
- Only tool **names** reach the status line, and app-tool names are
  stripped of their `@account` label (`gmail@pal.X` → `gmail`). Tool
  arguments — email bodies, URLs with tokens — are never shown.
- `chattools.run()` reports each tool start through `ctx.progress`;
  `ChatAgent.run_turn(progress=…)` passes the callback through.
- `PA_CHAT_PROGRESS=false` keeps the previous behaviour exactly:
  typing + one "working on it…" note after `PA_CHAT_WORKING_SECONDS`.

Voice-note turns get the same treatment ("on it — transcribing…").

Tests: `tests/test_progress.py` — status POST→PATCH→DELETE lifecycle,
reaction add/flip/remove, no args in the status line, the flag-off
fallback still posts "working on it…".

## 2. Question cards — `ask_owner`

New chat tool `ask_owner(question, choices?)`: the turn pauses on a
pending **`ask`** approval row, the owner gets a Discord card in DM —
a Yes/No pair, or one button per choice (max 5, labels ≤ 80 chars) —
and the clicked label is written back into the payload as `answer`,
which becomes the tool result `{"answer": …}`.

- Reuses the approval plumbing: `approvals.create_approval` /
  `wait_decision` / `decide`, same 24h TTL (expired clicks show
  "question expired").
- `ctx.ask()` handles the row + wait; `ctx.request_ask` is the host
  callback (Discord: `_request_chat_ask`). Clicks ride
  `chatask:<approval_id>:<index>` custom_ids through the existing
  owner-only component gate — non-owner clicks get the standard
  "Not authorized." ephemeral.
- Unattended job turns never block: `{skipped: true, reason:
  "unattended"}` and no row is created.

Tests: `tests/test_ask.py` — card shape + click answer round-trip,
default Yes/No, 5-choice/80-char caps, expiry, second-click ignored,
non-owner refusal, unattended skip.

## 3. Inbound images, PDFs + general attach-out

DM'd **images** (png/jpeg/webp/gif) and **PDFs** now route into the
chat turn instead of being ignored:

- `pa/attachments.py` classifies by content-type/filename, downloads
  via `voicemod.download_attachment` (Discord-CDN allowlist +
  `PA_ATTACH_MAX_BYTES` cap, default 8 MB), and saves under
  `chat_sandbox/<channel>/` with sanitised names.
- Images: the turn sees `[Attached image: name — WxH]` plus a stub
  note until item 4's vision caption lands (the `_image_caption`
  hook already calls `pa/vision.py` when it exists).
- PDFs: text extracted with `pypdf` (new pinned dep — pure Python),
  capped at `PA_PDF_MAX_PAGES` (20) / `PA_PDF_MAX_CHARS` (50k), and
  fenced `<<<UNTRUSTED PDF name>>>`. Under ~30 extracted chars the
  turn is told the PDF is probably scanned — pal does not OCR.
- The owner's message text rides along as the caption above the block.
- Attach-out generalised as `ctx.attach(filename, ctype, data)`:
  posts to the channel via `api.post_file` under ~8 MB, else saves to
  the sandbox and returns the path.

Tests: `tests/test_attachments.py` — classification, PNG dims, PDF
extract + caps + OCR stub, e2e routing/fencing, download-failure
reply, unknown type ignored, attach-out post vs save.

## 4. Vision (free-tier only)

`pa/vision.py` captions images through a configurable chain —
`PA_VISION_ORDER` default `groq,gemini,cloudflare` — all OpenAI-style
`image_url` data-URI calls (Groq / Gemini's compat endpoint /
Cloudflare `/ai/v1`).

- **Model probing:** `PA_VISION_MODEL_<BACKEND>` wins first, then the
  last-working model, then a short built-in list; a 400/404/422 walks
  to the next candidate, a 429 records a 5-min cooldown in
  `router_cooldowns` as `vision-<backend>` and moves on.
- **Off** when `PA_VISION=false` or no backend key exists — the image
  turn keeps the item-3 stub ("vision not configured").
- Images go as data URIs capped at `PA_VISION_MAX_BYTES` (4 MB); no
  re-encode (stdlib has no JPEG writer). Captions re-enter the model
  fenced `<<<UNTRUSTED VISION CAPTION>>>`.
- New `look` chat tool: sandbox path (realpath-checked under
  `chat_sandbox`) or public image URL via `egress.guarded_get` with an
  image-only `allowed` tuple (new param; text/html+plain stays the
  default).

Tests: `tests/test_vision.py` — payload shape (Bearer auth, data-URI),
model failover on 400, override order, 429 cooldown then straight to
the next backend, size/type skips, `PA_VISION=false` no-call, sandbox
escape refused, URL path through the egress guard.

## 5. Correctable memory + gated owner facts

- `memory_forget(id_or_key)` / `memory_correct(id_or_key, content)`:
  a bare number resolves to a per-assistant **memory** row (deletes/
  updates directly via `memory.py`, audited); anything else resolves
  to an **owner_fact** key — those always go through an `owner_fact`
  approval card (same kind the task-agent's `propose_memory` uses).
- New gated `owner_fact(key, value, delete?)` tool: attended turns
  post a card and apply on approve; unattended turns queue a
  `job_action` that writes on Approve via `approved_replay`.
- `memory_save` still writes private assistant memory directly
  (documented); `PA_MEMORY_SAVE_APPROVAL=true` gates it too.
- `PA_MEMORY_AUTO_PROPOSE=false` (default): end-of-turn fast pass may
  propose ≤ `PA_MEMORY_PROPOSE_MAX` owner_facts as **pending**
  approvals — never writes the profile, and a hostile "forget all
  facts" line can't produce deletes.
- `/memory` lists facts + memories; `/memory forget <id>` /
  `forget fact <key>` (the command IS the owner → direct, audited).
  Ephemeral, owner-only.

Tests: `tests/test_memory_ops.py` — forget/correct + audit, fact
requires approval, denied stays, unattended queue→replay, flag
gating, auto-propose pending-only, injection can't delete, `/memory`
list/forget/refusal.

## 6. Skills library (vault/SQLite)

- `skill_list` / `skill_load` / `skill_save` tools. A skill is a short
  owner-authored procedure stored as `Skills/<name>.md` in the vault
  (source of truth when VAULT_PATH is configured — it syncs to the
  owner's notes, with YAML front matter) or in the new `skills` table
  (schema v11) when it isn't.
- Names are slugged to `[a-z0-9-]` and become the filename — `../`,
  dots and separators can't exist in a slug, so writes can never
  escape `Skills/` even without the vault path checks.
- `skill_load` returns the body wrapped in an `<<<OWNER SKILL>>>`
  fence (owner procedure, size-capped 20k chars) — treated as owner
  instructions but it still can't bypass approvals.
- `skill_save` is approval-gated; saved skill names/descriptions are
  listed in the system prompt ("Saved skills ...").
- `pa/db.py`: upgrading an EXISTING database now takes an SQLite
  online backup into `DATA_DIR/backups/pre-migrate-v<n>-<ts>.db`
  first (fresh installs skip it; a backup failure only logs).

Tests: `tests/test_skills.py` — slug can't escape, SQLite round-trip,
vault note written + read back, vault wins over SQLite, tool wiring
+ approval gate + prompt block, pre-migrate backup file exists.

## 7. Profile note + daily journal

- `Profile.md` in the vault is rendered from `owner_facts` and
  refreshed after every owner-fact write (approval apply, approved
  job replay, `/memory forget fact`). It's the human-readable mirror
  of the shared profile — edits still go through chat, not the file.
- `daily-journal` is a DISABLED-seeded system job (schema v12,
  `/jobs daily-journal on`): each evening it writes
  `Journal/YYYY-MM-DD.md` — chat turns, inbox/job runs, approval
  decisions, new memories, audit counts — plus a one-paragraph
  fast-tier summary when a brain is wired. Without a vault the note
  lands in `DATA_DIR/journal/`. Registered in `pa/sysjobs.py` so a
  chat prompt can't clobber its schedule.

Tests: `tests/test_journal.py` — Profile.md written + synced on
approval, no-vault no-op, job seeded disabled + is_system, journal
note in vault and in the data-dir fallback, day counts.

## 8. Standing monitors + calendar-soon

- `monitors` table (schema v13) + `watch_add`/`watch_list`/`watch_set`
  tools (`/watch` was already inbox rules, so the owner command is
  `/monitor` — list plus `<id> on|off|del`).
- Kinds: `web` (URL → sha256 of bounded fetched text, alert on
  change), `gmail` (search → newest message id, alert on new),
  `calendar` (lead minutes → alert once per event starting soon —
  the "calendar soon" behavior).
- `monitor-tick` DISABLED-seeded system job runs every 5 min and
  only touches DUE monitors (`every_minutes`, clamped 5–1440).
  `last_state` fingerprints the previous check so alerts fire once
  per change; the first tick only records a baseline (no spam).
- `watch_add`/`watch_set` are approval-gated; alerts are owner DMs.

Tests: `tests/test_monitors.py` — kind/query validation, CRUD, job
seeded disabled, web change alerts once (baseline→change→steady),
calendar dedupe per event, paused skipped, tool approval gating,
command list/toggle/remove.

## 9. Signed inbound webhooks

- `POST /hooks/<name>` on the web app (schema v14,
  `webhook_endpoints`). Public route — the signature IS the
  credential: `X-PA-Signature: sha256=<hmac-sha256 of raw body>`
  (`X-Hub-Signature-256` also read). Constant-time compare; bad or
  missing signature → 401 + audit row, unknown/disabled name → 404.
- A valid call enqueues the body into `chat_inbox` as
  `source='webhook:<name>'` — the normal job drain turns it into an
  unattended turn with the payload inside an
  `<<<UNTRUSTED WEBHOOK PAYLOAD>>>` fence (32k cap).
- `/hooks` owner command: `add <name> [prompt]` creates an endpoint
  and shows the generated secret **once** (the list never shows
  secrets), `del <name>`, `<name> on|off`. Ephemeral, owner-only.

Tests: `tests/test_webhooks.py` — sign/verify incl. tamper, fenced
enqueue, name sanitising, list hides secret, HTTP 200/401/404,
disabled 404, unauthenticated valid call works, command add/list/
toggle/del without leaking the secret.

## 10. Standing allow rules

- `allow_rules` table (schema v15): tool + optional args-JSON
  substring. `ChatToolContext.approve()` checks rules first — a
  match returns "approved" without a card (chat) or a queued
  job_action (unattended), audited as `auto_allow`.
- STRICT whitelist (`ALWAYS_CAPABLE`): watch_add/watch_set,
  skill_save, owner_fact, memory_save, calendar_read, vault_write.
  send/pay/delete/shell/mac/secret/code tools can never get a rule
  — `add()` refuses them outright.
- Approval cards for capable tools show a third **Always allow**
  button (`chatappr:<id>:a`) which approves AND writes the rule.
- `/allow` lists, `add <tool> [match]`, `del <id>`.

Tests: `tests/test_allowrules.py` — dangerous tools refused, pattern
matching, auto-approve short-circuit (no card, no row, audited),
unattended runs instead of queueing, command CRUD.

## 11. Auto-review labels on approval cards

- `pa/review.py` — deterministic classifier (no model): every
  approval card gets 🔴 high / 🟠 review / 🟢 low plus a specific
  reason ("sends mail to example.com", "deletes an owner fact",
  "runs a command").
- Shown inline on chat approval DMs and held `job_action` cards.
  Advisory only — never decides and never auto-approves.

Tests: `tests/test_review.py` — tier assignment, specific reasons,
label renders into the approval DM body.

## 12. untrusted() fencing everywhere

- New `pa/untrusted.py`: `untrusted(source, text)` central fence +
  `fence_result(source, out)` which walks a result dict/list and
  wraps every remote-supplied string (snippet/content/text/body/
  excerpt/caption/subject) in-place, preserving shape.
- Applied to: `fetch_url` excerpts (`UNTRUSTED PAGE <url>`),
  `web_search` snippets, every connected-app tool result
  (`UNTRUSTED APP <provider>.<tool>`), `email_list` metadata.
- Already fenced before this item and unchanged: email bodies
  (`UNTRUSTED EMAIL`), vault notes, vision captions, PDF text,
  webhook payloads, attachment image notes.

Tests: `tests/test_untrusted.py` — fence markers, shape preserved,
each tool path emits its fence, app results fenced, PDF stays
fenced.

## 13. Audit log + /audit

- `approvals.decide()` now audits every owner decision
  (`approval_approved`/`approval_denied` + tool name) — joined to
  the existing `dbmod.audit` writes for memory ops, allow rules,
  webhook signatures, monitor changes and skills.
- `/audit [filter]` — ephemeral owner command listing the last 20
  audit rows (60 scanned, substring filter on action/detail).

Tests: `tests/test_audit.py` — approve/deny audited with tool
name, `/audit` lists and filters.

## 14. Credential vault + browse_fill_secret

- `credentials` table (schema v16): domain → AES-GCM sealed blob via
  secretsbox (aad `cred:<domain>`). `/credentials` is ephemeral: the
  list shows domains + labels only — **values are never displayed
  anywhere**. Capture (`PA_SECRET_CAPTURE` on): `/credentials
  add` opens a private modal, `/credentials form` issues a one-time
  HTTPS link, and a pasted secret is only sealed while a capture is
  armed — typed passwords are refused + deleted, never accepted.
- `browse_fill_secret` (always gated, excluded from allow rules):
  suffix-matches the current tab's host to a stored domain —
  `app.example.com` matches `example.com`, `evil-example.com` does
  NOT — then types username/password into the page's fields via the
  driver's own `type`, bypassing browse_type's password refusal so
  this audited path is the only way secrets enter a page. The
  result carries no values; submit stays a separate gated step.

Tests: `tests/test_credvault.py` — sealed storage, never-displayed,
suffix matching, phishing-domain refusal, fill types values but
returns none, no-password-field error, gated + not allow-able,
command CRUD without leakage.

## 15. generate_image

- `generate_image(prompt)` — Cloudflare Workers AI
  `flux-1-schnell` when CLOUDFLARE_ACCOUNT_ID+CLOUDFLARE_API_TOKEN
  are set, else Pollinations (no key) through the bounded egress
  guard (image/* only, magic-checked, 8 MB cap).
- Result goes through `ctx.attach` — posted to the channel when it
  fits, sandboxed with a path otherwise.

Tests: `tests/test_imagegen.py` — empty prompt, pollinations path
via egress, Cloudflare-first ordering, non-image bytes rejected,
tool attaches the PNG.

## 16. File generation

- `file_write(filename, content)` — .csv/.md/.txt/.json/.pdf only,
  content capped at 200k, filename through `safe_filename`, output
  attached or sandboxed via `ctx.attach`.
- `.pdf` is rendered by a ~60-line built-in PDF writer (Helvetica
  text pages) — pypdf only reads and no new dependency was wanted;
  tests round-trip it through pypdf.

Tests: `tests/test_filegen.py` — content types, bad extensions
refused, JSON pretty-print, valid PDF structure (pypdf reads it
back), paren escaping, tool attach + refusal.

## 17. Restricted run_python

- `run_python(code)` — always approval-gated (never allow-ruled).
  `python3 -I -c` inside the chat sandbox: env scrubbed to
  PATH/HOME/LANG (keys can't leak), best-effort POSIX rlimits
  (512MB AS / 10s CPU / 8MB files / 64 procs — each skipped where
  unsupported), timeout + capped output like sandbox.run, code ≤20k.

Tests: `tests/test_runpy.py` — exec, cwd is sandbox, env scrubbed
(secret never appears), file writes land in sandbox, size caps,
exit/stderr, timeout, gating + not allow-able.

## 18. Weather + places

- `weather(place?)` — Nominatim geocode → open-meteo current temp,
  condition (WMO code), wind, today's high/low + rain %. No place
  = the configured briefing city. Free + keyless.
- `places(query, near?)` — Nominatim search, up to 5
  name/kind/lat/lon hits.

Tests: `tests/test_geo.py` — named place, default city, place
search, empty query, geocode-miss errors.

## 19. Lightweight video understanding (off by default)

- `video_look(path)` — `PA_VIDEO=false` by default. With
  `PA_VIDEO=true` + ffmpeg installed + a vision backend: resolves
  inside the sandbox only, caps at `PA_VIDEO_MAX_MB` (50), samples
  4 evenly-spaced frames, captions each, returns one
  `UNTRUSTED VIDEO FRAMES` fenced summary. Audio and inter-frame
  motion are explicitly unseen.

Tests: `tests/test_video.py` — default-off, sandbox-only, size
cap, no-ffmpeg error, fenced multi-frame summary.

## 20. Delegation + result wake-up

- `delegate(prompt)` enqueues a `job:delegate` chat_inbox row for
  the CURRENT channel — the existing drain runs it as an
  unattended turn and posts the summary back into the same
  thread (the wake-up). Gated actions inside the delegated run
  still surface as Approve/Reject cards. Audited.

Tests: `tests/test_delegate.py` — row queued for the same channel
with the delegate prompt, audited, generic job path, empty
prompt refused.

## 21. Todos + /stop + mid-turn steering

- `chat_todos` table (schema v17) — per-channel scratch-pad.
- Tools `todo_add`, `todo_done`, `todo_list` (numbered within open
  items) + `/todo [add <text> | done <n> | clear]` (strikethrough
  for finished items).
- `/stop` flags the channel; `run_turn` checks `stop_check()` at
  the top of every step and exits with "stopped by the owner".
  Flag cleared when the turn ends.
- `/steer <note>` queues a mid-turn note injected as a user
  message at the next step ("The owner steered mid-turn: …").

Tests: `tests/test_todos.py` — add/list/done flow, channel
scoping, bad numbers, stop ends a running turn, steer notes
reach the model as a user message.

## 22. code_task via freebuff

- `code_task(prompt, assistant?)` queues a task on the first
  `freebuff-cli` (agent-session) assistant — or a named one —
  through the existing task pipeline (`worker.create_task`).
  Approval-gated; audited; result is reported by the normal
  task-completion path.

Tests: `tests/test_codetask.py` — queues on the freebuff
assistant, explicit name, not_configured error, approval flag.

## 23. mac_get / mac_put + Mac agent

- `macmod.transfer()` — `scp -q` wrapper on the existing
  MAC_SSH_* tailnet connection (BatchMode, connect timeout,
  offline-first errors like `mac.run`).
- `mac_get(remote_path, filename?)` pulls a file into the
  per-channel chat sandbox and attaches it; `mac_put(sandbox_path,
  remote_path)` pushes a sandbox file out — realpath must stay
  inside `chat_sandbox`, 32 MB cap. Both always approval-gated,
  both audited. Remote paths are `shlex.quote`d for the remote
  shell.
- The "optional Mac agent" is the same MAC_SSH_* integration the
  `mac` command tool already uses — no second transport.

Tests: `tests/test_mactf.py` — put argv + remote quoting, get
lands in the sandbox and attaches, outside-sandbox put refused,
both gated.

## 24. /as — per-channel assistant switching

- `chat_threads.assistant_name` (schema v18) holds a per-channel
  override; the engine resolves the assistant per channel and
  re-resolves brain/fallback when `/as` switches mid-process
  (`_brain_key` tracks which assistant the brain was built for).
- `/as` shows the effective assistant, `/as <name>` pins the
  channel, `/as default` clears. Changes audited.

Tests: `tests/test_as.py` — set/show/clear, unknown assistant
error with the roster, engine resolves the override per channel.

## 25. Shareable Markdown pages

- `pages` table (schema v19): slug + title + markdown + enabled.
- `page_publish(title, markdown, slug?)` — approval-gated, audited;
  returns `{public_url}/pages/<slug>` (relative when PUBLIC_URL is
  unset). Custom slugs are slugified.
- `GET /pages/<slug>` is public — the unguessable slug is the
  credential; no session needed. Rendered by a tiny renderer that
  escapes ALL stored text before Markdown transforms, so a page
  can never inject markup/scripts.
- `/pages` lists live links; `/pages <slug>` unpublishes.

Tests: `tests/test_pages.py` — publish/get/unpublish, custom slug
+ republish, empty refused, escaping + heading/list/inline render.

## 26. Telegram mirror (off by default)

- `PA_TELEGRAM=true` + `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID`
  (the owner's own bot chat — only that chat is read/answered).
- `pa/telegram.py`: stdlib Bot API (`sendMessage`, `getUpdates`
  long-poll); `send` splits at Telegram's 4096-char cap.
- Poller thread enqueues owner texts as `source='telegram'`
  chat_inbox rows on channel `tg:<chat_id>`; `_tg_worker` runs the
  turn (serialized with Discord turns) and answers on Telegram.
- Discord-DM replies also mirror to the Telegram chat.

Tests: `tests/test_telegram.py` — off by default, all-three
config gate, sendMessage payload, long-text split, update parse.

## 27. Scheduled encrypted backups

- `scheduled-backup` system job seeded DISABLED at schema v20
  (daily 03:30 Europe/London; `/jobs scheduled-backup on`).
- `pa/backupjob.run`: AES-256-GCM export via the existing
  `backup.export_backup` to `PA_BACKUP_DEST` or
  `DATA_DIR/backups/pa-<stamp>.db.enc`. Skips quietly (no failed
  job row) when `PA_BACKUP_PASSPHRASE` is unset; prunes to the
  newest `PA_BACKUP_KEEP` (default 7) exports; DMs the result.

Tests: `tests/test_backupjob.py` — skip without passphrase,
export lands + keep-pruning, sysjob registration.

## 28. Approval-gated /update + self-update --dry-run

- `pa/selfupdate.py`: `plan()` diffs HEAD vs `@{u}` via
  `git ls-remote` (read-only — no fetch); `apply()` runs
  `git fetch --all --prune` + `git merge --ff-only @{u}`.
- `self_update(dry_run?)` chat tool — always approval-gated,
  never allow-rule eligible, audited.
- `/update` dry-runs then posts a job_action approval card;
  approving executes the pull once via `_apply_job_action`.
- `python -m pa self-update` / `--dry-run` reports only;
  `--apply` performs the merge. Restart is left to the owner.

Tests: `tests/test_selfupdate.py` — plan/apply argv sequences,
no-fetch guarantee, merge failure, gating + command wiring.

## 29. /usage + /settings

- `/usage` — today's requests/tokens vs the daily caps plus
  pending-approval, monitor and schedule counts.
- `GET /settings` (owner-authed web UI) — the pa_settings toggles
  `voice_notes` and `chat_progress` (on/off/default-to-env), a
  whitelist so no arbitrary key can be written; other keys render
  read-only. Saves are audited. `chat_progress` is now read
  through the same pa_settings fallback as `voice_notes`.

Tests: `tests/test_usage.py` — usage text, settings save/read,
chat_progress setting overrides the env default.

---

## Notes

- The "Mac agent" of #23 is the existing `MAC_SSH_*` channel (scp
  transport); delegated runs post to the originating channel via the
  normal job path; Telegram is one-owner-chat scoped; self-update needs
  a manual restart; `PA_BACKUP_PASSPHRASE` gates the backup job by
  design.
- The self-improvement loop built on this base is documented in
  `docs/EVOLVE.md`.
