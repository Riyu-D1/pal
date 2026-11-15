# freebuff sidecar on a free/always-on box

Runs the **official freebuff CLI** on a machine that isn't your laptop, so the
`freebuff-cli` provider pin keeps working when your home machine is off. This is the
sanctioned path: a real CLI on hardware you control, behind the sidecar's
shared-token HTTP surface — no API spoofing (the direct API is gated by
`free_mode_cli_required`, verified in `docs/FREEBUFF_CONNECTION.md`).

## 1. Pick a box

| option | cost | notes |
|---|---|---|
| Oracle Cloud **Always Free** VM (Ampere ARM, up to 4 OCPU/24 GB) | $0 | best standing option; needs an Oracle account |
| AWS/GCP/Azure free-tier VM | $0–low | free for ~12 months only on most plans |
| Home Raspberry Pi / NAS / old laptop | $0 | residential IP — safest against any IP-based refusal |
| fly.io / Railway | $0-low | free allowances change; sidecar is stateful (login), prefer a VM |

Any Debian/Ubuntu host with outbound HTTPS works. 1 vCPU / 1 GB is plenty —
the CLI is a thin client; the model runs server-side.

> **Datacenter-IP caveat:** the free-mode gate checks that requests come from
> the official *client* — which this is — but the service may additionally
> distrust datacenter IPs (unverified). If a run is refused, the provider
> fails closed with the service's own error. A home box (Pi) is the safest
> bet; a cloud VM is worth trying first since it's the point of this kit.

## 2. Provision (on the box, ~5 min)

```sh
# get the repo (private — use your SSH key or a token)
git clone https://github.com/Riyu-D1/pal.git
cd pal

# installs tmux, python3, node LTS, freebuff CLI; writes ~/.pa-sidecar/env
deploy/freebuff-sidecar/install.sh

# one-time interactive login — prints a URL, you open it in any browser
deploy/freebuff-sidecar/login.sh
```

## 3. Run the sidecar

```sh
deploy/freebuff-sidecar/run-tmux.sh          # simple: tmux, logs to ~/.pa-sidecar/sidecar.log
# — or systemd —
cp deploy/freebuff-sidecar/sidecar.service ~/.config/systemd/user/pa-freebuff-sidecar.service
loginctl enable-linger "$USER"
systemctl --user daemon-reload && systemctl --user enable --now pa-freebuff-sidecar
```

The sidecar binds **loopback only** by design (`PA_FREEBUFF_SIDECAR_HOST=
127.0.0.1`). Never expose it on a public interface — the token header is the
only auth.

## 4. Reach it from the PA host

`PA_FREEBUFF_REMOTE_URL` refuses `http` to public IPs (API-key rule). Use one of:

```sh
# a) SSH tunnel (simplest, recommended)
ssh -N -L 8777:127.0.0.1:8777 you@box &
#    then on the PA host:
PA_FREEBUFF_REMOTE_URL=http://127.0.0.1:8777

# b) tailnet (tailscale on both ends) — private IP is allowed over http
PA_FREEBUFF_REMOTE_URL=http://100.x.y.z:8777

# c) https terminator (Caddy/nginx + cert) if you really want it public
PA_FREEBUFF_REMOTE_URL=https://sidecar.example.com
```

Plus, on the PA host:

```sh
PA_FREEBUFF_ENABLED=true
PA_FREEBUFF_REMOTE_TOKEN=<same value as ~/.pa-sidecar/env on the box>
```

Verify:

```sh
curl -s -H "x-pa-freebuff-token: $PA_FREEBUFF_REMOTE_TOKEN" \
  http://127.0.0.1:8777/healthz        # {"ok": true, "cli": "x.y.z"}
```

## How this acts as the fallback

Provider selection is per-assistant pin, **not** automatic failover (the
no-cross-provider-fallback policy is deliberate — a failing primary never
silently reroutes traffic elsewhere). The practical setup:

- **primary** — `LLM_*` → Pollinations anonymous (`docs/PROVIDERS.md`)
- **fallback** — configure the assistant on the `freebuff-cli` pin; with
  `PA_FREEBUFF_REMOTE_URL` set, that pin resolves to the remote sidecar
  instead of local tmux. Same code path, same adapter, same account.

## Failure modes (all fail closed)

| symptom | meaning |
|---|---|
| `provider_auth` on every call | token mismatch between PA host and box env |
| `/healthz` ok, `/run` fails "not logged in" | `login.sh` never completed — rerun it |
| run refused by the service | datacenter-IP or account-state refusal — surfaced verbatim, not worked around |
| sidecar down | tunnel/SSH died — restart `run-tmux.sh`; nothing retries silently |

## What CI verifies

`.github/workflows/ci.yml` → `sidecar-deploy` job runs `install.sh` twice
(idempotency) on a clean ubuntu-latest, then boots the sidecar and asserts
`/healthz` auth (401 wrong token / 200 right token, CLI version reported).
The one step CI can't do is the owner-interactive `freebuff login` — by
design; nothing here bypasses it.
