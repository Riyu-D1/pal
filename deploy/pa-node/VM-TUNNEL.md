# pa-node VM tunnel (pal VM side)

The phone node reaches pal over a **reverse SSH tunnel**: the phone
dials out (NAT/mobile-data friendly, no inbound port), pal connects to
`127.0.0.1:8791` on the VM.

These steps run **on the pal VM as root** (the parent agent applies
them after the owner approves — Devin never touches the VM).

## 1. Locked-down tunnel user

```sh
useradd -r -m -d /home/pa-tunnel -s /usr/sbin/nologin pa-tunnel
mkdir -p /home/pa-tunnel/.ssh
chmod 700 /home/pa-tunnel/.ssh
cat > /home/pa-tunnel/.ssh/authorized_keys <<'EOF'
restrict,port-forwarding,permitlisten="127.0.0.1:8791",command="/usr/sbin/nologin" ssh-ed25519 AAAA... phone-key
EOF
chmod 600 /home/pa-tunnel/.ssh/authorized_keys
chown -R pa-tunnel:pa-tunnel /home/pa-tunnel/.ssh
```

`restrict` kills PTY/shell/sftp; `permitlisten` pins the one remote
forward. Paste the phone's `~/.pa-node/id_ed25519.pub` on the key line.

## 2. sshd hardening for that user only

`/etc/ssh/sshd_config.d/pa-tunnel.conf`:

```
Match User pa-tunnel
    AllowTcpForwarding remote
    X11Forwarding no
    PermitTTY no
    PasswordAuthentication no
    PubkeyAuthentication yes
```

`systemctl reload sshd`.

## 3. pal side

```
PA_NODES=phone=http://127.0.0.1:8791
PA_NODE_PHONE_TOKEN=<shared bearer — matches ~/.pa-node/token on the phone>
```

The token lives only in `~/.pa/env` on the VM and `~/.pa-node/token`
on the phone — never in the repo, never in logs.

## Notes

- The tunnel is one-way: pal → `127.0.0.1:8791` → phone `127.0.0.1:8790`.
- If the phone drops, `/nodes` shows `down`/`breaker`; queued jobs
  resume when it reconnects (autossh retries forever).
- Android 8 is unpatched: treat the node as **untrusted-adjacent** —
  the only secrets on it are the node token + tunnel key. pal's env
  is never copied to the phone.
