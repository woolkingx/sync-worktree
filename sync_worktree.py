#!/usr/bin/env python3
"""
sync_worktree.py - Worktree sync.

Usage:
    sync-worktree [-C <path>] init <path> [--bare] [--url <url>] [--branch <name>]
    sync-worktree [-C <path>] migrate [<path>] [--dry-run]
    sync-worktree [-C <path>] target add <name> [--source <wt>]
    sync-worktree [-C <path>] target remove <name>
    sync-worktree [-C <path>] target list
    sync-worktree [-C <path>] config show|schema|init
    sync-worktree [-C <path>] status [--source <wt>] [--target <name>...]
    sync-worktree [-C <path>] sync [--source <wt>] [--target <name>...] [--apply] [-v] [-q]

Run either as the console script `sync-worktree` or directly with
`python3 sync_worktree.py`.
"""

import argparse
import atexit
import copy
from contextlib import contextmanager
import filecmp
import hashlib
import json
import logging
import tempfile
import shutil
import shlex
import subprocess
import sys
import time
from datetime import datetime
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None

__version__ = "0.5.0"


class HelpOnErrorParser(argparse.ArgumentParser):
    def error(self, message):
        self.print_help(sys.stderr)
        self.exit(2, f"{self.prog}: error: {message}\n")


# == Git Block ================================================================

def git_run(*args, cwd=None, timeout=30) -> str:
    """Run git command, return stdout. Raises on failure."""
    result = subprocess.run(
        ["git"] + list(args),
        cwd=cwd, capture_output=True, text=True, timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def git_try(*args, cwd=None, timeout=30) -> Tuple[bool, str]:
    """Run git command, return (success, stdout)."""
    result = subprocess.run(
        ["git"] + list(args),
        cwd=cwd, capture_output=True, text=True, timeout=timeout,
    )
    return result.returncode == 0, result.stdout.strip()


def git_detect_topology(cwd=None) -> Tuple[str, Path, Path]:
    """Detect git topology and return (topology, git_internal_dir, cwd_worktree_path).

    topology: 'bare' | 'worktree' | 'repo'
    git_internal_dir: Path to store config (.bare/, .git/, or git common dir)
    """
    try:
        effective_base = Path(cwd).resolve() if cwd else Path.cwd().resolve()
        raw_common = git_run("rev-parse", "--git-common-dir", cwd=cwd)
        raw_dir = git_run("rev-parse", "--git-dir", cwd=cwd)
        git_common = (effective_base / raw_common).resolve()
        git_dir = (effective_base / raw_dir).resolve()
    except RuntimeError:
        print("Error: not inside a git repository", file=sys.stderr)
        sys.exit(1)

    # Bare repo: git_common usually ends with .bare or is the bare dir itself
    is_bare_ok, is_bare = git_try("rev-parse", "--is-bare-repository", cwd=cwd)
    if is_bare == "true":
        return "bare", git_common, effective_base

    # Check for worktrees
    worktrees = git_parse_worktrees(git_common)
    if len(worktrees) > 1:
        # Find which worktree we're in
        for wt in worktrees:
            try:
                effective_base.relative_to(wt["path"])
                return "worktree" if git_common != git_dir else "bare", git_common, wt["path"]
            except ValueError:
                continue
        return "bare" if str(git_common).endswith(".bare") else "worktree", git_common, effective_base

    return "repo", git_common, effective_base


def git_parse_worktrees(git_common) -> List[Dict[str, Any]]:
    """Parse `git worktree list --porcelain` into structured data."""
    result = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=git_common, capture_output=True, text=True,
    )
    if result.returncode != 0:
        logging.debug("git worktree list failed: %s", result.stderr.strip())
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
            ref = line.split(" ", 1)[1]
            current["branch"] = ref.removeprefix("refs/heads/") if ref.startswith("refs/heads/") else ref
    if current and not current.get("bare"):
        worktrees.append(current)
    return worktrees


def git_resolve_worktree(name, git_common) -> Optional[Path]:
    """Resolve worktree name to its filesystem path."""
    worktrees = git_parse_worktrees(git_common)
    for wt in worktrees:
        if wt.get("branch") == name or wt["path"].name == name:
            return wt["path"]
    return None


def git_tracked_files(worktree_path) -> List[Path]:
    """Get git-tracked files from a worktree."""
    try:
        result = git_run("ls-files", cwd=worktree_path)
    except RuntimeError:
        return []
    if not result:
        return []
    return sorted(Path(line) for line in result.split("\n") if line)


def git_create_worktree(project_root, name) -> Path:
    """Create empty detached worktree."""
    name_error = _validate_target_name(name)
    if name_error:
        raise RuntimeError(name_error)
    logging.info(f"Creating worktree: {name}")
    wt_path = project_root / name
    git_run("worktree", "add", "--detach", str(wt_path))
    logging.debug(f"Created detached worktree at {wt_path}")
    return wt_path


def git_setup_orphan(name, wt_path) -> None:
    """Create orphan branch in worktree with empty initial commit."""
    git_run("checkout", "--orphan", name, cwd=wt_path)
    git_try("rm", "-rf", ".", cwd=wt_path)  # May fail if worktree is already empty
    result = subprocess.run(
        ["git", "-c", "user.email=init@local", "-c", "user.name=init",
         "commit", "--allow-empty", "-m", "init: empty orphan for sync"],
        cwd=wt_path, capture_output=True, text=True,
    )
    if result.returncode != 0:
        logging.warning(f"Empty commit may have failed: {result.stderr}")
    logging.debug(f"Created orphan branch '{name}'")


def git_write_pointer(cwd) -> None:
    """Write .git pointer file for bare repo."""
    (cwd / ".git").write_text("gitdir: ./.bare\n")
    logging.info("Created .git pointer")


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

DEFAULT_EXCLUDE = [".gitignore"]

DEFAULT_TARGET = {
    "delete_policy": "never",
}


def new_config() -> dict:
    """Return a fresh config scaffold."""
    return copy.deepcopy(DEFAULT_CONFIG)


def config_path(git_internal) -> Path:
    return git_internal / CONFIG_NAME


def state_path(git_internal) -> Path:
    return git_internal / STATE_NAME


def _state_write(git_internal, state) -> None:
    path = state_path(git_internal)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(state, indent=2) + "\n")
    temp.replace(path)


@contextmanager
def _state_lock(git_internal):
    lock_path = state_path(git_internal).with_suffix(".lock")
    lock_path.touch(exist_ok=True)
    with lock_path.open("r+") as lock_file:
        if fcntl is not None:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(lock_file, fcntl.LOCK_UN)


def _get_scope_label(topology, git_internal, cwd_worktree) -> str:
    if topology == "bare":
        return git_internal.parent.name if git_internal else "bare"
    if topology == "worktree":
        return cwd_worktree.parent.name if cwd_worktree else "worktree"
    return cwd_worktree.name if cwd_worktree else "repo"


def config_write(git_internal, config) -> Path:
    path = config_path(git_internal)
    path.write_text(json.dumps(config, indent=2) + "\n")
    return path


def _validate_target_name(name):
    if not name or name in {".", ".."}:
        return "Invalid target name"
    if Path(name).name != name or "/" in name or "\\" in name:
        return f"Invalid target name: {name}"
    return None


@dataclass(frozen=True)
class SyncPlan:
    """Resolved sync plan node.

    This is the main DAG edge bundle for prepare -> report -> execute.
    """

    source: Path
    dest: Path
    target: dict
    sync_files: list
    excluded: list
    unmatched: list
    blocked: list
    mode_label: str
    actions: dict
    check_results: list
    state_hashes: dict


def config_read(git_internal) -> Tuple[Optional[dict], List[str]]:
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


def config_get_target(config, target_name) -> Tuple[Optional[dict], Optional[str]]:
    """Merge target config with defaults. Returns (merged_dict, error_or_None)."""
    targets = config.get("targets", {})
    if target_name not in targets:
        return None, f"Unknown target: {target_name}"

    defaults = config.get("defaults", {})
    target = {**DEFAULT_TARGET, **defaults, **targets[target_name]}
    return target, None


def config_resolve_paths(target, git_internal, cwd_worktree) -> Tuple[Optional[Path], Optional[Path], List[str]]:
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
        if source_path is None:
            errors.append("Current worktree not set")
            return None, None, errors

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


def config_add_target(git_internal, name, source="master", exclude=None, delete_policy="tracked_only") -> Tuple[Optional[dict], Path, Optional[str]]:
    """Add target to config and save. Returns (config, path, error)."""
    name_error = _validate_target_name(name)
    if name_error:
        return None, config_path(git_internal), name_error

    path = config_path(git_internal)
    config = json.loads(path.read_text()) if path.exists() else new_config()

    if exclude is None:
        exclude = list(DEFAULT_EXCLUDE)

    config["targets"][name] = {
        "source": source,
        "exclude": exclude,
        "delete_policy": delete_policy,
    }
    config_write(git_internal, config)
    logging.info(f"Updated config: {path}")
    return config, path, None


def config_remove_target(git_internal, name) -> Tuple[Optional[dict], Path, Optional[str]]:
    """Remove target from config and clean state. Returns (config, path, error)."""
    name_error = _validate_target_name(name)
    if name_error:
        return None, config_path(git_internal), name_error

    path = config_path(git_internal)
    if not path.exists():
        return None, path, "Config not found"

    config = json.loads(path.read_text())
    if name not in config.get("targets", {}):
        return config, path, f"Target '{name}' not in config"

    del config["targets"][name]
    config_write(git_internal, config)
    logging.info(f"Removed target '{name}' from config: {path}")

    # Clean state
    sp = state_path(git_internal)
    if sp.exists():
        with _state_lock(git_internal):
            state = state_read(git_internal)
            state.get("last_sync", {}).pop(name, None)
            _state_write(git_internal, state)
            logging.info(f"Cleaned '{name}' from state")

    return config, path, None


def config_init(git_internal, git_common) -> bool:
    """Create default config. Auto-detect worktrees as targets."""
    path = config_path(git_internal)
    if path.exists():
        print(f"  Config already exists: {path}")
        return False

    config = new_config()
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
                "exclude": list(DEFAULT_EXCLUDE),
                "delete_policy": "unlisted",
            }
        else:
            config["targets"][name] = {
                "source": "master",
                "exclude": list(DEFAULT_EXCLUDE),
            }

    config_write(git_internal, config)
    print(f"  Created: {path}")
    print(f"  Targets: {', '.join(config['targets'].keys()) or '(none — add manually)'}")
    return True


# == Filter Block =============================================================

_GITIGNORE_TEMP_DIRS = set()


def _cleanup_gitignore_repos():
    for path_str in list(_GITIGNORE_TEMP_DIRS):
        shutil.rmtree(path_str, ignore_errors=True)
    _GITIGNORE_TEMP_DIRS.clear()


atexit.register(_cleanup_gitignore_repos)


def _normalize_patterns(patterns):
    return tuple(
        pat.replace("\\", "/")
        for pat in patterns
        if pat is not None and str(pat).strip() != ""
    )


@lru_cache(maxsize=256)
def _gitignore_repo(patterns_key):
    repo = Path(tempfile.mkdtemp(prefix="sync-worktree-gitignore-"))
    _GITIGNORE_TEMP_DIRS.add(str(repo))
    subprocess.run(["git", "init", "-q"], cwd=repo, capture_output=True, text=True)
    (repo / ".gitignore").write_text("\n".join(patterns_key) + ("\n" if patterns_key else ""))
    return repo


def _gitignore_match_batch(paths, patterns_key):
    """Return {posix_path: matched_bool} for worktree-relative posix paths."""
    if not patterns_key:
        return {path: False for path in paths}

    paths = tuple(paths)
    if not paths:
        return {}

    repo = _gitignore_repo(patterns_key)
    result = subprocess.run(
        ["git", "check-ignore", "--stdin"],
        cwd=repo, capture_output=True, text=True,
        input="\n".join(paths) + "\n",
    )
    matched = {line for line in result.stdout.splitlines() if line}
    return {path: path in matched for path in paths}


def filter_match_batch(paths, patterns) -> Dict[str, bool]:
    """Batch gitignore match for worktree-relative paths."""
    patterns_key = _normalize_patterns(patterns)
    posix_paths = tuple(Path(path).as_posix() for path in paths)
    return _gitignore_match_batch(posix_paths, patterns_key)


def filter_match(filepath, patterns) -> bool:
    """Check if filepath matches any configured pattern using gitignore syntax.

    Paths are matched as worktree-relative paths. Pattern syntax follows
    .gitignore rules: wildcards, directory rules, `**`, and `!` negation.
    """
    path = Path(filepath).as_posix()
    return filter_match_batch((path,), patterns).get(path, False)


def filter_pipeline(files, include_patterns=None, exclude_patterns=None) -> Tuple[List[Path], List[Path], List[Path]]:
    """Return files selected by config patterns, excluding `.git/`.

    Patterns use gitignore syntax. include selects candidates, exclude removes
    matches from the included set.
    """
    pool = [f for f in files if ".git" not in Path(f).parts]

    if include_patterns:
        include_hits = filter_match_batch(pool, include_patterns)
        included = [f for f in pool if include_hits.get(f.as_posix(), False)]
        unmatched = [f for f in pool if not include_hits.get(f.as_posix(), False)]
    else:
        included = pool
        unmatched = []

    if exclude_patterns:
        exclude_hits = filter_match_batch(included, exclude_patterns)
        blocked = [f for f in included if exclude_hits.get(f.as_posix(), False)]
        passed = [f for f in included if not exclude_hits.get(f.as_posix(), False)]
    else:
        blocked = []
        passed = included

    return sorted(passed), unmatched, blocked


# == Sync Block ===============================================================

def file_hash(filepath) -> str:
    """SHA256 hash of file content."""
    h = hashlib.sha256()
    with open(filepath, "rb") as fh:
        while True:
            chunk = fh.read(65536)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def sync_diff(source, dest, sync_files, delete_policy, state_hashes=None, protect=None) -> Dict[str, List[Path]]:
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
            protect_hits = filter_match_batch(sorted(candidates), protect)
            for f in sorted(candidates):
                if protect_hits.get(f.as_posix(), False):
                    actions['P'].append(f)
                else:
                    actions['D'].append(f)
        else:
            actions['D'] = sorted(candidates)

    return actions


def sync_apply(source, dest, add, update, delete) -> Dict[str, str]:
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


def _warn_on_zero_match(patterns, files, label, severity, target_config, warnings) -> None:
    if not patterns or not target_config.get("warn_no_match", True):
        return
    for pat in patterns:
        matched_map = filter_match_batch(files, [pat])
        if not any(matched_map.values()):
            warnings.append((severity, f"{label} pattern matches 0 files: {pat}"))


def _check_source_dirty(source, target_config, warnings) -> None:
    if not target_config.get("warn_dirty", True):
        return
    ok, status = git_try("status", "--porcelain", cwd=source)
    if ok and status:
        dirty_count = len(status.strip().split("\n"))
        warnings.append(("warn", f"C1: Source has {dirty_count} uncommitted change(s)"))


def _check_staged_overlap(source, sync_set, warnings) -> None:
    ok, staged_out = git_try("diff", "--cached", "--name-only", cwd=source)
    if ok and staged_out:
        staged = set(staged_out.strip().split("\n"))
        overlap = staged & sync_set
        if overlap:
            warnings.append(("warn", f"C3: {len(overlap)} staged-but-uncommitted file(s) in sync set: {', '.join(sorted(overlap)[:5])}"))


def _check_untracked_include(source, include_patterns, warnings) -> None:
    ok, untracked_out = git_try("ls-files", "--others", "--exclude-standard", cwd=source)
    if not ok or not untracked_out:
        return
    untracked = [Path(f) for f in untracked_out.strip().split("\n") if f]
    hits_map = filter_match_batch(untracked, include_patterns)
    hits = [f for f in untracked if hits_map.get(f.as_posix(), False)]
    if hits:
        names = [str(f) for f in hits[:5]]
        warnings.append(("warn", f"C4: {len(hits)} untracked file(s) match include patterns: {', '.join(names)}"))


def _check_gitignored_sync_files(source, sync_files, warnings) -> None:
    if not sync_files:
        return
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


def _check_target_modifications(dest, state_hashes, warnings) -> None:
    if not dest or not dest.exists() or not state_hashes:
        return
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


def _check_target_divergence(git_internal, target_config, warnings) -> None:
    if not git_internal:
        return
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


def sync_check(source, dest, target_config, sync_files, excluded_files,
               state_hashes=None, git_internal=None) -> List[Tuple[str, str]]:
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

    _check_source_dirty(source, target_config, warnings)
    _warn_on_zero_match(target_config.get("include"), sync_files + excluded_files, "C2: Include", "warn", target_config, warnings)
    _check_staged_overlap(source, sync_set, warnings)
    if target_config.get("include"):
        _check_untracked_include(source, target_config["include"], warnings)
    _check_gitignored_sync_files(source, sync_files, warnings)
    _check_target_modifications(dest, state_hashes, warnings)
    _check_target_divergence(git_internal, target_config, warnings)
    _warn_on_zero_match(target_config.get("exclude"), sync_files + excluded_files, "C8: Exclude", "silent", target_config, warnings)

    return warnings


def sync_classify_checks(check_results, force=False, verbose=False) -> Tuple[List[str], List[str]]:
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


# == State Block ==============================================================

def state_read(git_internal) -> dict:
    """Load sync state file."""
    path = state_path(git_internal)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return {}


def state_save_sync(git_internal, target_name, source, file_hashes, add_count, update_count, delete_count) -> None:
    """Save sync state for a target."""
    with _state_lock(git_internal):
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

        _state_write(git_internal, state)


def state_save_run(git_internal, targets, mode, checks_passed, warnings_count,
                   errors_count, all_actions) -> None:
    """Record last_run state for all targets.

    all_actions: dict mapping target_name -> actions dict
    """
    with _state_lock(git_internal):
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
        _state_write(git_internal, state)


def state_check_gate(git_internal, apply, force) -> bool:
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
                  verbose=False) -> None:
    """Print sync report for one target."""
    changes = len(actions['A']) + len(actions['M']) + len(actions['D'])

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


def report_diff(source, dest, update_files) -> None:
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


def report_config_schema() -> None:
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
      "include": ["lib/**/*.mjs"],  // gitignore syntax
      "exclude": [".gitignore"],    // gitignore syntax
      "protect": ["node_modules/"], // gitignore syntax
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
Default exclude: .gitignore
Match mode: gitignore syntax (.gitignore-compatible)
"""
    print(schema)
    sys.exit(0)


def report_config(config) -> None:
    """Print a human-readable config summary."""
    print(f"version: {config.get('version')}")
    defaults = config.get("defaults", {})
    if defaults:
        print("defaults:")
        for key in sorted(defaults):
            print(f"  {key}: {defaults[key]}")
    targets = config.get("targets", {})
    if not targets:
        print("targets: (none)")
        return
    print("targets:")
    for name in sorted(targets):
        target = targets[name]
        print(f"  {name}:")
        for key in sorted(target):
            value = target[key]
            print(f"    {key}: {value}")


def report_status(state) -> None:
    """Print a human-readable sync status summary."""
    last_run = state.get("last_run")
    last_sync = state.get("last_sync", {})

    if not last_run and not last_sync:
        print("status: empty")
        return

    if last_run:
        print("last_run:")
        for key in ("timestamp", "command", "mode", "checks_passed", "warnings", "errors"):
            if key in last_run:
                print(f"  {key}: {last_run[key]}")
        targets = last_run.get("targets", [])
        if targets:
            print(f"  targets: {', '.join(targets)}")
        summary = last_run.get("summary", {})
        if summary:
            print("  summary:")
            for key in sorted(summary):
                print(f"    {key}: {summary[key]}")

    if last_sync:
        print("last_sync:")
        for name in sorted(last_sync):
            entry = last_sync[name]
            print(f"  {name}:")
            for key in ("timestamp", "source_commit", "files_synced", "files_deleted"):
                if key in entry:
                    print(f"    {key}: {entry[key]}")


# == Sync Orchestration (no block marker, top-level functions) ================

def setup_logging(topology, targets, log_dir=None, no_log=False, scope_label=None) -> logging.Logger:
    """Configure logging. Returns logger.

    Log filename: <scope>-<target>-<YYYYMMDD>.log (single)
                  <scope>-all-<YYYYMMDD>.log (multi)
    Default log dir: script_dir/logs/
    """
    def reset_root_logger():
        logger = logging.getLogger()
        for handler in logger.handlers[:]:
            handler.close()
            logger.removeHandler(handler)
        return logger

    if no_log:
        logger = reset_root_logger()
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
    ts = datetime.utcnow().strftime("%Y%m%d")
    target_label = targets[0] if len(targets) == 1 else "all"
    scope = scope_label or topology
    log_file = log_dir / f"{scope}-{target_label}-{ts}.log"

    logger = reset_root_logger()
    logger.addHandler(logging.NullHandler())
    logger.setLevel(logging.DEBUG)

    # File handler (always write)
    fh = logging.FileHandler(str(log_file))
    fh.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    logging.info(f"Logging to {log_file}")
    logging.info("=== RUN START %s ===", datetime.utcnow().strftime("%H:%M:%S"))
    return logger


def prepare_sync(config, target_name, git_internal, topology, cwd_worktree) -> Tuple[Optional[SyncPlan], List[str]]:
    """Pure computation: resolve target, filter files, cross-check, compute actions.

    Returns SyncPlan with all computed data, or None + errors on failure.
    No I/O beyond git queries (read-only).
    """
    logging.info(f"Preparing sync for target: {target_name}")

    target, err = config_get_target(config, target_name)
    if err:
        return None, [err]

    target = {**target, "_name": target_name}
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

    return SyncPlan(
        source=source,
        dest=dest,
        target=target,
        sync_files=sync_files,
        excluded=excluded,
        unmatched=unmatched,
        blocked=blocked,
        mode_label=mode_label,
        actions=actions,
        check_results=check_results,
        state_hashes=state_hashes,
    ), []


def execute_sync(source, dest, actions, sync_files, state_hashes,
                 target, target_name, git_internal) -> Tuple[bool, Dict[str, str]]:
    """I/O boundary: copy/delete files, run hooks, save state.

    Returns (success, file_hashes).
    """
    logging.debug(
        "execute_sync start: target=%s add=%d update=%d delete=%d",
        target_name, len(actions['A']), len(actions['M']), len(actions['D']),
    )
    add, update, delete = actions['A'], actions['M'], actions['D']
    total = len(add) + len(update) + len(delete)
    if total == 0:
        logging.debug("execute_sync noop: target=%s", target_name)
        return True, {}

    pre_sync = target.get("pre_sync")
    if pre_sync:
        print(f"\n    Running pre_sync: {pre_sync}")
        result = subprocess.run(shlex.split(pre_sync), cwd=str(source))
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
    logging.debug("execute_sync done: target=%s", target_name)

    post_sync = target.get("post_sync")
    if post_sync:
        print(f"\n    Running post_sync: {post_sync}")
        result = subprocess.run(shlex.split(post_sync), cwd=str(dest))
        if result.returncode != 0:
            print(f"  ERROR: post_sync failed (exit {result.returncode})", file=sys.stderr)
            logging.warning("post_sync failed after sync: target=%s exit=%s", target_name, result.returncode)

    return True, file_hashes


def sync_target(config, target_name, git_internal, topology, cwd_worktree,
                apply=False, strict=False, force=False, verbose=False,
                show_diff=False) -> Tuple[bool, bool, Dict[str, List[Path]]]:
    """Orchestrate one target sync. Returns (success, has_warnings, actions)."""
    plan, errors = prepare_sync(
        config, target_name, git_internal, topology, cwd_worktree
    )
    if errors:
        for e in errors:
            print(f"  ERROR: {e}", file=sys.stderr)
        return False, False, {}

    source = plan.source
    dest = plan.dest
    actions = plan.actions
    warnings, errors = sync_classify_checks(
        plan.check_results, force=force, verbose=verbose
    )

    if errors:
        for e in errors:
            print(f"  ERROR: {e}", file=sys.stderr)
        print(f"  Use --force to override.", file=sys.stderr)
        return False, True, actions

    print(f"\n  Source: {source}")
    print(f"  Target: {dest}")

    report_target(target_name, actions, warnings, plan.excluded,
                  plan.mode_label, verbose=verbose)

    if show_diff:
        report_diff(source, dest, actions['M'])

    has_warnings = len(warnings) > 0

    if strict and has_warnings:
        print(f"\n  ABORT: --strict mode, {len(warnings)} warning(s)", file=sys.stderr)
        return True, True, actions

    if apply:
        ok, _ = execute_sync(
            source, dest, actions, plan.sync_files,
            plan.state_hashes, plan.target,
            target_name, git_internal,
        )
        if not ok:
            return False, has_warnings, actions
        add, update, delete = actions['A'], actions['M'], actions['D']
        if (len(add) + len(update) + len(delete)) > 0:
            print(f"\n    Done. A:{len(add)} M:{len(update)} D:{len(delete)}")
    else:
        add, update, delete = actions['A'], actions['M'], actions['D']
        if (len(add) + len(update) + len(delete)) > 0:
            print("\n    Run with --apply to execute.")

    return True, has_warnings, actions


def cmd_add_target(name, git_internal, topology) -> None:
    """Create empty orphan worktree and add to config."""
    project_root = git_internal.parent
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

    _, path, err = config_add_target(git_internal, name)
    if err:
        print(f"Error: {err}", file=sys.stderr)
        sys.exit(1)

    print(f"\nCreated worktree: {name}")
    print(f"  Path: {wt_path}")
    print(f"  Branch: {name} (orphan, empty)")
    print(f"\nAdded to config: {path}")
    print(f"  Source: master")
    print(f"  Delete policy: tracked_only")


def cmd_remove_target(name, git_internal, topology) -> None:
    """Remove worktree, branch, config entry, and state."""
    project_root = git_internal.parent
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


@dataclass
class Context:
    """Resolved execution context."""
    role: str               # 'source' | 'target' | 'project'
    project_root: Path      # project root (parent of .bare/)
    git_internal: Path      # .bare/ path
    source_name: str        # source worktree name
    source_path: Path       # source worktree path
    target_names: List[str] # resolved target list
    topology: str           # 'bare' | 'worktree' | 'repo'
    cwd_worktree: Path      # worktree containing cwd


def resolve_project_root(start: Path) -> Optional[Path]:
    """Find project root by searching upward for .bare/ directory."""
    p = start.resolve()
    while True:
        if (p / ".bare").is_dir():
            return p
        parent = p.parent
        if parent == p:
            return None
        p = parent


def resolve_context(c_path: Optional[str], source_flag: Optional[str],
                    target_flags: Optional[List[str]]) -> Context:
    """Resolve execution context from flags and cwd."""
    # Step 1: determine start path
    start = Path(c_path).resolve() if c_path else Path.cwd().resolve()

    # Step 2: find project root
    project_root = resolve_project_root(start)
    if project_root is None:
        print("Error: not inside a sync-worktree project (no .bare/ found)", file=sys.stderr)
        sys.exit(1)

    git_internal = project_root / ".bare"

    # Step 3: detect topology and worktrees
    topology, _, _ = git_detect_topology(cwd=str(start))
    worktrees = git_parse_worktrees(git_internal)

    # Step 4: read config to determine source/target roles
    config, _ = config_read(git_internal)
    configured_targets = list(config.get("targets", {}).keys()) if config else []

    # Step 5: determine which worktree cwd is in
    cwd_worktree = start
    cwd_branch = None
    for wt in worktrees:
        try:
            start.relative_to(wt["path"])
            cwd_worktree = wt["path"]
            cwd_branch = wt.get("branch")
            break
        except ValueError:
            continue

    # Step 6: determine role
    # Source worktrees are those referenced as "source" in config targets
    source_names = set()
    if config:
        for t_cfg in config.get("targets", {}).values():
            source_names.add(t_cfg.get("source", "master"))
    if not source_names:
        source_names.add("master")

    if source_flag:
        role = "source"
        source_name = source_flag
    elif cwd_branch in configured_targets:
        role = "target"
        source_name = config["targets"][cwd_branch].get("source", "master") if config else "master"
    elif cwd_branch in source_names:
        role = "source"
        source_name = cwd_branch
    else:
        role = "project"
        source_name = next(iter(source_names))

    # Step 7: resolve source path
    source_path = git_resolve_worktree(source_name, git_internal)
    if source_path is None:
        source_path = project_root / source_name

    # Step 8: resolve targets
    if target_flags:
        target_names = target_flags
    elif role == "source":
        target_names = configured_targets
    elif role == "target":
        target_names = [cwd_branch] if cwd_branch else []
    else:
        target_names = []

    return Context(
        role=role,
        project_root=project_root,
        git_internal=git_internal,
        source_name=source_name,
        source_path=source_path,
        target_names=target_names,
        topology=topology,
        cwd_worktree=cwd_worktree,
    )


def print_context_header(ctx: Context, mode: str = "dry-run", quiet: bool = False) -> None:
    """Print resolved direction header."""
    if quiet:
        return
    print(f"Context: source={ctx.source_name}")
    print(f"Targets: {', '.join(ctx.target_names) if ctx.target_names else '(none)'}")
    print(f"Mode: {mode}")
    print(f"Role: {ctx.role}")


# -- init command -------------------------------------------------------------

def cmd_init(path: str, url: Optional[str] = None, bare: bool = False,
             branch: Optional[str] = None) -> None:
    """Initialize a new sync-worktree project."""
    cwd = Path(path).resolve()
    cwd.mkdir(parents=True, exist_ok=True)
    bare_dir = cwd / ".bare"

    if bare_dir.exists():
        print(f"Error: .bare/ already exists in {cwd}", file=sys.stderr)
        sys.exit(1)

    if url:
        # Clone from remote
        try:
            git_run("clone", "--bare", url, str(bare_dir))
            logging.info("Cloned bare repo to .bare/")
        except RuntimeError as e:
            print(f"Error: Failed to clone: {e}", file=sys.stderr)
            sys.exit(1)
    elif bare:
        # Local init
        try:
            git_run("init", "--bare", str(bare_dir))
            logging.info("Initialized bare repo at .bare/")
        except RuntimeError as e:
            print(f"Error: Failed to init: {e}", file=sys.stderr)
            sys.exit(1)
        # Seed empty commit so worktree add works
        try:
            tree = git_run("hash-object", "-t", "tree", "/dev/null", cwd=bare_dir).strip()
            commit_hash = git_run("commit-tree", tree, "-m", "init", cwd=bare_dir).strip()
            branch_name = branch or "master"
            git_run("update-ref", f"refs/heads/{branch_name}", commit_hash, cwd=bare_dir)
            logging.info(f"Seeded {branch_name} with empty commit")
        except RuntimeError as e:
            print(f"Error: Failed to seed repo: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        print("Error: specify --url <url> or --bare for local init", file=sys.stderr)
        sys.exit(1)

    git_write_pointer(cwd)

    # Determine branch name
    if branch:
        branch_name = branch
    elif url:
        # Read HEAD from cloned bare
        ok, head_ref = git_try("symbolic-ref", "HEAD", cwd=bare_dir)
        if ok and head_ref.startswith("refs/heads/"):
            branch_name = head_ref.removeprefix("refs/heads/")
        else:
            branch_name = "master"
    else:
        branch_name = branch or "master"

    try:
        git_run("worktree", "add", branch_name, branch_name, cwd=cwd)
        logging.info(f"Created source worktree: {branch_name}")
    except RuntimeError as e:
        print(f"Error: Failed to create worktree: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"\nInitialized sync-worktree project")
    print(f"  Project: {cwd}")
    print(f"  Source:  {cwd / branch_name}")
    print(f"\nNext: cd {cwd / branch_name}")
    print(f"      sync-worktree target add release")


# -- migrate command ----------------------------------------------------------

def _finalize_migration(cwd, bare_dir, current_branch) -> Path:
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


def cmd_migrate(path: Optional[str] = None, dry_run: bool = False) -> None:
    """Convert existing regular repo to bare + worktree structure."""
    cwd = Path(path).resolve() if path else Path.cwd().resolve()
    git_dir = cwd / ".git"

    if not git_dir.exists() or not git_dir.is_dir():
        print(f"Error: Not a git repository: {cwd}", file=sys.stderr)
        sys.exit(1)

    try:
        current_branch = git_run("rev-parse", "--abbrev-ref", "HEAD", cwd=cwd)
    except RuntimeError as e:
        print(f"Error: Failed to get current branch: {e}", file=sys.stderr)
        sys.exit(1)

    if current_branch == "HEAD":
        print("Error: Detached HEAD. Checkout a branch first.", file=sys.stderr)
        sys.exit(1)

    bare_dir = cwd / ".bare"
    if bare_dir.exists():
        print("Error: .bare/ already exists", file=sys.stderr)
        sys.exit(1)

    if dry_run:
        print(f"Would migrate: {cwd}")
        print(f"  .git/ → .bare/")
        print(f"  Source worktree: {cwd / current_branch}")
        return

    logging.info(f"Migrating repo to bare + worktree (branch: {current_branch})")

    try:
        git_dir.rename(bare_dir)
        logging.info("Renamed .git → .bare")
    except OSError as e:
        print(f"Error: Failed to rename .git: {e}", file=sys.stderr)
        sys.exit(1)

    git_write_pointer(cwd)
    wt_path = _finalize_migration(cwd, bare_dir, current_branch)

    print(f"\nMigrated to sync-worktree project")
    print(f"  Source: {wt_path} ({current_branch})")
    print(f"\nNext: cd {current_branch}")
    print(f"      sync-worktree target add release")


# -- CLI parser ---------------------------------------------------------------

def build_cli_parser() -> argparse.ArgumentParser:
    epilog = """\
workflow (run from source worktree, or use -C PATH from anywhere):

  1. Create project
     %(prog)s init <path> --bare              Local empty repo
     %(prog)s init <path> --url <url>         Clone from remote
                          [--branch <name>]   Source branch (default: main)

  2. Add targets
     %(prog)s target add <name>               Create target worktree + config
                   [--exclude <pat>...]        Patterns to skip (gitignore-style)
                   [--include <pat>...]        Override excludes
                   [--protect <pat>...]        Never delete in target
                   [--delete-policy never|unlisted|tracked_only]

  3. Preview sync (dry-run, always safe)
     %(prog)s sync                            All targets
     %(prog)s sync --target <name>...         Specific targets
                   [--source <worktree>]      Explicit source
                   [--diff]                   Show file diffs
                   [--strict]                 Warnings → exit 2
                   [-v | -q]

  4. Apply sync (writes files)
     %(prog)s sync --apply                    All targets
     %(prog)s sync --target <name> --apply    Specific target
                   [--force]                  Skip pre-sync checks

  5. Inspect
     %(prog)s status [--target <name>...]     Source vs target diff summary
     %(prog)s config show                     Resolved config
     %(prog)s target list                     All registered targets

  Run from anywhere:
     %(prog)s -C ~/projects/myapp/master sync --apply

  Subcommand details: %(prog)s <command> --help
"""
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--log-dir", default=None, help="Log directory")
    common.add_argument("--no-log", action="store_true", help="Disable file logging")
    common.add_argument("--json", action="store_true", help="Machine-readable output")

    parser = HelpOnErrorParser(
        description="Source → target worktree sync. Dry-run by default.",
        epilog=epilog,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        parents=[common],
    )
    parser.add_argument("-C", dest="c_path", default=None, metavar="PATH",
                       help="Run as if invoked from PATH (resolves source/target context)")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    sub = parser.add_subparsers(dest="command", required=True)

    # init
    p = sub.add_parser("init", help="Initialize project", parents=[common],
                       description="Create a new bare+worktree project layout.",
                       epilog="Use --bare for local-only, --url to clone from remote.",
                       formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("path", help="Project root directory (created if absent)")
    p.add_argument("--bare", action="store_true", help="Local init without remote (creates empty repo)")
    p.add_argument("--url", default=None, help="Clone bare repo from this remote URL")
    p.add_argument("--branch", default=None, help="Source branch name (default: main)")
    p.set_defaults(command="init")

    # migrate
    p = sub.add_parser("migrate", help="Convert repo to bare+worktree", parents=[common])
    p.add_argument("path", nargs="?", default=None, help="Repo path (default: cwd)")
    p.add_argument("--dry-run", action="store_true", help="Preview only")
    p.add_argument("--source", default=None, help="Source worktree name")
    p.set_defaults(command="migrate")

    # target (with sub-subcommands)
    p_target = sub.add_parser("target", help="Manage targets", parents=[common])
    target_sub = p_target.add_subparsers(dest="target_command", required=True)

    p = target_sub.add_parser("add", help="Add target worktree", parents=[common],
                              description="Create a new target worktree and register it in config.",
                              epilog="After adding, use `sync --target <name>` to preview, then `--apply` to populate.",
                              formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("name", help="Target worktree name (becomes directory name)")
    p.add_argument("--source", default=None, help="Source worktree (default: auto from context)")
    p.add_argument("--exclude", nargs="*", default=None, help="Gitignore-style patterns to exclude from sync")
    p.add_argument("--include", nargs="*", default=None, help="Gitignore-style patterns to include (overrides exclude)")
    p.add_argument("--protect", nargs="*", default=None, help="Patterns for target files that should never be deleted")
    p.add_argument("--delete-policy", default=None, choices=["never", "unlisted", "tracked_only"],
                   help="When to delete target files (default: never)")
    p.set_defaults(target_command="add")

    p = target_sub.add_parser("remove", help="Remove target", parents=[common])
    p.add_argument("name", help="Target name")
    p.add_argument("--source", default=None, help="Source worktree")
    p.set_defaults(target_command="remove")

    p = target_sub.add_parser("list", help="List targets", parents=[common])
    p.add_argument("--source", default=None, help="Source worktree")
    p.set_defaults(target_command="list")

    p_target.set_defaults(command="target")

    # config (with sub-subcommands)
    p_config = sub.add_parser("config", help="Configuration", parents=[common])
    config_sub = p_config.add_subparsers(dest="config_command", required=True)

    p = config_sub.add_parser("show", help="Show config", parents=[common])
    p.add_argument("--source", default=None, help="Source worktree")
    p.add_argument("--target", nargs="*", default=None, help="Target filter")
    p.set_defaults(config_command="show")

    p = config_sub.add_parser("schema", help="Show config schema", parents=[common])
    p.set_defaults(config_command="schema")

    p = config_sub.add_parser("init", help="Create default config", parents=[common])
    p.set_defaults(config_command="init")

    p_config.set_defaults(command="config")

    # status
    p = sub.add_parser("status", help="Show sync status", parents=[common])
    p.add_argument("--source", default=None, help="Source worktree")
    p.add_argument("--target", nargs="*", default=None, help="Target filter")
    p.set_defaults(command="status")

    # sync
    p = sub.add_parser("sync", help="Sync source → target(s)", parents=[common],
                       description="Compare source and target(s), preview changes (dry-run), or apply.",
                       epilog="Dry-run by default. Add --apply to write files.",
                       formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--source", default=None, help="Source worktree (default: auto from context)")
    p.add_argument("--target", nargs="*", default=None,
                   help="Target(s) to sync (default: all targets in source context, self in target context)")
    p.add_argument("--apply", action="store_true", help="Write changes to disk (without this, only previews)")
    p.add_argument("--strict", action="store_true", help="Treat warnings as errors (exit 2)")
    p.add_argument("--force", action="store_true", help="Skip pre-sync checks (uncommitted changes, etc.)")
    p.add_argument("--diff", action="store_true", help="Show file-level diffs in output")
    p.add_argument("-v", "--verbose", action="store_true", help="Verbose output")
    p.add_argument("-q", "--quiet", action="store_true", help="Quiet output")
    p.set_defaults(command="sync")

    return parser


# -- Command runners ----------------------------------------------------------

def run_init(args) -> None:
    setup_logging("init", [], log_dir=args.log_dir, no_log=args.no_log,
                  scope_label=Path(args.path).name)
    cmd_init(args.path, url=args.url, bare=args.bare, branch=args.branch)


def run_migrate(args) -> None:
    path = args.path or str(Path.cwd())
    setup_logging("migrate", [], log_dir=args.log_dir, no_log=args.no_log,
                  scope_label=Path(path).name)
    cmd_migrate(path=args.path, dry_run=args.dry_run)


def run_target(args) -> None:
    ctx = resolve_context(args.c_path, getattr(args, "source", None), None)
    setup_logging(
        ctx.topology, [getattr(args, "name", "")],
        log_dir=args.log_dir, no_log=args.no_log,
        scope_label=_get_scope_label(ctx.topology, ctx.git_internal, ctx.cwd_worktree),
    )

    if ctx.role == "target" and not getattr(args, "source", None):
        print("Error: cannot manage targets from a target worktree (use --source)", file=sys.stderr)
        sys.exit(1)

    if args.target_command == "add":
        cmd_add_target(args.name, ctx.git_internal, ctx.topology)
    elif args.target_command == "remove":
        cmd_remove_target(args.name, ctx.git_internal, ctx.topology)
    elif args.target_command == "list":
        config, _ = config_read(ctx.git_internal)
        if config:
            print(f"Source: {ctx.source_name}")
            targets = config.get("targets", {})
            for name, t_cfg in targets.items():
                print(f"  → {name} (source={t_cfg.get('source', 'master')})")
            if not targets:
                print("  (no targets configured)")
        else:
            print("Error: no config found", file=sys.stderr)
            sys.exit(1)


def run_config(args) -> None:
    if args.config_command == "schema":
        report_config_schema()
        return

    ctx = resolve_context(args.c_path, getattr(args, "source", None), None)
    setup_logging(
        ctx.topology, [],
        log_dir=args.log_dir, no_log=args.no_log,
        scope_label=_get_scope_label(ctx.topology, ctx.git_internal, ctx.cwd_worktree),
    )

    if args.config_command == "show":
        config, config_errors = config_read(ctx.git_internal)
        if config is None:
            for e in config_errors:
                print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        report_config(config)
    elif args.config_command == "init":
        config_init(ctx.git_internal, ctx.git_internal)


def run_status(args) -> None:
    ctx = resolve_context(args.c_path, args.source, getattr(args, "target", None))
    setup_logging(
        ctx.topology, [],
        log_dir=args.log_dir, no_log=args.no_log,
        scope_label=_get_scope_label(ctx.topology, ctx.git_internal, ctx.cwd_worktree),
    )
    print_context_header(ctx)
    state = state_read(ctx.git_internal)
    report_status(state)


def run_sync(args) -> None:
    ctx = resolve_context(args.c_path, args.source, getattr(args, "target", None))

    config, config_errors = config_read(ctx.git_internal)
    if config is None:
        for e in config_errors:
            print(f"Error: {e}", file=sys.stderr)
        print(f"\nRun: sync-worktree config init", file=sys.stderr)
        sys.exit(1)

    target_names = ctx.target_names or list(config.get("targets", {}).keys())
    setup_logging(
        ctx.topology, target_names,
        log_dir=args.log_dir, no_log=args.no_log,
        scope_label=_get_scope_label(ctx.topology, ctx.git_internal, ctx.cwd_worktree),
    )

    mode = "apply" if args.apply else "dry-run"
    if not args.quiet:
        print_context_header(ctx, mode=mode)

    logging.info(f"Starting sync: mode={mode}")

    if not target_names:
        print("Error: no targets to sync", file=sys.stderr)
        sys.exit(1)

    if args.apply and not state_check_gate(ctx.git_internal, args.apply, args.force):
        sys.exit(1)

    all_ok = True
    any_warnings = False
    all_actions = {}
    total_warnings = 0
    total_errors = 0

    for name in target_names:
        ok, has_warn, actions = sync_target(
            config, name, ctx.git_internal, ctx.topology, ctx.cwd_worktree,
            apply=args.apply, strict=args.strict, force=args.force,
            verbose=args.verbose, show_diff=args.diff,
        )
        if not ok:
            all_ok = False
            total_errors += 1
        if has_warn:
            any_warnings = True
            total_warnings += 1
        if actions:
            all_actions[name] = actions

    checks_passed = all_ok and (not args.strict or not any_warnings)
    state_save_run(ctx.git_internal, target_names, mode, checks_passed,
                   total_warnings, total_errors, all_actions)

    logging.info(f"Sync complete: ok={all_ok}, warnings={total_warnings}, errors={total_errors}")

    if not all_ok:
        sys.exit(1)
    elif args.strict and any_warnings:
        sys.exit(2)


# -- Main dispatch ------------------------------------------------------------

def main() -> None:
    parser = build_cli_parser()
    try:
        if len(sys.argv) == 1 and sys.stdin.isatty():
            parser.print_help()
            return

        args = parser.parse_args()

        if args.command == "init":
            run_init(args)
        elif args.command == "migrate":
            run_migrate(args)
        elif args.command == "target":
            run_target(args)
        elif args.command == "config":
            run_config(args)
        elif args.command == "status":
            run_status(args)
        elif args.command == "sync":
            run_sync(args)
        else:
            parser.print_help()
    except SystemExit:
        raise
    except Exception as e:
        print(f"{parser.prog}: error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
