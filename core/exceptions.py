"""Custom exceptions for sync-worktree."""


class SyncWorktreeError(Exception):
    """Base exception for all sync-worktree errors."""
    pass


class ConfigError(SyncWorktreeError):
    """Configuration parsing or validation error."""
    pass


class PolicyViolationError(SyncWorktreeError):
    """Policy check failed."""
    def __init__(self, result: 'PolicyResult'):
        self.result = result
        super().__init__(result.message)


class GitCommandError(SyncWorktreeError):
    """Git command failed."""
    pass


class TopologyError(SyncWorktreeError):
    """Topology detection or resolution error."""
    pass


class NotASyncWorktreeError(SyncWorktreeError):
    """Current directory is not inside a sync-worktree project."""
    pass


class SyncAbortedError(SyncWorktreeError):
    """Sync was aborted due to policy violations or user cancellation."""
    pass
