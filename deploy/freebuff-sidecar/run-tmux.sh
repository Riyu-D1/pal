#!/usr/bin/env bash
# run-tmux.sh — start the freebuff sidecar detached in tmux.
# Works everywhere tmux does; no systemd needed. For systemd use
# sidecar.service instead.
set -euo pipefail

ENV_FILE="${PA_ENV_FILE:-$HOME/.pa-sidecar/env}"
SESSION="pa-sidecar"

command -v tmux >/dev/null || { echo "tmux missing — run install.sh" >&2; exit 1; }
[ -f "$ENV_FILE" ] || { echo "$ENV_FILE missing — run install.sh" >&2; exit 1; }
# shellcheck disable=SC1090
set -a; . "$ENV_FILE"; set +a
: "${PA_DIR:?set PA_DIR in $ENV_FILE to the repo checkout}"
[ -d "$PA_DIR/pa" ] || { echo "no pa/ package under PA_DIR=$PA_DIR" >&2; exit 1; }

tmux kill-session -t "$SESSION" 2>/dev/null || true
tmux new-session -d -s "$SESSION" -c "$PA_DIR" \
  "env $(grep -vE '^\s*#|^\s*$|PA_DIR=' "$ENV_FILE" | tr '\n' ' ') python3 -m pa freebuff-sidecar 2>&1 | tee -a $HOME/.pa-sidecar/sidecar.log"

sleep 2
tmux has-session -t "$SESSION" || { echo "sidecar exited — see $HOME/.pa-sidecar/sidecar.log" >&2; exit 1; }
echo "sidecar up in tmux session '$SESSION'"
echo "health check:  curl -s -H 'x-pa-freebuff-token: \$PA_FREEBUFF_REMOTE_TOKEN' http://${PA_FREEBUFF_SIDECAR_HOST:-127.0.0.1}:${PA_FREEBUFF_SIDECAR_PORT:-8777}/healthz"
