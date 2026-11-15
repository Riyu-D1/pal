# Connections library

`/connect <app>` in Discord gives you a sign-in link; you log in once in
the browser and the chat agent can use that account's tools from then
on — fully self-hosted.

Kinds of connection:

| kind | what | examples |
|---|---|---|
| `mcp` | official remote MCP server, OAuth 2.1 (PKCE + discovery + dynamic client registration where offered) | notion, linear, atlassian, github, any `url:<https://...>` |
| `mcp` (no auth) | public MCP server — connects instantly, no sign-in | deepwiki, cloudflare-docs, context7 |
| `google` | your own Google OAuth client for Gmail + Calendar (replaces `pa gmail-auth`) | `/connect google` |
| `composio` | managed auth through Composio when `COMPOSIO_API_KEY` is set and the app isn't in the catalog | whatever toolkit slug you ask for |

## Commands (owner only, ephemeral replies)

```
/connect <app>                    # sign-in link (valid ~10 min, single use)
/connect <app> account:<label>    # a second, labelled connection
/connect url:https://mcp.example.com/mcp   # any remote MCP server
/connections                      # list: provider, kind, account, status
/disconnect <app>                 # deletes the sealed token row (and
                                  # revokes when the server advertised
                                  # a revocation endpoint)
```

`/connect` and `/disconnect` autocomplete app names. Replies are
ephemeral — only you see them. Non-owners get nothing (autocomplete
returns an empty list; commands get "Not authorized").

## Any app, not just the catalog

When `COMPOSIO_API_KEY` is set, the `/connect` app box searches
Composio's full toolkit catalog **live** (`GET /toolkits?search=…`,
cached for 5 minutes — your keystrokes don't each hit the API) and
merges the results with the built-in catalog above. Pick any of the
~1000 Composio apps, sign in at the link, and its tools are available
in chat as `<app>.<tool>`. `url:https://…` still connects a raw remote
MCP server directly.

## Multiple accounts per app

Add `account:<label>` (letters/digits/`-`/`_`) to connect a second
account to the same app — pal's own Gmail is the example:

```
/connect gmail                # the owner's mailbox (default account)
/connect gmail account:pal    # pal's own mailbox
```

The label becomes part of the provider key (`gmail@pal`) and of its
tools (`gmail@pal.GMAIL_SEND_EMAIL`), so the model picks the right
account explicitly; the system prompt spells out which is which.
Rows from before this feature read as the default `owner` account —
nothing to migrate by hand. `url:` connections can't carry a label.

`email_owner(subject, body)` is the matching chat tool: it sends from
pal's mailbox (`gmail@pal` on Composio, or a native `google@pal`
grant) to `PA_OWNER_EMAIL`. It is the only send that needs no
approval — the recipient is fixed by env, so it can never mail anyone
else. Every other send/delete/modify still waits on Approve/Reject.

| var | notes |
|---|---|
| `PA_OWNER_EMAIL` | required for `email_owner`; the fixed recipient |

## Required env

| var | notes |
|---|---|
| `PA_SECRETS_KEY` | 32-byte key, encrypts every token blob. Generate: `python3 -c "import secrets; print(secrets.token_hex(32))"`. Stored ONLY in env — losing it means reconnecting every app. |
| `PA_PUBLIC_URL` | public https base of the web server, e.g. `https://pa.yourdomain.example`. Callback path is `<PA_PUBLIC_URL>/oauth/callback`. |
| `PA_GOOGLE_CLIENT_ID` / `PA_GOOGLE_CLIENT_SECRET` | your Google OAuth client (falls back to the existing `GMAIL_CLIENT_ID`/`GMAIL_CLIENT_SECRET` pair) |
| `PA_GITHUB_CLIENT_ID` / `PA_GITHUB_CLIENT_SECRET` | GitHub OAuth App — GitHub's auth server has no dynamic registration, so you register one by hand |
| `COMPOSIO_API_KEY` | optional; enables the Composio fallback catalog |
| `PA_MCP_TIMEOUT` | optional; per-request seconds for MCP/OAuth calls (default 20) |

`pa check-config` prints which of these are set (never values).

## Exposing the callback: Cloudflare Tunnel (free)

The web app binds loopback; the OAuth provider must be able to reach
`/oauth/callback` on a public https URL. Two `cloudflared` options:

**Quick tunnel** (no account, URL changes each run):

```sh
brew install cloudflared          # or: https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/
cloudflared tunnel --url http://127.0.0.1:8080
# prints e.g. https://abc-def-ghi.trycloudflare.com
export PA_PUBLIC_URL=https://abc-def-ghi.trycloudflare.com
```

**Named tunnel** (stable URL, needs a free Cloudflare account + a
domain on Cloudflare DNS):

```sh
cloudflared tunnel login
cloudflared tunnel create pa
cloudflared tunnel route dns pa pa.yourdomain.example
# ~/.cloudflared/<id>.json + this config.yml:
#   tunnel: <id>
#   credentials-file: /home/you/.cloudflared/<id>.json
#   ingress:
#     - hostname: pa.yourdomain.example
#       service: http://127.0.0.1:8080
#     - service: http_status:404
cloudflared tunnel run pa        # or: cloudflared service install
export PA_PUBLIC_URL=https://pa.yourdomain.example
```

The callback route is the only unauthenticated page besides `/login`
and `/healthz`; its only secret is the single-use `state` value.

## The catalog (data, not code)

Verified against each vendor's current docs on 2026-11-10 (see
`pa/connectflows.py:MCP_CATALOG` — one line each):

| app | endpoint | auth |
|---|---|---|
| notion | `https://mcp.notion.com/mcp` | OAuth + DCR |
| linear | `https://mcp.linear.app/mcp` | OAuth + DCR |
| atlassian | `https://mcp.atlassian.com/v2/mcp` | OAuth + DCR |
| github | `https://api.githubcopilot.com/mcp/` | OAuth, manual app (`PA_GITHUB_*`) |
| deepwiki | `https://mcp.deepwiki.com/mcp` | none — public |
| cloudflare-docs | `https://docs.mcp.cloudflare.com/mcp` | none — public |
| context7 | `https://mcp.context7.com/mcp` | none — public |

To add a server: add one dict line to `MCP_CATALOG`, or just
`/connect url:<endpoint>` without touching the catalog at all.

## How the OAuth flow works

1. `/connect notion` → pa fetches the server's protected-resource
   metadata (`/.well-known/oauth-protected-resource`), then the
   authorization-server metadata (`/.well-known/oauth-authorization-server`,
   `openid-configuration` fallback), registers a client via the
   `registration_endpoint` when offered (cached per issuer+redirect in
   `oauth_clients`), mints a PKCE pair and a single-use `state` row
   (10-minute expiry — the row is deleted when consumed).
2. You open the link, consent at the provider, and get redirected to
   `<PA_PUBLIC_URL>/oauth/callback?code=…&state=…`.
3. The callback exchanges the code (+ PKCE verifier + `resource`
   parameter), seals `{access_token, refresh_token}` with AES-256-GCM
   under `PA_SECRETS_KEY`, marks the connection `active`, and pal DMs
   you a confirmation. The tab shows a "you can close this" page.
4. Token refresh happens lazily inside `access_token()` — rotated
   refresh tokens are re-sealed; a dead refresh marks the row
   `expired` and chat tools tell you to reconnect.

Nothing token-shaped is ever logged, printed, or stored in plaintext.

## How tools reach the chat agent

Connected apps appear in the system prompt as names only — labelled
accounts as their qualified name (`gmail` vs `gmail@pal`), with a
line explaining which account is which. The agent calls
`app_tools {"app": "notion"}` to fetch that app's tool list
(schemas + `readOnlyHint`), then calls them as `notion.<tool>` (or
`gmail@pal.<tool>` for a labelled account — longest provider prefix
wins). `readOnlyHint: true` tools run directly; everything else
creates a `chat_action` approval and waits on your Approve/Reject
click — same gate as `send_email`. Tool output is capped (~8 KB) so
free-tier model context isn't flooded.

## Google click-by-click

`/connect google` uses *your* Google OAuth client — nothing is shared
with Composio or anyone else. One-time setup (~5 min):

1. <https://console.cloud.google.com> → select/create a project.
2. **APIs & Services → Library**: enable **Gmail API** and
   **Google Calendar API**.
3. **APIs & Services → OAuth consent screen**: user type *External* →
   app name e.g. `pal` → your email as support + developer contact.
4. **Audience → Test users**: add your own Gmail address.
5. **APIs & Services → Credentials → Create Credentials → OAuth client
   ID** → application type **Web application**.
6. Under *Authorized redirect URIs* add
   `<PA_PUBLIC_URL>/oauth/callback`
   (e.g. `https://pa.yourdomain.example/oauth/callback`).
7. Copy the client id/secret into `PA_GOOGLE_CLIENT_ID` /
   `PA_GOOGLE_CLIENT_SECRET` (or reuse `GMAIL_CLIENT_ID`/
   `GMAIL_CLIENT_SECRET` — the `/connect` path reads either name).
8. `/connect google` → consent for `gmail.readonly`, `gmail.compose`,
   `gmail.send`, `calendar.events`.

**Testing vs Published**: while the consent screen stays in *Testing*,
refresh tokens die after **7 days** — pal will report the google
connection as `expired` and you just `/connect google` again. Publish
the app (no verification needed for unlisted scopes like these — it
stays "unverified" but refresh tokens stop expiring) to make it stick.

After connecting, `gmail.credentials()` reads the sealed grant, so the
existing `email_*`/`calendar_*` tools — including `pa gmail-auth`-style
tokens in env — keep working with no duplicated client code.

## Composio fallback

Set `COMPOSIO_API_KEY` and any `/connect <app>` that isn't in the
self-hosted catalog goes through Composio's managed auth: a tool-router
session produces a connect link, pal polls the connected account for
~5 min and DMs you when it activates. Tools then run through Composio's
`POST /tools/execute/{slug}`.

The fallback also covers catalog apps whose own OAuth app isn't set
up: `/connect github` without `PA_GITHUB_*`, and `/connect google`
without `PA_GOOGLE_*`/`GMAIL_*`, route to Composio instead of failing.
Google is split into per-product toolkits there — `/connect google`
links the `gmail` toolkit and tells you to run
`/connect googlecalendar` for Calendar (its tools show up as
`gmail.<tool>`/`googlecalendar.<tool>`). When no native Google OAuth
grant exists but the `gmail`/`googlecalendar` Composio connection is
active, the built-in `email_*`/`calendar_*` chat tools run through it
automatically. Without `COMPOSIO_API_KEY` both still ask for the env
vars as before.

**Free limits (Platform pricing — the plan the API key uses, not the
consumer plan)**: the Hobby tier at time of writing allows 100k tool
calls/month total, of which **20k may run on Composio-managed apps**
(connect-link sessions are managed), 50k triggers, 3 seats, 2k req/min.
pa keeps a local monthly counter (`composio_usage`); at 80% of the
managed-app cap it warns you in Discord once per month, and the count
appears in each Composio tool result. Composio tools whose slug verb
is a pure read (FETCH/GET/LIST/SEARCH/FIND/…) — or that Composio tags
read-only — run without approval; send/create/delete/modify and
unknown verbs still go through Approve/Reject.
`PA_COMPOSIO_MONTHLY_LIMIT` overrides the assumed cap if Composio's
pricing changes.

## Failure modes you'll see

- `PA_SECRETS_KEY is not set` — set it, then `/connect` again.
- `PA_PUBLIC_URL is not set` — expose the web server (cloudflared
  above), set it, retry.
- "link expired or already used" on the callback page — states die
  after 10 min or first use; `/connect` again. (A late Composio
  redirect after pal's poller already activated the app shows
  "Connected" instead.)
- `expired` status in `/connections` — the refresh token died
  (revoked, or Google Testing-mode 7-day limit); `/connect` again.
- Composio usage warning — you're at 80% of the managed-app free cap;
  either wait for the month to roll over or disconnect the app.
