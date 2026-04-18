# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
