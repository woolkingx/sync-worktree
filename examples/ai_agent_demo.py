#!/usr/bin/env python3
"""
AI agent demo: inspect report -> decide -> apply.

Usage:
  python3 master/examples/ai_agent_demo.py --target runner --dry-run

The demo uses the default bounded report contract. Replace decide() with model
logic if the report is being consumed by an AI agent.
"""

import json
import subprocess
import sys
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "sync_worktree.py"


def run_cmd(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def inspect_report(target):
    """Step 1: read the bounded report for one target."""
    cmd = [sys.executable, str(SCRIPT_PATH), "inspect", "--target", target]
    result = run_cmd(cmd)
    if result.returncode != 0:
        print(f"[inspect] failed: {result.stderr}", file=sys.stderr)
        sys.exit(1)

    payload = json.loads(result.stdout)
    if payload.get("contract", {}).get("type") != "report":
        print("[inspect] expected report contract", file=sys.stderr)
        sys.exit(1)
    return payload


def decide(payload, target):
    """Step 2: build a minimal decision from report status."""
    report = payload["report"]
    metadata = {
        "ai_agent": "demo",
        "report_hash": payload["meta"]["report_hash"],
    }

    if report["status"] == "ready":
        return {
            "contract": {"version": "2.0", "type": "decision"},
            "metadata": metadata,
            "decision": {
                "action": "sync",
                "target": target,
                "apply": True,
                "force": False,
                "confidence": 0.8,
                "rationale": report["summary"],
            },
            "auto_fixes": [],
        }

    return {
        "contract": {"version": "2.0", "type": "decision"},
        "metadata": metadata,
        "decision": {
            "action": "abort",
            "target": target,
            "apply": False,
            "force": False,
            "confidence": 0.0,
            "rationale": report["recommendation"],
        },
        "auto_fixes": [],
    }


def apply_decision(decision, dry_run=False):
    """Step 3: execute the decision through the apply contract."""
    decision_file = Path("/tmp/sync_worktree_ai_decision.json")
    decision_file.write_text(json.dumps(decision, indent=2, ensure_ascii=False) + "\n")

    cmd = [sys.executable, str(SCRIPT_PATH), "apply", "--from-decision", str(decision_file)]
    if dry_run:
        cmd.append("--dry-run")
    else:
        cmd.append("--verify-report")

    print(f"[apply] {' '.join(cmd)}")
    result = run_cmd(cmd)
    try:
        payload = json.loads(result.stdout) if result.stdout else {}
    except json.JSONDecodeError:
        payload = {"raw_stdout": result.stdout, "raw_stderr": result.stderr}

    print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
    return result.returncode, payload


def main():
    import argparse

    parser = argparse.ArgumentParser(description="AI agent demo")
    parser.add_argument("--target", default="runner", help="target to inspect and sync")
    parser.add_argument("--dry-run", action="store_true", help="validate the decision without writing")
    args = parser.parse_args()

    report_payload = inspect_report(args.target)
    report = report_payload["report"]
    print(f"[inspect] target={args.target} status={report['status']} reason={report['reason']}")
    print(f"[inspect] recommendation={report['recommendation']}")

    decision = decide(report_payload, args.target)
    action = decision["decision"]["action"]
    print(f"[decide] action={action} target={args.target}")
    if action == "abort":
        print("[decide] report is not ready; stopping before apply")
        print(json.dumps(decision, indent=2, ensure_ascii=False))
        return 0

    rc, result = apply_decision(decision, dry_run=args.dry_run)
    status = result.get("outcome", {}).get("status", "unknown")
    print(f"[result] status={status}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
