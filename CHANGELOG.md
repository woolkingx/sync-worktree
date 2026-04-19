# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.4.5] - 2026-04-19

### Changed

- State writes now use a temp-file replace path
- `git` subprocess calls now have a default timeout
- Scope label logic is centralized again

### Fixed

- `git worktree list` failures now log debug output
- `config_resolve_paths` now guards against a missing current worktree

## [0.4.4] - 2026-04-19

### Changed

- Removed all backward-compat aliases
- Added a public `filter_match_batch()` API and routed sync planning through it
- Log filenames now use the correct scope for repo topology and include a run-start marker
- Logging scope is now consistent across sync/config/status/init/bootstrap commands

### Fixed

- Corrected repo topology scope label from parent directory name to repo directory name
- Eliminated sync-block calls to private filter helpers

## [0.4.3] - 2026-04-19

### Changed

- Log filenames now use a single daily file per scope and target: `<scope>-<target>-YYYYMMDD.log`
- Multi-target runs now use `<scope>-all-YYYYMMDD.log`
- Sync scope is derived from the project/worktree name, not the topology label

### Fixed

- Removed the last source of same-day log filename collisions

## [0.4.2] - 2026-04-19

### Changed

- Protected-path filtering now batches gitignore checks instead of spawning per-file subprocesses
- `sync` now names log files after the resolved target set again
- `SyncPlan` is plain data again; no Mapping protocol

### Fixed

- Removed the last `_gitignore_match_cached` path from the production code
- Restored exact filename and negative basename wildcard tests

## [0.4.1] - 2026-04-19

### Changed

- Batched gitignore matching to avoid per-file subprocess overhead
- Sync state is now saved before `post_sync` runs, so hook failures do not desync state
- `sync` startup now initializes logging before config load errors are reported
- CLI runtime exceptions now print a plain error instead of help text

### Fixed

- Removed dead `SyncPlan.as_dict()`
- Added traversal checks for target names
- Kept hook execution shell-free by using argument splitting

### Notes

- CLI remains subcommand-only; `--json` is intentionally removed
- New targets still default to excluding only `.gitignore`

## [0.4.0] - 2026-04-19

### Added

- `docs/architecture.md` — DAG-oriented architecture reference
- `SyncPlan` — structured planning node for unit and chain testing
- `tests/test_logging.py` — regression coverage for logging handler reuse

### Changed

- Refined the core flow into explicit DAG nodes for config, filter, diff, apply, state, and report
- Logging setup now closes existing handlers before reconfiguration
- Config scaffolding now uses fresh deep-copied defaults
- Worktree branch resolution preserves full branch names, including slashes
- Pattern matching remains `.gitignore`-style, with config examples and docs aligned to that behavior

### Fixed

- Prevented repeated logging handler accumulation
- Prevented temporary gitignore matcher repositories from persisting after process exit
- Fixed worktree resolution for branch names containing `/`

## [0.3.0] - 2026-04-18

### Added

- `--remove-target <name>` — remove worktree + branch + config entry + state cleanup
- `config_remove_target` API for programmatic target removal
- No-argument invocation shows help (terminal only)

### Changed

- **API blocks refactor** — codebase reorganized into 6 API blocks with consistent naming:
  - `git_*` — worktree/topology operations
  - `config_*` — CRUD + state manager (sole config/state file accessor)
  - `filter_*` — include/exclude pipeline
  - `sync_*` — diff, check, apply
  - `state_*` — persistence
  - `report_*` — output formatting
- Config block is now the unified state manager — all config/state mutations go through `config_*` and `state_*` APIs
- `resolve_target` split into `config_get_target` (pure merge) + `config_resolve_paths` (git query)
- Backward compatibility aliases retained for all renamed functions

## [0.2.0] - 2026-04-17

### Added

- `--init-bare <url>` — clone remote as bare repo + master worktree in one command
- `--add-target <name>` — create empty orphan worktree + add to config
- `--migrate` — convert existing .git repo to bare + worktree structure
- `last_run` state tracking — records every invocation (mode, checks, summary)
- Dry-run safety gate — `--apply` blocked without prior successful dry-run
- File logging to `logs/<topology>-<target>-<datetime>.log`
- `--log-dir` to override log directory, `--no-log` to disable
- `--help-config` — print config schema reference with field docs
- Workflow-based CLI help (phases 0-5 + CI)

### Changed

- README rewritten: AI-agent-first documentation, workflow-centric
- CLI epilog restructured as numbered workflow phases

## [0.1.0] - 2026-04-17

### Added

- Config-driven worktree sync with pipeline filter (`include → exclude → .git/`)
- Three topology auto-detection: bare repo, worktree, plain repo
- Git-style status codes: A (added), M (modified), D (deleted), U (unchanged), P (protected), ! (missing), X (excluded)
- Delete policies: `never`, `unlisted`, `tracked_only`
- Protect patterns: prevent deletion of target-only files
- 8 cross-checks (C1-C8): dirty source, include/exclude 0-match, staged uncommitted, untracked match, gitignored match, target local modifications, target divergence
- Structured severity levels: error > warn > silent
- State tracking with SHA256 file hashes and commit refs
- Hash inheritance from previous state (skip re-hashing unchanged files)
- `--apply` / dry-run default
- `--init` auto-detect worktrees and create config
- `--config` / `--status` info commands
- `--strict` (exit 2 on warnings) / `--force` (override C6 errors)
- `--diff` content preview for modified files
- `-v` verbose / `-q` quiet / `--json` output modes
- `--version` flag
- Pre/post sync hook execution (`pre_sync`, `post_sync`)
- Project files: pyproject.toml, LICENSE (MIT), .gitignore
