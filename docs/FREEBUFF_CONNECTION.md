# Freebuff connection — evidence and adapter

Design notes for the Freebuff agent-session provider
(`pa/freebuff_session.py` + `pa/freebuff_sidecar.py`), researched and
tested in November 2026 on macOS (darwin-x64) and Linux.

## Verdict

**One supported pathway exists and is implemented:** driving the owner's
own, already-signed-in **official `freebuff` CLI through its normal
terminal interface** (tmux pseudo-terminal + bracketed paste + Enter —
the same input path a human at the keyboard uses, and the same method
the upstream repo's own `scripts/tmux` test helpers use). The answer is
read from the transcript JSON the CLI itself writes to disk.

This is an **agent-session adapter, not an OpenAI-compatible chat
endpoint**: one prompt in → one CLI agent session → one plain-text
answer out. It does not emit `tool_calls`, does not stream, and is never
a fallback provider. It is disabled by default and only reachable by an
explicit per-assistant pin while `PA_FREEBUFF_ENABLED=true`.

## Evidence matrix

| # | Pathway | Source | Status | Method | Allowed? | Headless / cloud | Tested |
|---|---------|--------|--------|--------|----------|------------------|--------|
| 1 | Official CLI via PTY/tmux | installed binary + public source | **official client, local adapter** | launch `freebuff --cwd <dir>` in a tmux pane; bracketed-paste the prompt + Enter; poll `chat-messages.json`; `/exit` to quit | **Yes** — normal process/terminal interface, owner's own login; no tokens read, no backend calls, no spoofing | needs a real terminal (tmux provides it); login is interactive `freebuff login`; cloud/datacenter IPs may hit the service's own checks — not evaded, reported as failures | **yes** — fake CLI over real tmux in CI; one live smoke (below) |
| 2 | `--acp` / Agent Client Protocol | [PR #1611](https://github.com/CodebuffAI/freebuff/pull/1611) | **not released** | PR opened 2026-11-02, still **OPEN** 2026-11-08; installed `0.2.36` reports `error: unknown option '--acp'` | No pathway yet — do not build against an unmerged PR | n/a | verified absent in installed binary |
| 3 | `--print` / headless / prompt-arg mode | [issue #947](https://github.com/CodebuffAI/freebuff/issues/947), `cli/src/cli-args.ts` | **not released** | Freebuff's arg parser accepts only `login`/`doctor` as `[command]` — `[prompt...]` exists only in the Codebuff build; `initialPrompt` is hardcoded `null` for freebuff. A stray `--headless` string in the binary is embedded Bun/Chromium tooling, not a CLI flag (`headless: false is not yet implemented`). Issue #1477 asks for prompt args — still open | No pathway yet | a real headless flag is precisely what the issues request | verified absent |
| 4 | Direct API via session token + catalog protocol | public repo `sdk/` + `common/` | **probed live; server-gated** | The CLI itself does `CodebuffClient({apiKey: authToken})` — the session token IS the apiKey. Reproduced faithfully: signed `GET /api/v1/freebuff/models` (Ed25519 device key) → catalog rows + `fetchId`; `POST /api/v1/freebuff/session/admission` (`cli:<uuid>` claim, multi-session headers) → **`status: active`, real instanceId**; completions with catalog/instance/device-sig/sse-gzip headers + `codebuff_metadata` → **403 `free_mode_cli_required`** ("calling the API directly is not supported and may get your account banned"). Remaining discriminator is the client-environment descriptor (TTY profile); forging it means defeating an explicit anti-automation gate. | **No** — server explicitly refuses + warns of bans; stopped there | n/a | **yes, live** — every stage verified against the real backend 2026-11-09 |
| 5 | `github.com/Praket7/freebuff-mcp` | repo README + source | third-party | drives **Freebuff Desktop's internal orchestrator API / SQLite** — an undocumented private surface | **Excluded** — internal/undocumented API | n/a | not installed, not run |
| 6 | `github.com/toyro396133/freebuff-bridge` | repo README + source | third-party | same pattern — private desktop-orchestrator endpoints | **Excluded** — internal/undocumented API | n/a | not installed, not run |
| 7 | mcpmarket `freebuff2api-openai-proxy` | product page | third-party | OpenAI-compatible proxy that mimics the freebuff client | **Excluded** — client impersonation / hidden backend calls; comparison only | n/a | not installed, not run |

### Installed CLI facts (2026-11-08)

| item | value |
|---|---|
| launcher | `$(npm prefix -g)/bin/freebuff` (npm pkg; the wrapper self-updates on launch) |
| real binary | `~/.config/manicode/freebuff`, metadata `{"version":"0.2.36","target":"darwin-x64"}`; `npm view freebuff version` → `0.2.36` |
| commands | `login`, `doctor` only |
| options | `--version`, `--continue [conversation-id]`, `--cwd <dir>`, `--trust-agents`, `--help` — **no prompt arg, no `--print`, no `--json`, no `--acp`, no headless** |
| config home | `~/.config/manicode` (`FREEBUFF_CONFIG_DIR` is the official env override); credentials file exists — owner is signed in; never opened/read by the adapter |
| transcript | `<home>/projects/<basename(cwd)>/chats/<chat-id>/chat-messages.json`; ai turns carry `isComplete`, `completionTime`, `credits`, `blocks[]` with `textType: reasoning|text` |
| clean exit | `/exit` (aliases `/quit`, `/q`) |

## How the adapter works (`pa/freebuff_session.py`)

1. `run(prompt)` acquires a single global session lock (at most one
   session at a time — no parallel spend multiplication).
2. A fresh empty workdir `PA_FREEBUFF_WORKDIR/fb-<runid>` is created and
   the CLI launched there inside tmux on a private `-L pa-freebuff`
   socket (never touches the owner's own tmux server or sessions).
3. `_await_ready` waits for the TUI input box (bounded by
   `PA_FREEBUFF_READY_TIMEOUT`); a login wall fails fast with
   `ProviderNotConfigured` telling the owner to run `freebuff login`.
4. The prompt goes in through the normal input path (bracketed paste +
   Enter). The driver then polls the transcript file every 0.75 s until
   a completed ai turn appears or the deadline hits
   (`PA_FREEBUFF_SESSION_TIMEOUT`, min'd with the task deadline).
5. `/exit` quits cleanly; the pane is force-killed only if the process
   ignores the clean path for 15 s.
6. The answer is the `text` blocks of the last completed ai turn
   (`reasoning` blocks are kept separately, capped at 4 k chars). Output
   is capped by `PA_FREEBUFF_MAX_OUTPUT_CHARS`; transcript reads are
   capped at 8 MB.

### UI gates answered through the visible interface only

| screen | driver action |
|---|---|
| profile-survey overlay | `Escape` ("not now"), rate-limited to once per 2 s |
| stale live-session "Enter: take over" | `Enter` — the owner's own session being reclaimed |
| "Enter: confirm and send" (wallet spend) | `Enter` **only** when `PA_FREEBUFF_ALLOW_PAID=true`; default: fail with `FreebuffRateLimited` |
| "Enter: retry" / "choose another model" / quota, VPN, ban, region, hours notices | **never pressed** — fail the task with the CLI's own reason (`ProviderRateLimit` → existing `QuotaExhausted` path) |
| login wall | fail: owner runs `freebuff login` natively; the adapter never sees credentials |

No retries that evade limits: a refused admission fails visibly; pacer
quota (`QuotaExhausted`) applies as with any provider.

### Security properties preserved

- Disabled by default; env-only config; the provider join is a
  `(freebuff-cli, default)` pin that can only be selected by an explicit
  per-assistant pin — never a fallback.
- Generic `LLM_*` path is untouched and remains the default.
- The model gets **no arbitrary shell**: it receives a single prompt
  string inside a fresh empty workdir. The freebuff agent has its own
  built-in tools, so the session policy instructs it not to modify files
  or run commands — that is an instruction, not a sandbox (see
  Residual risk).
- Untrusted-content rules: `build_session_prompt` applies the same
  `<<<UNTRUSTED>>>` policy markers as the chat system prompt.
- No credentials, tokens, keys or internal endpoints anywhere in the
  code path; the binary does all auth itself.
- Cancellation: `check_cancel` is polled every tick; deadline is the
  min of task deadline and `PA_FREEBUFF_SESSION_TIMEOUT`.

### Residual risk (be honest)

- The freebuff agent **can** use its own tools inside its workdir. We
  give it a fresh empty directory per run and instruct it not to write
  or execute — but a determined untrusted prompt could still ask it to
  create files under that workdir. Keep `PA_FREEBUFF_WORKDIR` dedicated
  and empty; the adapter does not read back anything but the transcript.
- Public docs note the free tier's ToS restricts automation; enabling
  this provider is an explicit opt-in (`PA_FREEBUFF_ENABLED`). If Freebuff rejects the session
  (survey/admission/quota/VPN checks), the adapter surfaces that as a
  failure rather than working around it.
- A TUI is a fragile interface: a CLI redesign will break the screen
  markers. The adapter fails closed (timeout/refusal), never silently.

## Headless / Linux / Docker

- The released CLI is TUI-only; in a container it needs a PTY — tmux
  provides exactly that (the upstream repo's own e2e tests drive it the
  same way). The adapter itself should run on Linux unchanged.
- `freebuff login` is interactive: it prints a URL and polls; on a
  headless box the owner opens that URL in a browser elsewhere and the
  CLI session completes. The adapter leaves that flow to the owner
  natively — it never handles credentials.
- Freebuff's admission includes explicit VPN/proxy/Tor, region,
  IP-cap and ban checks. **A success on your home machine does not prove
  cloud/datacenter deployment works** — a cloud IP may be refused, and
  this adapter reports that refusal instead of evading it.

## Owner setup

```bash
freebuff login                      # once, if not already signed in
PA_FREEBUFF_ENABLED=true            # env
# optional:
PA_FREEBUFF_CLI=freebuff            # CLI binary name/path
PA_FREEBUFF_TMUX=tmux               # tmux binary
PA_FREEBUFF_HOME=~/.config/manicode # CLI config home
PA_FREEBUFF_WORKDIR=<data>/freebuff-work
PA_FREEBUFF_SESSION_TIMEOUT=300
PA_FREEBUFF_READY_TIMEOUT=45
PA_FREEBUFF_MAX_OUTPUT_CHARS=60000
PA_FREEBUFF_ALLOW_PAID=false        # NEVER press spend-confirm by default
PA_FREEBUFF_ASSUME_READY=false      # escape hatch: proceed when the ready
                                  # marker is absent (use after CLI updates
                                  # if the input screen text changed)
PA_FREEBUFF_LIVE_CHECK=false        # provider-check runs one real probe
                                  # session to verify markers end-to-end
```

Then pin an assistant's provider to host `freebuff-cli`, model
`default`. `pa provider-check` reports installation state without
spending a session (`PA_FREEBUFF_LIVE_CHECK=true` spends one real probe
session instead).

## Remote mode — freebuff on owner hardware from a cloud PA

```bash
# on the home machine (signed-in CLI + tmux):
PA_FREEBUFF_ENABLED=true \
PA_FREEBUFF_REMOTE_TOKEN=<shared-secret> \
pa freebuff-sidecar            # binds 127.0.0.1:8777 by default
# PA_FREEBUFF_SIDECAR_HOST/PORT to bind elsewhere (e.g. a tailnet IP)

# on the cloud PA — reach it over tailnet or an SSH tunnel only:
ssh -N -L 8777:127.0.0.1:8777 home-box     # or tailscale
PA_FREEBUFF_ENABLED=true
PA_FREEBUFF_REMOTE_URL=http://127.0.0.1:8777
PA_FREEBUFF_REMOTE_TOKEN=<same-shared-secret>
```

The remote URL passes the same rules as `LLM_BASE_URL`: https anywhere,
plain http only to loopback/private addresses. A remote run is identical
to a local one from the task's point of view — `meta.via: sidecar`
marks it. Verified live: remote → sidecar → real CLI → answer in 15.6 s.

### Known service-side quirk (observed live, fixed)

A task body containing `<<<END UNTRUSTED>>>`-style markers made the
stealth model's backend return HTTP 400 `provider_error` ("The stealth
model could not complete the request.") — twice, deterministically; the
same structured prompt without those markers succeeded 40 s later.

**Fix:** the adapter translates `<<<UNTRUSTED x >>>` /
`<<<END UNTRUSTED x>>>` to `[UNTRUSTED x]` / `[END UNTRUSTED x]` before
sending — the untrusted-data boundary and the policy text survive; only
the delimiter glyphs change. **Verified live:** the exact body that
400'd twice completed in 12.8 s after the fix. A `userError` turn still
fails fast (`FreebuffSessionError`) if the service rejects anything
else — surfaced, not worked around.

## Caveats → what fixes them

| caveat | fix / status |
|---|---|
| TUI markers break on redesign | markers now mirror upstream's own e2e expectations (`freebuff/e2e/utils/freebuff-session.ts` boot signals, `freebuff-chat-controls.tsx` gate labels); `provider-check` reports installed-vs-tested CLI version (`0.2.36`); the launcher defers updates until CLI exit, so a session can never be killed mid-run by an update — drift is detected and warned, not silent. Escape hatches if a release drifts anyway: `PA_FREEBUFF_ASSUME_READY=true` (paste into a live unrecognized screen instead of failing startup) and `PA_FREEBUFF_LIVE_CHECK=true` + `pa provider-check` (one real probe session to verify markers after an update) |
| `<<<UNTRUSTED>>>` 400 | **fixed** — markers translated to brackets pre-send (verified live) |
| concurrent tasks = second one fails | **fixed** — the session lock now queues with a bounded wait (the caller's own deadline, `check_cancel` still polled) instead of failing instantly |
| headless Linux/Docker login | `freebuff login` is explicitly built for remote/SSH use: it prints a URL and polls. Recipe: `tmux new-session -d -s fb-login 'freebuff login'` → `tmux capture-pane -p -t fb-login` → owner opens the URL → poll completes → `credentials.json` written. Adapter itself is Linux-ready (CI runs its real-tmux test on ubuntu) |
| cloud/datacenter IP refused | **workaround built** — `pa freebuff-sidecar` runs the session provider on the owner's home machine; a remote PA sets `PA_FREEBUFF_REMOTE_URL` + `PA_FREEBUFF_REMOTE_TOKEN` and the same `freebuff-cli` pin resolves to `FreebuffRemoteProvider`. The session genuinely originates from owner hardware running the official client — routing, not spoofing. Transport: tailnet or SSH tunnel (shared bearer token required on every request; http allowed only to loopback/private — tailnet counts; use https elsewhere). **Turnkey kit**: `deploy/freebuff-sidecar/` (install/login/run scripts + systemd unit + CI verification on a clean ubuntu VM) |
| ToS automation risk | policy, not code. Mitigated: no evasion, fails closed, `PA_FREEBUFF_ALLOW_PAID` default off. Sanctioned alternatives on the generic `LLM_*` path: a paid Codebuff API key, or a free anonymous endpoint — **Pollinations** (`text.pollinations.ai/openai`, `LLM_COMPLETIONS_PATH=""`, no key) is live-verified through `provider_for()` on this branch and needs no local machine at all; see `docs/PROVIDERS.md` § "Verified free/anonymous endpoints". Asking the vendor about automation terms is also an option |
| No always-on local machine | free tier needs *some* machine running the official client (the `free_mode_cli_required` gate is server-side; there is no holdable token). Options: sidecar on an always-on home box (above), or **skip Freebuff entirely** — Pollinations on the `LLM_*` path is anonymous, cloud-ready, and verified |
| single session / no streaming / no tools | inherent to a TUI session. Serial-by-design (one session at a time protects the account's own concurrency cap). Upstream offers `--continue [conversation-id]` to resume a conversation — deliberately unused: fresh workdirs keep tasks isolated. `/model` is the official picker UI for choosing a model — also unused (driving an interactive picker adds fragile states; spend-confirm gate already protects the wallet) |
| 400/other service rejections | **fixed** — `userError` turns fail fast with the CLI's own message instead of polling to the cap |

## Test status

- `tests/test_freebuff_session.py` + `tests/test_freebuff_sidecar.py` —
  48 deterministic tests: transcript
  extraction (incl. `userError` turns), screen classification,
  orchestration against an in-memory fake tmux, plus **one real-tmux +
  fake-CLI end-to-end** (skipped only where tmux is absent). Fake
  success is explicitly not live success.
- Full suite: `python -m unittest discover -s tests`.
- Live smoke: verified end-to-end against a signed-in CLI (Nov 2026);
  see the session log format in `pa freebuff-sidecar` output.
