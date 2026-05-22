"""POL-CON-001: Forbidden path inclusion check."""

from policy.base import Policy, PolicyContext, PolicyResult, policy
from core.filter import filter_match_batch


@policy("POL-CON-001", "content", "error")
class ForbiddenPathPolicy(Policy):
    """
    Ensure no file that would be synced matches any forbidden path pattern.
    
    This is a critical safety check: deployment targets must not receive
    test files, docs, logs, or other non-runtime artifacts.
    """
    
    def check(self, ctx: PolicyContext) -> PolicyResult:
        forbidden_patterns = ctx.target_config.get("forbidden_paths", [])
        if not forbidden_patterns:
            return self._pass()
        
        # Check if any sync file matches a forbidden pattern
        sync_files_str = [str(f) for f in ctx.sync_files]
        matches = filter_match_batch(sync_files_str, forbidden_patterns)
        
        violations = [f for f, matched in matches.items() if matched]
        if not violations:
            return self._pass()
        
        # Build evidence (first few)
        evidence = [f"  - {v}" for v in violations[:5]]
        if len(violations) > 5:
            evidence.append(f"  ... and {len(violations) - 5} more")
        
        return self._fail(
            message=f"{len(violations)} file(s) match forbidden paths for role '{ctx.target_config.get('role', 'unknown')}'",
            evidence=evidence,
            suggestion=(
                f"Remove these files from sync set. "
                f"Update include/exclude patterns or adjust role's forbidden_paths."
            ),
            fix_commands=[
                "# Option 1: Update include patterns to exclude these paths",
                f"# in rule.config, adjust 'include' to exclude: {forbidden_patterns[0]}",
                "# Option 2: Move files to allowed locations",
                "# Option 3: If this is intentional, override policy (not recommended)"
            ]
        )
