# Free-API router ("mini Freebuff")

`pa.router` provides a failover pool: an OpenAI-compatible provider pinned as `provider_host='router'`
that walks an ordered list of free-tier backends per call.

- **Tiers** — `smart` (agent/tool steps) and `fast` (plain chat,
  summaries) each have their own backend order and model. If the
  requested tier's backends are all cooling/capped/unconfigured, the
  call falls through to the other tier's list.
- **Failover** — 429 / 402 / 5xx / timeout / quota errors mark that
  backend cooling down (honouring `Retry-After` and `x-ratelimit-reset-*`
  headers, else exponential backoff) and the **same call** tries the next
  backend. Cooldowns persist in SQLite (`router_cooldowns`) so they
  survive restarts.
- **Quota tracking** — per-minute and per-day request/token usage is
  counted in `router_usage_minutes` / `router_usage_days` (migration 6);
  a backend is skipped *before* its configured cap is hit.
- **Needs config, never a secret** — a backend whose key env vars are
  unset is never called and simply reads "needs config" in
  `pa router-status`. Keys come only from env vars; logs are metadata
  only.
- **Deadline** — every call has a whole-call budget
  (`PA_ROUTER_CALL_SECONDS`, default 180) inside the chat turn deadline
  (`PA_CHAT_TURN_SECONDS`).

Enable it:

```sh
PA_ROUTER_ENABLED=true
# then pin an assistant: UPDATE assistants SET provider_host='router',
# provider_model='default' WHERE name='pal';
# (or set the pin in the web UI — 'router' appears once enabled)
```

Inspect it:

```sh
python -m pa router-status   # configured?, cooling until, today vs caps
python -m pa router-check    # one tiny live call per configured backend
```

## Backends

Limits below are what each provider's **official docs** stated on the
verification date (2026-11-10). Free plans change; `pa router-status`
shows what is actually configured, and every cap is overridable via
`PA_ROUTER_CAP_<BACKEND>_<RPM|TPM|RPD|TPD>`.

| backend | kind | key env | signup | documented free limits (verified 2026-11-10) | status |
|---|---|---|---|---|---|
| `gemini-cli` | sidecar | `PA_GEMINI_CLI_TOKEN` (local bearer) | [ai.google.dev](https://ai.google.dev/) → Antigravity CLI | plan-based session limits via signed-in Google account (AI Pro raises them); exact numbers aren't published | **conditional** — needs the `agy` CLI installed + signed in once on the host; see below |
| `copilot-cli` | sidecar | `PA_COPILOT_CLI_TOKEN` (local bearer) | [github.com/features/copilot](https://github.com/features/copilot) | subscription-based; Copilot Pro (free via Student Pack) | **conditional** — needs `copilot` CLI installed + signed in once; see below |
| `groq` | api | `GROQ_API_KEY` | [console.groq.com](https://console.groq.com/) | `openai/gpt-oss-120b`: 30 RPM / 1K RPD ([docs](https://console.groq.com/docs/rate-limits)) | works live |
| `gemini-api` | api | `GEMINI_API_KEY` | [aistudio.google.com/apikey](https://aistudio.google.com/apikey) | free-tier caps vary per model/project, shown in AI Studio; ~15 RPM / 450 RPD used ([docs](https://ai.google.dev/gemini-api/docs/rate-limits)) | works live |
| `openrouter-free` | api | `OPENROUTER_API_KEY` | [openrouter.ai/keys](https://openrouter.ai/keys) | `openrouter/free` + `:free` models: 20 RPM, 50 RPD ([docs](https://openrouter.ai/docs/faq)) | works live |
| `cloudflare` | api | `CLOUDFLARE_ACCOUNT_ID` + `CLOUDFLARE_API_TOKEN` | [dash.cloudflare.com](https://dash.cloudflare.com/) | 10K free neurons/day on Workers AI ([docs](https://developers.cloudflare.com/workers-ai/platform/pricing/)) | works live |
| `pollinations` | api | none (`POLLINATIONS_API_KEY` optional) | none needed | anonymous tier ~1 req / 30 s per IP; answers **HTTP 402** + Retry-After when spent ([docs](https://github.com/pollinations/pollinations/blob/master/APIDOCS.md)) | works live — always last fallback |
| `cerebras` | api | `CEREBRAS_API_KEY` | [cloud.cerebras.ai](https://cloud.cerebras.ai/) | **not in defaults**: current trial requires a verified payment method and grants expiring credits — opt in via `PA_ROUTER_ORDER` | payment-gated trial |
| `mistral` | api | `MISTRAL_API_KEY` | [console.mistral.ai](https://console.mistral.ai/) | **not in defaults**: as of 2026 the free mode no longer includes API access; keys require billing activation — opt in via `PA_ROUTER_ORDER` | free API retired |

Dropped entirely — verified dead:

| would-be backend | what the official docs say (2026-11-10) |
|---|---|
| `github-models` | **GitHub Models was fully retired 2026-07-30** — `models.github.ai/inference` no longer serves. The Copilot CLI sidecar is how the Student Pack value is used instead. |
| `gemini` CLI (as a signed-in CLI) | The `gemini` CLI stopped accepting **personal Google accounts** on 2026-06-18; consumer sign-in moved to the **Antigravity CLI (`agy`)**. `PA_GEMINI_CLI_BIN` still overrides the binary if you have a Code Assist seat. The plain Gemini **API** key (`gemini-api` backend) is unaffected. |

## Ordering and overrides

```sh
PA_ROUTER_ORDER=...            # CSV names; applies to BOTH tiers
PA_ROUTER_SMART_ORDER=...      # overrides PA_ROUTER_ORDER for smart
PA_ROUTER_FAST_ORDER=...       # overrides PA_ROUTER_ORDER for fast
PA_ROUTER_SMART_MODELS=groq=openai/gpt-oss-120b,cerebras=gpt-oss-120b
PA_ROUTER_FAST_MODELS=groq=openai/gpt-oss-20b
PA_ROUTER_CAP_GROQ_RPD=900     # any cap on any backend
PA_ROUTER_CALL_SECONDS=180     # whole-call deadline
PA_ROUTER_BACKEND_SECONDS=60   # per-attempt cap
```

Defaults (after the 2026-11-10 verification pass — `github-models`,
`cerebras` and `mistral` were removed from the suggested order because
the first is retired and the other two have no durable free tier):

- smart: `gemini-cli, copilot-cli, groq, gemini-api, openrouter-free, cloudflare, pollinations`
- fast: `groq, gemini-api, openrouter-free, cloudflare, gemini-cli, copilot-cli, pollinations`
- escalate (`PA_ROUTER_ESCALATE`, off by default): `gemini-cli,
  copilot-cli, gemini-api, groq, openrouter-free, cloudflare,
  pollinations` — the "think harder" tier (docs/EVOLVE.md #15):
  all-backends-failed retries, two weak replies, a low-confidence
  outcome, or `/think`. It walks the smart models of the same
  free backends and is capped at `PA_ROUTER_ESCALATE_RPD`
  (default 20/day) so a confused loop can't burn the quotas —
  12 Nov 2026 documented limits: Groq 30 RPM / 1k RPD, Gemini
  15 RPM / 450 RPD, OpenRouter free 20 RPM / 50 RPD, Cloudflare
  ~10k neurons/day. `github-models`, `cerebras` and `mistral`
  are excluded even by explicit `PA_ROUTER_ESCALATE_ORDER`.

## Switching the VM's `pal` assistant to the router

```sh
PA_ROUTER_ENABLED=true            # in the service env
sqlite3 $PA_DATA_DIR/pa.db \
  "UPDATE assistants SET provider_host='router', provider_model='default' WHERE name='pal';"
systemctl --user restart pa
python -m pa router-status        # sanity: at least one 'yes'
```

Reverting is the same UPDATE with the previous host/model.

## Official-CLI sidecars (loopback + bearer)

Two optional backends run an **official signed-in CLI** in headless
print mode on the VM and serve it over the freebuff wire protocol, so
the router treats them like any other session provider. Each sidecar
binds loopback and requires a bearer token (`PA_*_CLI_TOKEN`) that you
choose — the token is how the router authenticates to the sidecar, it is
not a provider secret.

```sh
PA_GEMINI_CLI_TOKEN=<random token>
PA_GEMINI_CLI_BIN=agy            # antigravity CLI; 'gemini' if you have Code Assist
PA_GEMINI_CLI_URL=http://127.0.0.1:8781
python -m pa cli-sidecar gemini  # serves the `agy` session on loopback

PA_COPILOT_CLI_TOKEN=<random token>
PA_COPILOT_CLI_URL=http://127.0.0.1:8782
python -m pa cli-sidecar copilot # `copilot -s --no-ask-user -p`
```

systemd user units ship in `deploy/cli-sidecars/`
(`pa-gemini-sidecar.service`, `pa-copilot-sidecar.service`) — same
shape as `deploy/freebuff-sidecar/sidecar.service`: drop them into
`~/.config/systemd/user/`, put `PA_DIR` and the `PA_*_CLI_TOKEN` in
`~/.pa-sidecar/env`, `loginctl enable-linger`, daemon-reload,
enable --now.

### One-time headless login on the VM

Done once, by the owner, on the host — never automated:

- **Antigravity (`agy`)**: install the CLI, run `agy auth login` — it
  prints a URL; open it on any machine, sign in with the Google account
  (AI Pro raises plan limits), paste the code back. Verify headless mode
  once: `agy -p "say ok"` prints an answer with no TUI.
- **Copilot**: install the official GitHub Copilot CLI, `copilot` →
  `/login` once (or set a token per the CLI's docs) on the VM's GitHub
  account (Student Pack = Copilot Pro). Verify:
  `copilot -s --no-ask-user -p "say ok"`.

Sidecar backends are skipped automatically for calls that carry OpenAI
tool schemas — they answer the fenced-JSON chat protocol (plain text)
but cannot emit `tool_calls`.

## Failure semantics

`pa router-check` calls each configured backend once and reports
pass/fail + latency — never keys. A backend that is capped or cooling
keeps its persisted state across `pa` restarts; `router-status` shows
`cooling until (reason)` and today's `requests/tokens` against the
effective caps.
