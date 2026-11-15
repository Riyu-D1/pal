# Webhooks — signed inbound endpoints + GitHub CI cards

`POST /hooks/<name>` is public; the HMAC-SHA256 signature over the raw
body IS the credential (`X-PA-Signature: sha256=<hex>`; GitHub's
`X-Hub-Signature-256` is also accepted). `/hooks` lists names +
enabled/last-seen only — the secret is shown **once** at creation.

## Generic hooks (docs/PARITY.md #9)

`/hooks add <name> [prompt]` creates an endpoint. A valid POST
enqueues the fenced payload (`source='webhook:<name>'`) → an
unattended agent turn that cannot obey instructions inside the fence.
`/hooks <name> off|on`, `/hooks del <name>`.

## GitHub CI/PR cards

An endpoint **named `github` or `ci`** is special: `workflow_run`,
`check_suite`, `pull_request`, and `issue_comment` (PR threads only)
events render as a short owner-DM **card** (repo, name, conclusion,
actor, short SHA, URL) — **no agent turn**, so a hostile or weird
payload can never reach the model. Any other body on those hooks is
accepted-and-ignored. Nothing writes back to GitHub.

`PA_GITHUB_HOOK_AGENT=true` restores the generic untrusted-turn path.

### Setup

`python -m pa github-webhook` (or `/hooks add github`) creates the
endpoint and prints:

- **Payload URL** — `<PA_PUBLIC_URL>/hooks/github`
- **Secret** — shown once; rerunning rotates it

In the repo: **Settings → Webhooks → Add webhook** —
content type `application/json`, the secret above, events: workflow
runs, check suites, pull requests, issue comments.
