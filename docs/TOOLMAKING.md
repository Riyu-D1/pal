# Tool making (K1)

Pal can write, test and register small pure-Python tools for itself —
LATM/CREATOR-style. Tools live under `<PA_DATA_DIR>/tools/<name>/v<N>/`
(**never inside `pa/`** — `pa/evolve/policy.py` protected paths are
untouched) and are registered in the `made_tools` table, one row per
version.

Flag: `PA_MADETOOLS=1` (default off — the `tool_*` chat tools stay
hidden).

## Flow

`tool_make(name, purpose, examples)` →

1. the model writes one module — a single `run(args)` function plus a
   `fetch(url)` global — and 3–5 `test_*` functions;
2. **static check** before anything runs: imports are limited to a
   stdlib allowlist (`json`, `math`, `re`, `datetime`, `urllib.parse`,
   …) — `socket`, `subprocess`, `os`, `urllib.request`, `ctypes`,
   `importlib` and friends are rejected outright, as are `eval`,
   `exec`, `open`, `getattr` and dunder access;
3. tests run in the `runpy` sandbox — no network except the injected
   `fetch()` which goes through `pa/egress.guarded_get` (public IPs
   only, size/time/redirect caps);
4. on pass the tool registers. The **first version of a name needs an
   owner approval** (`tool_register` approval — the payload carries the
   code+tests for review); later versions that keep the same manifest
   surface (name/purpose/inputs/network/node_ok) and still pass their
   tests auto-register with the diff noted in the audit log. A changed
   manifest is refused — retire the name instead.

`tool_run(name, args)` executes the active version in the same
sandbox (or on a node when the manifest says `node_ok`). Policy:
pure tools (`network: false`) are pal-owned writes and run free;
`network: true` tools are classified `exec` — every run needs owner
approval.

`tool_list` shows registered tools + manifests.

## Manifest

```json
{"name": "csv_sum", "purpose": "sum a column",
 "inputs": {"type": "object", "properties": {...}},
 "network": false, "node_ok": false}
```

`network` → `exec` policy class per run. `node_ok` → eligible for the
node agent's `run_tool` cap (X2); the phone's 2 GB RAM and the sandbox
limits apply there too.

## Limits

* code ≤ 400 lines / 20 KB, tests ≤ 10 KB;
* sandbox: no new deps, no network beyond `fetch()`, CPU/time/memory
  limits from `runpy`;
* storage is `<PA_DATA_DIR>/tools/` — deleting the folder removes the
  tools; the DB rows keep the manifests for audit.

# Integration builder (K2)

`integration_build(service, goal, docs_url?, account?)` generates a
small API client as a **bundle of K1 tools** plus a `connections`
row — Alita-style: pal builds its own integration instead of waiting
for a pre-built one.

Flag: `PA_INTEGRATIONS=1` (requires `PA_MADETOOLS=1`).

* The model returns `{base_url, auth:{scheme,param}, tools:[{name,
  purpose, code, tests, read_only}]}`. `base_url` must be public
  `https://` (private IPs, `.local`, non-https are refused); a key
  literal anywhere in generated code fails the build outright —
  before any registration is attempted.
* Each tool becomes a made tool whose manifest carries
  `integration: <name>` + `integration_provider: made:<name>@<acct>`.
  The connection row (`provider made:<name>@<acct>`, `kind='made'`)
  holds `base_url` + auth scheme in `meta_json` — it lists alongside
  Composio/MCP connections and feeds `caps_updated_at` so the model
  re-checks its tool surface.
* `account="pal"` marks the account pal-owned; anything else is
  owner-owned (`connectflows.owner_kind_for` fails closed to owner).
* **Registration needs owner approval** — the build queues an
  `integration_register` job_action; the owner's Approve replays it
  through `execute_approved` → `integrations.register_approved`,
  which writes the connection + registers each tool (the made-tools
  first-version approval is skipped here — the bundle approval covers
  it).
* If the service needs a key and none exists, the build ends with an
  `ask_owner` message saying where to get a free key — pal never
  signs up for anything itself in this run.

## Keys

`integration_key(name, key)` seals the key into the connection's
`token_blob` (`secretsbox`, AAD `pa-connection:<provider>`). The key
is **never** in generated source, logs or tool results: at run time
`run_integration` writes it to a 0600 tempfile under the data dir
that only the sandbox *wrapper* (not tool code) reads and injects
into `fetch()` as `?param=key` / bearer / basic, then deletes it.

`fetch()` inside an integration tool is additionally **pinned to the
integration's `base_url`** — off-base URLs get a 403, so a tool can't
exfiltrate the key elsewhere. Plain `guarded_get` rules still apply
underneath.

## Policy

`tool_run` classifies by manifest: `read_only` tools are reads (run
free), pal-tagged integration writes are pal-owned writes, everything
else network-capable is `exec` (owner approval per run).
`integration_build`/`integration_key`/`integration_list` themselves
are pal-owned actions — the registration gate is the owner's click,
not the build.

# Skill auto-extraction (K3)

Flag: `PA_SKILLEXTRACT=1` (default off).

After an attended turn lands an `ok` outcome with **≥3 successful
tool steps**, `pa/skillextract.py` asks the fast model to distill the
trace into `{name, description, body}` (generic procedure, ≤25 lines).

* **Dedupe**: identical slug, ≥60% keyword overlap with an existing
  skill, or a repeated content hash (`pa_settings` marker — no new
  table) all suppress the proposal.
* **Approval**: the proposal queues a `job_action` for `skill_save` —
  the ordinary Approve path writes it (`execute_approved` replays the
  chat tool, no second approval is created). Nothing saves a skill
  without the owner's click.
* **Retrieval**: with the flag on, `_system` annotates the skills
  block with the top-3 skills for the incoming message (keyword
  overlap on name+description) so the model `skill_load`s the right
  procedure.
