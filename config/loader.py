"""Configuration loader: loads Rule (immutable) + Settings (mutable)."""

import json
import os
from dataclasses import asdict
from pathlib import Path
from typing import Tuple

from .rule import RuleConfig, rule_config_path
from .setting import load_settings as load_settings_core
from core.topology import TopologyContext
from core.exceptions import ConfigError


def load_all(topology: TopologyContext, cli_overrides: dict = None) -> Tuple[RuleConfig, 'Settings']:
    """
    Load both rule config and settings.
    
    Returns:
        (rule_config, settings) tuple
    """
    # 1. Load Rule Config (single source, read-only)
    rule_path = rule_config_path(topology)
    if not rule_path.exists():
        # Auto-generate default rule config, but require explicit review
        default_rule = RuleConfig.default()
        # Write it out
        rule_path.parent.mkdir(parents=True, exist_ok=True)
        import json
        rule_path.write_text(json.dumps(asdict(default_rule), indent=2))
        raise ConfigError(
            f"Rule config not found. Created default at: {rule_path}\n"
            f"Please review and customize it before using sync-worktree."
        )
    
    try:
        rule_config = RuleConfig.from_file(rule_path)
    except Exception as e:
        raise ConfigError(f"Failed to load rule config: {e}")
    
    # 2. Load Settings (multi-layer)
    try:
        settings = load_settings_core(topology, cli_overrides or {})
    except Exception as e:
        raise ConfigError(f"Failed to load settings: {e}")
    
    return rule_config, settings
