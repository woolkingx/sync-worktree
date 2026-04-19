# Architecture

`sync-worktree` is intentionally a single-file tool, but the runtime is organized as a DAG so each node can be unit-tested, chained, and exercised end-to-end.

## Goals

- Keep the tool copyable as one file
- Keep business logic deterministic and testable
- Keep I/O at the boundary
- Preserve git worktree semantics
- Make sync behavior explicit through config

## High-Level DAG

```text
git topology
  -> config load
  -> target resolve
  -> source file list
  -> pattern filter
  -> cross-checks
  -> action diff
  -> report/apply
  -> state write
```

Each arrow is a data edge. Each node should be testable in isolation.

## Node Map

### 1. Topology node

Functions:
- `git_detect_topology()`
- `git_parse_worktrees()`
- `git_resolve_worktree()`

Input:
- current repo state

Output:
- topology label
- git internal directory
- current worktree path

Notes:
- This node is read-only
- It determines how config and target paths are resolved
- Branch names with slashes must remain intact

### 2. Config node

Functions:
- `config_read()`
- `config_get_target()`
- `config_resolve_paths()`
- `config_init()`
- `config_add_target()`
- `config_remove_target()`

Input:
- config file in `.git/` or `.bare/`
- target name
- topology result

Output:
- merged target config
- resolved source and destination paths

Notes:
- `new_config()` returns a fresh scaffold
- Config should not share mutable nested state across calls
- Default target exclude currently includes `.gitignore`

### 3. Source scan node

Functions:
- `git_tracked_files()`

Input:
- source worktree path

Output:
- sorted list of tracked `Path` objects

Notes:
- This node is git-backed and read-only
- It defines the candidate set for filtering

### 4. Pattern node

Functions:
- `filter_match()`
- `filter_pipeline()`

Input:
- tracked file list
- `include`
- `exclude`

Output:
- `sync_files`
- `unmatched`
- `blocked`

Notes:
- Pattern syntax follows `.gitignore` semantics
- `.git` files are always excluded
- `include` narrows the candidate set
- `exclude` removes from the included set

### 5. Cross-check node

Functions:
- `sync_check()`
- `sync_classify_checks()`

Input:
- source path
- target path
- target config
- sync file list
- previous state hashes

Output:
- structured warnings/errors

Notes:
- Checks are grouped as C1-C8
- Errors block apply unless forced
- Warnings block `--strict`

### 6. Diff node

Functions:
- `sync_diff()`

Input:
- source path
- target path
- sync file list
- delete policy
- state hashes
- protect patterns

Output:
- action buckets: `A/M/D/U/P/!`

Notes:
- This is the core decision node
- It must not modify the filesystem
- `protect` prevents deletion, not sync

### 7. Apply node

Functions:
- `sync_apply()`
- `execute_sync()`

Input:
- action buckets
- source path
- target path
- hooks

Output:
- copied/deleted files
- new file hashes

Notes:
- This is the main side-effect boundary
- Pre-sync hooks run before file changes
- Post-sync hooks run after file changes

### 8. State node

Functions:
- `state_read()`
- `state_save_sync()`
- `state_save_run()`
- `state_check_gate()`

Input:
- current sync result
- last run state

Output:
- `.bare/sync-worktree.state.json`

Notes:
- State stores last sync hashes and last run summary
- `--apply` requires a successful prior dry-run unless forced

### 9. Report node

Functions:
- `report_target()`
- `report_diff()`
- `report_config_schema()`

Input:
- sync plan
- warnings
- mode

Output:
- human or JSON output

Notes:
- Reporting must not mutate state
- JSON output should stay stable for automation

### 10. CLI orchestration node

Function:
- `main()`

Input:
- argv

Output:
- program exit code

Notes:
- `main()` wires nodes together
- It should not own sync policy
- It should only choose which nodes run and in what order

## Core Data Objects

### `SyncPlan`

`prepare_sync()` returns `SyncPlan`, which bundles the resolved graph state:

- `source`
- `dest`
- `target`
- `sync_files`
- `excluded`
- `unmatched`
- `blocked`
- `mode_label`
- `actions`
- `check_results`
- `state_hashes`

`SyncPlan` is the main DAG edge bundle between planning, reporting, and applying.

## Test Layers

### Unit tests

Use when the node is pure or nearly pure:

- `filter_match()`
- `filter_pipeline()`
- `sync_diff()`
- `sync_check()`
- `sync_classify_checks()`

### Chain tests

Use when validating a small chain of nodes:

- `prepare_sync()`
- `execute_sync()`

### E2E tests

Use when validating CLI behavior:

- `main()`
- `init`
- `sync --apply`
- `sync --strict`
- `status`

## Invariants

- Sync candidates are derived from tracked files
- `.git/` is always excluded
- Pattern evaluation is config-driven
- Planning must stay side-effect free
- Apply must be gated by state
- Logging must not affect sync results

## Why Single-File Still Works

Single-file does not mean single blob.

The file stays copyable, while the runtime stays DAG-shaped:

- clear node boundaries
- explicit inputs and outputs
- side effects only at the edge
- testable intermediate nodes

That is the tradeoff that fits this tool.
