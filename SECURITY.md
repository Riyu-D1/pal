# Security policy

pal is a single-owner, self-hosted service — it intentionally holds your
credentials, email, calendar, and notes, and it approval-gates every write
on your accounts. The full threat model, approval floor, untrusted-content
fencing, secret-handling, and bind-safety rules live in
[`docs/SECURITY.md`](docs/SECURITY.md).

## Reporting a vulnerability

Please **do not open a public issue** for security problems. Report
privately via GitHub's "Report a vulnerability" flow (Security tab →
Advisories) or by contacting the maintainer directly. Include a
reproduction path and which trust boundary it crosses (Discord input,
fetched web content, email content, the web session, secrets at rest).

## Running your own instance safely

- Never commit `.env`, `data/`, backups, tokens, or keys — the repo
  gitignores them and `scripts/check_no_personal_data.py` scans for
  accidental leaks before pushes.
- Keep `PA_BIND=127.0.0.1` behind Caddy/Cloudflare Tunnel; `config.py`
  refuses other unsafe binds, don't override it.
- Owner-account writes always require an approval click — that floor is
  load-bearing; treat any change that weakens it as a security bug.
- Rotate `DISCORD_BOT_TOKEN` and `PA_SECRETS_KEY` if a `.env` or data dir
  ever leaves your machine.
