"""Path filtering using gitignore syntax (batch-optimized)."""

import atexit
import shutil
import subprocess
import tempfile
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Tuple


# Temp gitignore repo management
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
    """Create a temporary git repo to evaluate gitignore patterns."""
    repo = Path(tempfile.mkdtemp(prefix="sync-worktree-gitignore-"))
    _GITIGNORE_TEMP_DIRS.add(str(repo))
    subprocess.run(["git", "init", "-q"], cwd=repo, capture_output=True, text=True)
    (repo / ".gitignore").write_text("\n".join(patterns_key) + ("\n" if patterns_key else ""))
    return repo


def _gitignore_match_batch(paths, patterns_key) -> Dict[str, bool]:
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
    posix_paths = tuple(Path(p).as_posix() for p in paths)
    return _gitignore_match_batch(posix_paths, patterns_key)


def filter_pipeline(files, include_patterns=None, exclude_patterns=None) -> Tuple[List[Path], List[Path], List[Path]]:
    """
    Return files selected by config patterns, excluding `.git/`.
    
    Returns:
        (passed, unmatched, blocked) where:
          - passed: files that match include (if any) and not exclude
          - unmatched: files that didn't match include (when include specified)
          - blocked: files that matched exclude
    """
    # Always exclude .git directory
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


def filter_match(path: str, pattern: str) -> bool:
    """Single pattern gitignore match."""
    result = filter_match_batch([path], [pattern])
    return result.get(Path(path).as_posix(), False)
