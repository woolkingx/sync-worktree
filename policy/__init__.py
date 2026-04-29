"""Policy package: Import all policies to trigger registration."""

# Import all policy modules to register them via decorators
from .topology.role import RoleOperationPolicy  # noqa: F401
from .topology.cleanliness import TargetCleanlinessPolicy  # noqa: F401
from .content.forbidden_paths import ForbiddenPathPolicy  # noqa: F401

# Future policies:
# from .content.naming import NamingPolicy
# from .security.secrets import SecretsScanPolicy
# from .history.signature import SignaturePolicy

__all__ = [
    "RoleOperationPolicy",
    "TargetCleanlinessPolicy",
    "ForbiddenPathPolicy",
]
