"""check command: compute plan and run policy validation (no execution)."""

import json
import sys
from pathlib import Path

import policy  # noqa: F401  Ensure all policy modules are registered

from core.topology import detect_topology, TopologyError
from core.exceptions import ConfigError
from config.loader import load_all
from planner.compute import compute_plan
from policy.base import load_policies, PolicyEngine, PolicyContext
from reporting.json_reporter import JSONReporter
from reporting.human import print_validation_summary


def cmd_check(args):
    """
    sync-worktree check <target>
    
    Compute the sync plan, evaluate all policies, and output results.
    Does NOT execute any file operations.
    """
    caller_root = Path(getattr(args, "caller_root", Path.cwd())).resolve()

    try:
        topology = detect_topology(caller_root)
    except TopologyError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    
    # Load configuration
    try:
        rule_config, settings = load_all(topology, cli_overrides={})
    except ConfigError as e:
        print(f"Config error: {e}", file=sys.stderr)
        return 1
    
    # Compute plan
    target_name = args.target
    plan = compute_plan(topology, rule_config, settings, target_name, source_override=args.source)
    if plan is None:
        return 1
    
    # Build policy context
    ctx = PolicyContext(
        operation="sync",
        target_name=target_name,
        target_config=plan.target_config,
        source_path=plan.source,
        dest_path=plan.dest,
        source_branch=plan.source_branch,
        dest_branch=plan.dest_branch,
        sync_files=plan.sync_files,
        excluded_files=plan.excluded_files,
        actions=plan.actions,
        git_state={
            "source_commit": plan.source_commit,
            "source_branch": plan.source_branch,
        },
        policy_params={},  # not used
    )
    
    # Load and run policies
    policies = load_policies(rule_config, target_name)
    engine = PolicyEngine(policies)
    validation = engine.validate(ctx, strict=False)
    
    # Output
    if getattr(args, 'json', False):
        reporter = JSONReporter()
        print(reporter.format(plan, validation))
    else:
        print_validation_summary(validation, plan)
    
    # Exit code
    if not validation.valid:
        if validation.has_errors:
            return 1
        else:
            return 2
    return 0
