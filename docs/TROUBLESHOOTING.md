# Troubleshooting

`python -m pa doctor` diagnoses most of this automatically — run it
first; every FAIL line names the missing env var.

## Discord

**`Command description exceeds 100 characters`** — Discord's hard
limit is 100 chars for slash-command descriptions and options; the
bots' `discordbot.py` registers all commands at startup and the API
rejects the whole payload if any description is longer. If you add a
command, keep `description=` ≤100 chars (the `/credentials` command
was shortened for exactly this). The test
`test_all_command_descriptions_fit_discord_limit` guards it.

**Invalid token / retry loop in logs** — a wrong `DISCORD_BOT_TOKEN`
fails *gracefully*: the bot keeps retrying the gateway while web +
scheduler keep running. Fix the token in `.env`, restart. A missing
`DISCORD_OWNER_ID` fails **closed** — everyone including you is
refused; it must be your numeric user id, not your username.

**Slash commands present but no replies** — enable **Message Content
intent** in the dev portal (Bot tab). Slash commands work without it;
free-form chat doesn't.

## Models / providers

**Groq `rate_limit_exceeded` / quota** — expected on free tier at
heavy load; two options: (a) set `PA_ROUTER_ENABLED=true` with a
second backend key (`GEMINI_API_KEY`, `OPENROUTER_API_KEY`,
`CLOUDFLARE_*`, or nothing needed for Pollinations) and pal falls over
automatically — `docs/ROUTER.md`; (b) lower `PA_CHAT_MAX_STEPS` /
`PA_DAILY_REQUEST_LIMIT` / `PA_DAILY_TOKEN_LIMIT` quotas to pace usage.

**`LLM_BASE_URL` refused** — config.py enforces an allowlist of
public HTTPS providers + loopback/private hosts. A random endpoint
refuses by design; use an allowed host or set up the router.

**No tool calls in chat** — your endpoint doesn't support function
calling: set `LLM_TOOLS=false` (prompted tools still work, worse UX).

## Serve / web

**`curl: (7) Failed to connect`** — check `PA_BIND`/`PA_PORT`, and
remember the default bind is loopback: from another machine you need
the tunnel/compose mapping, not the VM's LAN IP.

**`unsafe bind` error at boot** — config.py refuses non-loopback
binds outside the `.container-bind`/`.fly` markers. That's the safety
rail working; use compose (sets the marker) or keep loopback + tunnel.

**`database is locked`** — only ever seen when a second process has
the data dir open (`serve` + a stray `chat`/test against the same
`PA_DATA_DIR`). One writer per data dir; point the second command at
its own dir or stop the server first.

## macOS-specific

**`ssl.SSLCertVerificationError: CERTIFICATE_VERIFY_FAILED` on every
HTTPS call** — the python.org/Homebrew Python ships without certifi
roots wired up. Fix once:
`pip install certifi` then run with
`SSL_CERT_FILE=$(python -m certifi) python -m pa serve`,
or run the "Install Certificates.command" in your Python install dir.
(Running `python -m pa serve` on macOS hits this for the Discord
gateway especially — the process stays alive and retries, which is
the graceful-failure path doing its job.)

**`osascript` / Terminal permission prompts** — the mac-desktop
features (notifications, AppleScript actions in `mac_node` /
computer-agent kits) need **System Settings → Privacy & Security →
Automation** approval for the terminal app that launched `serve`.
Deny = those calls log `osascript failed` and are skipped; grant =
they work. Nothing else on the Mac path needs it.

## Data / backups

**Restore test fails** — `docs/DR.md` is the runbook; the short
version is the `scheduled-backup` job / `python -m pa export-backup`
write encrypted `.db.enc` snapshots (to `PA_BACKUP_DEST` or
`PA_DATA_DIR/backups/`) and restore (`import-backup`) needs the same
`PA_BACKUP_PASSPHRASE` that encrypted them. Plain `python -m pa backup`
is an unencrypted online SQLite copy.

**`needs config` on an integration you set** — env var name typo is
the #1 cause; cross-check spelling in `docs/CONFIG.md` /
`.env.example` and confirm the var is in the `.env` the service's
working dir reads (or exported / `PA_ENV_FILE` points at it).

## Tests

**`python -m unittest discover -s tests` shows scary tracebacks** —
most are deliberate error-path tests (fake Discord gateway drops,
fake Gmail 500s, division-by-zero fixtures). The suite passes iff the
last line says `OK`. ~1500 tests, a few minutes.

**evals fail after a prompt change** — `python -m pa eval` runs the
offline fixtures in `evals/`; manifest-drift failures mean you changed
behaviour the fixtures pin — update the fixture deliberately, never
to silence.
