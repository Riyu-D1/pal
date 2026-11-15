# S2 — Research reports

`research(question, depth)` hands a question to a `researcher`
sub-agent (S1) and turns its findings into a cited Markdown report in
the workspace.

Off by default: `PA_RESEARCH=1` (requires `PA_SUBAGENTS=1` and the
workspace for the report write).

## Flow

1. The chat tool builds a structured prompt asking the child to end
   its answer with a fenced `json` block of `{claims, sources}` —
   each claim carries the indices of the sources that support it.
2. `subagents.delegate` queues an ordinary worker task bound to the
   `sub:researcher` assistant (memory/web/fetch tools only) under a
   per-depth budget:

   | depth    | tool calls | minutes | max sources |
   |----------|-----------:|--------:|------------:|
   | quick    | 12         | 5       | 5           |
   | standard | 25         | 15      | 10          |
   | deep     | 50         | 30      | 20          |

3. A `research_jobs` row (m39) links the task to the question, depth,
   target project and publish flag.
4. When the task finishes, the completion-notification pass calls
   `research.finish`, which writes `reports/<slug>-t<id>.md` into the
   workspace and appends a `report:` line to the posted result.

## Citation rules

- Findings only list claims tied to at least one numbered source.
- Claims citing nothing (or a bad index) drop into an
  **Unverified** section rather than being presented as findings.
- If the child returns no JSON ledger, URLs found in the summary
  become the source list and the report is marked as leads.

## Publishing

`publish=True` queues a held `page_publish` action — the report goes
public only after the owner approves, same as every publish.

Everything the child reads is untrusted external content: it appears
in the report as data, never as instructions pal executes.
