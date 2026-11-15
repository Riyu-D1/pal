# pal evolve — self-healing, self-improving loop

Branch `feature/evolve` on top of `feature/parity`. Discord stays the
only chat interface; every approval surface is a Discord DM card or a
slash command. Nothing here merges or deploys pal's own code without an
owner approval.

The loop: **notice** (incidents) → **plan** (evolve items) → **build**
(skill/prompt/config, or a fix PR via `code_task`) → **score** (eval
harness) → **apply safely** (auto only for low-risk artifacts, with
rollback; code always needs an approval, CI, a health-checked deploy
and auto-rollback) → **record** (changelog + digest).

## Flags

All off by default; see `.env.example` for the full list.

| flag | default | what it gates |
|---|---|---|
| `PA_EVOLVE` | `false` | master switch for every acting stage |
| `PA_EVOLVE_AUTO_APPLY` | `false` | Track A low-risk auto-apply |
| `PA_EVOLVE_SELF_HEAL` | `false` | approval-gated `code_task` fix PRs |
| `PA_EVOLVE_PLAN_CALLS` | `10` | planner model calls/day |
| `PA_EVOLVE_REFLECT_MAX` | `3` | lesson/skill drafts per night |
| `PA_EVOLVE_DETECT_THRESHOLD` | `3` | fingerprint hits/24h to open an item |
| `PA_EVOLVE_DETECT_MAX_ITEMS` | `3` | new items per detect run |
| `PA_EVOLVE_HEALTH_SECONDS` | `60` | canary health window after deploy |
| `PA_HEALTH_RSS_MB` | `700` | `/healthz` RSS ceiling |
| `PA_HEALTH_EXPECT_WORKER` | `false` | also require a fresh worker beat |
| `PA_HEALTH_WORKER_MAX_SECONDS` | `120` | worker beat staleness limit |
| `PA_HEALTH_DISCORD` | `false` | also require a live gateway (in-process bot only) |
| `PA_ROUTER_ESCALATE` | `false` | the 'think harder' router tier |
| `PA_ROUTER_ESCALATE_ORDER` | documented order | CSV override; retired names dropped |
| `PA_ROUTER_ESCALATE_RPD` | `20` | escalate calls/day hard cap |

## System jobs (all seeded disabled — `/jobs <name> on`)

| job | cadence | does |
|---|---|---|
| `evolve-detect` | every 30 min | cluster incidents → open/update items |
| `evolve-digest` | weekly | DM "what I improved" report |
| `evolve-reflect` | nightly | draft lesson/skill items from the day |

## Ledger

- **item 1 — incidents.** `incidents` table (v21): source/kind/
  fingerprint/scrubbed summary+detail. `pa/evolve/incidents.py:record()`
  never raises and no-ops when the table is missing. Hooks: chat turns
  (exception, tool `error`/`not_configured`), failed worker tasks,
  router all-backends-failed and repeated 429 strikes, sysjob handler
  exceptions. Secrets are scrubbed with the same patterns as the log
  filter before anything is stored. Writing is unconditional (cheap);
  acting on incidents needs `PA_EVOLVE=true` + the job enabled.
- **item 2 — turn outcomes + feedback.** `turn_outcomes` table (v22):
  every attended turn lands one row from a deterministic heuristic
  (`ok` / `error` / `low_confidence`; two tool errors or an
  error/refusal-shaped reply downgrades it). With `PA_EVOLVE=true` a
  background thread may spend ONE fast-tier call grading an `ok` —
  never blocks the reply. `/good`, `/bad [note]` (owner-only), 👍/👎
  buttons on every reply, and the ungated `feedback` chat tool write
  `feedback_*` outcomes + incidents. A message starting like "no, I
  meant X" records an `owner_correction` incident only — it never
  rewrites memory or skills.
- **item 3 — deep `/healthz`.** `pa/health.py:check()` — public,
  secret-free, cheap (no model calls). `ok` now requires a real SQLite
  read AND a tiny `health_beat` write plus RSS under
  `PA_HEALTH_RSS_MB` (default 700). Opt-in checks:
  `PA_HEALTH_EXPECT_WORKER` (fresh `worker_beat`, stamped by the
  worker loop at most once per heartbeat) and `PA_HEALTH_DISCORD`
  (in-process gateway frame within 180 s — only meaningful when the
  bot shares the process, which `pa serve` does). Any failure returns
  **503** `{"ok": false, "failed": ["db"|"rss"|"worker"|"discord"]}`
  — short codes only, never paths or env.
- **item 4 — gap detector.** `evolve_items` table (v23): shape
  (lesson/skill/prompt/config/connector/tool/fix/eval), risk
  (low/high), status (open→needs_plan→planned→building→awaiting_eval→
  awaiting_approval→applied | rolled_back | rejected | closed),
  dedupe_key, hits, artifacts, eval_score, error. `evolve-detect`
  sysjob (seeded disabled, every 30 min): with `PA_EVOLVE=true` it
  clusters new incidents by fingerprint and opens/updates one item
  when ≥ `PA_EVOLVE_DETECT_THRESHOLD` hits in 24 h, any `health_fail`,
  or ≥2 `not_configured` for the same tool — capped at
  `PA_EVOLVE_DETECT_MAX_ITEMS` new items per run, and never DMs the
  owner.
- **item 5 — changelog + `/evolve` + digest.** `evolve_changelog`
  (v24) is an append-only ledger (`log_event(conn, s, item_id, verb,
  text)`) mirrored to the vault note `Evolve/YYYY-MM.md` (or
  `<data>/evolve/` without a vault). `/evolve` (owner, ephemeral)
  lists open items + recent applies/rollbacks; `/evolve <id>` shows
  the plan. `evolve-digest` (seeded disabled, Sundays 18:00) DMs a
  short "applied / rolled back / still needs you" report and stays
  quiet on an empty week.
- **item 6 — capability requests.** Chat tool
  `need_capability(what, why)` opens/dedupes an evolve_item
  (`cap:<sha>` dedupe; shape guessed from `what` — existing tool
  names → `tool`, connect/login/app words → `connector`, else
  `skill`). With `PA_EVOLVE=true` it drafts a plan via ONE
  smart-tier call, capped at `PA_EVOLVE_PLAN_CALLS`/day (default
  10); model down or cap spent → `needs_plan`. The planner prompt
  lists every CHAT_TOOLS name and steers app gaps to `/connect`;
  the `why` is wrapped in an untrusted fence. Connector items
  tell the owner what to connect — nothing is built and nothing
  waits on a card.
- **item 7 — nightly reflection.** `evolve-reflect` (seeded
  disabled, 23:30 — after daily-journal): with `PA_EVOLVE=true`
  it feeds today's journal text, incidents, and bad/low-confidence
  feedback to one fast-tier `sysjobs.model_text` call and opens at
  most `PA_EVOLVE_REFLECT_MAX` (default 3) pending `lesson`/`skill`
  items (`ref:<sha>` dedupe). It never writes `owner_facts` and
  never applies anything — Track A (item 10) is the only apply
  path. A quiet day or a `NONE` reply opens nothing.
- **item 8 — versioned skills + prompt snippets.** Every
  `skill_save` records `skill_versions(name, version, body,
  source)` (vault and SQLite paths both); `snapshot_skill` /
  `restore_skill(name, version)` are Track A's rollback hooks.
  `prompt_snippets` are capped system-prompt addenda with the
  same version ledger, listed at the end of
  `chattools.prompt_lines` as advisory text — `sanitize()` drops
  any line that asks to skip approvals, ignore allow-rules, or
  drop untrusted fences, so a snippet can never weaken a gate.
  `skill_save` is still owner-approval-gated.
- **item 9 — eval harness.** `python -m pa eval` replays
  `evals/*.json` through the real ChatEngine with a scripted
  brain — deterministic, zero network. A fixture asserts on the
  tool trace (`expect_tools` / `forbidden_tools`), the reply
  (`must_contain` / `must_not_contain`), and the observations
  the brain saw (`obs_must_contain`). `evals/baseline.json`
  names the fixtures that must stay green; a baseline failure
  or any failing fixture exits non-zero. `--live` (real fast
  tier) is opt-in and never runs in unittest or CI. Items 10+
  reuse `eval.run(...)`/`store_score` for the apply gate; the
  starter set covers `code_task` stays approval-gated, a
  hostile email can't touch owner facts, and fetched pages
  arrive fenced.
- **item 10 — Track A auto-apply (default OFF).** With
  `PA_EVOLVE_AUTO_APPLY=true`, `apply.maybe_apply` runs
  snapshot → apply → offline eval + deep `/healthz` →
  `applied` (changelog + DM). Eligible: `lesson`/`prompt`
  (become prompt snippets), `skill` updates to names the
  owner already accepted, and allowlisted `config` kinds
  (`schedule_every`, `monitor_every`, `router_model` —
  catalog names only). Any eval < 1.0 or health failure
  restores the snapshot, marks `rolled_back`, incidents
  `eval_regress`/`health_fail`, and cools the dedupe key
  for 24 h. Flag off (or a new skill name, ineligible
  shape, or config key outside the allowlist) →
  `awaiting_approval` + an Approve/Reject DM card
  (`evappr:` components); the click itself is the
  approval and runs the same pipeline.

## Deliberately not built

Matrix/Element, Open WebUI, a self-hosted ntfy daemon, phone/SMS, group
rooms, consumer-web-UI automation (ChatGPT / Claude.ai / consumer
Gemini), unattended account registration, GitHub Models / Cerebras /
Mistral in the default router order, a process pool or second always-on
browser on the 1 GB VM, and any flag that skips owner approval for a
code checkout.
- **item 11 — protected paths.** `pa/evolve/policy.py` lists the
  paths that are never Track A and flag a Track B card high-risk
  (approvals/allow-rules/review, vault/credvault/secretsbox/
  passwords, egress/untrusted, the Discord owner-gate + webapp
  session/CSRF, this policy itself, `.github/workflows/`,
  `deploy/`, dotenv files). `touches_protected(paths)` returns
  the hits; items 12–13 consult it. A hit means owner review as
  a normal code change — no "Always allow" on that card.
- **item 12 — self-heal (default OFF).** With
  `PA_EVOLVE_SELF_HEAL=true`, a `fix`/`tool` item in `planned`
  gets a reproducing eval fixture on its artifacts and queues a
  `code_task` **through the normal approval gate** — the owner
  approves "spend a coding turn", never a deploy. The prompt
  carries plan + scrubbed traces + fixture + hard rules (new
  branch, one concern, tests, no protected paths, no merge, no
  deploy, no secrets). GitHub creds present → asks for a DRAFT
  PR; absent → a patch under `DATA_DIR/evolve/patches/` (path
  recorded on the item). Nothing merges or pulls.
- **item 13 — canary deploy.** `selfupdate.apply_sha(sha)` /
  `rollback(prev)` run the real canary: fetch → protected-path
  check (`policy.touches_protected` refuses the diff outright)
  → `merge --ff-only` → `systemctl --user restart pa`
  (injectable; no systemd → `{needs_restart}`, never a lie)
  → poll deep `/healthz` for `PA_EVOLVE_HEALTH_SECONDS` →
  healthy: done; failing: explicit `reset --hard <prev>` +
  restart + re-check + `rolled_back`. Owner approval is still
  mandatory — `/update` and `self_update` now run the same
  canary instead of "please restart it yourself". No force-push,
  no hard reset to anything but the recorded previous SHA.
- **item 14 — watchdog (not installed by this branch).**
  `python -m pa watchdog` is a separate process: polls
  `127.0.0.1:<port>/healthz`, and after `fail_after` (default
  60s) of continuous sickness runs `systemctl --user restart
  pa`, writes a best-effort incident, and DMs the owner once
  per outage window. `deploy/pa-watchdog.service` is the
  systemd unit (Restart=always, MemoryMax=128M) — install notes
  are in its comments; nothing here touches the VMs.
- **item 15 — escalate tier (default OFF).** `PA_ROUTER_ESCALATE`
  adds a third router tier: order `gemini-cli, copilot-cli,
  gemini-api, groq, openrouter-free, cloudflare, pollinations`
  (smart models; `github-models`/`cerebras`/`mistral` excluded
  even by override). Triggers: all current-tier backends failed
  (one in-turn retry), a `low_confidence` outcome arms the next
  turn on that channel, `/think` arms it manually, and the
  planner/eval path can request it. Flag off → `tier=escalate`
  behaves exactly like `smart`. `PA_ROUTER_ESCALATE_RPD`
  (default 20) is a hard daily cap — spent → ProviderUnavailable
  + incident; each escalate answer records a `low_confidence`
  incident with the reason trail, and `/usage` (item 19) shows
  used/cap. docs/ROUTER.md has the 12 Nov 2026 limits.
- **item 16 — owner-approved provider setup.**
  `provision.propose(conn, s, bot, what, why, url)` posts a DM
  card (`evprov:` buttons): what, why, the official https URL,
  and the promise that pal never types passwords/OTPs/cards and
  never self-registers. Approve → `browse_goto` on the computer
  when `PA_COMPUTER_ENABLED`, else a DM with just the URL.
  Reject → nothing. There is deliberately no code anywhere in
  `pa/` that drives chatgpt.com / claude.ai / consumer Gemini —
  a test asserts it stays absent.
- **item 19 — flags, /usage, docs.** `/usage` now appends an
  `evolve:` section when the tables exist: flag state, open item
  count, last apply/rollback, escalate used/cap. `.env.example`
  lists every evolve/health/escalate flag (all off / disabled).
  The settings whitelist still writes only `_SETTINGS_KEYS` — a
  POST with any other key is a no-op (tested). README's docs
  list and `docs/PARITY.md` point here.
