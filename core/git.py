"""Git command wrappers with error handling and caching."""

import subprocess
from functools import lru_cache
from pathlib import Path
from typing import List, Tuple, Optional

from .exceptions import GitCommandError


def git_run(*args, cwd=None, timeout=30) -> str:
    """Run git command, return stdout. Raises GitCommandError on failure."""
    try:
        result = subprocess.run(
            ["git"] + list(args),
            cwd=cwd, capture_output=True, text=True, timeout=timeout,
        )
        if result.returncode != 0:
            raise GitCommandError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
        return result.stdout.strip()
    except subprocess.TimeoutExpired as e:
        raise GitCommandError(f"git command timed out: {e}")
    except FileNotFoundError:
        raise GitCommandError("git command not found (is Git installed?)")


def git_try(*args, cwd=None, timeout=30) -> Tuple[bool, str]:
    """Run git command, return (success, stdout)."""
    try:
        result = subprocess.run(
            ["git"] + list(args),
            cwd=cwd, capture_output=True, text=True, timeout=timeout,
        )
        return result.returncode == 0, result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False, ""


@lru_cache(maxsize=128)
def git_revparse_head(cwd: Path) -> str:
    """Get HEAD commit SHA of a worktree (cached)."""
    return git_run("rev-parse", "HEAD", cwd=cwd)


@lru_cache(maxsize=128)
def git_tracked_files(cwd: Path) -> List[Path]:
    """Get all tracked files (git ls-files)."""
    output = git_run("ls-files", cwd=cwd)
    if not output:
        return []
    return sorted(Path(line) for line in output.split("\n") if line)


def git_status_porcelain(cwd: Path) -> List[str]:
    """Get git status --porcelain output as list of lines."""
    ok, output = git_try("status", "--porcelain", cwd=cwd)
    return output.splitlines() if ok and output else []


def git_diff_name_status(cwd: Path, commit_a: str, commit_b: str = "HEAD") -> List[Tuple[str, str]]:
    """
    Get diff between two commits as (status, filename) tuples.
    Uses: git diff-index --name-status <commit_a> <commit_b>
    """
    output = git_run("diff-index", "--name-status", commit_a, commit_b, cwd=cwd)
    result = []
    for line in output.splitlines():
        if not line:
            continue
        parts = line.split("\t", 1)
        if len(parts) == 2:
            status, filename = parts
            result.append((status, Path(filename)))
    return result


def git_check_ignore(files: List[Path], cwd: Path) -> set:
    """
    Check which files are ignored by git.
    Returns set of relative paths that match .gitignore.
    """
    if not files:
        return set()
    
    # Batch check using git check-ignore --stdin
    input_data = "\n".join(str(f) for f in files)
    result = subprocess.run(
        ["git", "check-ignore", "--stdin"],
        input=input_data, cwd=cwd, capture_output=True, text=True, timeout=30,
    )
    if result.returncode == 0 and result.stdout:
        return set(result.stdout.strip().splitlines())
    return set()


def git_branch_exists(cwd: Path, branch: str) -> bool:
    """Check if a branch exists in the repository."""
    ok, _ = git_try("branch", "--list", branch, cwd=cwd)
    return ok


def git_current_branch(cwd: Path) -> Optional[str]:
    """Get current branch name (or None if in detached HEAD)."""
    ok, branch = git_try("branch", "--show-current", cwd=cwd)
    return branch if ok and branch else None


def git_create_worktree(project_root: Path, name: str, branch: Optional[str] = None) -> Path:
    """
    Create a new worktree.
    Returns path to the new worktree.
    """
    wt_path = project_root / name
    
    # Check if worktree already exists
    if wt_path.exists():
        raise GitCommandError(f"Worktree path already exists: {wt_path}")
    
    args = ["worktree", "add"]
    if branch:
        args.append(branch)
    else:
        args.append("--detach")  # Create detached worktree
    
    args.append(str(wt_path))
    
    git_run(*args, cwd=project_root)
    return wt_path


def git_setup_orphan_branch(wt_path: Path, branch_name: str) -> None:
    """
    Create an orphan branch in a worktree (empty initial commit).
    """
    try:
        # Checkout orphan branch
        git_run("checkout", "--orphan", branch_name, cwd=wt_path)
        
        # Remove all files (may fail if directory is already empty)
        subprocess.run(
            ["git", "rm", "-rf", "."],
            cwd=wt_path, capture_output=True, timeout=10
        )
        
        # Create empty commit
        subprocess.run(
            ["git", "commit", "--allow-empty", "-m", "init: empty orphan branch"],
            cwd=wt_path, capture_output=True, timeout=10,
            env={"GIT_AUTHOR_NAME": "init", "GIT_AUTHOR_EMAIL": "init@local",
                 "GIT_COMMITTER_NAME": "init", "GIT_COMMITTER_EMAIL": "init@local"}
        )
    except Exception as e:
        raise GitCommandError(f"Failed to setup orphan branch: {e}")


def git_remove_worktree(project_root: Path, name: str) -> None:
    """Remove a worktree and its branch."""
    wt_path = project_root / name
    if not wt_path.exists():
        raise GitCommandError(f"Worktree not found: {wt_path}")
    
    git_run("worktree", "remove", str(wt_path), "--force", cwd=project_root)
    git_run("branch", "-D", name, cwd=project_root)


def git_parse_worktrees(git_common: Path) -> List[dict]:
    """Parse `git worktree list --porcelain` into structured data."""
    try:
        result = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=git_common, capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            return []
    except (subprocess.TimeoutExpired, FileNotFoundError):
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


def git_resolve_worktree(name: str, git_common: Path) -> Optional[Path]:
    """Resolve worktree name to its filesystem path."""
    worktrees = git_parse_worktrees(git_common)
    for wt in worktrees:
        if wt.get("branch") == name or wt["path"].name == name:
            return wt["path"]
    return None


def git_current_branch(worktree: Path) -> Optional[str]:
    """Get current branch name for a worktree."""
    ok, branch = git_try("branch", "--show-current", cwd=worktree)
    return branch if ok and branch else None
