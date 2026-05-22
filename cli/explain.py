"""explain command: read-only decision explanation for agents."""

import json
import sys
from pathlib import Path

import policy  # noqa: F401  Ensure all policy modules are registered

from config.loader import load_all
from core.exceptions import ConfigError
from core.topology import detect_topology, TopologyError
from planner.compute import compute_plan
from policy.base import PolicyContext, PolicyEngine, load_policies
from reporting.human import print_report
from reporting.report import build_report, wrap_report


def cmd_explain(args):
    caller_root = Path(getattr(args, "caller_root", Path.cwd())).resolve()
    target_name = args.target

    try:
        topology = detect_topology(caller_root)
    except TopologyError as e:
        print("Error: {error}".format(error=e), file=sys.stderr)
        return 1

    try:
        rule_config, settings = load_all(topology, cli_overrides={})
    except ConfigError as e:
        print("Config error: {error}".format(error=e), file=sys.stderr)
        return 1

    plan = compute_plan(topology, rule_config, settings, target_name, source_override=getattr(args, "source", None))
    if plan is None:
        return 1

    validation = _validate_plan(plan, rule_config)
    report = build_report(plan, validation, settings)
    payload = {
        "contract": {"version": "2.0", "type": "explanation"},
        "target": target_name,
        "report": wrap_report(report),
        "edges": _explain_edges(plan, validation, rule_config),
    }

    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
    else:
        print_report(report)
        print("")
        print("Edges:")
        for edge in payload["edges"]:
            print("  {source} -> {dest}: {meaning}".format(
                source=edge["from"],
                dest=edge["to"],
                meaning=edge["meaning"],
            ))
    return 0


def _validate_plan(plan, rule_config):
    policies = load_policies(rule_config, plan.target_name)
    engine = PolicyEngine(policies)
    ctx = PolicyContext(
        operation="sync",
        target_name=plan.target_name,
        target_config=plan.target_config,
        source_path=plan.source,
        dest_path=plan.dest,
        source_branch=plan.source_branch,
        dest_branch=plan.dest_branch,
        sync_files=plan.sync_files,
        excluded_files=plan.excluded_files,
        actions=plan.actions,
        git_state={"source_commit": plan.source_commit, "source_branch": plan.source_branch},
        policy_params={},
    )
    return engine.validate(ctx, strict=False)


def _explain_edges(plan, validation, rule_config):
    target_binding = rule_config.targets.get(plan.target_name)
    role_name = getattr(target_binding, "role", None)
    action_counts = {code: len(plan.actions.get(code, [])) for code in ("A", "M", "D", "P")}
    edges = [
        {
            "from": "config.targets.{target}".format(target=plan.target_name),
            "to": "planner.plan",
            "meaning": "selects source, destination, role, and overrides",
            "evidence": {"role": role_name},
        },
        {
            "from": "planner.actions",
            "to": "report.evidence.changes",
            "meaning": "summarizes candidate sync actions without listing files",
            "evidence": action_counts,
        },
        {
            "from": "advisor.recommend",
            "to": "report.reason",
            "meaning": "validation status chooses stable reason and next workflow",
            "evidence": {
                "valid": validation.valid,
                "has_errors": validation.has_errors,
                "has_warnings": validation.has_warnings,
            },
        },
    ]
    if validation.failed:
        edges.insert(2, {
            "from": "policy.results",
            "to": "report.risks",
            "meaning": "failed policy checks become bounded report risks",
            "evidence": ",".join(result.code for result in validation.failed),
        })
    return edges
