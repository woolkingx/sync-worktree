# sync-worktree

AI decision support for Git worktree operations with bounded decision reports.

Current version: `0.5.19`

Use `master/sync_worktree.py` as the supported entry point.

## Why

AI agents use this to inspect a repository, receive a bounded decision report, generate a decision, and apply it through a policy gate. The project exists to keep agents from making Git/worktree changes without topology context, target role rules, suggested commands, workflow guidance, and explicit execution evidence.

Architecture truth: [docs/handbook/index.html](docs/handbook/index.html)
Legacy architecture reference: [docs/architecture.md](docs/architecture.md)
Build artifact spec: [docs/build.md](docs/build.md)

Source tree: `master/`
Build artifacts: `master/dist/`
Deployment worktree: `runner/`

## AI-Native Interface

`sync-worktree` exposes a structured JSON API for AI agents, enabling preflight diagnosis, one-shot inspection, explanation, decision-making, and execution without parsing human-readable output. The core owned data is `config` and `report`: config defines team and personal command style, while report gives the agent a compact status, recommendation, commands, workflow, evidence, and `report_hash`. Syncing a target is one supported decision path; the core contract is `doctor -> inspect report -> explain/check -> decision -> apply`.

### Three-Stage Workflow

```python
# Stage 1: Inspect (bounded report by default)
payload = json.loads(subprocess.check_output([
    "python3", "master/sync_worktree.py", "inspect", "--target", "release"
]))
report = payload["report"]
report_hash = payload["meta"]["report_hash"]

# Stage 2: Decide (your AI logic here)
if report["status"] == "ready":
    decision = {
        "contract": {"version": "2.0", "type": "decision"},
        "metadata": {"ai_agent": "my-agent", "report_hash": report_hash},
        "decision": {"action": "sync", "target": "release", "apply": True, "force": False}
    }
else:
    # follow report["workflow"] and report["commands"], or abort
    ...

# Stage 3: Apply
result = json.loads(subprocess.check_output([
    "python3", "master/sync_worktree.py", "apply", "--from-decision", "decision.json", "--verify-report"
]))
print(result["outcome"]["status"])  # "success" / "failed" / "dry_run"
```

### Commands

| Command | Purpose | Output |
|---------|---------|--------|
| `doctor [--json]` | Diagnose topology/config/targets before action | Report contract |
| `inspect [--target <name>]` | Export bounded decision report JSON | Report contract |
| `inspect --full [--target <name>] [--deep]` | Export full repo state as JSON | Context contract |
| `explain <target> [--json]` | Explain config/policy/fact edges behind a report | Explanation contract |
| `check <target> [--json] [--full]` | Validate sync plan without writing | Human report or report JSON |
| `sync <target> [--apply] [--json] [--full]` | Validate, then optionally execute | Report JSON or execution output |
| `apply --from-decision <file> [--dry-run] [--verify-report] [--verify-context]` | Execute AI decision | Result contract |
| `config show [--rule] [--setting]` | Show effective config | Config JSON |
| `config schema` | Print config schema reference | Schema JSON |
| `target add <name> [--role <role>] ...` | Add or update a target binding | Target JSON |
| `target remove <name>` | Remove a target binding | Target JSON |

**See full spec**: [docs/contract.md](docs/contract.md)

`sync <target>` is the canonical sync command shape. `sync --target <target>` is accepted only as a legacy compatibility alias for older agent templates and emits a deprecation warning on stderr.

### Full Workflow Example

```bash
# 1. Get report (AI reads compact decision support by default)
python3 master/sync_worktree.py doctor --json
python3 master/sync_worktree.py inspect --target release > report.json
python3 master/sync_worktree.py explain release --json

# Optional: get full context hash for --verify-context workflows
python3 master/sync_worktree.py inspect --target release --full > context.json

# 2. AI analyzes and generates decision
# Example: approve a ready sync decision
cat > decision.json << EOF
{
  "contract": {"version": "2.0", "type": "decision"},
  "metadata": {
    "ai_agent": "my-agent",
    "report_hash": "'"$(jq -r .meta.report_hash report.json)"'"
  },
  "decision": {
    "action": "sync",
    "target": "release",
    "apply": true,
    "force": false,
    "confidence": 0.9,
    "rationale": "Report status is ready"
  },
  "auto_fixes": []
}
EOF

# 3. Dry-run first (safe)
python3 master/sync_worktree.py apply --from-decision decision.json --dry-run

# 4. Execute for real
python3 master/sync_worktree.py apply --from-decision decision.json --verify-report
```

## Install

```bash
python3 -m pip install .
sync-worktree inspect --target release
```

## Status Codes

`A` added · `M` modified · `D` deleted · `U` unchanged · `P` protected · `!` missing source · `X` excluded

## Quick Links

- [Full contract spec](docs/contract.md)
- [Handbook](docs/handbook/index.html)
- [Build artifact spec](docs/build.md)
- [Example agent](examples/ai_agent_demo.py)
