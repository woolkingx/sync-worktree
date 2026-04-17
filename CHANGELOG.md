# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
