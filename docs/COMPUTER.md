# pal's computer — persistent browser + `/desktop`

Give pal its own machine: a **persistent-profile Chromium** it drives
for research and logged-in sites, plus a **time-limited remote
desktop** the owner opens to complete logins/2FA once — pal then
reuses the saved sessions. Everything is **off by default** behind
`PA_COMPUTER_ENABLED`, and nothing here touches the existing pal
service until you set the env flags and restart it.

```
pal (existing 1 GB micro)                    computer host (A1 recommended)
  chat turn ── browse_goto ──┐                 ┌── python -m pa computer-agent
  /desktop ──────────────────┼── tailnet ─────►│      :8788, bearer token
  (link DMed to owner)       │   100.64/10     ├── persistent Chromium (0700 profile)
                             │                └── on-demand Xvfb+noVNC :6080
        your laptop/phone ──┴── opens vnc.html?password=<token> in a browser
```

## Recommended host: a second Always Free VM

Oracle Ampere **A1.Flex** is free up to **2 OCPUs + 12 GB total** per
tenancy (1,500 OCPU-h + 9,000 GB-h/month) and must be created in your
tenancy's **home region**.

- Create: Ubuntu 24.04 Minimal, **2 OCPU / 12 GB**, ~50 GB boot
  volume, in any AD with capacity. "Out of host capacity" is common —
  try another AD or retry later.
- **Idle reclamation**: an A1 is reclaimable if, over 7 days, CPU and
  network each stay <20% AND memory <20%. pal's browser is on-demand,
  so a quiet box CAN look empty — optional light heartbeat:
  `systemctl --user enable --now` a timer that hits `curl` on the
  agent's `/healthz` every 10 min, or just run `/desktop` for a
  minute weekly.

### Same-VM fallback

No second host? The same stack runs **on-demand** on the existing
micro under a `pa-browser` user, cgroup-capped at 400M, idle-exiting
after `PA_COMPUTER_IDLE_SECONDS` (300 s). Headless Chromium idles
around 150–300 MiB and real pages can double that — never run it
always-on on 1 GB. The fallback is for occasional use; the browser
starts and stops per task.

## Setup — recommended A1 host

1. **Tailscale on everything.** On the A1 box, the pal host, and
   your own devices:
   `curl -fsSL https://tailscale.com/install.sh | sh && sudo tailscale up`
   (install Tailscale on the pal host too; it
   only needs to *reach* the computer, nothing inbound). Tailscale
   Personal is free: unlimited devices, ≤6 users.
2. **Install on the A1** (as a user with sudo; run from the repo):
   `sudo bash deploy/computer/install.sh`
   It handles aarch64 and x86_64, picks `MemoryMax=3G` on a 12 GB box,
   and prints the generated `PA_COMPUTER_TOKEN` location at the end.
3. `tailscale up` on the A1 (interactive once), note
   `tailscale ip -4` → e.g. `100.64.1.5`. Re-run the installer or set
   `PA_COMPUTER_AGENT_BIND=<that ip>` in `~pa-browser/.pa-computer/env`
   so the agent listens on the tailnet for pal.
4. **Start the agent:**
   `sudo -u pa-browser -i XDG_RUNTIME_DIR=/run/user/$(id -u pa-browser) systemctl --user enable --now pa-computer-agent`
   (`loginctl enable-linger pa-browser` — already done by the
   installer — keeps it alive after logout.)
5. **Point pal at it.** On the pal VM, add to pal's `.env`:
   ```
   PA_COMPUTER_ENABLED=true
   PA_COMPUTER_URL=http://100.64.1.5:8788
   PA_COMPUTER_TOKEN=<same token from step 2>
   ```
   then `systemctl --user restart pa`.
6. **`/desktop`** in Discord → pal DMs you a MagicDNS noVNC link that
   expires (default 30 min, cap 120; `/desktop off` ends early). Log
   into whatever you need — **all passwords and 2FA are typed by you**,
   never by pal. The session's profile is the same one pal's headless
   browser uses, so the login sticks when the desktop closes.
7. **Try it:** "research the docs site for X — use your browser and
   give me the setup steps" — `read_url`/`fetch_url` first, Chromium
   only when a page needs JS or the login.

## Setup — same-VM fallback

On the pal VM: `sudo bash deploy/computer/install.sh` (it picks the
400M cap when RAM <2 GiB), enable the `pa-computer-agent` user unit
for `pa-browser` exactly as in step 4, then on pal set
`PA_COMPUTER_URL=http://127.0.0.1:8788` + the token from
`~pa-browser/.pa-computer/env`. Install Tailscale anyway — `/desktop`
fails closed without it (the browse tools work over loopback without
Tailscale; the owner-facing desktop does not).

## Env vars (all optional, all off by default)

| var | default | meaning |
|-----|---------|---------|
| `PA_COMPUTER_ENABLED` | `false` | master switch for every browse_*/read_url/desktop path |
| `PA_COMPUTER_PROFILE` | `<data>/browser-profile` | persistent profile dir, chmod 700 |
| `PA_COMPUTER_MAX_TABS` | `3` | hard tab cap |
| `PA_COMPUTER_IDLE_SECONDS` | `300` | idle-exit while no desktop is live |
| `PA_COMPUTER_MEMORY_MB` | `600` | self-check recycle; the cgroup is the real cap |
| `PA_COMPUTER_LOCK_WAIT` | `30` | seconds a second driver waits on the one-browser lock |
| `PA_COMPUTER_URL` | unset | when set, pal is a CLIENT of a remote computer-agent; `http://` only to loopback/private/100.64/10 (https anywhere) |
| `PA_COMPUTER_TOKEN` | unset | shared bearer secret (required with the URL) |
| `PA_COMPUTER_AGENT_BIND` | `127.0.0.1` | agent host only: loopback or 100.64/10 — never 0.0.0.0 |
| `PA_COMPUTER_AGENT_PORT` | `8788` | agent port |
| `PA_COMPUTER_DOWNLOAD_MAX` | `20971520` | download cap (20 MB), into the chat sandbox only |
| `PA_COMPUTER_BROWSER_BIN` | unset | chromium path override (else playwright cache / PATH) |
| `PA_DESKTOP_MINUTES` | `30` | /desktop TTL, hard-capped at 120 |
| `PA_DESKTOP_PORT` | `6080` | noVNC port on the tailnet bind |
| `PA_JINA_READER` | on-with-computer | `read_url`'s r.jina.ai fallback |
| `JINA_API_KEY` | unset | raises the keyless ~20 rpm jina cap |

## Security (short version — see docs/SECURITY.md)

- **Tailscale is the security layer.** The agent and the noVNC port
  bind loopback or `100.64.0.0/10` only — never `0.0.0.0`, never the
  public VNIC. `/desktop` **fails closed** when Tailscale is down:
  no Cloudflare quick tunnel, no `tailscale funnel`, no public
  endpoint. (If you later have a domain on Cloudflare you *may* front
  it with a named tunnel + Access — documented, not implemented.)
- The `/desktop` link carries a per-session VNC password and goes to
  your **DMs only**; the session dies at expiry and takes the token
  with it. The profile dir stays (0700, owned by `pa-browser`).
- Risky browser actions pause for the same Discord approval you
  already use: form submits, login/checkout/identity flows, uploads,
  oversized downloads, and **every write-like action in unattended
  jobs**. Reading pages, public navigation and screenshots don't ask.
- pal **never** types passwords, OTP/2FA codes or card numbers — the
  tool refuses outright and tells you to use `/desktop`. Don't use
  this for banking/password entry; you type those.
- Cloud datacentre IPs are often challenged by Google/banks —
  prefer `/connect` (Composio/Google/GitHub) for sites that have an
  API. The browser is for sites that don't. Expect lockouts if you
  push logins through automation; first-time logins are yours via
  `/desktop`.

## Not built

Steel, Browserless, Kasm Workspaces, browser-use as the agent,
SearXNG, Cloudflare Access, Tailscale Funnel, WhatsApp, PSTN calls, a
new web dashboard, image generation. All viable later — none needed
for a single owner.
