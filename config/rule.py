"""Rule configuration - team policy (immutable, read-only)."""

import json
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Dict, List, Optional, Any

from core.exceptions import ConfigError


# =========== Data Structures ===========

@dataclass(frozen=True)
class RoleDefinition:
    """Definition of a role (deployment, development, etc.)."""
    allowed_operations: List[str] = field(default_factory=lambda: ["sync", "status"])
    denied_operations: List[str] = field(default_factory=list)
    require_clean_source: bool = True
    require_clean_target: bool = True
    require_up_to_date: bool = True
    allow_auto_commit: bool = False
    delete_policy: str = "never"   # "never" | "unlisted" | "tracked_only"
    
    # Filter patterns (gitignore syntax)
    include: List[str] = field(default_factory=list)      # include only these (empty = all)
    exclude: List[str] = field(default_factory=list)      # exclude from sync
    
    # Protected from deletion (patterns)
    protect: List[str] = field(default_factory=list)
    
    # Policy constraints
    forbidden_paths: List[str] = field(default_factory=list)   # must not appear in sync set
    required_paths: List[str] = field(default_factory=list)    # must exist in source
    naming: Dict[str, Any] = field(default_factory=dict)       # naming rules
    content: Dict[str, Any] = field(default_factory=dict)      # content scanning
    history: Dict[str, Any] = field(default_factory=dict)      # commit history
    limits: Dict[str, Any] = field(default_factory=dict)       # size/depth limits
    
    # Policy enablement overrides
    policies: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TargetBinding:
    """Binding of a target name to a role, with overrides."""
    role: str
    description: Optional[str] = None
    source: Optional[str] = None      # source worktree name (default: current)
    overrides: Dict[str, Any] = field(default_factory=dict)   # merge into role
    
    def effective_config(self, base_role: RoleDefinition) -> RoleDefinition:
        """Apply overrides to base role and return new RoleDefinition."""
        base_dict = asdict(base_role)
        merged = _deep_merge(base_dict, self.overrides)
        return RoleDefinition(**merged)


@dataclass(frozen=True)
class PolicyEnablement:
    enabled: List[str] = field(default_factory=lambda: ["POL-TOP-*", "POL-CON-*", "POL-SEC-*"])
    disabled: List[str] = field(default_factory=list)
    severity_default: Dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class RuleConfig:
    version: int
    roles: Dict[str, RoleDefinition]
    targets: Dict[str, TargetBinding]
    policies: PolicyEnablement
    
    @classmethod
    def default(cls) -> 'RuleConfig':
        """Minimal default config for first-time init."""
        return cls(
            version=2,
            roles={
                "deployment": RoleDefinition(
                    allowed_operations=["sync", "status"],
                    denied_operations=["commit", "push", "merge", "rebase", "branch", "tag", "reset"],
                    require_clean_source=True,
                    require_clean_target=True,
                    require_up_to_date=True,
                    exclude=[
                        "tests/", "test/",
                        "docs/", "*.md",  # docs optional
                        "*.log", "*.tmp",
                        "__pycache__/", ".pytest_cache/",
                        ".git/", ".github/",
                        "node_modules/", "dist/", "build/",
                        "*.pyc", ".DS_Store"
                    ],
                    forbidden_paths=["tests/", "*.log"],  # strict: never allow these
                    protect=["node_modules/", "vendor/"]
                ),
                "development": RoleDefinition(
                    allowed_operations=["*"],
                    denied_operations=[],
                    require_clean_source=False,
                    require_clean_target=False,
                    require_up_to_date=False,
                    exclude=[],
                    forbidden_paths=[]
                )
            },
            targets={},
            policies=PolicyEnablement()
        )
    
    @classmethod
    def from_file(cls, path: Path) -> 'RuleConfig':
        try:
            data = json.loads(path.read_text())
        except json.JSONDecodeError as e:
            raise ConfigError(f"Invalid JSON in {path}: {e}")
        return cls._parse(data, path)
    
    @classmethod
    def _parse(cls, data: dict, source_path: Path) -> 'RuleConfig':
        # Version check
        version = data.get("version")
        if version != 2:
            raise ConfigError(f"Unsupported rule config version: {version} (expected 2)")
        
        # Parse roles
        roles = {}
        for name, role_data in data.get("roles", {}).items():
            roles[name] = RoleDefinition(**role_data)
        
        # Parse targets
        targets = {}
        for name, tdata in data.get("targets", {}).items():
            targets[name] = TargetBinding(**tdata)
        
        # Parse policies
        pol_data = data.get("policies", {})
        policies = PolicyEnablement(**pol_data)
        
        config = cls(version=version, roles=roles, targets=targets, policies=policies)
        config._validate()
        return config
    
    def _validate(self) -> None:
        self._validate_target_roles_exist()
        self._validate_policy_codes()
    
    def _validate_target_roles_exist(self) -> None:
        for tname, tbinding in self.targets.items():
            if tbinding.role not in self.roles:
                raise ConfigError(
                    f"Target '{tname}' references undefined role '{tbinding.role}'. "
                    f"Available roles: {', '.join(self.roles.keys())}"
                )
    
    def _validate_policy_codes(self) -> None:
        exact_pat = re.compile(r"^POL-[A-Z]+-\d{3}$")
        wild_pat = re.compile(r"^POL-[A-Z]+-\*$")
        for code in self.policies.enabled + self.policies.disabled:
            if not (exact_pat.match(code) or wild_pat.match(code)):
                raise ConfigError(f"Invalid policy code: {code} (expected POL-XXX-### or POL-XXX-*)")
        for code in self.policies.severity_default:
            if not exact_pat.match(code):
                raise ConfigError(f"Invalid policy code in severity_default: {code}")
    
    def get_target_role(self, target_name: str) -> RoleDefinition:
        tbinding = self.targets.get(target_name)
        if tbinding is None:
            raise ConfigError(f"Unknown target: {target_name}")
        base_role = self.roles.get(tbinding.role)
        if base_role is None:
            raise ConfigError(f"Role '{tbinding.role}' not defined for target '{target_name}'")
        return tbinding.effective_config(base_role)
    
    def is_policy_enabled(self, code: str, target_name: Optional[str] = None) -> bool:
        # Target-specific overrides
        if target_name:
            tbinding = self.targets.get(target_name)
            if tbinding:
                pol = tbinding.overrides.get("policies", {})
                if code in pol.get("enabled", []): return True
                if code in pol.get("disabled", []): return False
        # Global wildcard matching
        for pat in self.policies.disabled:
            if _wildcard_match(code, pat): return False
        for pat in self.policies.enabled:
            if _wildcard_match(code, pat): return True
        return False


def _wildcard_match(code: str, pattern: str) -> bool:
    if pattern.endswith("*"):
        return code.startswith(pattern[:-1])
    return code == pattern


def _deep_merge(base: dict, overrides: dict) -> dict:
    result = base.copy()
    for k, v in overrides.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def rule_config_path(topology: "TopologyContext") -> Path:
    """
    Determine the path to the rule config file based on topology.
    
    Rule config is stored in the git internal directory:
    - bare: .bare/rule.config.json
    - worktree: .git/rule.config.json (common-dir)
    - repo: .git/rule.config.json
    """
    return topology.git_internal / "rule.config.json"

