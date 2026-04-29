# AI Contract Specification

## Overview

The AI contract defines a **machine-readable interface** between sync-worktree and AI agents. It consists of three JSON structures:

| Contract | Direction | Command | Purpose |
|----------|-----------|---------|---------|
| **Context** | SW → AI | `inspect` | Full repository state snapshot |
| **Decision** | AI → SW | `apply --from-decision` | Action request from AI |
| **Result** | SW → AI | `apply` output | Execution outcome |

---

## 1. Context Contract (`inspect` output)

### Schema

```json
{
  "contract": {"version": "2.0", "type": "context"},
  "meta": {
    "generated_at": "2026-04-29T01:35:00Z",
    "tool_version": "0.5.2-ai",
    "context_hash": "sha256:..."  // deterministic hash of this entire JSON
  },
  "repository": {
    "name": "my-project",
    "topology": "bare|worktree|repo",
    "bare_path": ".bare",
    "worktrees": [
      {
        "name": "master",
        "path": "/abs/path/to/master",
        "branch": "refs/heads/master",
        "head": "abc123...",
        "is_current": true
      }
    ]
  },
  "global_state": {
    "master": {
      "dirty": true,
      "uncommitted": ["src/app.py"],
      "staged": [],
      "untracked": ["tmp/"],
      "ahead_of": {"origin/master": 2},
      "behind_of": {"origin/master": 1}
    }
  },
  "config": {
    "file": ".bare/rule.config.json",
    "version": 2,
    "targets": {
      "release": {
        "role": "deployment",
        "source": "master",
        "overrides": {
          "include": ["src/**/*.py"],
          "exclude": ["tests/"],
          "protect": [".env"],
          "delete_policy": "tracked_only"
        }
      }
    }
  },
  "targets": [
    {
      "name": "release",
      "exists": true,
      "health": "ok|warning|error|critical",
      "health_score": 0-100,
      "source_worktree": "master",
      "dest_path": "/opt/release",
      "last_sync": {
        "timestamp": "2026-04-29T06:19:53.552296Z",
        "source_commit": "abc123",
        "files_synced": 42,
        "file_hashes": {"src/app.py": "sha256:..."}
      },
      "sync_plan": {
        "total_source_files": 150,
        "matched_files": 142,
        "excluded_files": 8,
        "actions": {"A": 0, "M": 5, "D": 0, "U": 137, "P": 0, "X": 8},
        "action_details": [
          {"path": "src/app.py", "status": "M", "protected": false, "size": 2048}
        ],
        "protected_hits": [],
        "delete_candidates": []
      },
      "checks": [
        {
          "id": "POL-TOP-001",
          "passed": true,
          "severity": "error",
          "title": "Role-based operation allowlist",
          "description": "OK",
          "evidence": [],
          "remediable": false,
          "remediation": null
        },
        {
          "id": "POL-TOP-002",
          "passed": false,
          "severity": "error",
          "title": "Target worktree must be clean",
          "description": "Target worktree has 1 uncommitted change(s)",
          "evidence": ["?? logs/"],
          "remediable": true,
          "remediation": {
            "description": "Stash, commit, or discard changes before syncing",
            "commands": [
              "git -C /path/to/runner status  # review changes",
              "git -C /path/to/runner stash     # temporary stash"
            ]
          }
        }
      ],
      "risk_assessment": {
        "overall_score": 50,
        "blockers": ["POL-TOP-002"],
        "warnings": [],
        "safe_to_apply": false,
        "requires_force": false,
        "recommended_action": "fix_blockers_first"
      }
    }
  ],
  "dependencies": {
    "path_overlaps": [],
    "conflicts": []
  },
  "decision_engine": {
    "auto_fixable_checks": ["POL-TOP-001", "POL-CON-001"],
    "requires_human": ["POL-TOP-002"],
    "blocking_checks": ["POL-TOP-001", "POL-TOP-002"],
    "suggested_workflow": ["Fix POL-TOP-002 first", "Then retry sync"]
  },
  "summary": {
    "total_targets": 1,
    "healthy": 0,
    "warning": 0,
    "error": 1,
    "critical": 0,
    "next_action": "sync runner (after fix)"
  }
}
```

### Key Fields

- `contract.version`: Must be `"2.0"`.
- `meta.context_hash`: SHA256 of the full JSON (sorted keys). AI can store this and later verify with `--verify-context`.
- `repository.topology`: One of `"bare"`, `"worktree"`, `"repo"`.
- `global_state.<worktree>.ahead_of` / `behind_of`: Divergence counts against the tracked upstream branch when available.
- `targets[].last_sync`: The last persisted sync state for that target, loaded from `.bare/sync-worktree.state.json` when present.
- `targets[].health`: Derived from checks. `"ok"` = all passed, `"warning"` = non-blocking failures, `"error"` = blocking failures, `"critical"` = unrecoverable.
- `targets[].risk_assessment.safe_to_apply`: `true` iff no error-level failed checks.

---

## 2. Decision Contract (AI → SW)

AI agent generates this JSON and passes to `apply --from-decision file.json`.

```json
{
  "contract": {"version": "2.0", "type": "decision"},
  "metadata": {
    "ai_agent": "my-agent",
    "timestamp": "2026-04-29T01:40:00Z",
    "context_hash": "sha256:..."  // optional but recommended
  },
  "decision": {
    "action": "sync" | "fix_then_sync" | "ask" | "abort",
    "target": "release" | "runner" | "all",
    "apply": true | false,
    "force": false | true,
    "confidence": 0.0-1.0,
    "rationale": "Short explanation"
  },
  "auto_fixes": [  // required if action="fix_then_sync"
    {
      "check_id": "POL-TOP-002",
      "description": "Target worktree is dirty",
      "commands": [
        "git -C runner stash"
      ]
    }
  ],
  "questions": [  // required if action="ask"
    {
      "id": "merge_diverged",
      "prompt": "Target 'release' is ahead by 3 commits. Merge?",
      "options": [
        {"value": "merge", "label": "Merge (preserve commits)", "risk": "low"},
        {"value": "overwrite", "label": "Overwrite (force)", "risk": "high"},
        {"value": "abort", "label": "Abort", "risk": "none"}
      ],
      "default": "merge"
    }
  ]
}
```

### Action Semantics

| action | meaning | apply flag | auto_fixes required? | questions required? |
|--------|---------|------------|---------------------|---------------------|
| `sync` | Run sync operation | must be `true` for real apply, `false` for dry-run | no | no |
| `fix_then_sync` | Run auto_fixes then sync | `true` recommended | yes | no |
| `ask` | Requires human input | ignored | no | yes |
| `abort` | Cancel operation | ignored | no | no |

---

## 3. Result Contract (SW → AI)

Output of `apply --from-decision`:

```json
{
  "contract": {"version": "2.0", "type": "result"},
  "metadata": {
    "decision_hash": "abc123...",
    "executed_at": "2026-04-29T01:41:00Z"
  },
  "outcome": {
    "status": "success" | "failed" | "partial" | "cancelled" | "dry_run" | "stale_context",
    "phase": "sync" | "fix_1" | "fix_2" | ...,
    "target": "runner",
    "apply": true,
    "returncode": 0,
    "stdout": "...",
    "stderr": "...",
    "success": true
  }
}
```

### Status Values

- `success`: All phases completed without error.
- `partial`: Some phases succeeded, one failed (e.g., fix1 ok, fix2 failed).
- `failed`: A critical phase failed.
- `cancelled`: Decision action was `abort`.
- `dry_run`: `--dry-run` flag was used; no actions executed.
- `stale_context`: Context hash mismatch (use `--verify-context` to enable this check).

---

## 4. Usage Examples

### Simple sync (all clear)

```bash
# 1. Get context
python3 master/sync_worktree.py inspect --target release > context.json

# 2. AI decides (example: check health)
# if ctx['targets'][0]['risk_assessment']['safe_to_apply']:
decision='{
  "contract":{"version":"2.0","type":"decision"},
  "metadata":{"ai_agent":"demo","context_hash":"'"$(jq -r .meta.context_hash context.json)"'"},
  "decision":{"action":"sync","target":"release","apply":true,"force":false}
}'

# 3. Apply
echo "$decision" > decision.json
python3 master/sync_worktree.py apply --from-decision decision.json
```

### Auto-fix then sync

```json
{
  "decision": {
    "action": "fix_then_sync",
    "target": "runner",
    "apply": true
  },
  "auto_fixes": [
    {
      "check_id": "POL-TOP-002",
      "commands": ["git -C runner stash"]
    }
  ]
}
```

### Abort on error

```json
{
  "decision": {"action": "abort", "target": null}
}
```

---

## 5. Idempotency & Safety

- **`context_hash`**: Optional but recommended. If decision includes it and `apply --verify-context` is set, SW will reject execution if repo state changed since `inspect`.
- **`decision_hash`**: Included in result for traceability.
- **Dry-run**: Always test decisions with `apply --dry-run` before real execution.

---

## 6. Error Handling

All errors return a result contract with `"status": "failed"` or `"error"` field at top-level. The `stderr` contains human-readable diagnostics.

---

## 7. Future Extensions

- `confirm` command for interactive questions (for `ask` action)
- `init_repo` and `create_worktree` actions
- Multi-target batch operations
- Streaming event output
