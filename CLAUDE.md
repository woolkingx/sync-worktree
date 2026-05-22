# sync-worktree

Scope: AI decision support for Git worktree operations with policy gates.

Architecture truth: `docs/handbook/index.html`

## Rules

| rule | action |
|---|---|
| Handbook first | Architecture, data flow, operation semantics, and acceptance gates live in `docs/handbook/`. |
| Two data owners | Persistent owned data is `config` and `report`; topology, planner, policy, advisor, git, and executor are transform submodules. |
| AI decision first | The product is a bounded report with recommendation, commands, and workflow. Sync is one executable decision, not the whole system. |
| Git topology first | Read project root, git internal dir, current worktree, source, and target before advising or executing any Git/worktree operation. |
| Report before execution | `check`, `inspect`, and `sync` must produce a bounded decision report before any write boundary. |
| Config-driven commands | Suggested Git commands come from config style and policy; do not hardcode personal workflow into report logic. |
| Output discipline | Human and AI output should stay compact; full file lists belong in explicit detail/full mode. |
| No direct target edits | Source changes start in source worktrees; release and runner targets receive synced projections. |

## Navigation

| path | purpose |
|---|---|
| `docs/handbook/index.html` | Handbook index and current architecture map. |
| `docs/handbook/system-model.html` | Core concept: AI git decision support over topology context. |
| `docs/handbook/agent-interface.html` | Primary-user rule: optimize surfaces for agents, not human browsing. |
| `docs/handbook/data-model.html` | Two-owner data model: config and report. |
| `docs/handbook/config.html` | Config owner, command style, and personal/team preferences. |
| `docs/handbook/operations.html` | CLI operation semantics and command boundaries. |
| `docs/handbook/reporting.html` | Report owner: bounded context, recommendations, commands, workflow, evidence. |
| `docs/handbook/policies.html` | Policy/checker submodule; config-driven risk projection into report. |
| `docs/handbook/roadmap.html` | Known correction plan for CLI clarity and output size. |
| `docs/architecture.md` | Older architecture reference; handbook is the current source of truth. |
| `docs/contract.md` | Existing AI context/decision/result contract reference. |

## Decisions

- 0.5.18 (2026-05-22): primary user is the AI agent; added report hash verification plus bounded `doctor` and `explain` preflight surfaces.
- 0.5.13 (2026-05-22): implemented bounded report runtime defaults; `inspect`, `check --json`, and `sync --json` return compact reports unless explicit `--full` is requested, and `apply --dry-run` includes report evidence.
- 0.5.12 (2026-05-22): data ownership collapsed to `config` and `report`; all other modules are transforms or adapters.
