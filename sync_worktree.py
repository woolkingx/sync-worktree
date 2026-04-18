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
import logging
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

__version__ = "0.3.0"


# == Git Block ================================================================

def git_run(*args, cwd=None):
    """Run git command, return stdout. Raises on failure."""
    result = subprocess.run(
        ["git"] + list(args),
        cwd=cwd, capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def git_try(*args, cwd=None):
    """Run git command, return (success, stdout)."""
    result = subprocess.run(
        ["git"] + list(args),
        cwd=cwd, capture_output=True, text=True,
    )
    return result.returncode == 0, result.stdout.strip()


def git_detect_topology():
    """Detect git topology and return (topology, git_internal_dir, cwd_worktree_path).

    topology: 'bare' | 'worktree' | 'repo'
    git_internal_dir: Path to store config (.bare/, .git/, or git common dir)
    """
    try:
        git_common = Path(git_run("rev-parse", "--git-common-dir")).resolve()
        git_dir = Path(git_run("rev-parse", "--git-dir")).resolve()
    except RuntimeError:
        print("Error: not inside a git repository", file=sys.stderr)
        sys.exit(1)

    # Bare repo: git_common usually ends with .bare or is the bare dir itself
    is_bare_ok, is_bare = git_try("rev-parse", "--is-bare-repository")
    if is_bare == "true":
        return "bare", git_common, Path.cwd().resolve()

    # Check for worktrees
    worktrees = git_parse_worktrees(git_common)
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


def git_parse_worktrees(git_common):
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


def git_resolve_worktree(name, git_common):
    """Resolve worktree name to its filesystem path."""
    worktrees = git_parse_worktrees(git_common)
    for wt in worktrees:
        if wt.get("branch") == name or wt["path"].name == name:
            return wt["path"]
    return None


def git_tracked_files(worktree_path):
    """Get git-tracked files from a worktree."""
    try:
        result = git_run("ls-files", cwd=worktree_path)
    except RuntimeError:
        return []
    if not result:
        return []
    return sorted(Path(line) for line in result.split("\n") if line)


def git_create_worktree(project_root, name):
    """Create empty detached worktree."""
    logging.info(f"Creating worktree: {name}")
    wt_path = project_root / name
    git_run("worktree", "add", "--detach", str(wt_path))
    logging.debug(f"Created detached worktree at {wt_path}")
    return wt_path


def git_setup_orphan(name, wt_path):
    """Create orphan branch in worktree with empty initial commit."""
    git_run("checkout", "--orphan", name, cwd=wt_path)
    git_run("rm", "-rf", ".", cwd=wt_path)
    result = subprocess.run(
        ["git", "-c", "user.email=init@local", "-c", "user.name=init",
         "commit", "--allow-empty", "-m", "init: empty orphan for sync"],
        cwd=wt_path, capture_output=True, text=True,
    )
    if result.returncode != 0:
        logging.warning(f"Empty commit may have failed: {result.stderr}")
    logging.debug(f"Created orphan branch '{name}'")


def git_write_pointer(cwd):
    """Write .git pointer file for bare repo."""
    (cwd / ".git").write_text("gitdir: ./.bare\n")
    logging.info("Created .git pointer")


# Backward compat (tests)
_git = git_run
_git_ok = git_try
detect_topology = git_detect_topology
parse_worktrees = git_parse_worktrees
resolve_worktree_path = git_resolve_worktree
get_tracked_files = git_tracked_files
_setup_orphan_branch = git_setup_orphan
_write_git_pointer = git_write_pointer


# == Config Block =============================================================

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


def config_read(git_internal):
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


def config_get_target(config, target_name):
    """Merge target config with defaults. Returns (merged_dict, error_or_None)."""
    targets = config.get("targets", {})
    if target_name not in targets:
        return None, f"Unknown target: {target_name}"

    defaults = config.get("defaults", {})
    target = {**DEFAULT_TARGET, **defaults, **targets[target_name]}
    return target, None


def config_resolve_paths(target, git_internal, cwd_worktree):
    """Resolve source/dest paths for a target. Returns (source_path, dest_path, errors)."""
    errors = []

    # Resolve source
    source_name = target.get("source")
    if source_name:
        source_path = git_resolve_worktree(source_name, git_internal)
        if not source_path:
            errors.append(f"Source worktree not found: {source_name}")
            return None, None, errors
    else:
        source_path = cwd_worktree

    # Resolve dest
    if "dest" in target:
        dest_path = Path(target["dest"])
        if not dest_path.is_absolute():
            dest_path = source_path.parent / dest_path
    else:
        target_name = target.get("_name", "")
        if target_name:
            dest_path = git_resolve_worktree(target_name, git_internal)
            if not dest_path:
                errors.append(f"Target worktree not found: {target_name}. Use 'dest' field for external directories.")
                return None, None, errors
        else:
            errors.append("Target name not set")
            return None, None, errors

    if source_path == dest_path:
        errors.append(f"Source and target are the same: {source_path}")
        return None, None, errors

    return source_path, dest_path, errors


def config_add_target(git_internal, name, source="master", exclude=None, delete_policy="tracked_only"):
    """Add target to config and save. Returns (config, path, error)."""
    path = config_path(git_internal)
    config = json.loads(path.read_text()) if path.exists() else dict(DEFAULT_CONFIG)

    if exclude is None:
        exclude = list(DEFAULT_EXCLUDE)

    config["targets"][name] = {
        "source": source,
        "exclude": exclude,
        "delete_policy": delete_policy,
    }
    path.write_text(json.dumps(config, indent=2) + "\n")
    logging.info(f"Updated config: {path}")
    return config, path, None


def config_remove_target(git_internal, name):
    """Remove target from config and clean state. Returns (config, path, error)."""
    path = config_path(git_internal)
    if not path.exists():
        return None, path, "Config not found"

    config = json.loads(path.read_text())
    if name not in config.get("targets", {}):
        return config, path, f"Target '{name}' not in config"

    del config["targets"][name]
    path.write_text(json.dumps(config, indent=2) + "\n")
    logging.info(f"Removed target '{name}' from config: {path}")

    # Clean state
    sp = state_path(git_internal)
    if sp.exists():
        state = json.loads(sp.read_text())
        state.get("last_sync", {}).pop(name, None)
        sp.write_text(json.dumps(state, indent=2) + "\n")
        logging.info(f"Cleaned '{name}' from state")

    return config, path, None


def config_init(git_internal, git_common):
    """Create default config. Auto-detect worktrees as targets."""
    path = config_path(git_internal)
    if path.exists():
        print(f"  Config already exists: {path}")
        return False

    config = dict(DEFAULT_CONFIG)
    config["targets"] = {}

    # Auto-detect worktrees as potential targets
    worktrees = git_parse_worktrees(git_common)
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


# Backward compat (tests)
load_config = config_read


# == Filter Block =============================================================

def filter_match(filepath, patterns):
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

    Returns (passed, unmatched, blocked):
      - passed: files that will sync (through both filters)
      - unmatched: files that didn't match any include pattern
      - blocked: files that matched include but were blocked by exclude
    """
    pool = list(files)

    # Step 2: include filter (whitelist)
    if include_patterns:
        included = [f for f in pool if filter_match(f, include_patterns)]
        unmatched = [f for f in pool if not filter_match(f, include_patterns)]
    else:
        included = pool
        unmatched = []

    # Step 3: exclude filter (blacklist)
    if exclude_patterns:
        blocked = [f for f in included if filter_match(f, exclude_patterns)]
        passed = [f for f in included if not filter_match(f, exclude_patterns)]
    else:
        blocked = []
        passed = included

    # Step 4: .git/ hardcoded exclusion
    passed = [f for f in passed if ".git" not in f.parts]

    return sorted(passed), unmatched, blocked


# Backward compat (tests)
_match_any = filter_match


# == Sync Block ===============================================================

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


def sync_diff(source, dest, sync_files, delete_policy, state_hashes=None, protect=None):
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
                if filter_match(f, protect):
                    actions['P'].append(f)
                else:
                    actions['D'].append(f)
        else:
            actions['D'] = sorted(candidates)

    return actions


def sync_apply(source, dest, add, update, delete):
    """Execute sync actions. Returns file_hashes for state."""
    hashes = {}

    logging.info(f"Applying actions: +{len(add)} ~{len(update)} -{len(delete)}")

    for rel in add + update:
        src = source / rel
        dst = dest / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        hashes[str(rel)] = file_hash(src)
        logging.debug(f"Synced: {rel}")

    for rel in delete:
        dst = dest / rel
        if dst.exists():
            dst.unlink()
            logging.debug(f"Deleted: {rel}")
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


def sync_check(source, dest, target_config, sync_files, excluded_files,
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
    logging.debug(f"Cross-checking {len(sync_files)} files")

    # C1: Source dirty (uncommitted changes)
    if target_config.get("warn_dirty", True):
        ok, status = git_try("status", "--porcelain", cwd=source)
        if ok and status:
            dirty_count = len(status.strip().split("\n"))
            warnings.append(("warn", f"C1: Source has {dirty_count} uncommitted change(s)"))

    # C2: Include pattern matches 0 files
    if target_config.get("include") and target_config.get("warn_no_match", True):
        all_files = sync_files + excluded_files
        for pat in target_config["include"]:
            matched = any(filter_match(f, [pat]) for f in all_files)
            if not matched:
                warnings.append(("warn", f"C2: Include pattern matches 0 files: {pat}"))

    # C3: Source has staged but uncommitted changes that overlap sync files
    ok, staged_out = git_try("diff", "--cached", "--name-only", cwd=source)
    if ok and staged_out:
        staged = set(staged_out.strip().split("\n"))
        overlap = staged & sync_set
        if overlap:
            warnings.append(("warn", f"C3: {len(overlap)} staged-but-uncommitted file(s) in sync set: {', '.join(sorted(overlap)[:5])}"))

    # C4: Include patterns match untracked files (won't be synced)
    if target_config.get("include"):
        ok, untracked_out = git_try(
            "ls-files", "--others", "--exclude-standard", cwd=source
        )
        if ok and untracked_out:
            untracked = [Path(f) for f in untracked_out.strip().split("\n") if f]
            hits = [f for f in untracked if filter_match(f, target_config["include"])]
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
            ok, log_out = git_try(
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
            matched = any(filter_match(f, [pat]) for f in all_files)
            if not matched:
                warnings.append(("silent", f"C8: Exclude pattern matches 0 files: {pat}"))

    return warnings


def sync_classify_checks(check_results, force=False, verbose=False):
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


# Backward compat (tests)
compute_actions = sync_diff
cross_check = sync_check
classify_checks = sync_classify_checks
apply_actions = sync_apply


# == State Block ==============================================================

def state_read(git_internal):
    """Load sync state file."""
    path = state_path(git_internal)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return {}


def state_save_sync(git_internal, target_name, source, file_hashes, add_count, update_count, delete_count):
    """Save sync state for a target."""
    state = state_read(git_internal)

    # Get source commit
    try:
        commit = git_run("rev-parse", "--short", "HEAD", cwd=source)
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


def state_save_run(git_internal, targets, mode, checks_passed, warnings_count,
                   errors_count, all_actions):
    """Record last_run state for all targets.

    all_actions: dict mapping target_name -> actions dict
    """
    state = state_read(git_internal)

    # Aggregate summary across all targets
    summary = {'A': 0, 'M': 0, 'D': 0}
    for target_name, actions in all_actions.items():
        summary['A'] += len(actions.get('A', []))
        summary['M'] += len(actions.get('M', []))
        summary['D'] += len(actions.get('D', []))

    state["last_run"] = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "command": "sync_worktree.py " + " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "sync_worktree.py",
        "mode": mode,
        "checks_passed": checks_passed,
        "warnings": warnings_count,
        "errors": errors_count,
        "targets": targets,
        "summary": summary,
    }

    logging.debug(f"Saving last_run: {state['last_run']}")
    path = state_path(git_internal)
    path.write_text(json.dumps(state, indent=2) + "\n")


def state_check_gate(git_internal, apply, force):
    """Safety gate: check if --apply can proceed based on last_run.

    Returns True if safe to proceed, False otherwise.
    """
    if not apply or force:
        return True

    state = state_read(git_internal)
    last_run = state.get("last_run")

    if not last_run:
        print("No successful dry-run found. Run without --apply first, or use --force to override.",
              file=sys.stderr)
        logging.warning("--apply attempted without prior dry-run")
        return False

    if last_run.get("mode") != "dry-run":
        print("No successful dry-run found. Run without --apply first, or use --force to override.",
              file=sys.stderr)
        logging.warning(f"--apply attempted after {last_run.get('mode')} mode")
        return False

    if not last_run.get("checks_passed"):
        print("No successful dry-run found. Run without --apply first, or use --force to override.",
              file=sys.stderr)
        logging.warning("--apply attempted after failed checks")
        return False

    return True


# Backward compat (tests)
load_state = state_read
save_state = state_save_sync
save_last_run = state_save_run
check_last_run_gate = state_check_gate


# == Report Block =============================================================

STATUS_LABELS = {
    'A': ('A  added',      '+'),
    'M': ('M  modified',   '~'),
    'D': ('D  deleted',    '-'),
    'U': ('U  unchanged',  '='),
    'P': ('P  protected',  '#'),
    '!': ('!  missing src', '?'),
}


def report_target(target_name, actions, warnings, excluded, mode_label,
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


def report_diff(source, dest, update_files):
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


def report_config_schema():
    """Print config schema reference and exit."""
    schema = """{
  "version": 1,
  "defaults": {
    "delete_policy": "never",       // never | unlisted | tracked_only
    "follow_symlinks": false,
    "warn_untracked": true,
    "warn_dirty": true,
    "warn_no_match": true
  },
  "targets": {
    "runner": {
      "source": "master",           // source worktree (default: current)
      "dest": "/opt/app/",          // dest path (default: worktree by name)
      "include": ["lib/**"],        // keep only matching (default: all)
      "exclude": ["tests/"],        // remove matching
      "protect": ["node_modules/"], // never delete these in dest
      "delete_policy": "never",     // override default
      "pre_sync": "npm run build",
      "post_sync": "pm2 restart"
    }
  }
}

Delete policies:
  never        No deletions (safe default)
  unlisted     Delete all dest files not in sync set
  tracked_only Delete only previously synced files

Filter pipeline: git ls-files → include → exclude → .git/ (hardcoded)
"""
    print(schema)
    sys.exit(0)


# Backward compat (tests)
print_report = report_target
print_diff = report_diff
print_help_config = report_config_schema


# == Sync Orchestration (no block marker, top-level functions) ================

def setup_logging(topology, targets, log_dir=None, no_log=False):
    """Configure logging. Returns logger.

    Log filename: <topology>-<target>-<YYYYMMDD-HHMMSS>.log (single)
                  <topology>-all-<YYYYMMDD-HHMMSS>.log (multi)
    Default log dir: script_dir/logs/
    """
    if no_log:
        logger = logging.getLogger()
        logger.addHandler(logging.NullHandler())
        logger.setLevel(logging.DEBUG)
        return logger

    # Determine log dir
    if log_dir is None:
        log_dir = Path(__file__).parent / "logs"
    else:
        log_dir = Path(log_dir)

    log_dir.mkdir(parents=True, exist_ok=True)

    # Determine filename
    ts = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    target_label = targets[0] if len(targets) == 1 else "all"
    log_file = log_dir / f"{topology}-{target_label}-{ts}.log"

    logger = logging.getLogger()
    logger.addHandler(logging.NullHandler())
    logger.setLevel(logging.DEBUG)

    # File handler (always write)
    fh = logging.FileHandler(str(log_file))
    fh.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    logging.info(f"Logging to {log_file}")
    return logger


def prepare_sync(config, target_name, git_internal, topology, cwd_worktree):
    """Pure computation: resolve target, filter files, cross-check, compute actions.

    Returns dict with all computed data, or None + errors on failure.
    No I/O beyond git queries (read-only).
    """
    logging.info(f"Preparing sync for target: {target_name}")

    target, err = config_get_target(config, target_name)
    if err:
        return None, [err]

    target["_name"] = target_name
    source, dest, path_errors = config_resolve_paths(target, git_internal, cwd_worktree)
    if path_errors:
        logging.error(f"Failed to resolve target {target_name}: {path_errors}")
        return None, path_errors

    logging.debug(f"Source: {source}, Dest: {dest}")

    source_files = git_tracked_files(source)
    logging.debug(f"Found {len(source_files)} tracked files")

    include_patterns = target.get("include")
    exclude_patterns = target.get("exclude")
    sync_files, unmatched, blocked = filter_pipeline(source_files, include_patterns, exclude_patterns)
    excluded = unmatched + blocked
    logging.debug(f"After filter: {len(sync_files)} sync, {len(unmatched)} unmatched, {len(blocked)} blocked")

    parts = []
    if include_patterns:
        parts.append(f"include:{len(include_patterns)}")
    if exclude_patterns:
        parts.append(f"exclude:{len(exclude_patterns)}")
    mode_label = " + ".join(parts) if parts else "all files"

    state = state_read(git_internal)
    state_hashes = state.get("last_sync", {}).get(target_name, {}).get("file_hashes", {})

    check_results = sync_check(source, dest, target, sync_files, excluded,
                               state_hashes=state_hashes, git_internal=git_internal)

    delete_policy = target.get("delete_policy", "never")
    protect = target.get("protect", [])
    actions = sync_diff(source, dest, sync_files, delete_policy, state_hashes, protect)

    return {
        "source": source, "dest": dest, "target": target,
        "sync_files": sync_files, "excluded": excluded,
        "unmatched": unmatched, "blocked": blocked,
        "mode_label": mode_label, "actions": actions,
        "check_results": check_results, "state_hashes": state_hashes,
    }, []


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

    file_hashes = sync_apply(source, dest, add, update, delete)

    for rel in sync_files:
        rel_str = str(rel)
        if rel_str not in file_hashes:
            if rel_str in state_hashes:
                file_hashes[rel_str] = state_hashes[rel_str]
            else:
                src = source / rel
                if src.exists():
                    file_hashes[rel_str] = file_hash(src)

    state_save_sync(git_internal, target_name, source, file_hashes,
                    len(add), len(update), len(delete))

    post_sync = target.get("post_sync")
    if post_sync:
        print(f"\n    Running post_sync: {post_sync}")
        subprocess.run(post_sync, shell=True, cwd=str(dest))

    return True, file_hashes


def sync_target(config, target_name, git_internal, topology, cwd_worktree,
                apply=False, strict=False, force=False, verbose=False,
                show_diff=False, as_json=False):
    """Orchestrate one target sync. Returns (success, has_warnings, actions)."""
    prepared, errors = prepare_sync(
        config, target_name, git_internal, topology, cwd_worktree
    )
    if errors:
        for e in errors:
            print(f"  ERROR: {e}", file=sys.stderr)
        return False, False, {}

    source = prepared["source"]
    dest = prepared["dest"]
    actions = prepared["actions"]
    warnings, errors = sync_classify_checks(
        prepared["check_results"], force=force, verbose=verbose
    )

    if errors:
        for e in errors:
            print(f"  ERROR: {e}", file=sys.stderr)
        print(f"  Use --force to override.", file=sys.stderr)
        return False, True, actions

    if not as_json:
        print(f"\n  Source: {source}")
        print(f"  Target: {dest}")

    report_target(target_name, actions, warnings, prepared["excluded"],
                  prepared["mode_label"], verbose=verbose, as_json=as_json)

    if show_diff:
        report_diff(source, dest, actions['M'])

    has_warnings = len(warnings) > 0

    if strict and has_warnings:
        print(f"\n  ABORT: --strict mode, {len(warnings)} warning(s)", file=sys.stderr)
        return True, True, actions

    if apply:
        ok, _ = execute_sync(
            source, dest, actions, prepared["sync_files"],
            prepared["state_hashes"], prepared["target"],
            target_name, git_internal,
        )
        if not ok:
            return False, has_warnings, actions
        add, update, delete = actions['A'], actions['M'], actions['D']
        if not as_json and (len(add) + len(update) + len(delete)) > 0:
            print(f"\n    Done. A:{len(add)} M:{len(update)} D:{len(delete)}")
    elif not as_json:
        add, update, delete = actions['A'], actions['M'], actions['D']
        if (len(add) + len(update) + len(delete)) > 0:
            print("\n    Run with --apply to execute.")

    return True, has_warnings, actions


def cmd_add_target(name, git_internal, topology):
    """Create empty orphan worktree and add to config."""
    project_root = git_internal.parent if topology == "bare" else Path.cwd().resolve()
    logging.info(f"Adding target: {name}")

    worktrees = git_parse_worktrees(git_internal)
    if any(wt.get("branch") == name or wt["path"].name == name for wt in worktrees):
        print(f"Error: Worktree '{name}' already exists", file=sys.stderr)
        sys.exit(1)

    try:
        wt_path = git_create_worktree(project_root, name)
    except RuntimeError as e:
        print(f"Error: Failed to create worktree: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        git_setup_orphan(name, wt_path)
    except RuntimeError as e:
        print(f"Error: Failed to set up orphan branch: {e}", file=sys.stderr)
        sys.exit(1)

    _, path, _ = config_add_target(git_internal, name)

    print(f"\nCreated worktree: {name}")
    print(f"  Path: {wt_path}")
    print(f"  Branch: {name} (orphan, empty)")
    print(f"\nAdded to config: {path}")
    print(f"  Source: master")
    print(f"  Delete policy: tracked_only")


def cmd_remove_target(name, git_internal, topology):
    """Remove worktree, branch, config entry, and state."""
    project_root = git_internal.parent if topology == "bare" else Path.cwd().resolve()
    wt_path = git_resolve_worktree(name, git_internal)

    # Remove worktree
    if wt_path and wt_path.exists():
        try:
            git_run("worktree", "remove", str(wt_path), "--force")
            logging.info(f"Removed worktree: {wt_path}")
        except RuntimeError as e:
            print(f"Error: Failed to remove worktree: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        logging.info(f"Worktree path not found, skipping removal")

    # Remove branch
    ok, _ = git_try("branch", "-D", name, cwd=git_internal)
    if ok:
        logging.info(f"Deleted branch: {name}")

    # Remove from config + state
    config, path, err = config_remove_target(git_internal, name)
    if err:
        print(f"Warning: {err}", file=sys.stderr)

    print(f"\nRemoved target: {name}")
    if wt_path:
        print(f"  Worktree: {wt_path} (deleted)")
    print(f"  Branch: {name} (deleted)")
    print(f"  Config: {path} (entry removed)")


# == CLI Block ================================================================

def init_bare(url):
    """Clone remote as bare repo + set up worktree structure."""
    cwd = Path.cwd()
    bare_dir = cwd / ".bare"

    if bare_dir.exists():
        print(f"Error: .bare/ already exists in {cwd}", file=sys.stderr)
        sys.exit(1)

    try:
        git_run("clone", "--bare", url, str(bare_dir))
        logging.info(f"Cloned bare repo to .bare/")
    except RuntimeError as e:
        print(f"Error: Failed to clone: {e}", file=sys.stderr)
        sys.exit(1)

    git_write_pointer(cwd)

    try:
        git_run("worktree", "add", "master", "master")
        logging.info("Created master worktree")
    except RuntimeError as e:
        print(f"Error: Failed to create master worktree: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"\nInitialized bare repo + master worktree")
    print(f"  Directory: {cwd}")
    print(f"  Bare repo: {bare_dir}")
    print(f"  Worktree: {cwd / 'master'}")
    print(f"\nNext: cd master && git config user.email/user.name")
    print(f"      {Path(__file__).name} --add-target release")


def _finalize_migration(cwd, bare_dir, current_branch):
    """Complete post-migration setup: config, prune, worktree."""
    try:
        git_run("config", "core.bare", "true", cwd=bare_dir)
        git_run("worktree", "prune", cwd=cwd)
    except RuntimeError as e:
        logging.warning(f"Post-migrate setup: {e}")

    wt_path = cwd / current_branch
    try:
        git_run("worktree", "add", str(wt_path), current_branch, cwd=cwd)
        logging.info(f"Created worktree '{current_branch}'")
    except RuntimeError as e:
        print(f"Error: Failed to create worktree: {e}", file=sys.stderr)
        sys.exit(1)

    return wt_path


def migrate_to_bare():
    """Convert existing regular repo to bare + worktree structure."""
    cwd = Path.cwd().resolve()
    git_dir = cwd / ".git"

    if not git_dir.exists() or not git_dir.is_dir():
        print(f"Error: Not in a git repository (no .git/ directory)", file=sys.stderr)
        sys.exit(1)

    try:
        current_branch = git_run("rev-parse", "--abbrev-ref", "HEAD", cwd=cwd)
    except RuntimeError as e:
        print(f"Error: Failed to get current branch: {e}", file=sys.stderr)
        sys.exit(1)

    if current_branch == "HEAD":
        print(f"Error: Detached HEAD. Checkout a branch first.", file=sys.stderr)
        sys.exit(1)

    logging.info(f"Migrating repo to bare + worktree (branch: {current_branch})")

    bare_dir = cwd / ".bare"
    if bare_dir.exists():
        print(f"Error: .bare/ already exists", file=sys.stderr)
        sys.exit(1)

    try:
        git_dir.rename(bare_dir)
        logging.info("Renamed .git → .bare")
    except OSError as e:
        print(f"Error: Failed to rename .git: {e}", file=sys.stderr)
        sys.exit(1)

    git_write_pointer(cwd)
    wt_path = _finalize_migration(cwd, bare_dir, current_branch)

    print(f"\nMigrated to bare repo + worktree structure")
    print(f"  Bare repo: {bare_dir}")
    print(f"  Worktree: {wt_path} ({current_branch})")
    print(f"\nNext: cd {current_branch} && work normally")
    print(f"      {Path(__file__).name} --init")


# Legacy names for CLI (backward compat)
add_target = cmd_add_target


def main():
    epilog = """Workflow:
  0. Repo     %(prog)s --init-bare <url>         clone bare + master worktree
              %(prog)s --add-target release      empty orphan + add to config
              %(prog)s --remove-target release   remove worktree + config entry
              %(prog)s --migrate                 convert existing repo to bare
  1. Setup    %(prog)s --init            auto-detect worktrees, create config
              %(prog)s --help-config     config schema reference with field docs
              %(prog)s --config          verify resolved config as JSON
  2. Preview  %(prog)s                   dry-run all targets
              %(prog)s runner            dry-run specific target
              %(prog)s runner -v         include unchanged/excluded/protected
              %(prog)s runner --diff     show file content diffs
  3. Execute  %(prog)s runner --apply    sync files (requires prior dry-run)
              %(prog)s --apply --force   skip dry-run gate + override C6 errors
  4. Verify   %(prog)s --status          last sync state (commit, hashes, counts)
              %(prog)s runner --json     machine-readable output for scripting
  5. Commit   cd ../release && git add -A && git status -s
              git commit -m "feat: vX.Y.Z" && git push origin release
  CI:         %(prog)s --strict          exit 2 on any warning (C1-C7)
              %(prog)s --strict --json   CI pipeline with structured output
Safety: dry-run → preview → --apply. No prior dry-run = blocked unless --force.
Targets start empty (orphan branch). Edit in master/, sync out. Never edit targets.
"""
    parser = argparse.ArgumentParser(
        description="Config-driven worktree sync. Dry-run by default.",
        epilog=epilog,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "targets", nargs="*", default=[],
        help="Target name(s) from config. Default: all targets."
    )
    parser.add_argument("--init-bare", metavar="URL", help="Clone remote as bare repo + master worktree")
    parser.add_argument("--add-target", metavar="NAME", help="Create empty orphan worktree + add to config")
    parser.add_argument("--remove-target", metavar="NAME", help="Remove worktree + branch + config entry")
    parser.add_argument("--migrate", action="store_true", help="Convert existing repo to bare + worktree")
    parser.add_argument("--apply", action="store_true", help="Execute sync (default: dry-run)")
    parser.add_argument("--strict", action="store_true", help="Abort on any warning (CI mode)")
    parser.add_argument("--force", action="store_true", help="Overwrite target local modifications")
    parser.add_argument("--init", action="store_true", help="Create default config")
    parser.add_argument("--config", action="store_true", help="Print resolved config and exit")
    parser.add_argument("--status", action="store_true", help="Print last sync state and exit")
    parser.add_argument("--help-config", action="store_true", help="Show config schema and exit")
    parser.add_argument("--diff", action="store_true", help="Show content diff for updates")
    parser.add_argument("-v", "--verbose", action="store_true", help="Show filtered/skipped files")
    parser.add_argument("-q", "--quiet", action="store_true", help="Summary only")
    parser.add_argument("--json", action="store_true", help="JSON output (CI)")
    parser.add_argument("--log-dir", default=None, help="Override log directory (default: script_dir/logs/)")
    parser.add_argument("--no-log", action="store_true", help="Disable file logging")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args()

    # No arguments at all → show help (only from terminal)
    if len(sys.argv) == 1 and sys.stdin.isatty():
        parser.print_help()
        return

    # --help-config
    if args.help_config:
        report_config_schema()

    # --init-bare (before topology detection since repo doesn't exist yet)
    if args.init_bare:
        setup_logging("init-bare", [], log_dir=args.log_dir, no_log=args.no_log)
        init_bare(args.init_bare)
        return

    # --migrate (detect topology first, must be a regular repo)
    if args.migrate:
        # Try simple repo detection without full topology
        try:
            git_run("rev-parse", "--git-dir")
        except RuntimeError:
            print("Error: not inside a git repository", file=sys.stderr)
            sys.exit(1)
        setup_logging("migrate", [], log_dir=args.log_dir, no_log=args.no_log)
        migrate_to_bare()
        return

    # Detect topology
    topology, git_internal, cwd_worktree = git_detect_topology()

    # Determine targets early for logging setup
    # Load config first
    config, config_errors = config_read(git_internal)
    if config is None and not args.init:
        for e in config_errors:
            print(f"Error: {e}", file=sys.stderr)
        print(f"\nRun with --init to create default config.", file=sys.stderr)
        sys.exit(1)

    target_names = args.targets or (list(config.get("targets", {}).keys()) if config else [])
    setup_logging(topology, target_names, log_dir=args.log_dir, no_log=args.no_log)

    if not args.quiet and not args.json:
        print(f"Topology: {topology}")
        print(f"Config: {config_path(git_internal)}")

    logging.info(f"Starting sync: mode={'apply' if args.apply else 'dry-run'}")

    # --add-target
    if args.add_target:
        setup_logging("add-target", [args.add_target], log_dir=args.log_dir, no_log=args.no_log)
        cmd_add_target(args.add_target, git_internal, topology)
        return

    # --remove-target
    if args.remove_target:
        setup_logging("remove-target", [args.remove_target], log_dir=args.log_dir, no_log=args.no_log)
        cmd_remove_target(args.remove_target, git_internal, topology)
        return

    # --init
    if args.init:
        config_init(git_internal, git_internal)
        return

    # --config
    if args.config:
        print(json.dumps(config, indent=2))
        return

    # --status
    if args.status:
        state = state_read(git_internal)
        print(json.dumps(state, indent=2))
        return

    if not target_names:
        print("No targets defined in config.", file=sys.stderr)
        sys.exit(1)

    if not args.quiet and not args.json:
        run_mode = "APPLY" if args.apply else "DRY RUN"
        print(f"Mode: {run_mode}")
        print(f"Targets: {', '.join(target_names)}")

    # Safety gate: check last_run before --apply
    if args.apply and not state_check_gate(git_internal, args.apply, args.force):
        sys.exit(1)

    # Sync each target
    all_ok = True
    any_warnings = False
    all_actions = {}
    total_warnings = 0
    total_errors = 0

    for name in target_names:
        ok, has_warn, actions = sync_target(
            config, name, git_internal, topology, cwd_worktree,
            apply=args.apply, strict=args.strict, force=args.force,
            verbose=args.verbose, show_diff=args.diff, as_json=args.json,
        )
        if not ok:
            all_ok = False
            total_errors += 1
        if has_warn:
            any_warnings = True
            total_warnings += 1
        if actions:
            all_actions[name] = actions

    # Save last_run state (after all targets complete)
    mode = "apply" if args.apply else "dry-run"
    checks_passed = all_ok and (not args.strict or not any_warnings)
    state_save_run(git_internal, target_names, mode, checks_passed,
                   total_warnings, total_errors, all_actions)

    logging.info(f"Sync complete: ok={all_ok}, warnings={total_warnings}, errors={total_errors}")

    # Exit code
    if not all_ok:
        sys.exit(1)
    elif args.strict and any_warnings:
        sys.exit(2)


if __name__ == "__main__":
    main()
