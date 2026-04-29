# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.5.9] - 2026-04-29

### Fixed

- AI context now reports persisted `last_sync` metadata and per-worktree divergence counts.
- `apply --dry-run` now validates against live context instead of returning a summary stub.
- Policy validation now collects the full failed-check list instead of stopping at the first error.

### Changed

- Added `config show/schema` and `target add/remove` commands to the main CLI.
- Strengthened auto-fix command checks and extended decision hashes to 128 bits.

## [0.5.8] - 2026-04-29

### Fixed

- Sync state updates now degrade to a warning if the bare metadata area is read-only.

### Changed

- Aligned legacy `.bare/sync-worktree.json` with the bundle runtime deployment model.

## [0.5.7] - 2026-04-29

### Fixed

- `sync` now writes `sync-worktree.state.json` after successful deployment so planner state hashes stay current.
