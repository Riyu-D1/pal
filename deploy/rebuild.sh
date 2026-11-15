#!/usr/bin/env bash
# deploy/rebuild.sh — rebuild pal on a FRESH Ubuntu micro from an
# encrypted backup (docs/DR.md case a/c). Idempotent; safe to re-run.
#
# Usage (run as root or with sudo):
#   ./deploy/rebuild.sh /path/to/pa-YYYYMMDD-HHMMSS.db.enc
#
# Prompts for PA_BACKUP_PASSPHRASE (never stored, never echoed to
# logs). Requires: ubuntu 22.04+/24.04, network, the repo reachable
# via git (private repo → provide a deploy key or PAT in GIT_URL).
set -euo pipefail

REPO_URL="${GIT_URL:-https://github.com/Riyu-D1/pal.git}"
BRANCH="${PA_BRANCH:-main}"
APP_DIR=/opt/pal
ENV_DIR=/root/.pa
BACKUP_SRC="${1:?usage: rebuild.sh <encrypted-backup.enc>}"

log() { printf '[rebuild] %s\n' "$*"; }
have() { command -v "$1" >/dev/null 2>&1; }

log "installing base packages"
if ! have python3.12; then
  apt-get update -qq
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
    python3 python3-venv python3-pip git sqlite3 >/dev/null
fi

log "cloning $REPO_URL ($BRANCH)"
if [ ! -d "$APP_DIR/.git" ]; then
  git clone --depth 1 --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
else
  git -C "$APP_DIR" fetch --depth 1 origin "$BRANCH"
  git -C "$APP_DIR" checkout --force FETCH_HEAD
fi

log "python venv + deps"
if [ ! -x "$APP_DIR/.venv/bin/python" ]; then
  python3 -m venv "$APP_DIR/.venv"
fi
"$APP_DIR/.venv/bin/pip" install -q --upgrade pip
"$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

log "env file (secrets come from $ENV_DIR/env — fill it yourself)"
mkdir -p "$ENV_DIR"
chmod 700 "$ENV_DIR"
if [ ! -f "$ENV_DIR/env" ]; then
  sed 's/^# //' "$APP_DIR/.env.example" | grep -E '^PA_|^DISCORD|^GROQ|^GEMINI' \
    > "$ENV_DIR/env" || true
  chmod 600 "$ENV_DIR/env"
  log "WROTE $ENV_DIR/env template — edit it BEFORE starting pal"
fi

log "restoring database from $BACKUP_SRC"
# shellcheck disable=SC1091
set -a; [ -f "$ENV_DIR/env" ] && . "$ENV_DIR/env"; set +a
DATA_DIR="${PA_DATA_DIR:-/var/lib/pal}"
mkdir -p "$DATA_DIR"
DB_PATH="${PA_DB:-$DATA_DIR/pa.sqlite3}"
if [ ! -f "$DB_PATH" ]; then
  read -r -s -p "backup passphrase: " PA_BACKUP_PASSPHRASE </dev/tty || true
  echo
  PA_DATA_DIR="$DATA_DIR" "$APP_DIR/.venv/bin/python" -m pa import-backup \
    --src "$BACKUP_SRC" <<<"$PA_BACKUP_PASSPHRASE" || {
      log "restore failed — check the passphrase"; exit 1; }
  unset PA_BACKUP_PASSPHRASE
fi
PA_DATA_DIR="$DATA_DIR" "$APP_DIR/.venv/bin/python" -m pa migrate

log "systemd units"
for unit in pa.service pa-watchdog.service; do
  if [ -f "$APP_DIR/deploy/$unit" ]; then
    sed "s|/opt/pal|$APP_DIR|g; s|/root/.pa|$ENV_DIR|g" \
      "$APP_DIR/deploy/$unit" > "/etc/systemd/system/$unit"
  fi
done
systemctl daemon-reload
systemctl enable pa.service >/dev/null 2>&1 || true

log "dr-check"
PA_DATA_DIR="$DATA_DIR" "$APP_DIR/.venv/bin/python" -m pa dr-check || true

log "starting pal"
systemctl restart pa.service
systemctl enable --now pa-watchdog.service >/dev/null 2>&1 || true
log "done — pal should answer on Discord within a minute"
