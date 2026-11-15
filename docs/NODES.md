# X1/X2/X4 — Nodes: pal's remote hands

A node is a small HTTP agent running on hardware pal owns — the
an Android phone (Termux — e.g. the Moto E5 in `docs/PHONE.md`), the computer VM, or any Linux box. Pal
dispatches capability jobs to nodes through `pa/nodes.py`; every job
is policy-classified and budget-charged before it leaves the pal VM.

Off by default — no `PA_NODES`, no nodes, identical behaviour.

## Configure

```bash
PA_NODES=phone=http://127.0.0.1:8791,computer=http://100.64.1.5:8788
PA_NODE_PHONE_TOKEN=<shared bearer>     # never commit
PA_NODE_COMPUTER_TOKEN=<shared bearer>
PA_NODE_CONNECT_SECONDS=10              # connect timeout
PA_NODE_HEALTH_SECONDS=30               # health-poll cadence
```

URL rules are the same as `PA_COMPUTER_URL`: `http://` only to
loopback / tailnet / private addresses, `https://` otherwise, no
credentials in the URL. The phone reaches the pal VM over a reverse
SSH tunnel — see `deploy/pa-node/VM-TUNNEL.md` and `docs/PHONE.md`.

## Registry + dispatch (`pa/nodes.py`)

- `health()` / `caps()` / `submit(cap, args, budget_s)` / `poll(id)` /
  `cancel(id)` — constant-time bearer compare, 10 s connect timeout.
- **Circuit breaker**: 3 consecutive failures opens it for the node;
  half-open probe on `breaker_until`.
- **Scheduler** picks a node by capability + health (battery/temp for
  the phone — it self-reports `paused` under 25 % / over 42 °C) +
  budget; jobs queue in `node_jobs` when nothing is free.
- Every job is `policy.decide`-classified before dispatch and holds a
  `node_sec:<name>` budget reservation that settles on completion.
- `/nodes` lists health; `/status` shows node lines.

## `pa-node` agent (`pa/node_agent.py`)

Stdlib-only `http.server` agent (Python 3.11+; Termux `pkg install
python`), ≤40 MB RSS. Token file `~/.pa-node/token` (0600,
`hmac.compare_digest`). One job at a time, queue of five. Capability
**allowlist** — no arbitrary shell:

`fetch` (egress-filtered GET), `rss`, `ping_http`,
`file_put`/`file_get` (inside `~/pa-node-data` only),
`run_tool` (a K1 made tool whose manifest says `node_ok`, in a
resource-limited subprocess), plus Termux caps when the binaries
exist (docs/PHONE.md).

## Computer VM as a node

Setting `PA_COMPUTER_URL` alone registers the existing
`pa/computer.py` agent as node `computer` (no `PA_NODES` entry
needed; an explicit `PA_NODES=computer=<agent url>` also works).
Caps are `browse`, `screenshot`, `read_url_js` — the same actions
the computer-agent's `/call` endpoint already serves, reached through
a thin adapter on the same URL + `PA_COMPUTER_TOKEN`. It shows up in
`/nodes` and `/capabilities`; dispatches reserve `node_sec:computer`
and charge `browser_min` per session minute; the `browse_*` chat
tools keep working unchanged.
