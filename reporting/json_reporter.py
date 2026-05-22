"""Structured JSON reporter for check output."""

import json
from datetime import datetime
from typing import Any, Dict

from planner.plan import SyncPlan
from policy.base import ValidationSummary
from reporting.report import build_report, wrap_report


class JSONReporter:
    """Generate machine-readable JSON output."""
    
    def format(self, plan: SyncPlan, validation: ValidationSummary, settings, full: bool = False) -> str:
        trace = {
            "operation": "check",
            "target": plan.target_name,
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "plan": self._plan_to_dict(plan),
            "validation": validation.to_dict(),
            "can_proceed": validation.valid and not validation.has_errors,
        }
        full_trace = trace if full else None
        report = build_report(plan, validation, settings, full_trace=full_trace)
        if not full:
            return json.dumps(wrap_report(report), indent=2, sort_keys=False)

        output = {
            "report": wrap_report(report),
            "trace": trace,
        }
        return json.dumps(output, indent=2, sort_keys=False)
    
    def _plan_to_dict(self, plan: SyncPlan) -> Dict[str, Any]:
        return {
            "source": str(plan.source),
            "dest": str(plan.dest),
            "source_commit": plan.source_commit,
            "source_branch": plan.source_branch,
            "dest_branch": plan.dest_branch,
            "sync_files": [str(f) for f in plan.sync_files],
            "excluded_files": [str(f) for f in plan.excluded_files],
            "actions": {
                k: [str(f) for f in v] for k, v in plan.actions.items()
            },
        }
