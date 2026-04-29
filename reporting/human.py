"""Human-readable terminal output."""

from pathlib import Path
from typing import List

from policy.base import PolicyResult, ValidationSummary
from planner.plan import SyncPlan


def print_validation_summary(validation: ValidationSummary, plan: SyncPlan):
    """Pretty-print validation results for terminal."""
    print(f"\nTarget: {plan.target_name}")
    if plan.source_commit:
        print(f"Source: {plan.source} @ {plan.source_commit[:8]}")
    else:
        print(f"Source: {plan.source}")
    print(f"Dest:   {plan.dest}")
    print(f"Files to sync: {len(plan.sync_files)}")
    
    # Show passed policies if verbose? Keep concise.
    
    if validation.failed:
        print("\n❌ Policy violations:")
        for r in validation.failed:
            print(f"\n  [{r.code}] {r.severity.upper()}: {r.title}")
            print(f"    {r.message}")
            if r.evidence:
                print("    Evidence:")
                for ev in r.evidence:
                    print(f"      • {ev}")
            if r.suggestion:
                print(f"    💡 {r.suggestion}")
            if r.fix_commands:
                print("    Commands to fix:")
                for cmd in r.fix_commands:
                    print(f"      $ {cmd}")
    else:
        print("\n✅ All policy checks passed")
    
    # Action summary
    actions = plan.actions
    parts = []
    for code in ['A', 'M', 'D']:
        cnt = len(actions.get(code, []))
        if cnt:
            parts.append(f"{code}:{cnt}")
    if parts:
        print(f"\nChanges: {' | '.join(parts)}")
    else:
        print("\nNo changes needed.")
