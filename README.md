# sync‑worktree

> Config‑driven file sync between Git worktrees

[![MIT License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.8+](https://img.shields.io/badge/python-3.8%2B-success.svg)](https://www.python.org)

## About

Bare‑repo + worktree workflows need a way to push a **subset** of files from `master/` to `release/` or `runner/`. `sync‑worktree` does that with one JSON config and one command — dry‑run by default, 8 safety checks, zero dependencies.

## Features

- Pipeline filter: `include` → `exclude` → `.git/` (composable, not mode‑based)
- 8 cross‑checks — dirty source, staged conflicts, target drift, stale patterns, etc.
- Protect patterns — never touch `node_modules/`, `.env`, target‑only files
- Git‑style status codes: `A` `M` `D` `U` `P` `!` `X`
- SHA‑256 state tracking with hash inheritance
- Pre/post sync hooks
- Three topologies auto‑detected: bare repo, worktree, plain repo
- Single file, zero dependencies beyond Python 3.8 + git

## Installation

```bash
# Just copy the script
cp sync_worktree.py /path/to/your/project/master/

# Or pip install
pip install sync-worktree
```

## Quick Start

```bash
python3 sync_worktree.py --init            # generate config
python3 sync_worktree.py release           # dry-run
python3 sync_worktree.py release --apply   # execute
```

Output:

```
[release] exclude:3
  A  added (6):
    + .gitignore
    + CHANGELOG.md
    + LICENSE
    + README.md
    + pyproject.toml
    + sync_worktree.py

  Summary: A:6 | X:8
```

## Configuration

Config lives in `.bare/sync-worktree.json` (bare) or `.git/sync-worktree.json` (repo). Not tracked.

```json
{
  "version": 1,
  "targets": {
    "release": {
      "source": "master",
      "exclude": ["tests/", "plan/", "__pycache__/"],
      "delete_policy": "tracked_only"
    },
    "runner": {
      "source": "master",
      "include": ["lib/**/*.mjs", "package.json"],
      "protect": ["node_modules/", ".gitignore"],
      "delete_policy": "unlisted",
      "post_sync": "pm2 restart app"
    }
  }
}
```

| Field | Description |
|-------|-------------|
| `source` | Source worktree or branch name |
| `include` | Keep only matching files (glob) |
| `exclude` | Remove matching files (glob) |
| `protect` | Never delete these in target |
| `delete_policy` | `never` / `unlisted` / `tracked_only` |
| `pre_sync` / `post_sync` | Shell commands before/after sync |

## Command Reference

| Option | Description |
|--------|-------------|
| `--apply` | Execute sync (default: dry‑run) |
| `--strict` | Exit 2 on any warning (CI mode) |
| `--force` | Override target modification errors |
| `--init` | Create default config |
| `--config` | Print resolved config |
| `--status` | Print last sync state |
| `--diff` | Show content diff for modified files |
| `-v` | Show unchanged / protected / excluded |
| `-q` | Summary line only |
| `--json` | Machine‑readable JSON output |
| `--version` | Print version |

## Status Codes

| Code | Meaning |
|------|---------|
| `A` | Added (new in target) |
| `M` | Modified (content changed) |
| `D` | Deleted (removed from target) |
| `U` | Unchanged (in sync) |
| `P` | Protected (would delete, but protected) |
| `!` | Missing source file |
| `X` | Excluded by pattern |

## Cross‑Checks

| # | Check | Severity |
|---|-------|----------|
| C1 | Source has uncommitted changes | warn |
| C2 | Include pattern matches 0 files | warn |
| C3 | Staged but uncommitted in sync set | warn |
| C4 | Untracked files match include | warn |
| C5 | Sync files are gitignored | warn |
| C6 | Target locally modified | error |
| C7 | Target branch diverged | warn |
| C8 | Exclude pattern matches 0 files | silent |

## Setup Target Worktree

```bash
# 1. Empty orphan worktree
git worktree add --detach release
cd release && git checkout --orphan release && git rm -rf .

# 2. Config, check, apply
cd ../master
python3 sync_worktree.py release           # dry-run
python3 sync_worktree.py release --apply   # file copy

# 3. Commit release
cd ../release
git add -A && git commit -m "v0.1.0"
```

## License

[MIT](LICENSE)
