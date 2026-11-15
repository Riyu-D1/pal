# Sub-agents (S1)

`delegate(role, prompt, budget, tools, node)` hands a bounded task to
a background sub-agent and returns immediately; the result posts back
into the same channel when the task finishes (the normal
task-completion notification — no polling).

Flag: `PA_SUBAGENTS=1` (default off — `delegate` keeps its old
behaviour of queueing a `chat_inbox` turn). `PA_SUBAGENT_MAX=2` caps
live children.

## How it works

A role maps to an assistants row named `sub:<role>`:

| role        | tools (allowlist enforced by ToolContext)                    |
|-------------|--------------------------------------------------------------|
| researcher  | memory_search, web_search, fetch_url, propose_memory          |
| analyst     | + run_python                                                 |
| browser     | web_search, fetch_url, node_submit, node_poll, propose_memory |
| coder       | the freebuff-cli assistant (owner's own signed-in CLI)     |

`provider_host`/`provider_model` are cloned from the channel's chat
assistant, so a sub-agent uses the same free providers pal does.
The `tools` argument may only *narrow* the role's set.

Children are ordinary rows in `tasks`: the single worker claims them
sequentially (no extra processes on the 1 GB pal VM), `deadline`
comes from `budget.minutes`, and `/tasks`, `/status <id>`,
`/cancel <id>` all work as usual — `/tasks` annotates children with
their role and tool-call/token spend.

## Budgets

`budget={tool_calls, minutes, tokens}`:

* `minutes` → the task deadline (`DeadlineReached`);
* `tool_calls` and `tokens` → caps in `subagents.budget_json`,
  enforced inside the agent loop as `OverBudget` (defaults
  40 calls / 15 min; hard ceilings 100 calls / 120 min);
* node work the child submits through `node_submit` still spends
  `node_sec:*` / `browser_min` — node budgets are not bypassed.

## Policy

Nothing in a child bypasses the floor: the worker tool surface is
read-mostly, `propose_memory` and sensitive `web_search` queries
create the usual approval rows, and a gated node cap comes back
`approval_required` — the child reports it instead of running.
Owner-account writes stay impossible inside a sub-agent.

## Node tools (worker-side)

`node_submit(node, cap, args)` → `{job_id, status}`; `node_poll(job_id)`
→ status + bounded result. These exist for `browser`-role children so
JS-heavy pages run on the computer VM, not locally.
