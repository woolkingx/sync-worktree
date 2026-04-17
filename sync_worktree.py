#!/usr/bin/env python3
"""
sync_worktree.py — Config-driven worktree sync.

One config file in .git/ internal dir. Pipeline filter: include → exclude.
Three topologies: bare repo, normal repo + worktrees, plain repo.
Dry-run by default. --apply to execute.

Config location (auto-detected):
  bare:      .bare/sync-worktree.json
  worktree:  <git-common-dir>/sync-worktree.json
  repo:      .git/sync-worktree.json

Usage:
    python3 sync_worktree.py                     # dry-run all targets
    python3 sync_worktree.py runner              # dry-run specific target
    python3 sync_worktree.py --apply             # execute all targets
    python3 sync_worktree.py runner --apply      # execute specific target
    python3 sync_worktree.py --init              # create default config
    python3 sync_worktree.py --config            # print resolved config
    python3 sync_worktree.py --status            # print last sync state
    python3 sync_worktree.py --strict            # abort on any warning (CI)
    python3 sync_worktree.py --json              # JSON output
"""

import argparse
import filecmp
import fnmatch
import hashlib
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

__version__ = "0.1.0"


# ── Git helpers ──────────────────────────────────────────────────────────────

def _git(*args, cwd=None):
    """Run git command, return stdout. Raises on failure."""
    result = subprocess.run(
        ["git"] + list(args),
        cwd=cwd, capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _git_ok(*args, cwd=None):
    """Run git command, return (success, stdout)."""
    result = subprocess.run(
        ["git"] + list(args),
        cwd=cwd, capture_output=True, text=True,
    )
    return result.returncode == 0, result.stdout.strip()


# ── Topology detection ───────────────────────────────────────────────────────

def detect_topology():
    """Detect git topology and return (topology, git_internal_dir, cwd_worktree_path).

    topology: 'bare' | 'worktree' | 'repo'
    git_internal_dir: Path to store config (.bare/, .git/, or git common dir)
    """
    try:
        git_common = Path(_git("rev-parse", "--git-common-dir")).resolve()
        git_dir = Path(_git("rev-parse", "--git-dir")).resolve()
    except RuntimeError:
        print("Error: not inside a git repository", file=sys.stderr)
        sys.exit(1)

    # Bare repo: git_common usually ends with .bare or is the bare dir itself
    is_bare_ok, is_bare = _git_ok("rev-parse", "--is-bare-repository")
    if is_bare == "true":
        return "bare", git_common, Path.cwd().resolve()

    # Check for worktrees
    worktrees = parse_worktrees(git_common)
    if len(worktrees) > 1:
        # Find which worktree we're in
        cwd = Path.cwd().resolve()
        for wt in worktrees:
            try:
                cwd.relative_to(wt["path"])
                return "worktree" if git_common != git_dir else "bare", git_common, wt["path"]
            except ValueError:
                continue
        return "bare" if str(git_common).endswith(".bare") else "worktree", git_common, cwd

    return "repo", git_common, Path.cwd().resolve()


def parse_worktrees(git_common):
    """Parse `git worktree list --porcelain` into structured data."""
    result = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=git_common, capture_output=True, text=True,
    )
    if result.returncode != 0:
        return []

    worktrees = []
    current = {}
    for line in result.stdout.splitlines():
        if not line:
            if current and not current.get("bare"):
                worktrees.append(current)
            current = {}
        elif line.startswith("worktree "):
            current["path"] = Path(line.split(" ", 1)[1])
        elif line == "bare":
            current["bare"] = True
        elif line.startswith("branch "):
            current["branch"] = line.split(" ", 1)[1].split("/")[-1]
    if current and not current.get("bare"):
        worktrees.append(current)
    return worktrees


def resolve_worktree_path(name, git_common):
    """Resolve worktree name to its filesystem path."""
    worktrees = parse_worktrees(git_common)
    for wt in worktrees:
        if wt.get("branch") == name or wt["path"].name == name:
            return wt["path"]
    return None


# ── Config ───────────────────────────────────────────────────────────────────

CONFIG_NAME = "sync-worktree.json"
STATE_NAME = "sync-worktree.state.json"
CONFIG_VERSION = 1

DEFAULT_CONFIG = {
    "version": CONFIG_VERSION,
    "defaults": {
        "delete_policy": "never",
        "follow_symlinks": False,
        "warn_untracked": True,
        "warn_dirty": True,
        "warn_no_match": True,
    },
    "targets": {},
}

DEFAULT_EXCLUDE = [
    "tests/",
    "test/",
    "plan/",
    ".backup/",
    ".cleanup/",
    "*.log",
    "__pycache__/",
    ".claude/",
]

DEFAULT_TARGET = {
    "delete_policy": "never",
}


def config_path(git_internal):
    return git_internal / CONFIG_NAME


def state_path(git_internal):
    return git_internal / STATE_NAME


def load_config(git_internal):
    """Load config from git internal dir. Returns (config, warnings)."""
    path = config_path(git_internal)
    if not path.exists():
        return None, [f"Config not found: {path}"]

    try:
        config = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        return None, [f"Config parse error: {e}"]

    if config.get("version") != CONFIG_VERSION:
        return None, [f"Config version mismatch: expected {CONFIG_VERSION}, got {config.get('version')}"]

    return config, []


def resolve_target(config, target_name, git_internal, topology, cwd_worktree):
    """Resolve a target config into concrete source/dest paths.

    Returns (source_path, dest_path, target_config, errors).
    """
    targets = config.get("targets", {})
    if target_name not in targets:
        return None, None, None, [f"Unknown target: {target_name}"]

    defaults = config.get("defaults", {})
    target = {**DEFAULT_TARGET, **defaults, **targets[target_name]}

    errors = []

    # Resolve source
    source_name = target.get("source")
    if source_name:
        source_path = resolve_worktree_path(source_name, git_internal)
        if not source_path:
            errors.append(f"Source worktree not found: {source_name}")
            return None, None, target, errors
    else:
        source_path = cwd_worktree

    # Resolve dest
    if "dest" in target:
        dest_path = Path(target["dest"])
        if not dest_path.is_absolute():
            dest_path = source_path.parent / dest_path
    else:
        dest_path = resolve_worktree_path(target_name, git_internal)
        if not dest_path:
            errors.append(f"Target worktree not found: {target_name}. Use 'dest' field for external directories.")
            return None, None, target, errors

    if source_path == dest_path:
        errors.append(f"Source and target are the same: {source_path}")
        return None, None, target, errors

    return source_path, dest_path, target, errors


def init_config(git_internal, topology, git_common):
    """Create default config. Auto-detect worktrees as targets."""
    path = config_path(git_internal)
    if path.exists():
        print(f"  Config already exists: {path}")
        return False

    config = dict(DEFAULT_CONFIG)
    config["targets"] = {}

    # Auto-detect worktrees as potential targets
    worktrees = parse_worktrees(git_common)
    cwd = Path.cwd().resolve()

    for wt in worktrees:
        name = wt.get("branch", wt["path"].name)
        # Skip the current worktree (source)
        try:
            cwd.relative_to(wt["path"])
            continue
        except ValueError:
            pass

        if name in ("release", "public", "main"):
            config["targets"][name] = {
                "source": "master",
                "exclude": list(DEFAULT_EXCLUDE),
                "delete_policy": "tracked_only",
            }
        elif name == "runner":
            config["targets"][name] = {
                "source": "master",
                "include": ["lib/**/*.mjs", "src/**/*.mjs", "package.json"],
                "exclude": ["tests/", "test/", "plan/"],
                "protect": ["node_modules/", ".gitignore"],
                "delete_policy": "unlisted",
            }
        else:
            config["targets"][name] = {
                "source": "master",
                "exclude": list(DEFAULT_EXCLUDE),
            }

    path.write_text(json.dumps(config, indent=2) + "\n")
    print(f"  Created: {path}")
    print(f"  Targets: {', '.join(config['targets'].keys()) or '(none — add manually)'}")
    return True


# ── File operations ──────────────────────────────────────────────────────────

def get_tracked_files(worktree_path):
    """Get git-tracked files from a worktree."""
    try:
        result = _git("ls-files", cwd=worktree_path)
    except RuntimeError:
        return []
    if not result:
        return []
    return sorted(Path(line) for line in result.split("\n") if line)


def _match_any(filepath, patterns):
    """Check if filepath matches any of the glob patterns."""
    s = str(filepath)
    for pat in patterns:
        if fnmatch.fnmatch(s, pat):
            return True
        # Recursive glob: "lib/**/*.mjs" should also match "lib/core.mjs" (zero dirs)
        if "**/" in pat and fnmatch.fnmatch(s, pat.replace("**/", "")):
            return True
        # Directory prefix: "tests/" matches "tests/foo.mjs"
        if pat.endswith("/") and s.startswith(pat.rstrip("/")):
            return True
        # Basename match: "*.log" matches "deep/dir/app.log"
        if fnmatch.fnmatch(filepath.name, pat):
            return True
    return False


def filter_pipeline(files, include_patterns=None, exclude_patterns=None):
    """Filter files through include → exclude pipeline.

    Pipeline:
      1. git ls-files → all tracked files (input)
      2. include (if defined) → keep only matching files
      3. exclude (if defined) → remove matching files
      4. .git/ → always excluded (hardcoded)

    Returns (sync_files, excluded_files).
    """
    # Step 1: start with all files
    pool = list(files)

    # Step 2: include filter (narrow down)
    if include_patterns:
        pool = [f for f in pool if _match_any(f, include_patterns)]

    # Step 3: exclude filter (remove)
    if exclude_patterns:
        pool = [f for f in pool if not _match_any(f, exclude_patterns)]

    # Step 4: .git/ hardcoded exclusion
    pool = [f for f in pool if ".git" not in f.parts]

    sync_set = set(pool)
    excluded = [f for f in files if f not in sync_set]

    return sorted(pool), excluded


def file_hash(filepath):
    """SHA256 hash of file content."""
    h = hashlib.sha256()
    with open(filepath, "rb") as fh:
        while True:
            chunk = fh.read(65536)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def compute_actions(source, dest, sync_files, delete_policy, state_hashes=None, protect=None):
    """Compare source vs dest. Returns dict of categorized file actions.

    Status codes (git-style):
      A  = added (new file in target)
      M  = modified (content changed)
      D  = deleted (removed from target)
      U  = unchanged (in sync)
      P  = protected (would be deleted but protected)
      !  = missing source (in sync list but source file gone)

    Returns: { 'A': [...], 'M': [...], 'D': [...], 'U': [...], 'P': [...], '!': [...] }
    """
    actions = {'A': [], 'M': [], 'D': [], 'U': [], 'P': [], '!': []}
    should_exist = set(sync_files)

    for rel in sync_files:
        src = source / rel
        dst = dest / rel
        if not src.exists():
            actions['!'].append(rel)
        elif not dst.exists():
            actions['A'].append(rel)
        elif not filecmp.cmp(src, dst, shallow=False):
            actions['M'].append(rel)
        else:
            actions['U'].append(rel)

    # Delete policy
    if delete_policy != "never":
        dest_files = set()
        for p in dest.rglob("*"):
            if p.is_file() and ".git" not in p.parts:
                dest_files.add(p.relative_to(dest))

        if delete_policy == "unlisted":
            candidates = dest_files - should_exist
        elif delete_policy == "tracked_only" and state_hashes:
            tracked = set(Path(k) for k in state_hashes.keys())
            candidates = (tracked & dest_files) - should_exist
        else:
            candidates = set()

        # Split into delete vs protected
        if protect and candidates:
            for f in sorted(candidates):
                if _match_any(f, protect):
                    actions['P'].append(f)
                else:
                    actions['D'].append(f)
        else:
            actions['D'] = sorted(candidates)

    return actions


def apply_actions(source, dest, add, update, delete):
    """Execute sync actions. Returns file_hashes for state."""
    hashes = {}

    for rel in add + update:
        src = source / rel
        dst = dest / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        hashes[str(rel)] = file_hash(src)

    for rel in delete:
        dst = dest / rel
        if dst.exists():
            dst.unlink()
        # Clean empty parent dirs
        parent = dst.parent
        while parent != dest and parent.exists():
            try:
                next(parent.iterdir())
                break  # not empty
            except StopIteration:
                parent.rmdir()
                parent = parent.parent

    return hashes


# ── Cross-checks ─────────────────────────────────────────────────────────────

def cross_check(source, dest, target_config, sync_files, excluded_files,
                 state_hashes=None, git_internal=None):
    """L0 (git) x L1 (config) cross-check. Returns list of (severity, message).

    Checks:
      C1  Source dirty (uncommitted)             warn
      C2  Include pattern 0 match                warn
      C3  Source staged but uncommitted           warn
      C4  Include hits untracked file             warn
      C5  Include hits gitignored file            warn
      C6  Target has local modifications          error
      C7  Target worktree diverged                warn
      C8  Exclude pattern 0 match                 silent (logged verbose only)
    """
    warnings = []
    sync_set = set(str(f) for f in sync_files)

    # C1: Source dirty (uncommitted changes)
    if target_config.get("warn_dirty", True):
        ok, status = _git_ok("status", "--porcelain", cwd=source)
        if ok and status:
            dirty_count = len(status.strip().split("\n"))
            warnings.append(("warn", f"C1: Source has {dirty_count} uncommitted change(s)"))

    # C2: Include pattern matches 0 files
    if target_config.get("include") and target_config.get("warn_no_match", True):
        all_files = sync_files + excluded_files
        for pat in target_config["include"]:
            matched = any(_match_any(f, [pat]) for f in all_files)
            if not matched:
                warnings.append(("warn", f"C2: Include pattern matches 0 files: {pat}"))

    # C3: Source has staged but uncommitted changes that overlap sync files
    ok, staged_out = _git_ok("diff", "--cached", "--name-only", cwd=source)
    if ok and staged_out:
        staged = set(staged_out.strip().split("\n"))
        overlap = staged & sync_set
        if overlap:
            warnings.append(("warn", f"C3: {len(overlap)} staged-but-uncommitted file(s) in sync set: {', '.join(sorted(overlap)[:5])}"))

    # C4: Include patterns match untracked files (won't be synced)
    if target_config.get("include"):
        ok, untracked_out = _git_ok(
            "ls-files", "--others", "--exclude-standard", cwd=source
        )
        if ok and untracked_out:
            untracked = [Path(f) for f in untracked_out.strip().split("\n") if f]
            hits = [f for f in untracked if _match_any(f, target_config["include"])]
            if hits:
                names = [str(f) for f in hits[:5]]
                warnings.append(("warn", f"C4: {len(hits)} untracked file(s) match include patterns: {', '.join(names)}"))

    # C5: Include patterns match gitignored files
    if target_config.get("include") and sync_files:
        try:
            result = subprocess.run(
                ["git", "check-ignore", "--stdin"],
                input="\n".join(str(f) for f in sync_files),
                cwd=source, capture_output=True, text=True,
            )
            if result.stdout.strip():
                ignored = result.stdout.strip().split("\n")
                warnings.append(("warn", f"C5: {len(ignored)} sync file(s) are gitignored: {', '.join(ignored[:5])}"))
        except (OSError, RuntimeError):
            pass  # git check-ignore not available

    # C6: Target has local modifications (full state hash comparison)
    if dest and dest.exists() and state_hashes:
        modified = []
        for rel_str, expected_hash in state_hashes.items():
            dst = dest / rel_str
            if dst.exists():
                actual = file_hash(dst)
                if actual != expected_hash:
                    modified.append(rel_str)
        if modified:
            names = modified[:5]
            warnings.append(("error", f"C6: Target has {len(modified)} locally modified file(s): {', '.join(names)}"))

    # C7: Target worktree has diverged (commits in target not in source)
    if git_internal:
        source_branch = target_config.get("source", "master")
        target_name = target_config.get("_name", "")
        if target_name:
            ok, log_out = _git_ok(
                "log", f"{source_branch}..{target_name}", "--oneline",
                cwd=git_internal,
            )
            if ok and log_out:
                diverged = len(log_out.strip().split("\n"))
                warnings.append(("warn", f"C7: Target '{target_name}' has {diverged} commit(s) not in '{source_branch}'"))

    # C8: Exclude pattern matches 0 files (may indicate stale pattern)
    if target_config.get("exclude") and target_config.get("warn_no_match", True):
        all_files = sync_files + excluded_files
        for pat in target_config["exclude"]:
            matched = any(_match_any(f, [pat]) for f in all_files)
            if not matched:
                warnings.append(("silent", f"C8: Exclude pattern matches 0 files: {pat}"))

    return warnings


# ── State management ─────────────────────────────────────────────────────────

def load_state(git_internal):
    """Load sync state file."""
    path = state_path(git_internal)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return {}


def save_state(git_internal, target_name, source, file_hashes, add_count, update_count, delete_count):
    """Save sync state for a target."""
    state = load_state(git_internal)

    # Get source commit
    try:
        commit = _git("rev-parse", "--short", "HEAD", cwd=source)
    except RuntimeError:
        commit = "unknown"

    if "last_sync" not in state:
        state["last_sync"] = {}

    state["last_sync"][target_name] = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source_commit": commit,
        "files_synced": add_count + update_count,
        "files_deleted": delete_count,
        "file_hashes": file_hashes,
    }

    path = state_path(git_internal)
    path.write_text(json.dumps(state, indent=2) + "\n")


# ── Output ───────────────────────────────────────────────────────────────────

STATUS_LABELS = {
    'A': ('A  added',      '+'),
    'M': ('M  modified',   '~'),
    'D': ('D  deleted',    '-'),
    'U': ('U  unchanged',  '='),
    'P': ('P  protected',  '#'),
    '!': ('!  missing src', '?'),
}


def print_report(target_name, actions, warnings, excluded, mode_label,
                 verbose=False, as_json=False):
    """Print sync report for one target."""
    changes = len(actions['A']) + len(actions['M']) + len(actions['D'])

    if as_json:
        print(json.dumps({
            "target": target_name,
            "mode": mode_label,
            "actions": {k: [str(f) for f in v] for k, v in actions.items()},
            "warnings": warnings,
            "summary": {k: len(v) for k, v in actions.items()},
            "changes": changes,
        }))
        return

    print(f"\n  [{target_name}] {mode_label}")

    for w in warnings:
        print(f"    WARN: {w}")

    # Action groups — always show A/M/D, show U/P/! only with -v
    for code in ('A', 'M', 'D'):
        files = actions[code]
        if not files:
            continue
        label, prefix = STATUS_LABELS[code]
        print(f"\n    {label} ({len(files)}):")
        for f in files:
            print(f"      {prefix} {f}")

    if verbose:
        for code in ('U', 'P', '!'):
            files = actions[code]
            if not files:
                continue
            label, prefix = STATUS_LABELS[code]
            print(f"\n    {label} ({len(files)}):")
            for f in files:
                print(f"      {prefix} {f}")

        if excluded:
            print(f"\n    X  excluded ({len(excluded)}):")
            for f in excluded:
                print(f"      x {f}")

    # Summary line
    if changes == 0 and not warnings:
        print("    Already in sync.")
    else:
        parts = []
        for code, (label, _) in STATUS_LABELS.items():
            n = len(actions[code])
            if n > 0:
                parts.append(f"{code}:{n}")
        if excluded:
            parts.append(f"X:{len(excluded)}")
        print(f"\n    Summary: {' | '.join(parts)}")


def print_diff(source, dest, update_files):
    """Show brief diff for updated files."""
    if not update_files:
        return
    print("\n    DIFF:")
    for rel in update_files[:10]:
        result = subprocess.run(
            ["diff", "--brief", str(source / rel), str(dest / rel)],
            capture_output=True, text=True,
        )
        if result.stdout.strip():
            print(f"      {rel}")
    if len(update_files) > 10:
        print(f"      ... and {len(update_files) - 10} more")


# ── Sync one target ─────────────────────────────────────────────────────────

def prepare_sync(config, target_name, git_internal, topology, cwd_worktree):
    """Pure computation: resolve target, filter files, cross-check, compute actions.

    Returns dict with all computed data, or None + errors on failure.
    No I/O beyond git queries (read-only).
    """
    source, dest, target, errors = resolve_target(
        config, target_name, git_internal, topology, cwd_worktree
    )
    if errors:
        return None, errors

    source_files = get_tracked_files(source)
    include_patterns = target.get("include")
    exclude_patterns = target.get("exclude")
    sync_files, excluded = filter_pipeline(source_files, include_patterns, exclude_patterns)

    parts = []
    if include_patterns:
        parts.append(f"include:{len(include_patterns)}")
    if exclude_patterns:
        parts.append(f"exclude:{len(exclude_patterns)}")
    mode_label = " + ".join(parts) if parts else "all files"

    state = load_state(git_internal)
    state_hashes = state.get("last_sync", {}).get(target_name, {}).get("file_hashes", {})

    target["_name"] = target_name
    check_results = cross_check(source, dest, target, sync_files, excluded,
                                state_hashes=state_hashes, git_internal=git_internal)

    delete_policy = target.get("delete_policy", "never")
    protect = target.get("protect", [])
    actions = compute_actions(source, dest, sync_files, delete_policy, state_hashes, protect)

    return {
        "source": source, "dest": dest, "target": target,
        "sync_files": sync_files, "excluded": excluded,
        "mode_label": mode_label, "actions": actions,
        "check_results": check_results, "state_hashes": state_hashes,
    }, []


def classify_checks(check_results, force=False, verbose=False):
    """Split check results into warnings/errors/silent lists.

    --force downgrades errors to warnings. --verbose includes silent.
    """
    warnings = [msg for sev, msg in check_results if sev == "warn"]
    errors = [msg for sev, msg in check_results if sev == "error"]
    silent = [msg for sev, msg in check_results if sev == "silent"]

    if force and errors:
        warnings = warnings + errors
        errors = []
    if verbose and silent:
        warnings = warnings + silent

    return warnings, errors


def execute_sync(source, dest, actions, sync_files, state_hashes,
                 target, target_name, git_internal):
    """I/O boundary: copy/delete files, run hooks, save state.

    Returns (success, file_hashes).
    """
    add, update, delete = actions['A'], actions['M'], actions['D']
    total = len(add) + len(update) + len(delete)
    if total == 0:
        return True, {}

    pre_sync = target.get("pre_sync")
    if pre_sync:
        print(f"\n    Running pre_sync: {pre_sync}")
        result = subprocess.run(pre_sync, shell=True, cwd=str(source))
        if result.returncode != 0:
            print(f"  ERROR: pre_sync failed (exit {result.returncode})", file=sys.stderr)
            return False, {}

    file_hashes = apply_actions(source, dest, add, update, delete)

    for rel in sync_files:
        rel_str = str(rel)
        if rel_str not in file_hashes:
            if rel_str in state_hashes:
                file_hashes[rel_str] = state_hashes[rel_str]
            else:
                src = source / rel
                if src.exists():
                    file_hashes[rel_str] = file_hash(src)

    save_state(git_internal, target_name, source, file_hashes,
               len(add), len(update), len(delete))

    post_sync = target.get("post_sync")
    if post_sync:
        print(f"\n    Running post_sync: {post_sync}")
        subprocess.run(post_sync, shell=True, cwd=str(dest))

    return True, file_hashes


def sync_target(config, target_name, git_internal, topology, cwd_worktree,
                apply=False, strict=False, force=False, verbose=False,
                show_diff=False, as_json=False):
    """Orchestrate one target sync. Returns (success, has_warnings)."""
    prepared, errors = prepare_sync(
        config, target_name, git_internal, topology, cwd_worktree
    )
    if errors:
        for e in errors:
            print(f"  ERROR: {e}", file=sys.stderr)
        return False, False

    source = prepared["source"]
    dest = prepared["dest"]
    actions = prepared["actions"]
    warnings, errors = classify_checks(
        prepared["check_results"], force=force, verbose=verbose
    )

    if errors:
        for e in errors:
            print(f"  ERROR: {e}", file=sys.stderr)
        print(f"  Use --force to override.", file=sys.stderr)
        return False, True

    if not as_json:
        print(f"\n  Source: {source}")
        print(f"  Target: {dest}")

    print_report(target_name, actions, warnings, prepared["excluded"],
                 prepared["mode_label"], verbose=verbose, as_json=as_json)

    if show_diff:
        print_diff(source, dest, actions['M'])

    has_warnings = len(warnings) > 0

    if strict and has_warnings:
        print(f"\n  ABORT: --strict mode, {len(warnings)} warning(s)", file=sys.stderr)
        return True, True

    if apply:
        ok, _ = execute_sync(
            source, dest, actions, prepared["sync_files"],
            prepared["state_hashes"], prepared["target"],
            target_name, git_internal,
        )
        if not ok:
            return False, has_warnings
        add, update, delete = actions['A'], actions['M'], actions['D']
        if not as_json and (len(add) + len(update) + len(delete)) > 0:
            print(f"\n    Done. A:{len(add)} M:{len(update)} D:{len(delete)}")
    elif not as_json:
        add, update, delete = actions['A'], actions['M'], actions['D']
        if (len(add) + len(update) + len(delete)) > 0:
            print("\n    Run with --apply to execute.")

    return True, has_warnings


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Config-driven worktree sync. Dry-run by default."
    )
    parser.add_argument(
        "targets", nargs="*", default=[],
        help="Target name(s) from config. Default: all targets."
    )
    parser.add_argument("--apply", action="store_true", help="Execute sync (default: dry-run)")
    parser.add_argument("--strict", action="store_true", help="Abort on any warning (CI mode)")
    parser.add_argument("--force", action="store_true", help="Overwrite target local modifications")
    parser.add_argument("--init", action="store_true", help="Create default config")
    parser.add_argument("--config", action="store_true", help="Print resolved config and exit")
    parser.add_argument("--status", action="store_true", help="Print last sync state and exit")
    parser.add_argument("--diff", action="store_true", help="Show content diff for updates")
    parser.add_argument("-v", "--verbose", action="store_true", help="Show filtered/skipped files")
    parser.add_argument("-q", "--quiet", action="store_true", help="Summary only")
    parser.add_argument("--json", action="store_true", help="JSON output (CI)")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args()

    # Detect topology
    topology, git_internal, cwd_worktree = detect_topology()

    if not args.quiet and not args.json:
        print(f"Topology: {topology}")
        print(f"Config: {config_path(git_internal)}")

    # --init
    if args.init:
        init_config(git_internal, topology, git_internal)
        return

    # Load config
    config, config_errors = load_config(git_internal)
    if config is None:
        for e in config_errors:
            print(f"Error: {e}", file=sys.stderr)
        print(f"\nRun with --init to create default config.", file=sys.stderr)
        sys.exit(1)

    # --config
    if args.config:
        print(json.dumps(config, indent=2))
        return

    # --status
    if args.status:
        state = load_state(git_internal)
        print(json.dumps(state, indent=2))
        return

    # Determine which targets to sync
    target_names = args.targets or list(config.get("targets", {}).keys())

    if not target_names:
        print("No targets defined in config.", file=sys.stderr)
        sys.exit(1)

    if not args.quiet and not args.json:
        run_mode = "APPLY" if args.apply else "DRY RUN"
        print(f"Mode: {run_mode}")
        print(f"Targets: {', '.join(target_names)}")

    # Sync each target
    all_ok = True
    any_warnings = False

    for name in target_names:
        ok, has_warn = sync_target(
            config, name, git_internal, topology, cwd_worktree,
            apply=args.apply, strict=args.strict, force=args.force,
            verbose=args.verbose, show_diff=args.diff, as_json=args.json,
        )
        if not ok:
            all_ok = False
        if has_warn:
            any_warnings = True

    # Exit code
    if not all_ok:
        sys.exit(1)
    elif args.strict and any_warnings:
        sys.exit(2)


if __name__ == "__main__":
    main()
