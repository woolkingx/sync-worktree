"""Human-readable terminal output."""

from config.setting import Settings
from policy.base import ValidationSummary
from planner.plan import SyncPlan
from reporting.report import build_report


def print_report(report):
    """Print bounded report output for terminal users and AI agents."""
    print("")
    print("Status: {status}".format(status=report.status))
    print("Reason: {reason}".format(reason=report.reason))
    print("Summary: {summary}".format(summary=report.summary))
    print("Recommendation: {recommendation}".format(recommendation=report.recommendation))

    if report.risks:
        print("")
        print("Risks:")
        for risk in report.risks:
            print("  [{id}] {severity}: {message}".format(
                id=risk.id,
                severity=risk.severity,
                message=risk.message,
            ))
            for item in risk.evidence:
                print("    - {item}".format(item=item))

    if report.commands:
        print("")
        print("Commands:")
        for command in report.commands:
            print("  $ {command}".format(command=command))

    if report.workflow:
        print("")
        print("Workflow:")
        for index, step in enumerate(report.workflow, start=1):
            print("  {index}. {step}".format(index=index, step=step))


def print_validation_summary(validation: ValidationSummary, plan: SyncPlan, settings=None):
    """Compatibility wrapper for older call sites."""
    print_report(build_report(plan, validation, settings or Settings()))
