"""Policy base classes, registry, and engine."""

import abc
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional, Type
from pathlib import Path

# =========== Data Structures ===========

@dataclass(frozen=True)
class PolicyContext:
    """Immutable context passed to all policies during validation."""
    operation: str                      # "sync", "commit", "push", etc.
    target_name: str                    # Target name (e.g., "runner")
    target_config: Dict[str, Any]       # Resolved role config (merged)
    source_path: Path
    dest_path: Path
    source_branch: Optional[str]
    dest_branch: Optional[str]
    sync_files: List[Path]              # Files that would be synced
    excluded_files: List[Path]
    actions: Dict[str, List[Path]]      # {'A': [...], 'M': [...], 'D': [...]}
    git_state: Dict[str, Any]           # Commits, dirty status, etc.
    policy_params: Dict[str, Any]       # Extracted policy parameters


@dataclass(frozen=True)
class PolicyResult:
    """Result of a single policy check."""
    valid: bool
    code: str                           # e.g., "POL-TOP-001"
    title: str                          # Human-readable title
    message: str                        # Full description
    severity: str                       # "error" | "warn" | "info" | "silent"
    evidence: List[str] = field(default_factory=list)
    suggestion: Optional[str] = None
    fix_commands: List[str] = field(default_factory=list)
    documentation_url: Optional[str] = None


@dataclass(frozen=True)
class ValidationSummary:
    """Aggregated result of all policy checks."""
    valid: bool
    results: List[PolicyResult]
    
    @property
    def has_errors(self) -> bool:
        return any(not r.valid and r.severity == "error" for r in self.results)
    
    @property
    def has_warnings(self) -> bool:
        return any(not r.valid and r.severity == "warn" for r in self.results)
    
    @property
    def failed(self) -> List[PolicyResult]:
        return [r for r in self.results if not r.valid]
    
    @property
    def passed(self) -> List[PolicyResult]:
        return [r for r in self.results if r.valid]
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to JSON-serializable dict."""
        return {
            "valid": self.valid,
            "has_errors": self.has_errors,
            "has_warnings": self.has_warnings,
            "passed": [self._result_to_dict(r) for r in self.passed],
            "failed": [self._result_to_dict(r) for r in self.failed],
        }
    
    def _result_to_dict(self, r: PolicyResult) -> Dict[str, Any]:
        return {
            "code": r.code,
            "title": r.title,
            "message": r.message,
            "severity": r.severity,
            "evidence": r.evidence,
            "suggestion": r.suggestion,
            "fix_commands": r.fix_commands,
            "documentation_url": r.documentation_url,
        }


# =========== Policy Base Class ===========

class Policy(abc.ABC):
    """Base class for all policies."""
    
    code: str = ""          # e.g., "POL-TOP-001"
    title: str = ""         # Human-readable title
    description: str = ""   # Long description
    severity: str = "error" # default severity
    
    @abc.abstractmethod
    def check(self, ctx: PolicyContext) -> PolicyResult:
        """Check policy, return result."""
        pass
    
    def _pass(self) -> PolicyResult:
        return PolicyResult(
            valid=True,
            code=self.code,
            title=self.title,
            message="OK",
            severity=self.severity,
        )
    
    def _fail(self, message: str, *,
              evidence: Optional[List[str]] = None,
              suggestion: Optional[str] = None,
              fix_commands: Optional[List[str]] = None,
              severity: Optional[str] = None) -> PolicyResult:
        return PolicyResult(
            valid=False,
            code=self.code,
            title=self.title,
            message=message,
            severity=severity or self.severity,
            evidence=evidence or [],
            suggestion=suggestion,
            fix_commands=fix_commands or [],
        )


# =========== Policy Registry ===========

POLICY_REGISTRY: Dict[str, Type[Policy]] = {}


def policy(code: str, category: str, default_severity: str = "error"):
    """
    Decorator to register a policy class.
    
    Example:
        @policy("POL-TOP-001", "topology", "error")
        class RolePolicy(Policy):
            def check(self, ctx): ...
    """
    def decorator(cls: Type[Policy]) -> Type[Policy]:
        cls.code = code
        cls.category = category
        cls.default_severity = default_severity
        POLICY_REGISTRY[code] = cls
        return cls
    return decorator


def load_policies(rule_config, target_name: str, severity_overrides: Optional[Dict[str, str]] = None) -> List[Policy]:
    """
    Instantiate policies for a given target based on rule config.
    
    Args:
        rule_config: Parsed RuleConfig object
        target_name: Target name to get policies for
        severity_overrides: Optional per-target severity overrides
    
    Returns:
        List of Policy instances (in evaluation order)
    """
    policies = []
    target_cfg = rule_config.get_target_role(target_name)
    
    # Which policy codes are enabled for this target?
    enabled_codes = set()
    disabled_codes = set()
    
    # Global policy config
    global_policies = rule_config.policies
    enabled_codes.update(global_policies.enabled)
    disabled_codes.update(global_policies.disabled)
    
    # Target-specific policy overrides
    t_policies = target_cfg.policies if hasattr(target_cfg, 'policies') else {}
    if "enabled" in t_policies:
        enabled_codes = set(t_policies["enabled"])
    if "disabled" in t_policies:
        disabled_codes.update(t_policies["disabled"])
    
    # Severity overrides
    severity_map = dict(global_policies.severity_default)
    if severity_overrides:
        severity_map.update(severity_overrides)
    if "severity_override" in t_policies:
        severity_map.update(t_policies["severity_override"])
    
    # Instantiate enabled policies
    for code, policy_cls in POLICY_REGISTRY.items():
        # Check if enabled (wildcard matching)
        is_enabled = any(_wildcard_match(code, pattern) for pattern in enabled_codes)
        is_disabled = any(_wildcard_match(code, pattern) for pattern in disabled_codes)
        
        if is_enabled and not is_disabled:
            severity = severity_map.get(code, policy_cls.default_severity)
            instance = policy_cls()
            instance.severity = severity
            policies.append(instance)
    
    return policies


def _wildcard_match(code: str, pattern: str) -> bool:
    """Match policy code against wildcard pattern (e.g., POL-TOP-*)."""
    if pattern.endswith("*"):
        return code.startswith(pattern[:-1])
    return code == pattern


# =========== Policy Engine ===========

class PolicyEngine:
    """Evaluates policies against a validation context."""
    
    def __init__(self, policies: List[Policy]):
        self.policies = policies
        self.results: List[PolicyResult] = []
    
    def validate(self, ctx: PolicyContext, strict: bool = False) -> ValidationSummary:
        """
        Run all policies, collect results.
        
        Args:
            ctx: Validation context
            strict: If True, treat warnings as blocking
        
        Returns:
            ValidationSummary with all results
        """
        self.results = []
        
        for policy in self.policies:
            try:
                result = policy.check(ctx)
            except Exception as e:
                # Policy threw exception → treat as error
                result = PolicyResult(
                    valid=False,
                    code=policy.code,
                    title=policy.title,
                    message=f"Policy check crashed: {e}",
                    severity="error",
                    evidence=[f"Exception: {type(e).__name__}: {e}"],
                    suggestion="Report this as a bug in sync-worktree"
                )
            self.results.append(result)
            
        # Determine overall validity
        valid = True
        for r in self.results:
            if not r.valid:
                if r.severity == "error":
                    valid = False
                elif r.severity == "warn" and strict:
                    valid = False

        return ValidationSummary(valid=valid, results=self.results)
