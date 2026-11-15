#!/usr/bin/env bash
# login.sh — headless `freebuff login` on a remote/SSH box.
#
# The official login flow prints a URL and polls: we run it in tmux, surface
# the URL for the owner to open in any browser, then wait for the CLI to
# write credentials.json. No credentials are ever printed by this script.
set -euo pipefail

CRED_DIR="${FREEBUFF_HOME:-$HOME/.config/manicode}"
CRED_FILE="$CRED_DIR/credentials.json"
SESSION="fb-login"
URL_WAIT="${1:-120}"      # seconds to wait for the URL to appear
AUTH_WAIT=600             # seconds to wait for the owner to finish login

command -v tmux >/dev/null || { echo "tmux missing — run install.sh" >&2; exit 1; }
command -v freebuff >/dev/null || { echo "freebuff missing — run install.sh" >&2; exit 1; }

tmux kill-session -t "$SESSION" 2>/dev/null || true
tmux new-session -d -s "$SESSION" 'freebuff login'

url=""
end=$((SECONDS + URL_WAIT))
while [ $SECONDS -lt $end ]; do
  pane="$(tmux capture-pane -p -t "$SESSION" 2>/dev/null || true)"
  url="$(printf '%s' "$pane" | grep -oE 'https://[^[:space:]]+' | head -1 || true)"
  [ -n "$url" ] && break
  grep -q 'credential\|logged' <<<"$pane" && [ -f "$CRED_FILE" ] && break
  sleep 2
done

if [ -n "$url" ]; then
  echo "=============================================================="
  echo "  OWNER ACTION REQUIRED — open this URL in a browser:"
  echo
  echo "    $url"
  echo
  echo "  (the login completes on the box once you approve it)"
  echo "=============================================================="
fi

echo "waiting up to ${AUTH_WAIT}s for credentials.json ..."
end=$((SECONDS + AUTH_WAIT))
while [ $SECONDS -lt $end ]; do
  [ -s "$CRED_FILE" ] && { echo "login complete — $CRED_FILE written"; tmux kill-session -t "$SESSION" 2>/dev/null || true; exit 0; }
  if ! tmux has-session -t "$SESSION" 2>/dev/null; then
    echo "login process exited; last screen:" >&2
    tmux capture-pane -p -t "$SESSION" 2>/dev/null | tail -10 >&2 || true
    [ -s "$CRED_FILE" ] && { echo "login complete"; exit 0; }
    exit 1
  fi
  sleep 5
done
echo "timed out waiting for login — rerun ./login.sh" >&2
exit 1
