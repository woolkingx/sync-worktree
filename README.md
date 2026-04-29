# sync-worktree

Config-driven file sync between Git worktrees. This is the only supported entry point.

## Why

AI agents use this to inspect a repository, generate a decision, and apply it through a policy gate.

Architecture reference: [docs/architecture.md](docs/architecture.md)
Build artifact spec: [docs/build.md](docs/build.md)

Source tree: `master/`
Build artifacts: `master/dist/`
Deployment worktree: `runner/`

## AI-Native Interface

`sync-worktree` exposes a structured JSON API for AI agents, enabling one-shot inspection, decision-making, and execution without parsing human-readable output.

### Three-Stage Workflow

```python
# Stage 1: Inspect (one-call global view)
context = json.loads(subprocess.check_output([
    "python3", "master/sync_worktree.py", "inspect", "--target", "release"
]))

# Stage 2: Decide (your AI logic here)
if context["targets"][0]["risk_assessment"]["safe_to_apply"]:
    decision = {
        "contract": {"version": "2.0", "type": "decision"},
        "metadata": {"ai_agent": "my-agent", "context_hash": context["meta"]["context_hash"]},
        "decision": {"action": "sync", "target": "release", "apply": True, "force": False}
    }
else:
    # auto-fix or abort
    ...

# Stage 3: Apply
result = json.loads(subprocess.check_output([
    "python3", "master/sync_worktree.py", "apply", "--from-decision", "decision.json"
]))
print(result["outcome"]["status"])  # "success" / "failed" / "dry_run"
```

### Commands

| Command | Purpose | Output |
|---------|---------|--------|
| `inspect [--target <name>] [--deep]` | Export full repo state as JSON | Context contract |
| `apply --from-decision <file> [--dry-run] [--verify-context]` | Execute AI decision | Result contract |
| `config show [--rule] [--setting]` | Show effective config | Config JSON |
| `config schema` | Print config schema reference | Schema JSON |
| `target add <name> [--role <role>] ...` | Add or update a target binding | Target JSON |
| `target remove <name>` | Remove a target binding | Target JSON |

**See full spec**: [docs/contract.md](docs/contract.md)
### Full Workflow Example

```bash
# 1. Get context (AI reads repo state in one shot)
python3 master/sync_worktree.py inspect --target release > context.json

# 2. AI analyzes and generates decision
# Example: auto-fix C1 (dirty source) then sync
cat > decision.json << 'EOF'
{
  "contract": {"version": "2.0", "type": "decision"},
  "metadata": {
    "ai_agent": "my-agent",
    "context_hash": "'"$(jq -r .meta.context_hash context.json)"'"
  },
  "decision": {
    "action": "fix_then_sync",
    "target": "release",
    "apply": true,
    "force": false,
    "confidence": 0.9,
    "rationale": "Fixing uncommitted changes automatically"
  },
  "auto_fixes": [
    {
      "check_id": "POL-TOP-002",
      "description": "Target worktree dirty",
      "commands": [
        "git -C /path/to/release add . && git commit -m 'auto: prepare sync'"
      ]
    }
  ]
}
EOF

# 3. Dry-run first (safe)
python3 master/sync_worktree.py apply --from-decision decision.json --dry-run

# 4. Execute for real
python3 master/sync_worktree.py apply --from-decision decision.json
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
- [Build artifact spec](docs/build.md)
- [Example agent](examples/ai_agent_demo.py)
