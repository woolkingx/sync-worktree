"""SyncPlan dataclass definition."""

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Any


@dataclass(frozen=True)
class SyncPlan:
    """Immutable plan describing a sync operation."""
    source: Path
    dest: Path
    source_commit: str
    source_branch: Optional[str]
    dest_branch: Optional[str]
    target_name: str
    target_config: Dict[str, Any]    # resolved role config as dict
    sync_files: List[Path]           # Files to sync (after include/exclude)
    excluded_files: List[Path]       # Excluded by filter (unmatched + blocked)
    unmatched_files: List[Path]      # Didn't match include patterns
    blocked_files: List[Path]        # Matched exclude patterns
    actions: Dict[str, List[Path]]   # {'A': [...], 'M': [...], 'D': [...], 'U': [...], '!': [...], 'P': [...]}
    policy_context: Optional[Dict] = None   # will be filled by PolicyContext builder