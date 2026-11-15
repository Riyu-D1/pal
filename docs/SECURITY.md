# Security

Threat model: one owner, cloud-hosted, internet-facing behind TLS. The app
must defend the network boundary, the session, the egress path, and the
secrets — and assume fetched web content is adversarial.

## Network boundary

- Development binds loopback only.
- Production binds loopback, **or** a non-loopback address only when the
  documented Fly topology is detected (`FLY_APP_NAME` set — Fly's proxy
  terminates TLS and injects `Fly-Client-IP`). Any other non-loopback bind
  is refused at startup. An env flag alone is never treated as proof of TLS.
- `Fly-Client-IP` is trusted only for logging/rate-limiting, and only in the
  Fly topology. The login throttle uses a **global bucket** keyed on nothing
  spoofable, so forged client-IP headers cannot bypass it (tested).
- On a VM: Caddy on the same host, app on loopback.
- `/healthz` is the only unauthenticated path and returns `{"ok":true}` —
  no version, no stack.

## Auth & requests

- One owner account; password hashed with `hashlib.scrypt`
  (N=2**14, r=8, p=1, per-hash salt) via `python -m pa create-owner`.
- Session ids are random 256-bit, stored hashed. **Rotated on login**,
  idle-expired, and HttpOnly always.
- Production cookie: `pa_session`, `HttpOnly; Secure; SameSite=Strict`,
  host-only (no `Domain=`). Development on loopback HTTP uses a **different
  name** (`pa_session_dev`) without `Secure`; production never accepts the
  dev cookie (tested both directions).
- Login throttled after repeated failures (429 with a plain message).
- Every POST requires a per-session CSRF token **and** a matching `Origin`
  (or `Referer` fallback); missing/foreign → 403.
- Request bounds: body size, header size, JSON depth; headers built, never
  concatenated from user input.
- Security headers on every response: CSP, `nosniff`, `DENY`, `Referrer-
  Policy: no-referrer`.

## Egress for `fetch_url`

Only http/https on ports 80/443. Resolution happens **once**; *every*
returned address is checked and the request is refused if any is loopback,
private, link-local, CGNAT (100.64/10), multicast, reserved, IPv4-mapped
IPv6, NAT64, ULA, or a metadata address (169.254.169.254). The socket then
connects **to the vetted IP itself** while the hostname is kept for TLS
SNI, certificate verification, and the `Host` header — no second DNS lookup
can happen (DNS-rebinding tested). ≤3 redirects, each re-vetted. Bounds:
5 MB total *including after decompression*, 10 s, `text/html`/`text/plain`
only. No cookies, no `Authorization`, no credentials. Gzip bombs, oversized
bodies and slow-loris fixtures are all covered by tests.

## Separate provider/integration client

`pa.netclient` is a second client used only for: the owner-configured
`LLM_BASE_URL` endpoint, `api.tavily.com`, the Discord and Gmail API
bases, and (dev only) `PA_OLLAMA_BASE_URL`. The provider key is attached
only to the configured LLM host — redirects are never followed, so there
is no path that leaks it elsewhere. `https` public or `http`
loopback/private endpoint URLs only; `http://public` is rejected at
config load. The general web tool can never reach provider/search hosts;
the dev Ollama URL is refused in `PA_ENV=production`.

## pal's computer (`browse_*`, `read_url`, `/desktop`)

All off unless `PA_COMPUTER_ENABLED=true`. Browser navigation uses the
same egress host/port policy as `fetch_url` — public http(s) on
80/443, every resolved address vetted, plus a post-navigation re-check
that walks the browser back to about:blank if a redirect lands on a
private or metadata address. `read_url`'s Jina path hits one fixed
host (`r.jina.ai`) through the pinned `netclient` with a hard body cap.
Remote mode is a shared-secret bearer token over the tailnet
(`PA_COMPUTER_URL` + `PA_COMPUTER_TOKEN`; `http://` accepted only to
loopback/private/100.64/10 — CGNAT gets an explicit check since
Python's `ipaddress` calls it neither private nor global). The
computer-agent refuses any bind outside loopback/`100.64.0.0/10`;
`/desktop`'s noVNC port binds the tailnet IPv4 only and **fails closed**
when Tailscale is down — no quick tunnel, no Funnel, no public bind.
Its VNC password is a per-session token DMed to the owner; expiry
(PA_DESKTOP_MINUTES, ≤120) or `/desktop off` kills the whole process
tree and the token with it; the profile dir stays chmod 700 under a
dedicated `pa-browser` user that cannot read pal's pal's `.env`.
Form-submit/login/checkout/identity-shaped elements, uploads,
oversized downloads, and every write-like browser action in unattended
jobs go through the existing Discord approval — no second gate, no
auto-continue. Password/OTP/payment fields are refused outright: pal
never sees or types the owner's secrets — `/desktop` is how logins and
2FA get done, by the owner. Docs: `docs/COMPUTER.md`.

## Credential vault (`/credentials`)

Saved logins are AES-GCM-sealed per domain (`PA_SECRETS_KEY`); listing
shows domain/label only. `browse_fill_secret` is the only read path —
it suffix-matches the live tab host (so `evil-example.com` never gets
`example.com`'s login) and types through the computer service.

With `PA_SECRET_CAPTURE` (default **on** — a safety fix; `false`
restores the base-branch text-args path):

- `/credentials add` (no args) opens a private Discord **modal** —
  values arrive in an interaction submit, never as a channel message.
  `/credentials form` shows a **one-time, ~10-minute HTTPS link**
  (`/secret/<token>`; single use, needs an https `PA_PUBLIC_URL`).
- `/credentials otp` captures a **one-time code** stored under an
  `otp:` slot with a 10-minute expiry; `read_otp`/`fill_secret`
  consume it on first use — it cannot be replayed.
- The old "password as a typed command argument" path is **removed**:
  `credentials add <d> <u> <p>` text/slash-args deletes the message
  best-effort and seals only while a capture is pending, otherwise
  refuses. A pasted `domain user secret` DM while a capture is armed
  is sealed + deleted instead of journaled. Discord often **cannot**
  delete the owner's DM messages — the modal/form are the real fix;
  deletion is best-effort mitigation.
- Secret values never appear in logs, audit rows (domain/label/event
  only), chat transcripts, or replies — tested with a planted secret.

## Secrets

- Only from environment variables / Fly secrets. Never in the database,
  prompts, UI, responses, error pages, or logs.
- `logsetup.ScrubFilter` redacts `gsk_*`, `tvly-*`, `sk-*`, `AIza*`,
  Discord bot-token shapes (`M…`, `Bot …` auth strings), `ya29.*` OAuth
  tokens, bearer headers and private-key material from every log record;
  logs record metadata only, never request bodies.
- A test plants a secret-shaped value and asserts it appears in **no** log
  line, DB dump, response body, or error page.
- Gmail credentials come from env or a chmod-600 token file (looser
  permissions refused); the OAuth access token lives only in memory.
- The Discord bot token is read from env and used only for the gateway +
  REST client; it is never sent to any other host.

## Prompt injection / untrusted content

- Fetched pages, **email bodies** and **vault notes** are all wrapped in
  delimited, labelled untrusted blocks (`<<<UNTRUSTED PAGE/EMAIL/NOTE>>>`);
  the system prompt states they are data, never instructions — and email
  content can never be replied to or sent.
- The *only* state write reachable from a task is `propose_memory`, which
  creates an approval — applying it needs the owner (UI or Discord
  owner-id). External writes are previewed and gated; none are enabled by
  default.
- Injection fixture test: a hostile page instructing memory writes produces
  at most one pending approval and zero writes.
- Deterministic allowlists and approval gates, not claimed "perfect
  detection": we test the policy outcome, not the model's skill.

## Discord boundary

- One allowlisted `DISCORD_OWNER_ID`; everyone else is refused (ephemeral
  interaction reply or silence). With no owner id configured the bot
  refuses *everyone* — fail closed.
- Slash commands/approvals are owner-only; routed channel posts and owner
  DMs are deduplicated durably (`discord_events` / `discord_notified_at`)
  so reconnects and redeliveries never double-act.
- Missing token → the bot simply does not start; a clear log line says so.

## Scheduler & vault

- A firing is one `schedule_runs` row keyed `(schedule_id, fire_key)` —
  restart-safe, crash-safe, no duplicate runs.
- Vault indexing never follows symlinks, never reads outside `VAULT_PATH`,
  skips hidden and secret-looking files, and caps file/note sizes.

## Files

Everything under `DATA_DIR`; paths resolved with `realpath` and opened with
`O_NOFOLLOW`. No tool takes a filesystem path.

## Data at rest

SQLite WAL. `python -m pa export-backup` produces an AES-256-GCM encrypted
file (scrypt-derived key, owner passphrase) for off-site storage. Plain
`backup` is an online backup for same-volume snapshots — **not** disaster
recovery.

## What this does not claim

- The query PII screen is heuristic — it fails closed on hits but cannot be
  perfect.
- SQLite + one worker is a single-machine design (see RUNBOOK for the
  Postgres trigger).
- There is no fallback provider — one pinned endpoint, period.
- Prompt-injection resistance relies on tool allowlists and approval gates;
  no guarantee a hostile email/note can't induce a *read-only* tool call
  (which is all the tools it can reach anyway).

## Action policy floor

`pa/policy.py` classifies every tool call (read/draft_local/write/send/
delete/spend/settings/publish/exec) and decides the verdict:
**writes, sends, deletes, spends, settings and publishes on the owner's
accounts always need an explicit Discord approval** — standing allow
rules and unattended job runs can never cover them (unattended queues a
job_action draft instead). pal's own accounts (`gmail@pal`, its VM, its
nodes) act freely within budgets; a first send to a new third party
from a pal account asks once. `PA_POLICY_ENGINE=off` restores the
legacy per-tool gates for emergencies. A turn that has ingested fenced
untrusted content (email bodies, web pages, MCP payloads) is tainted:
every subsequent non-read action upgrades to an approval whose card
names the source. `/policy <tool> [args]` explains any decision.
