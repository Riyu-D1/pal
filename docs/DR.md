# Disaster recovery — pal

Backups: nightly `scheduled-backup` sysjob writes AES-256-GCM exports
to `PA_BACKUP_DEST` (or `<PA_DATA_DIR>/backups/pa-<stamp>.db.enc`). With
`PA_BACKUP_OFFSITE=gdrive@pal` a copy also lands in pal's own Google
Drive (pal's account, not the owner's — no approval needed).

**The backup passphrase is yours.** It lives in pal's `.env` as
`PA_BACKUP_PASSPHRASE` on the VM and nowhere in the repo. If you lose
it, the `.enc` files are unrecoverable — keep an offline copy
(password manager). Never paste it into Discord, logs, or tickets.

Sanity drill any time: `python -m pa dr-check` (also weekly, seeded
disabled: `/jobs dr-check on` — DMs only on red). The
drill actually **restores** the newest `.enc` into a temp SQLite DB —
`integrity_check`, a `schema_migrations` floor, and probe tables —
and reports the last `PA_BACKUP_OFFSITE` copy outcome (`skip` when
offsite is unset, green/red on the persisted result).

---

## (a) pal VM dies

1. Build a fresh Ubuntu 24.04 VM (any host; an Always Free micro
   works), attach your SSH key.
2. Copy the newest `.enc` to it (rsync from your laptop copy, or pull
   from pal's Drive: it’s under `pal-backups/`).
3. Run `deploy/rebuild.sh` on the new box — it installs Python, the
   repo, systemd units, restores the DB, and starts pal.
4. If the public IP changed, update the Cloudflare tunnel target and
   the Discord/webhook URLs that point at the box.
5. `python -m pa dr-check` — all rows green before you call it done.

## (b) computer VM dies

pal itself is unaffected — `browse_*`/computer tools answer
`not_configured` until a new computer node registers:

1. Rebuild an Ubuntu micro.
2. `PA_COMPUTER_TOKEN` rotate first (it's pal↔computer auth): generate
   a new token, put it in pal's `.env` on BOTH boxes, restart pal.
3. Install the computer agent per `docs/COMPUTER.md`
   (`deploy/computer/` has the unit + installer).
4. `/nodes` in Discord should list `computer` online again.

## (c) Cloud account is lost

1. You still have: the encrypted backups (they're off-box if
   `PA_BACKUP_OFFSITE` was on — check pal's Drive first) and any local
   copies.
2. New cloud account → new VM → same as (a). Nothing on the lost
   account is needed: the passphrase is yours, the DB is in the
   backup, the code is in GitHub.
3. Rotate Discord token + API keys anyway — the lost account held the
   live env file (see (d)/(e)).

## (d) Discord bot token leaked

1. Discord Developer Portal → Bot → **Reset Token** (instant revoke).
2. New token into pal's `.env` (`DISCORD_BOT_TOKEN=`) on the VM,
   `systemctl --user restart pa`.
3. Rotate `PA_COMPUTER_TOKEN`/`PA_NODE_<NAME>_TOKEN` too if the leak vector is
   unclear (they lived in the same env file).
4. Skim `/audit` in Discord for actions you didn't approve.

## (e) LLM/other API key leaked (Groq, Gemini, OpenRouter, Composio, Cloudflare)

1. Revoke the key at the provider console; issue a new one.
2. Update the name in pal's `.env`, restart `pa.service`.
3. `python -m pa provider-check` to confirm the router is healthy.

## (f) DB corrupt

1. `python -m pa dr-check` confirms: decrypt fails or restore yields a
   schema mismatch.
2. Stop pal: `systemctl --user stop pa`.
3. Restore the newest good backup:
   `python -m pa import-backup --src <file>.enc` (prompts for the
   passphrase) — it restores over `PA_DB`/`data_dir` path.
4. `python -m pa migrate && python -m pa dr-check`, restart.

Never `rm` the corrupt file first — keep it; recovery sometimes can
salvage rows the backup missed.

## (g) phone stolen

1. **Revoke its tunnel key:** on the VM, remove the phone's key from
   `~/.pa/nodes/<phone>/` (or `nodes remove phone` via `/nodes` admin
   command), then restart the `pa` service — the node can no longer
   connect or pick up queued jobs.
2. **Rotate `PA_NODE_<NAME>_TOKEN`/`PA_COMPUTER_TOKEN`** if shared.
3. Any queued phone jobs (sms_send etc.) stay queued and die with the
   revoked registration; audit recent `phone_*` actions.
4. Re-pair a replacement phone per `docs/PHONE.md`.
