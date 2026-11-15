#!/usr/bin/env bash
# install.sh — provision a fresh Debian/Ubuntu box as a freebuff sidecar host.
#
# Idempotent: safe to re-run. Installs tmux + python3 + node + the official
# freebuff CLI, then writes ~/.pa-sidecar/env with a fresh shared token
# (existing env is preserved). Does NOT log you in — run login.sh next.
set -euo pipefail

PA_ENV_DIR="${PA_ENV_DIR:-$HOME/.pa-sidecar}"
ENV_FILE="$PA_ENV_DIR/env"

say() { printf '[install] %s\n' "$*"; }
have() { command -v "$1" >/dev/null 2>&1; }

SUDO=""
if [ "$(id -u)" -ne 0 ]; then
  have sudo || { echo "need root or sudo for apt" >&2; exit 1; }
  SUDO="sudo"
fi

if ! have apt-get; then
  echo "this script targets Debian/Ubuntu (apt-get missing)" >&2
  exit 1
fi

say "apt deps: tmux python3 curl ca-certificates git"
$SUDO apt-get update -qq
$SUDO apt-get install -y -qq tmux python3 curl ca-certificates git >/dev/null

# node >= 18 for the freebuff launcher (npm package).
need_node=1
if have node; then
  major="$(node -e 'console.log(process.versions.node.split(".")[0])')"
  [ "$major" -ge 18 ] && need_node=0
fi
if [ "$need_node" -eq 1 ]; then
  say "installing Node.js LTS via NodeSource"
  curl -fsSL https://deb.nodesource.com/setup_lts.x | $SUDO -E bash - >/dev/null
  $SUDO apt-get install -y -qq nodejs >/dev/null
fi
say "node $(node --version)"

# freebuff CLI. Global install needs a writable npm prefix; fall back to a
# user prefix when the default isn't writable without sudo.
if npm config get prefix | grep -qE '^/(usr|opt)' && [ "$(id -u)" -ne 0 ]; then
  say "installing freebuff CLI (sudo npm -g)"
  $SUDO npm install -g freebuff >/dev/null
else
  say "installing freebuff CLI (npm -g)"
  npm install -g freebuff >/dev/null
fi
have freebuff || { echo "freebuff not on PATH after install" >&2; exit 1; }
say "freebuff $(freebuff --version 2>/dev/null | head -1)"

mkdir -p "$PA_ENV_DIR"
chmod 700 "$PA_ENV_DIR"
if [ ! -f "$ENV_FILE" ]; then
  token="$(head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')"
  cat > "$ENV_FILE" <<EOF
# freebuff sidecar environment — loaded by run-tmux.sh / sidecar.service
# PA_DIR: where this repo is checked out on this box
PA_DIR=$HOME/pal

PA_FREEBUFF_ENABLED=true
# shared bearer token — the remote PA sends the same value
PA_FREEBUFF_REMOTE_TOKEN=$token
# loopback only: reach it over an SSH tunnel or tailnet, never public http
PA_FREEBUFF_SIDECAR_HOST=127.0.0.1
PA_FREEBUFF_SIDECAR_PORT=8777
EOF
  chmod 600 "$ENV_FILE"
  say "wrote $ENV_FILE (fresh PA_FREEBUFF_REMOTE_TOKEN generated)"
else
  say "$ENV_FILE exists — keeping it"
fi

say "done. next: ./login.sh   (owner clicks the printed URL once)"
