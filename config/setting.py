"""Setting configuration - personal/environment preferences (mutable, multi-layer)."""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from core.exceptions import ConfigError


@dataclass(frozen=True)
class LoggingSettings:
    level: str = "INFO"  # DEBUG, INFO, WARN, ERROR
    dir: str = "./logs"
    format: str = "text"  # "text" | "json"


@dataclass(frozen=True)
class BehaviorSettings:
    dry_run_default: bool = True
    auto_commit: bool = False
    commit_message_template: str = "sync: {target} from {source} @ {commit}"
    create_backup_branch: bool = True
    backup_branch_prefix: str = "backup/sync-{timestamp}"
    cleanup_backups_after_days: int = 7
    cleanup_logs_after_days: int = 30


@dataclass(frozen=True)
class OutputSettings:
    json: bool = False
    verbose: bool = False
    quiet: bool = False
    show_diff: bool = False
    color: str = "auto"  # "always" | "never" | "auto"


@dataclass(frozen=True)
class ReportSettings:
    max_lines: int = 80
    changed_preview_limit: int = 12
    default_detail: str = "compact"  # "compact" | "details" | "full"


@dataclass(frozen=True)
class CommandStyleSettings:
    dirty_target_strategy: str = "ask"  # "ask" | "stash" | "commit"
    commit_message_template: str = "prepare {target} sync"
    preferred_remote: str = "origin"
    allow_commands: list = field(default_factory=lambda: [
        "git status",
        "git diff",
        "git add",
        "git commit",
        "git stash",
    ])
    deny_commands: list = field(default_factory=lambda: [
        "git reset --hard",
        "git clean",
        "git push --force",
    ])


@dataclass(frozen=True)
class HooksSettings:
    pre_sync: Optional[str] = None   # shell command string
    post_sync: Optional[str] = None  # shell command string


@dataclass(frozen=True)
class ExperimentalSettings:
    parallel_sync: bool = False
    max_workers: int = 3
    use_git_archive: bool = False


@dataclass(frozen=True)
class Settings:
    """
    Immutable settings object (merged from multiple sources).
    
    Sources (priority order, low to high):
      1. DEFAULTS (built-in)
      2. ~/.config/sync-worktree/settings.json (global user)
      3. <project_root>/setting.config.json (project-wide)
      4. <worktree>/.setting.config.json (per-worktree)
      5. CLI flags (highest)
    """
    executor: str = "git"  # Currently only "git" is supported
    logging: LoggingSettings = field(default_factory=LoggingSettings)
    behavior: BehaviorSettings = field(default_factory=BehaviorSettings)
    output: OutputSettings = field(default_factory=OutputSettings)
    report: ReportSettings = field(default_factory=ReportSettings)
    command_style: CommandStyleSettings = field(default_factory=CommandStyleSettings)
    hooks: HooksSettings = field(default_factory=HooksSettings)
    experimental: ExperimentalSettings = field(default_factory=ExperimentalSettings)
    
    @classmethod
    def DEFAULTS(cls) -> dict:
        """Get default settings as dict."""
        return {
            "executor": "git",
            "logging": {"level": "INFO", "dir": "./logs", "format": "text"},
            "behavior": {
                "dry_run_default": True,
                "auto_commit": False,
                "commit_message_template": "sync: {target} from {source} @ {commit}",
                "create_backup_branch": True,
                "backup_branch_prefix": "backup/sync-{timestamp}",
                "cleanup_backups_after_days": 7,
                "cleanup_logs_after_days": 30,
            },
            "output": {"json": False, "verbose": False, "quiet": False, "show_diff": False, "color": "auto"},
            "report": {"max_lines": 80, "changed_preview_limit": 12, "default_detail": "compact"},
            "command_style": {
                "dirty_target_strategy": "ask",
                "commit_message_template": "prepare {target} sync",
                "preferred_remote": "origin",
                "allow_commands": ["git status", "git diff", "git add", "git commit", "git stash"],
                "deny_commands": ["git reset --hard", "git clean", "git push --force"],
            },
            "hooks": {"pre_sync": None, "post_sync": None},
            "experimental": {"parallel_sync": False, "max_workers": 3, "use_git_archive": False},
        }


def _deep_update(base: dict, updates: dict) -> dict:
    """Deep merge two dictionaries (updates into base)."""
    result = base.copy()
    for key, value in updates.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_update(result[key], value)
        else:
            result[key] = value
    return result


def load_settings(topology_context, cli_overrides: Optional[dict] = None) -> Settings:
    """
    Load settings from multiple sources and merge.
    
    Priority (low to high):
      1. Built-in defaults
      2. ~/.config/sync-worktree/settings.json
      3. <project_root>/setting.config.json
      4. <worktree>/.setting.config.json
      5. CLI flags
    """
    # Start with defaults
    merged = Settings.DEFAULTS()
    
    # 2. Global user config
    global_path = Path.home() / ".config" / "sync-worktree" / "settings.json"
    if global_path.exists():
        try:
            user_cfg = json.loads(global_path.read_text())
            merged = _deep_update(merged, user_cfg)
        except (json.JSONDecodeError, OSError) as e:
            raise ConfigError(f"Invalid global settings file {global_path}: {e}")
    
    # 3. Project-level setting.config.json
    project_setting = topology_context.project_root / "setting.config.json"
    if project_setting.exists():
        try:
            project_cfg = json.loads(project_setting.read_text())
            merged = _deep_update(merged, project_cfg)
        except (json.JSONDecodeError, OSError) as e:
            raise ConfigError(f"Invalid project settings file {project_setting}: {e}")
    
    # 4. Worktree-level .setting.config.json (personal preferences)
    wt_setting = topology_context.cwd_worktree / ".setting.config.json"
    if wt_setting.exists():
        try:
            wt_cfg = json.loads(wt_setting.read_text())
            merged = _deep_update(merged, wt_cfg)
        except (json.JSONDecodeError, OSError) as e:
            raise ConfigError(f"Invalid worktree settings file {wt_setting}: {e}")
    
    # 5. CLI overrides
    if cli_overrides:
        merged = _deep_update(merged, cli_overrides)
    
    # Convert to dataclass (validation happens here)
    try:
        return Settings(
            executor=merged.get("executor", "git"),
            logging=LoggingSettings(**merged.get("logging", {})),
            behavior=BehaviorSettings(**merged.get("behavior", {})),
            output=OutputSettings(**merged.get("output", {})),
            report=ReportSettings(**merged.get("report", {})),
            command_style=CommandStyleSettings(**merged.get("command_style", {})),
            hooks=HooksSettings(**merged.get("hooks", {})),
            experimental=ExperimentalSettings(**merged.get("experimental", {})),
        )
    except TypeError as e:
        raise ConfigError(f"Settings validation error: {e}")
