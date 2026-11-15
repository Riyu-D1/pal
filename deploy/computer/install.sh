#!/usr/bin/env bash
# pal's computer — host installer (docs/COMPUTER.md)
#
# Sets up the computer-agent on an Always Free Ampere A1 (aarch64) box
# or the same-VM fallback (x86_64). Idempotent: safe to re-run.
#
#   sudo bash deploy/computer/install.sh [memory-cap]
#
#   memory-cap   systemd MemoryMax for the agent unit.
#                default: 400M when the host has <2 GiB RAM, else 3G.
#
# What it does:
#   1. apt: xvfb openbox x11vnc novnc websockify + python venv + curl
#      (Chromium comes from `playwright install` — Ubuntu's apt
#      chromium is a snap and doesn't run cleanly on a headless VM;
#      Playwright ships real aarch64 + x86_64 builds.)
#   2. Tailscale from the official repo (does NOT `tailscale up` —
#      that's interactive; instructions print at the end).
#   3. A dedicated `pa-browser` user (nologin; cannot read pal's
#      .env — unix 0700 does that, the unit adds ProtectHome).
#   4. The repo at /opt/pa-computer + a venv with
#      requirements-computer.txt + `playwright install chromium`.
#   5. ~/.pa-computer/env with a generated PA_COMPUTER_TOKEN (kept on
#      re-runs) and the systemd user units — disabled until you
#      enable them yourself.
set -euo pipefail

REPO_ROOT="$(git -C "$(dirname "$0")" rev-parse --show-toplevel \
             2>/dev/null || pwd)"
BROWSER_USER="pa-browser"
INSTALL_DIR="/opt/pa-computer"
MEM_CAP="${1:-}"

if [ "$(id -u)" -ne 0 ]; then
  echo "run with sudo: sudo bash deploy/computer/install.sh" >&2
  exit 1
fi

case "$(uname -m)" in
  aarch64|x86_64) : ;;                      # both supported
  *) echo "unsupported arch $(uname -m)" >&2; exit 1 ;;
esac

if [ -z "$MEM_CAP" ]; then
  total_kb="$(awk '/MemTotal/ {print $2}' /proc/meminfo)"
  if [ "$total_kb" -lt 2000000 ]; then MEM_CAP="400M"; else MEM_CAP="3G"; fi
fi
echo "==> arch=$(uname -m) memory-cap=$MEM_CAP repo=$REPO_ROOT"

echo "==> apt packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq \
  xvfb openbox x11vnc novnc websockify \
  python3 python3-venv python3-pip curl ca-certificates git

echo "==> tailscale (official install script; 'tailscale up' is yours)"
if ! command -v tailscale >/dev/null 2>&1; then
  curl -fsSL https://tailscale.com/install.sh | sh
fi

echo "==> pa-browser user"
if ! id "$BROWSER_USER" >/dev/null 2>&1; then
  useradd --system --create-home --shell /usr/sbin/nologin "$BROWSER_USER"
fi
loginctl enable-linger "$BROWSER_USER" >/dev/null 2>&1 || true
BHOME="$(getent passwd "$BROWSER_USER" | cut -d: -f6)"

echo "==> repo -> $INSTALL_DIR"
mkdir -p "$INSTALL_DIR"
tar -C "$REPO_ROOT" -cf - \
    --exclude .git --exclude __pycache__ --exclude '.venv*' . \
  | tar -C "$INSTALL_DIR" -xf -
chown -R "$BROWSER_USER:$BROWSER_USER" "$INSTALL_DIR"

echo "==> venv + playwright chromium (runs as $BROWSER_USER)"
sudo -u "$BROWSER_USER" bash -lc "
  set -euo pipefail
  cd '$INSTALL_DIR'
  python3 -m venv .venv
  .venv/bin/pip -q install -r requirements-computer.txt
  .venv/bin/playwright install chromium
"

echo "==> ~/.pa-computer/env"
sudo -u "$BROWSER_USER" mkdir -p "$BHOME/.pa-computer" "$BHOME/.config/systemd/user"
ENVFILE="$BHOME/.pa-computer/env"
# `tailscale ip -4` when logged out can hang and spawn `tailscale up`.
# Only accept a real tailnet IPv4; otherwise bind loopback.
TS_IP=""
if command -v tailscale >/dev/null 2>&1; then
  if ts_ip="$(timeout 3 tailscale ip -4 2>/dev/null | head -1)"; then
    case "$ts_ip" in
      100.*) TS_IP="$ts_ip" ;;
    esac
  fi
fi
if [ ! -f "$ENVFILE" ]; then
  TOKEN="$(head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')"
  sudo -u "$BROWSER_USER" bash -c "cat > '$ENVFILE'" <<ENVEOF
# pal computer-agent environment — chmod 600, never commit.
PA_ENV=production
PA_DATA_DIR=$BHOME/.pa-computer/data
PA_COMPUTER_ENABLED=true
PA_COMPUTER_TOKEN=$TOKEN
# bind: 127.0.0.1 until `tailscale up`; then the tailnet IPv4 (below)
PA_COMPUTER_AGENT_BIND=${TS_IP:-127.0.0.1}
PA_COMPUTER_AGENT_PORT=8788
PA_DESKTOP_PORT=6080
ENVEOF
  sudo -u "$BROWSER_USER" chmod 600 "$ENVFILE"
  echo "    generated PA_COMPUTER_TOKEN (see $ENVFILE)"
else
  echo "    env exists — keeping it"
fi

echo "==> systemd user units (installed DISABLED)"
for unit in pa-computer-agent.service pa-desktop.service; do
  sed -e "s|__MEMORY_MAX__|$MEM_CAP|g" \
      -e "s|__INSTALL_DIR__|$INSTALL_DIR|g" \
      "$REPO_ROOT/deploy/computer/$unit" \
      | sudo -u "$BROWSER_USER" tee "$BHOME/.config/systemd/user/$unit" \
        >/dev/null
done
sudo -u "$BROWSER_USER" XDG_RUNTIME_DIR="/run/user/$(id -u "$BROWSER_USER")" \
  systemctl --user daemon-reload 2>/dev/null || true

cat <<DONE

Done. Next steps (docs/COMPUTER.md has the full walkthrough):

  1. tailscale up                      # join the tailnet, then re-run
                                       #   this script or edit the bind
  2. tailscale ip -4                   # this box's 100.x address
  3. sudo -u $BROWSER_USER -i \\
       XDG_RUNTIME_DIR=/run/user/\$(id -u $BROWSER_USER) \\
       systemctl --user enable --now pa-computer-agent
  4. On the PAL host, set in pal's .env:
       PA_COMPUTER_ENABLED=true
       PA_COMPUTER_URL=http://<tailnet-ip>:8788
       PA_COMPUTER_TOKEN=<same token as $ENVFILE>
     then: systemctl --user restart pa

Same-host fallback (no second VM): run this script on the pal VM with
the 400M cap — already picked when RAM <2 GiB — and use
PA_COMPUTER_URL=http://127.0.0.1:8788 on pal.
DONE
