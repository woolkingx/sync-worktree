"""Git-based sync executor (simple, stable)."""

import filecmp
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, List

from core.git import git_run
from core.exceptions import SyncAbortedError


class GitExecutor:
    """Execute sync plan using shutil + git add -A."""
    
    def apply(self, plan, settings, backup_branch: bool = None) -> Dict:
        """
        Execute the sync plan.
        
        Returns dict with stats: files_copied, files_deleted, backup_branch, commit_sha.
        """
        if backup_branch is None:
            backup_branch = settings.behavior.create_backup_branch
        
        dest = plan.dest
        source = plan.source
        
        # Create backup branch if configured
        backup_name = None
        if backup_branch:
            backup_name = self._create_backup(dest)
        
        try:
            # 1. Copy files (A + M)
            for rel in plan.actions['A'] + plan.actions['M']:
                src_file = source / rel
                dst_file = dest / rel
                dst_file.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src_file, dst_file)  # preserves mode, times
            
            # 2. Delete files (D)
            for rel in plan.actions['D']:
                dst_file = dest / rel
                if dst_file.exists():
                    dst_file.unlink()
                    self._remove_empty_parents(dst_file.parent, dest)
            
            # 3. Update Git index
            index_updated = True
            try:
                subprocess.run(
                    ["git", "add", "-A"],
                    cwd=dest,
                    check=True,
                    capture_output=True,
                    text=True,
                )
            except subprocess.CalledProcessError as e:
                stderr = (e.stderr or "").strip()
                stdout = (e.stdout or "").strip()
                message = "\n".join(part for part in [stderr, stdout] if part)
                if "index.lock" in message or "Read-only file system" in message:
                    index_updated = False
                    print(
                        f"Warning: git index update skipped for {dest}: {message}",
                        file=sys.stderr,
                    )
                else:
                    raise
            
            # 4. (Optional) Commit
            commit_sha = None
            if settings.behavior.auto_commit:
                commit_msg = settings.behavior.commit_message_template.format(
                    target=plan.target_name,
                    source=plan.source.name if hasattr(plan.source, 'name') else str(plan.source),
                    commit=plan.source_commit[:8],
                    files=f"+{len(plan.actions['A'])} ~{len(plan.actions['M'])} -{len(plan.actions['D'])}"
                )
                subprocess.run(["git", "commit", "-m", commit_msg], cwd=dest, check=True)
                commit_sha = git_run("rev-parse", "HEAD", cwd=dest)
            
            return {
                "success": True,
                "files_copied": len(plan.actions['A']) + len(plan.actions['M']),
                "files_deleted": len(plan.actions['D']),
                "backup_branch": backup_name,
                "commit_sha": commit_sha,
                "index_updated": index_updated,
            }
            
        except Exception as e:
            if backup_name:
                print(f"\nSync failed. Recovery: git -C {dest} reset --hard {backup_name}", file=sys.stderr)
            raise SyncAbortedError(f"Sync execution failed: {e}") from e
    
    def _create_backup(self, dest: Path) -> str:
        """Create backup branch with current HEAD."""
        timestamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
        backup_name = f"backup-sync-{timestamp}"
        current = git_run("rev-parse", "HEAD", cwd=dest)
        subprocess.run(["git", "branch", backup_name, current], cwd=dest, check=True)
        return backup_name
    
    def _remove_empty_parents(self, parent: Path, dest: Path):
        """Remove empty parent directories recursively."""
        while parent != dest and parent != parent.parent:
            try:
                next(parent.iterdir())
                break
            except StopIteration:
                try:
                    parent.rmdir()
                except OSError:
                    break
                parent = parent.parent
