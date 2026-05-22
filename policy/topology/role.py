"""POL-TOP-001: Role-based operation allowlist."""

from policy.base import Policy, PolicyContext, PolicyResult, policy


@policy("POL-TOP-001", "topology", "error")
class RoleOperationPolicy(Policy):
    """
    Ensure the requested operation is allowed for the target's role.
    
    This is the primary gatekeeper: if a target's role forbids an operation,
    no further policies are evaluated.
    """
    
    def check(self, ctx: PolicyContext) -> PolicyResult:
        op = ctx.operation
        role_config = ctx.target_config
        
        allowed = role_config.get("allowed_operations", [])
        denied = role_config.get("denied_operations", [])
        role_name = role_config.get("role", "unknown")
        
        # Deny list takes precedence
        if "*" in denied or op in denied:
            return self._fail(
                message=f"Operation '{op}' is explicitly denied for role '{role_name}'",
                evidence=[
                    f"Role: {role_name}",
                    f"Denied operations: {denied}",
                    f"Attempted: {op}"
                ],
                suggestion=(
                    f"Target '{ctx.target_name}' is a {role_name} and does not allow '{op}'. "
                    f"Allowed operations: {allowed if allowed != ['*'] else 'all'}"
                ),
                fix_commands=[
                    f"# Option 1: Use a different target that allows '{op}'",
                    f"sync-worktree sync <other-target> --apply",
                    f"# Option 2: Change the target's role in rule.config (if appropriate)",
                ]
            )
        
        # Check allowlist (if defined and not wildcard)
        if allowed and allowed != ["*"] and op not in allowed:
            return self._fail(
                message=f"Operation '{op}' not in allowlist for role '{role_name}'",
                evidence=[f"Allowed: {allowed}", f"Attempted: {op}"],
                suggestion=f"Target '{ctx.target_name}' only allows: {', '.join(allowed)}",
                fix_commands=[
                    f"# Either use a different target,",
                    f"# or add '{op}' to allowed_operations for role '{role_name}' in rule.config"
                ]
            )
        
        return self._pass()
