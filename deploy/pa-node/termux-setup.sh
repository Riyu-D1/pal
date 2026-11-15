#!/data/data/com.termux/files/usr/bin/sh
# pa-node Termux setup — idempotent. Run inside Termux on the phone.
#
#   sh termux-setup.sh <PAL_VM_HOST>
#
# It installs the deps, creates ~/.pa-node (mode 700), reads the shared
# node token from STDIN (never printed, never stored anywhere else),
# installs the Termux:Boot start script and prints the autossh line to
# run. Pair this script with deploy/pa-node/VM-TUNNEL.md on the VM.
#
# No secrets are echoed. The token goes to ~/.pa-node/token (0600).

set -eu

PAL_VM_HOST="${1:-}"
NODE_PORT=8790
FORWARD_PORT=8791
TUNNEL_USER=pa-tunnel

say() { printf '%s\n' "$*"; }

say "==> pa-node setup (Termux)"

# --- packages -------------------------------------------------------------
say "==> installing packages"
pkg update -y >/dev/null 2>&1 || true
pkg install -y python openssh autossh termux-api termux-services >/dev/null
say "    python, openssh, autossh, termux-api, termux-services installed"

# --- dirs + token ----------------------------------------------------------
say "==> creating ~/.pa-node"
mkdir -p "$HOME/.pa-node" "$HOME/pa-node-data/tools"
chmod 700 "$HOME/.pa-node" "$HOME/pa-node-data"

TOKEN_FILE="$HOME/.pa-node/token"
if [ ! -s "$TOKEN_FILE" ]; then
    say "==> paste the node token now (from 'pal-node-token' on the VM),"
    say "    then press Enter and Ctrl-D. It is written to $TOKEN_FILE"
    say "    with mode 600 and is never printed back."
    cat > "$TOKEN_FILE".tmp
    mv "$TOKEN_FILE".tmp "$TOKEN_FILE"
    chmod 600 "$TOKEN_FILE"
else
    say "    token already present ($TOKEN_FILE) — keeping it"
fi

# --- keep the agent alive --------------------------------------------------
if command -v termux-wake-lock >/dev/null 2>&1; then
    termux-wake-lock || true
    say "==> termux-wake-lock set"
fi

# --- agent files -----------------------------------------------------------
# Copy node_agent.py + node_proto.py next to the data dir if they are
# not there yet (you scp them once, or `pkg install git` + clone).
AGENT_DIR="$HOME/pa-node-data"
if [ ! -f "$AGENT_DIR/node_agent.py" ]; then
    say "NOTE: copy pa/node_agent.py and pa/node_proto.py into"
    say "      $AGENT_DIR/ (scp or `git clone`) before starting."
fi

cat > "$AGENT_DIR/start.sh" <<'EOS'
#!/data/data/com.termux/files/usr/bin/sh
cd "$HOME/pa-node-data"
exec python node_agent.py --port 8790 --data-dir "$HOME/pa-node-data" \
    --token-file "$HOME/.pa-node/token"
EOS
chmod 700 "$AGENT_DIR/start.sh"

# --- boot hook -------------------------------------------------------------
BOOT_DIR="$HOME/.termux/boot"
mkdir -p "$BOOT_DIR"
cat > "$BOOT_DIR/pa-node.sh" <<EOS
#!/data/data/com.termux/files/usr/bin/sh
termux-wake-lock
sh "$HOME/pa-node-data/start.sh" &
# reverse tunnel: pal reaches the agent at 127.0.0.1:$FORWARD_PORT
[ -n "$PAL_VM_HOST" ] && autossh -M 0 -N \
    -o ServerAliveInterval=30 \
    -o ServerAliveCountMax=3 \
    -o ExitOnForwardFailure=yes \
    -R 127.0.0.1:$FORWARD_PORT:127.0.0.1:$NODE_PORT \
    $TUNNEL_USER@$PAL_VM_HOST &
EOS
chmod 700 "$BOOT_DIR/pa-node.sh"

say ""
say "==> done. Next:"
say "  1. copy pa/node_agent.py + pa/node_proto.py into $AGENT_DIR/"
say "  2. ssh-keygen -t ed25519 -f ~/.pa-node/id_ed25519 -N ''"
say "  3. put the pubkey in the VM's pa-tunnel authorized_keys"
say "     (see deploy/pa-node/VM-TUNNEL.md)"
say "  4. start: sh ~/.termux/boot/pa-node.sh   (or let Termux:Boot do it)"
say "  5. on the VM: PA_NODES=phone=http://127.0.0.1:$FORWARD_PORT and"
say "     PA_NODE_PHONE_TOKEN=<the token you pasted>"
if [ -z "$PAL_VM_HOST" ]; then
    say ""
    say "  (re-run with the VM host as \$1 to fill the autossh line:"
    say "   sh termux-setup.sh <PAL_VM_HOST>)"
fi
