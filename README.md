# sync-worktree

Config-driven file sync between Git worktrees. Single file, zero dependencies.

## Why

AI agents can use this to publish subsets of a repo without memorizing git incantations.

Architecture reference: [docs/architecture.md](docs/architecture.md)

## For AI Agents

`--help` is the contract. Use it first.

```
python3 sync_worktree.py --help
```

Minimal flow:

```bash
# setup
sync_worktree.py init-bare <url>
cd master
sync_worktree.py add-target release

# config
sync_worktree.py init
sync_worktree.py help-config
sync_worktree.py config

# preview
sync_worktree.py sync release
sync_worktree.py sync release -v
sync_worktree.py sync release --diff

# apply
sync_worktree.py sync release --apply

# verify
sync_worktree.py status
```

## Config

`.bare/sync-worktree.json`
New targets default-exclude only `.gitignore`.

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
      "exclude": [".gitignore"],
      "delete_policy": "unlisted",
      "post_sync": "pm2 restart app"
    }
  }
}
```

Full schema: `sync_worktree.py help-config`

## Status Codes

`A` added · `M` modified · `D` deleted · `U` unchanged · `P` protected · `!` missing source · `X` excluded

## Install

```bash
cp sync_worktree.py /path/to/project/master/
```

Python 3.8+, git. No pip dependencies.

## License

[MIT](LICENSE)
