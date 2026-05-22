"""doctor command: read-only agent preflight diagnosis."""

import json
from pathlib import Path

from config.loader import load_all
from core.topology import detect_topology
from reporting.human import print_report
from reporting.report import DecisionReport, ReportRisk, wrap_report


def cmd_doctor(args):
    caller_root = Path(getattr(args, "caller_root", Path.cwd())).resolve()
    report = build_doctor_report(caller_root)
    if getattr(args, "json", False):
        print(json.dumps(wrap_report(report), indent=2, ensure_ascii=False, default=str))
    else:
        print_report(report)
    return 0 if report.status == "ready" else 1


def build_doctor_report(caller_root: Path) -> DecisionReport:
    try:
        topology = detect_topology(caller_root)
    except Exception as e:
        return _blocked("doctor_topology_error", "Topology detection failed: {error}".format(error=e))

    try:
        rule_config, _settings = load_all(topology, cli_overrides={})
    except Exception as e:
        return _blocked("doctor_config_error", "Config load failed: {error}".format(error=e))

    target_names = sorted(rule_config.targets.keys())
    risks = []
    if not target_names:
        risks.append(ReportRisk(
            id="DOCTOR-NO-TARGETS",
            severity="error",
            message="No configured targets found.",
            evidence=[],
        ))

    status = "blocked" if risks else "ready"
    reason = "doctor_blocked" if risks else "doctor_ready"
    commands = [
        "python3 sync_worktree.py inspect --target {target}".format(target=target)
        for target in target_names[:3]
    ]
    if not commands:
        commands.append("python3 sync_worktree.py target add <name> --role deployment")

    return DecisionReport(
        status=status,
        reason=reason,
        summary="sync-worktree can inspect {count} configured target(s).".format(count=len(target_names))
        if not risks else "sync-worktree preflight found blocking issue(s).",
        risks=risks,
        recommendation="Run inspect or check for the target you intend to change."
        if not risks else "Fix doctor risks before syncing.",
        commands=commands,
        workflow=["review doctor report", "inspect intended target", "run check before apply"]
        if not risks else ["review doctor risks", "fix config or topology", "rerun doctor"],
        evidence={
            "topology": topology.mode,
            "project_root": str(topology.project_root),
            "git_internal": str(topology.git_internal) if topology.git_internal else None,
            "current_worktree": str(topology.cwd_worktree),
            "targets": len(target_names),
            "target_names": target_names,
        },
        full_trace=None,
    )


def _blocked(reason: str, message: str) -> DecisionReport:
    return DecisionReport(
        status="blocked",
        reason=reason,
        summary=message,
        risks=[ReportRisk(
            id=reason.upper().replace("_", "-"),
            severity="error",
            message=message,
            evidence=[],
        )],
        recommendation="Fix this environment before using sync-worktree.",
        commands=["pwd", "git status --short"],
        workflow=["review error", "fix environment", "rerun doctor"],
        evidence={},
        full_trace=None,
    )
