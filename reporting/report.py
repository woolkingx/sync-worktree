"""Report owner data shape."""

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional

from reporting.advisor import recommend, suggest_commands, suggest_workflow


@dataclass(frozen=True)
class ReportRisk:
    id: str
    severity: str
    message: str
    evidence: List[str]


@dataclass(frozen=True)
class DecisionReport:
    status: str
    reason: str
    summary: str
    risks: List[ReportRisk]
    recommendation: str
    commands: List[str]
    workflow: List[str]
    evidence: Dict
    full_trace: Optional[Dict] = None


def report_to_dict(report: DecisionReport) -> Dict:
    return asdict(report)


def report_hash(report_or_dict) -> str:
    data = report_to_dict(report_or_dict) if isinstance(report_or_dict, DecisionReport) else report_or_dict
    payload = json.dumps(data, sort_keys=True, ensure_ascii=False, default=str, separators=(",", ":"))
    return "sha256:{digest}".format(digest=hashlib.sha256(payload.encode("utf-8")).hexdigest())


def wrap_report(report_or_dict, generated_at=None, extra_meta=None) -> Dict:
    data = report_to_dict(report_or_dict) if isinstance(report_or_dict, DecisionReport) else report_or_dict
    meta = {
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat(),
        "report_hash": report_hash(data),
    }
    if extra_meta:
        meta.update(extra_meta)
    return {
        "contract": {"version": "2.0", "type": "report"},
        "meta": meta,
        "report": data,
    }


def build_report(plan, validation, settings, full_trace=None) -> DecisionReport:
    risks = [
        ReportRisk(
            id=result.code,
            severity=result.severity,
            message=result.message,
            evidence=list(result.evidence),
        )
        for result in validation.failed
    ]
    action_counts = {code: len(plan.actions.get(code, [])) for code in ("A", "M", "D", "P")}
    changed = sum(action_counts.values())
    source = _format_source(plan)
    status, reason, recommendation = recommend(validation, settings)
    commands = suggest_commands(plan, validation, settings)
    workflow = suggest_workflow(plan, validation, settings)

    return DecisionReport(
        status=status,
        reason=reason,
        summary="{target}: {changed} change(s) from {source}.".format(
            target=plan.target_name,
            changed=changed,
            source=source,
        ),
        risks=risks,
        recommendation=recommendation,
        commands=commands,
        workflow=workflow,
        evidence={
            "target": plan.target_name,
            "source": source,
            "dest": str(plan.dest),
            "changes": action_counts,
            "policy_errors": sum(1 for risk in risks if risk.severity == "error"),
            "policy_warnings": sum(1 for risk in risks if risk.severity == "warn"),
        },
        full_trace=full_trace,
    )


def _format_source(plan) -> str:
    branch = plan.source_branch or getattr(plan.source, "name", str(plan.source))
    commit = getattr(plan, "source_commit", "") or ""
    if commit:
        return "{branch}@{commit}".format(branch=branch, commit=commit[:8])
    return str(branch)
