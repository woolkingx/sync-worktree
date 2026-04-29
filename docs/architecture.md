# Architecture

`sync-worktree` is a topology-aware file sync tool with a narrow core:

- detect the current Git topology
- load rule and setting config
- resolve source and target worktrees
- compute a sync plan
- validate the plan with policies
- apply or report the result

## Core Principles

- Keep the tool copyable and easy to inspect.
- Keep filesystem I/O at the boundary.
- Keep rule config immutable and setting config layered.
- Keep policy evaluation separate from execution.
- Keep the AI contract stable and machine-readable.

## System Flow

```text
topology
  -> config
  -> target resolution
  -> source scan
  -> filter
  -> plan
  -> policy validation
  -> report or apply
  -> context/result output
```

Each step consumes plain data from the previous step and returns plain data for the next step. That keeps the flow testable and keeps side effects isolated.

## Boundaries

### Topology

Topology detection decides whether the current project is running in bare, worktree, or repo mode. It resolves the Git internal directory and the current worktree path.

### Config

Rule config defines what is allowed. Setting config defines how the tool behaves.

- Rule config lives in Git internals
- Setting config is layered from defaults, user config, project config, worktree config, then CLI flags
- Rule config drives targets, roles, and policies

### Planning

Planning turns source files and target state into a stable `SyncPlan`.

- source tracked files are scanned from Git
- include and exclude patterns narrow the candidate set
- delete policy and protect patterns classify the target actions
- policies then decide whether the plan may run

### Execution

Execution is the only side-effect boundary.

- copy and delete happen after validation
- hooks run around execution
- state and context outputs are written after the action

### Reporting

Reporting is a pure output layer. It should not change repository state. The human CLI and JSON contract both read from the same plan and validation data.

## Core Data Objects

### `TopologyContext`

Carries the resolved Git mode, project root, Git internal directory, current worktree, and branch.

### `SyncPlan`

Carries the source, destination, selected files, excluded files, action buckets, and policy-relevant metadata.

### `PolicyContext`

Carries the plan plus Git state into the policy engine.

### `ValidationSummary`

Carries policy results and the aggregate pass/fail state.

## Testing

The main test layers are:

- unit tests for filter and diff logic
- contract tests for `inspect` and `apply`
- integration tests for CLI and end-to-end sync behavior

The rule of thumb is simple: if a step is pure, test it directly. If a step crosses I/O boundaries, test it through the command path.
