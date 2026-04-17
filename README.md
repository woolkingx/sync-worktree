# sync-worktree

Config-driven file sync between Git worktrees. Single file, zero dependencies, AI-native.

## Why

AI agents (Claude Code, Cursor, Copilot) work in git repos but can't safely publish subsets of files to production branches. Manual `cp`, `rsync`, or `git checkout` breaks state tracking, skips safety checks, and doesn't compose with config.

sync-worktree solves this: one JSON config, one command, deterministic pipeline. The agent runs `--help`, follows the workflow, and handles the entire bare-repo lifecycle without memorizing git incantations.

## For AI Agents

`--help` is the complete interface contract. Every operation the agent needs is a flag:

```
python3 sync_worktree.py --help
```

The workflow section in `--help` covers the full lifecycle:
- Phase 0: repo setup (`--init-bare`, `--add-target`, `--migrate`)
- Phase 1-4: config → preview → execute → verify
- CI mode: `--strict --json` for structured output

Schema reference: `--help-config`. Machine output: `--json`. State query: `--status`.

No implicit behavior. Dry-run by default. `--apply` requires prior dry-run unless `--force`.

## Workflow

```bash
# 0. New project from remote
sync_worktree.py --init-bare <url>
cd master
sync_worktree.py --add-target release

# 1. Configure
sync_worktree.py --init              # auto-detect worktrees, create config
sync_worktree.py --help-config       # config schema reference
sync_worktree.py --config            # verify resolved config

# 2. Preview
sync_worktree.py release             # dry-run
sync_worktree.py release -v          # include unchanged/excluded/protected
sync_worktree.py release --diff      # file content diffs

# 3. Execute
sync_worktree.py release --apply     # sync (requires prior dry-run)

# 4. Verify
sync_worktree.py --status            # last sync state
```

## Structure

```
project/
  .bare/                          bare repo (git objects only)
  .bare/sync-worktree.json       sync config (not tracked)
  .bare/sync-worktree.state.json sync state (auto-generated)
  .git                            gitdir: ./.bare
  master/                         development (all files, edit here)
  release/                        publish (exclude tests/plan)
  runner/                         production (include runtime only)
```

Targets start as empty orphan branches. All edits happen in master/. sync-worktree copies the configured subset out.

## Config

`.bare/sync-worktree.json` — pipeline: `include` → `exclude` → `.git/`

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
      "protect": ["node_modules/"],
      "delete_policy": "unlisted",
      "post_sync": "pm2 restart app"
    }
  }
}
```

Full schema: `sync_worktree.py --help-config`

## Safety

| Layer | Mechanism |
|-------|-----------|
| Dry-run gate | `--apply` blocked without prior dry-run |
| 8 cross-checks | C1-C8: dirty source, staged conflicts, target drift, stale patterns |
| Protect patterns | Never delete `node_modules/`, `.env`, target-only files |
| State hashes | SHA-256 per file, detect target tampering (C6) |
| Strict mode | `--strict` exits 2 on any warning, for CI |
| Logging | Every run logged to `logs/<topology>-<target>-<datetime>.log` |

## Status Codes

`A` added · `M` modified · `D` deleted · `U` unchanged · `P` protected · `!` missing source · `X` excluded

## Install

```bash
cp sync_worktree.py /path/to/project/master/
```

Python 3.8+, git. No pip dependencies.

## License

[MIT](LICENSE)
