"""POL-TOP-002: Target worktree must be clean (no uncommitted changes)."""

from pathlib import Path
from typing import List

from policy.base import Policy, PolicyContext, PolicyResult, policy
from core.git import git_status_porcelain


@policy("POL-TOP-002", "topology", "error")
class TargetCleanlinessPolicy(Policy):
    """Target worktree must have no uncommitted changes before sync."""
    
    def check(self, ctx: PolicyContext) -> PolicyResult:
        # Allow opt-out via config
        if not ctx.target_config.get("require_clean_target", True):
            return self._pass()
        
        dest = ctx.dest_path
        
        # Check git status
        status_lines = git_status_porcelain(dest)
        if not status_lines:
            return self._pass()
        
        # Parse status to get count
        changes = []
        for line in status_lines:
            status = line[:2].strip()
            filename = line[3:]
            changes.append((status, filename))
        
        # Separate by type (staged, unstaged, untracked)
        all_issues = [(s, f) for s, f in changes if s[0] in 'MADRC?' or s[1] in 'MADRC?']
        if not all_issues:
            return self._pass()
        
        # Build evidence
        evidence = []
        for status, filename in all_issues[:5]:
            evidence.append(f"{status} {filename}")
        if len(all_issues) > 5:
            evidence.append(f"... and {len(all_issues) - 5} more")
        
        return self._fail(
            message=f"Target worktree has {len(all_issues)} uncommitted change(s)",
            evidence=evidence,
            suggestion="Stash, commit, or discard changes before syncing",
            fix_commands=[
                f"git -C {dest} status  # review changes",
                f"git -C {dest} stash     # temporary stash",
                f"# or: git -C {dest} add . && git commit -m 'WIP'",
                f"# or: git -C {dest} reset --hard  # DESTROY UNSAVED WORK"
            ]
        )
