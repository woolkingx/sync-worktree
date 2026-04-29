"""Git topology detection and context management."""

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple, Literal

from .exceptions import GitCommandError, TopologyError


@dataclass(frozen=True)
class TopologyContext:
    """Immutable context describing the current git topology."""
    mode: Literal["bare", "worktree", "repo"]
    project_root: Path
    git_internal: Path      # .bare/ or .git/ (common-dir for worktrees)
    cwd_worktree: Path
    current_branch: Optional[str] = None


def find_project_root(start: Path) -> Optional[Path]:
    """Find project root by searching upward for .bare/ or .git/."""
    p = start.resolve()
    # 優先找 .bare/ (因為它一定是 project root)
    while p != p.parent:
        if (p / ".bare").is_dir():
            return p
        p = p.parent
    
    # 沒找到 .bare，再找 .git (傳統 repo)
    p = start.resolve()
    while p != p.parent:
        if (p / ".git").is_dir():
            return p
        p = p.parent
    
    return None


def _git_run(*args, cwd=None, timeout=30) -> str:
    """Run git command, return stdout. Raises on failure."""
    result = subprocess.run(
        ["git"] + list(args),
        cwd=cwd, capture_output=True, text=True, timeout=timeout,
    )
    if result.returncode != 0:
        raise GitCommandError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _git_try(*args, cwd=None, timeout=30) -> Tuple[bool, str]:
    """Run git command, return (success, stdout)."""
    result = subprocess.run(
        ["git"] + list(args),
        cwd=cwd, capture_output=True, text=True, timeout=timeout,
    )
    return result.returncode == 0, result.stdout.strip()


def _git_parse_worktrees(git_common: Path) -> List[dict]:
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


def _find_containing_worktree(cwd: Path, worktrees: List[dict]) -> Optional[Path]:
    """Find which worktree contains the given path."""
    for wt in worktrees:
        try:
            cwd.relative_to(wt["path"])
            return wt["path"]
        except ValueError:
            continue
    return None


def _current_branch_in_worktree(worktree: Path) -> Optional[str]:
    """Get current branch name for a given worktree."""
    try:
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=worktree, capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            branch = result.stdout.strip()
            return branch if branch else None
    except Exception:
        pass
    return None


def detect_topology(cwd: Path) -> TopologyContext:
    """
    Auto-detect git topology: bare | worktree | repo.
    
    Returns TopologyContext with all resolved paths.
    
    Raises TopologyError if not inside a sync-worktree project.
    """
    start = cwd.resolve()
    
    # Step 1: Find project root (must have .bare/ or .git/)
    project_root = find_project_root(start)
    if project_root is None:
        raise TopologyError("Not inside a sync-worktree project (missing .bare/ or .git/)")
    
    # Step 2: Check for bare repo (.bare/ exists)
    bare_dir = project_root / ".bare"
    if bare_dir.is_dir():
        # Bare mode: worktrees are siblings of .bare/
        # Determine which worktree we're in
        worktrees = _git_parse_worktrees(bare_dir)
        cwd_wt = _find_containing_worktree(start, worktrees)
        
        return TopologyContext(
            mode="bare",
            project_root=project_root,
            git_internal=bare_dir,
            cwd_worktree=cwd_wt or start,
            current_branch=_current_branch_in_worktree(cwd_wt) if cwd_wt else None
        )
    
    # Step 3: Non-bare: use git commands to detect
    try:
        git_common = Path(_git_run("rev-parse", "--git-common-dir", cwd=str(start)))
        git_dir = Path(_git_run("rev-parse", "--git-dir", cwd=str(start)))
    except GitCommandError as e:
        raise TopologyError(f"Git command failed: {e}")
    
    # Resolve to absolute paths
    effective_base = start
    git_common = (effective_base / git_common).resolve()
    git_dir = (effective_base / git_dir).resolve()
    
    # Check if this is a worktree (multiple worktrees exist)
    worktrees = _git_parse_worktrees(git_common)
    if len(worktrees) > 1:
        # Worktree mode
        cwd_wt = _find_containing_worktree(start, worktrees)
        if cwd_wt is None:
            # Fallback: assume cwd is itself a worktree
            cwd_wt = start
        
        # Determine current branch for this worktree
        branch = None
        for wt in worktrees:
            if wt["path"] == cwd_wt:
                branch = wt.get("branch")
                break
        
        return TopologyContext(
            mode="worktree",
            project_root=project_root,
            git_internal=git_common,
            cwd_worktree=cwd_wt,
            current_branch=branch
        )
    
    # Step 4: Single repo mode
    return TopologyContext(
        mode="repo",
        project_root=project_root,
        git_internal=git_dir,
        cwd_worktree=project_root,
        current_branch=_current_branch_in_worktree(project_root)
    )
