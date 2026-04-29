"""Compute a sync plan (what would happen)."""

import filecmp
import json
import os
from dataclasses import asdict
from pathlib import Path
from typing import Optional, Dict, List, Set, Tuple

from core.git import (
    git_revparse_head,
    git_tracked_files,
    git_current_branch,
)
from core.filter import filter_pipeline, filter_match
from planner.plan import SyncPlan


def compute_plan(topology, rule_config, settings, target_name: str,
                 source_override: Optional[str] = None) -> Optional[SyncPlan]:
    """
    Compute the immutable sync plan for a target.
    
    Returns:
        SyncPlan if successful, None on error (error already printed).
    """
    # 1. Resolve target binding
    tbinding = rule_config.targets.get(target_name)
    if tbinding is None:
        print(f"Error: Unknown target '{target_name}'", file=__import__('sys').stderr)
        return None
    
    # Resolve effective role config
    role_cfg = rule_config.get_target_role(target_name)  # RoleDefinition
    
    # 2. Source resolution
    source_name = source_override or tbinding.source
    if not source_name:
        # Default to current branch (if running from a worktree)
        source_name = topology.current_branch or "master"
    source_path, source_mode = _resolve_source_path(source_name, topology)
    if source_path is None:
        print(f"Error: Source '{source_name}' not found", file=__import__('sys').stderr)
        return None
    
    # 3. Dest worktree resolution (target itself)
    dest_path = _resolve_worktree_path(target_name, topology.git_internal)
    if dest_path is None:
        print(f"Error: Target worktree '{target_name}' not found (create with 'target add')",
              file=__import__('sys').stderr)
        return None
    
    if source_path == dest_path:
        print("Error: Source and target are the same worktree", file=__import__('sys').stderr)
        return None
    
    # 4. Get source state
    if source_mode == "worktree":
        source_commit = git_revparse_head(source_path)
        source_branch = git_current_branch(source_path)
        all_source_files = git_tracked_files(source_path)  # relative paths
    else:
        source_commit = ""
        source_branch = None
        all_source_files = _list_source_files(source_path)
    dest_branch = git_current_branch(dest_path)
    
    # 5. Apply include/exclude filters
    include_patterns = role_cfg.include   # List[str]
    exclude_patterns = role_cfg.exclude   # List[str]
    
    sync_files, unmatched, blocked = filter_pipeline(
        all_source_files, include_patterns, exclude_patterns
    )
    excluded_files = unmatched + blocked   # for reporting
    
    # 6. Compute file-level actions by comparing source vs dest
    state_hashes = _load_state_hashes(topology.git_internal, target_name)
    actions = _compute_file_actions(
        source_path,
        dest_path,
        sync_files,
        role_cfg,
        state_hashes=state_hashes,
    )
    
    # 7. Build plan
    plan = SyncPlan(
        source=source_path,
        dest=dest_path,
        source_commit=source_commit,
        source_branch=source_branch,
        dest_branch=dest_branch,
        target_name=target_name,
        target_config=asdict(role_cfg),  # convert to dict for policy engine
        sync_files=sync_files,
        excluded_files=excluded_files,
        unmatched_files=unmatched,
        blocked_files=blocked,
        actions=actions,
        policy_context=None,  # will be filled by PolicyEngine
    )
    return plan


def _resolve_source_path(source_name: str, topology) -> Tuple[Optional[Path], str]:
    """Resolve a source name to either a worktree path or a filesystem bundle path."""
    worktree_path = _resolve_worktree_path(source_name, topology.git_internal)
    if worktree_path is not None:
        return worktree_path, "worktree"

    path_candidate = Path(source_name)
    candidates = []
    if path_candidate.is_absolute():
        candidates.append(path_candidate)
    else:
        candidates.append(topology.project_root / path_candidate)
        candidates.append(path_candidate.resolve())

    for candidate in candidates:
        if candidate.exists():
            return candidate, "bundle"

    return None, "missing"


def _resolve_worktree_path(name: str, git_internal: Path) -> Optional[Path]:
    """
    Resolve a worktree name (branch name or directory name) to its absolute path.
    Searches all worktrees registered in the repository.
    """
    from core.git import git_parse_worktrees
    worktrees = git_parse_worktrees(git_internal)
    for wt in worktrees:
        if wt.get("branch") == name or wt["path"].name == name:
            return wt["path"]
    return None


def _list_source_files(root: Path) -> List[Path]:
    files = []
    for current_root, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in {".git", "__pycache__"}]
        current_dir = Path(current_root)
        for filename in filenames:
            if filename.endswith(".pyc") or filename == ".DS_Store":
                continue
            file_path = current_dir / filename
            files.append(file_path.relative_to(root))
    return sorted(files)


def _compute_file_actions(
    source: Path,
    dest: Path,
    sync_files: List[Path],
    role_cfg,
    state_hashes: Optional[Dict[str, str]] = None,
) -> Dict[str, List[Path]]:
    """
    Determine what to do for each file.
    
    Returns dict with keys: A (add), M (modify), D (delete), U (unchanged), ! (missing), P (protected)
    """
    actions = {'A': [], 'M': [], 'D': [], 'U': [], '!': [], 'P': []}
    sync_set = set(sync_files)
    
    # Phase 1: compare source vs dest for files in sync_set
    for rel in sync_files:
        src = source / rel
        dst = dest / rel
        
        if not src.exists():
            actions['!'].append(rel)
        elif not dst.exists():
            actions['A'].append(rel)
        else:
            try:
                if not filecmp.cmp(str(src), str(dst), shallow=False):
                    actions['M'].append(rel)
                else:
                    actions['U'].append(rel)
            except (OSError, ValueError):
                actions['M'].append(rel)
    
    # Phase 2: Deletion & protection analysis
    delete_policy = getattr(role_cfg, 'delete_policy', 'never')
    protect_patterns = getattr(role_cfg, 'protect', [])

    # Gather target-side candidates from the filesystem, not only tracked files.
    existing_files = _list_worktree_files(dest)

    if delete_policy == "never":
        candidates: Set[Path] = set()
    elif delete_policy == "tracked_only":
        tracked_state = {
            Path(rel_path)
            for rel_path in (state_hashes or {}).keys()
            if rel_path
        }
        candidates = {rel_path for rel_path in existing_files if rel_path in tracked_state}
    else:
        candidates = existing_files

    candidates -= sync_set
    
    # Classify candidates: D (delete) vs P (protected from deletion)
    for rel in candidates:
        # Check protect patterns
        is_protected = False
        for pattern in protect_patterns:
            if filter_match(str(rel), pattern):
                is_protected = True
                break
        if is_protected:
            actions['P'].append(rel)
        else:
            actions['D'].append(rel)
    
    return actions


def _list_worktree_files(root: Path) -> Set[Path]:
    files = set()
    for current_root, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        current_dir = Path(current_root)
        for filename in filenames:
            file_path = current_dir / filename
            files.add(file_path.relative_to(root))
    return files


def _load_state_hashes(git_internal: Path, target_name: str) -> Dict[str, str]:
    state_path = git_internal / "sync-worktree.state.json"
    if not state_path.exists():
        return {}

    try:
        data = json.loads(state_path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}

    candidates = [
        data.get("last_sync", {}).get(target_name, {}).get("file_hashes", {}),
        data.get("targets", {}).get(target_name, {}).get("file_hashes", {}),
        data.get("file_hashes", {}),
    ]
    for candidate in candidates:
        if isinstance(candidate, dict):
            return {str(key): str(value) for key, value in candidate.items()}
    return {}
