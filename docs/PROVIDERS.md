# Providers

## Policy

- One owner-configured OpenAI-compatible chat endpoint per deployment:
  `LLM_BASE_URL` + `LLM_MODEL` (+ `LLM_API_KEY` unless the endpoint is
  genuinely anonymous, e.g. a local server).
- The endpoint is **pinned server-side**. It can only be set through the
  environment — never by a user, an assistant, a task, the model, or a
  fetched page. Each assistant is pinned to one `(host, model)` pair from
  the allowlist derived from that config.
- `https` is allowed to any public host. Plain `http` is allowed **only**
  to loopback/private owner-chosen endpoints — `http://public.host` is
  rejected at config load.
- The API key is attached only to requests to the configured base URL.
  The client never follows redirects and there is no code path that sends
  the key to any other host.
- **No cross-provider fallback.** A key being present in the environment
  never enables a provider; there is no automatic or paid fallback.
- **Serial tool calls only** — the agent loop drives one call per turn.
- URL hardening: credentials-in-URL, non-http(s) schemes, malformed URLs
  are all rejected at config load.

## Pacing and limits (tune to your endpoint)

Quota pacing is configurable because limits depend on the endpoint:

| var | meaning |
|---|---|
| `PA_MAX_INPUT_TOKENS` | per-call estimated input cap (default 5500) |
| `PA_MAX_COMPLETION_TOKENS` | per-call completion cap (default 1024) |
| `PA_TPM_LIMIT` | tokens/minute bucket (default 8000) |
| `PA_RPM_LIMIT` | requests/minute bucket (default 30) |
| `PA_DAILY_REQUEST_LIMIT` | requests/day ceiling (default 1000) |
| `PA_DAILY_TOKEN_LIMIT` | tokens/day ceiling (default 200000) |
| `PA_RATE_MAX_RETRIES` | bounded 429 retries (default 3) |

The pacer is **token-aware**: shared per-minute and per-day buckets; a
request that would exceed a bucket waits rather than firing (bounded by
the task deadline). Response `x-ratelimit-*` headers tighten the local
buckets to the server's truth. Per-day usage is persisted and shown live
in the UI.

## Error taxonomy (no fallback, ever)

| response | outcome |
|---|---|
| 401 / 403 | task fails `provider_auth` — fix the key |
| 429 | bounded retries honouring `Retry-After` / `x-ratelimit-*`, only before any dependent tool action; exhaustion → `quota_exceeded` **with the reset time** |
| timeout / 5xx | bounded retry, then failed |
| endpoint not on allowlist | refused before request |

Retries never re-run a completed tool: the ledger only ever retries the
*pending* model call. Cancellation is checked between steps.

## Endpoint toggles

- `LLM_STREAMING=true` — consume SSE and fold deltas (off by default;
  useful for endpoints that time out on long non-streamed generations).
- `LLM_TOOLS=false` — never send tool schemas; the agent asks for a plain
  answer (for endpoints without tool-calling support).
- `LLM_COMPLETIONS_PATH` — override the request route appended to
  `LLM_BASE_URL` (default `/chat/completions`). Must start with `/`; the
  empty string posts directly to `LLM_BASE_URL` as configured — required
  for endpoints like Pollinations where the OpenAI-compatible surface is
  the base path itself.

## Verified free/anonymous endpoints

These were live-probed end-to-end through `provider_for()` (assistant `PONG` round trip, usage reported):

| endpoint | config | key? | notes |
|---|---|---|---|
| **Pollinations** `text.pollinations.ai/openai` | `LLM_BASE_URL=https://text.pollinations.ai/openai`, `LLM_MODEL=gpt-oss-20b`, `LLM_COMPLETIONS_PATH=` (empty) | none — anonymous tier | works from any cloud box; no account; subject to anonymous rate limits, no uptime guarantee; `/chat/completions` suffix must be blanked (route is the base path) |
| local Ollama / any OpenAI-compatible | `LLM_BASE_URL`, `LLM_MODEL` | per endpoint | standard path, no special casing |

GitHub Models was probed and dropped: the `gh` OAuth token gets only a
stub response — it needs a real PAT (owner action).

## `pa provider-check` — conformance test

Once the owner has configured `LLM_BASE_URL`/`LLM_MODEL`, run:

```sh
python -m pa provider-check
```

Checks, each reported pass/fail with a detail line:

1. **configured** — settings resolve to one allowlisted provider entry.
2. **models** — `GET /models` if exposed (optional; endpoints without it
   are reported, not failed) and whether `LLM_MODEL` is listed.
3. **chat** — one plain text completion round trip.
4. **tool_call** — one controlled read-only round trip: the endpoint must
   emit a call to a test `echo_check` tool, receive the result, and
   produce a final answer. Skipped (pass, noted) when `LLM_TOOLS=false`.

Mock-tested in CI.

## Ollama (development/tests only)

`PA_OLLAMA_BASE_URL` points the provider client at a local OpenAI-
compatible endpoint for development and tests. It is refused when
`PA_ENV=production` (tested) and unreachable from `fetch_url` (loopback is
blocked by egress). No live result may be recorded against it.

## Search — Tavily (opt-in)

Tavily is used only to find URLs; pages are fetched by our own guarded
fetcher. Enabled only when **both** `SEARCH_ENABLED=true` and
`TAVILY_API_KEY` are set; otherwise `web_search` returns `search_disabled`
with no HTTP call. Queries are screened against memories/owner
facts/secrets, and hits (or any query on a `private` task) produce a
preview approval or fail closed. The screen is heuristic, not perfect.

## Why not the alternatives

- **Freebuff** — free tier rejects unofficial clients
  (`403 free_mode_cli_required`), a proxy-user suspension was publicly
  reported, and ToS effective 12 Nov 2026 forbids wrappers/scripts/agent
  use. The only integration is an opt-in **agent-session adapter that
  drives the owner's own official `freebuff` CLI through its normal
  terminal interface** — no proxy, no impersonation, no private APIs;
  see `docs/FREEBUFF_CONNECTION.md`. Off by default
  (`PA_FREEBUFF_ENABLED`).
- **OpenRouter** — privacy varies by upstream; paid-fallback billing risk.
- Anything else the owner picks must simply be an OpenAI-compatible
  endpoint you are willing to pay for/host — set `LLM_*` and run
  `pa provider-check`.
