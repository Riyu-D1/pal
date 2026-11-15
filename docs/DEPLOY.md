# Deploy pal

One process (`python -m pa serve` = web + worker + scheduler + Discord
bot) + one volume (`PA_DATA_DIR`, default `./data`). Anywhere Python
3.11+ or Docker runs works; all options below run at £0 on free tiers.

## A. systemd on a Linux VM (recommended standing deploy)

On an Oracle Always Free instance, home box, Pi — any always-on Linux:

```sh
git clone <repo> ~/pal && cd ~/pal
scripts/setup.sh --non-interactive   # or export env first; see .env
# create-owner prompts for the web login — run once:
.venv/bin/python -m pa create-owner

sudo cp deploy/pa.service /etc/systemd/system/
sudoedit /etc/systemd/system/pa.service   # fix User=, ExecStart path,
                                        # EnvironmentFile=/home/you/pal/.env
sudo systemctl enable --now pa
curl http://127.0.0.1:8080/healthz
journalctl -u pa -f
```

`deploy/pa.service` runs as your user, loopback-only, restarts on
failure. Upgrades: `deploy/rebuild.sh` (pull + pip install + restart +
health gate + auto-rollback) — point it at your checkout path.

### Backups

```cron
17 */6 * * *  cd ~/pal && .venv/bin/python -m pa backup >> data/backup.log 2>&1
```

Encrypted volume snapshots + offsite restore procedure: `docs/BACKUP.md`,
`docs/DR.md`, `docs/RUNBOOK.md`.

## B. Docker / docker compose

```sh
cp .env.example .env            # fill the 3 required values
docker compose up -d --build    # single container, named volume for /data
docker compose exec pa python -m pa create-owner
curl http://127.0.0.1:8080/healthz
```

compose binds **`127.0.0.1:8080`** on the host (loopback only — the
web UI is never exposed) and mounts a named volume at `/data`. The
container runs as a non-root `pa` user. Plain `docker build` works
equally; the CI Docker smoke job exercises this path.

## C. Tunnel (needed only for OAuth callbacks)

`/connect <app>` and webhook ingress need a public HTTPS URL. Two free
options — no open ports on the VM either way:

```sh
# Cloudflare quick tunnel — URL changes every restart; fine for testing:
cloudflared tunnel --url http://127.0.0.1:8080
# → PA_PUBLIC_URL=https://<whatever>.trycloudflare.com

# Named tunnel — stable URL: cloudflared tunnel create pal
# (needs a free Cloudflare account + a domain on Cloudflare DNS)
```

Set `PA_PUBLIC_URL` to the tunnel URL and restart. Callback URIs for
Google/Composio use it automatically. The tunnel only forwards to
loopback — bind stays `127.0.0.1`. Details: `docs/CONNECTIONS.md` §tunnel.

## D. fly.io

`fly.toml.example` is the reference — the only documented topology
where pal binds outside loopback (`PA_CONTAINER_BIND=true`, because
fly's private network is the boundary). Copy to `fly.toml`, `fly launch`,
set secrets with `fly secrets set`, attach a volume for `/data`.
The container's `.fly/bind` marker file flips the bind safety rail;
outside that marker it refuses non-loopback binds.

## E. Local Mac/Linux dev

```sh
PA_ENV=development PA_DATA_DIR=./data scripts/setup.sh
python -m pa serve
```

`PA_ENV=test` adds an isolated db dir + no real network assumptions.

## Upgrade checklist (any host)

1. `python -m pa backup` first (or rely on the 6-hourly cron).
2. `git pull` (or new image tag), `pip install -r requirements.txt`.
3. `python -m pa migrate` — schema migrations run forward-only and are
   also applied automatically by `serve` on boot.
4. Restart the service; `curl …/healthz` must return `"ok":true`
   before you walk away. `deploy/rebuild.sh` automates 2–4 + rollback.

## What NOT to do

- Don't bind `PA_BIND=0.0.0.0` directly to the internet — the policy
  and approval flow assume the web UI is private. Config.py refuses
  unsafe binds outside container markers; don't fight it.
- Don't put tokens in compose/systemd files in git — use
  `EnvironmentFile=.env` / `--env-file` with the gitignored file.
- Don't run two replicas against one SQLite dir — single-writer by
  design; scale by splitting nodes, not the core.
