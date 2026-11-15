# Phone node — Motorola Moto E5 (Termux)

pal's phone hand: a **stdlib-only** Python agent (`pa/node_agent.py`)
running under Termux on the Moto E5, reached over a reverse SSH
tunnel (see `deploy/pa-node/VM-TUNNEL.md`). One bearer token, a fixed
capability allowlist — no shell, no inbound port on the phone.

## Hardware reality

Moto E5 (2018): Snapdragon 425, 2 GB RAM, Android 8.0, 32-bit armv7.
Termux (F-Droid) + Termux:API + Termux:Boot.

**It can:** run the agent 24/7, Termux:API calls (notify, TTS, SMS,
camera, location, battery, sensors, vibrate), `fetch`/`ping_http`,
pal-made tools (`run_tool`), file storage.

**It cannot:** Chromium/Playwright, local TTS/STT/LLM, heavy pip
packages, anything needing root or Accessibility automation, fast
reboots — Android 8 under load means jobs can sit queued.

## Capabilities → chat tools

| node cap         | chat tool        | gate                                        |
|------------------|------------------|---------------------------------------------|
| `notify`         | `phone_notify`   | pal→owner channel — free                    |
| `tts_speak`      | `phone_say`      | pal→owner channel — free                    |
| `sms_list`       | `phone_sms_read` | read; job turns need PA_NODE_SMS_READ_UNATTENDED or an approval |
| `sms_send`       | `phone_sms_send` | send on the owner's SIM — approve **every** time |
| `camera_photo`   | `phone_photo`    | sensitive — approve unless an allow rule    |
| `location`       | `phone_locate`   | sensitive — approve unless an allow rule    |
| `battery`/health | `phone_status`   | read — free                                 |

SMS bodies and anything else the phone reports are fenced
`<<<UNTRUSTED>>>` — SMS is a classic injection vector.

## Setup

1. Install Termux + Termux:API + Termux:Boot from F-Droid.
2. `sh termux-setup.sh <PAL_VM_HOST>` inside Termux
   (`deploy/pa-node/termux-setup.sh`) — installs deps, reads the node
   token on stdin (0600, never echoed), writes the boot script.
3. `ssh-keygen -t ed25519 -f ~/.pa-node/id_ed25519 -N ''`; put the
   pubkey in the VM's `pa-tunnel` user (`deploy/pa-node/VM-TUNNEL.md`).
4. Boot script starts `node_agent.py` + `autossh -R
   127.0.0.1:8791:127.0.0.1:8790`.
5. On the VM: `PA_NODES=phone=http://127.0.0.1:8791`,
   `PA_NODE_PHONE_TOKEN=<token>`.

## Android 8 keep-alive

- Disable battery optimisation for **Termux** AND **Termux:API**.
- Termux:Boot runs `~/.termux/boot/pa-node.sh` on boot.
- The agent holds a wake-lock (`termux-wake-lock`); keep "Wi-Fi on
  during sleep" enabled.
- The agent self-throttles below 25 % battery or above 42 °C — jobs
  queue until it recovers.
- Keep it plugged in and cool; check the battery monthly for swelling
  (2018 handset).

## Security model

Android 8 is years past patches — **the phone is an untrusted-adjacent
node**. On it live ONLY the node token and the tunnel key. pal's env
never gets copied down. All pal→phone traffic is the one Bearer token
over the SSH tunnel; a stolen token can't mint capabilities beyond the
fixed allowlist, and policy still gates the sensitive ones.
