"""sync command: validate then optionally execute."""

import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict

import policy  # Ensure policies are registered

from core.topology import detect_topology, TopologyError
from core.exceptions import ConfigError
from config.loader import load_all
from planner.compute import compute_plan
from policy.base import load_policies, PolicyEngine, PolicyContext
from reporting.json_reporter import JSONReporter
from reporting.human import print_validation_summary
from executor.git import GitExecutor


def cmd_sync(args):
    """
    sync worktree: check policy, then optionally apply.
    """
    caller_root = Path(getattr(args, "caller_root", Path.cwd())).resolve()
    try:
        topology = detect_topology(caller_root)
    except TopologyError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    
    try:
        rule_config, settings = load_all(topology, cli_overrides={})
    except Exception as e:
        print(f"Config error: {e}", file=sys.stderr)
        return 1
    
    plan = compute_plan(topology, rule_config, settings, args.target, source_override=args.source)
    if plan is None:
        return 1
    
    ctx = PolicyContext(
        operation="sync",
        target_name=args.target,
        target_config=plan.target_config,
        source_path=plan.source,
        dest_path=plan.dest,
        source_branch=plan.source_branch,
        dest_branch=plan.dest_branch,
        sync_files=plan.sync_files,
        excluded_files=plan.excluded_files,
        actions=plan.actions,
        git_state={"source_commit": plan.source_commit, "source_branch": plan.source_branch},
        policy_params={}
    )
    
    policies = load_policies(rule_config, args.target)
    engine = PolicyEngine(policies)
    validation = engine.validate(ctx, strict=False)
    
    if getattr(args, 'json', False):
        reporter = JSONReporter()
        print(reporter.format(plan, validation))
    else:
        print_validation_summary(validation, plan)
    
    # Determine if we can proceed
    if not validation.valid:
        if validation.has_errors:
            print("\n❌ Sync blocked due to policy errors.", file=sys.stderr)
            return 1
        else:
            # Only warnings
            if not getattr(args, 'force', False):
                print("\n⚠️  Warnings present. Use --strict to treat warnings as errors, or --force to proceed.", file=sys.stderr)
                return 2
            # else: --force ignores warnings
    
    if not args.apply:
        print("\n💡 Plan validated. Re-run with --apply to execute.")
        return 0
    
    executor = GitExecutor()
    try:
        result = executor.apply(plan, settings, backup_branch=settings.behavior.create_backup_branch)
        _write_sync_state(topology, args, plan, validation, result)
        print(f"\n✅ Sync complete: +{result['files_copied']} ~{result['files_deleted']} deleted")
        if result.get('backup_branch'):
            print(f"   Backup branch: {result['backup_branch']}")
        if result.get('commit_sha'):
            print(f"   Commit: {result['commit_sha'][:8]}")
        return 0
    except Exception as e:
        print(f"\n❌ Sync failed: {e}", file=sys.stderr)
        return 1


def _write_sync_state(topology, args, plan, validation, result) -> None:
    state_path = topology.git_internal / "sync-worktree.state.json"
    state = {}
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text())
        except (OSError, json.JSONDecodeError):
            state = {}

    timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    target_hashes = _hash_tree(plan.dest)

    last_sync = state.get("last_sync", {})
    last_sync[plan.target_name] = {
        "timestamp": timestamp,
        "source_commit": plan.source_commit,
        "files_synced": result.get("files_copied", 0),
        "files_deleted": result.get("files_deleted", 0),
        "file_hashes": target_hashes,
    }

    state["last_sync"] = last_sync
    state["last_run"] = {
        "timestamp": timestamp,
        "command": _format_sync_command(args, plan.target_name),
        "mode": "apply" if getattr(args, "apply", False) else "check",
        "checks_passed": bool(validation.valid),
        "warnings": len(getattr(validation, "warnings", [])),
        "errors": len(getattr(validation, "errors", [])),
        "targets": [plan.target_name],
        "summary": {
            "A": len(plan.actions.get("A", [])),
            "M": len(plan.actions.get("M", [])),
            "D": len(plan.actions.get("D", [])),
        },
    }

    try:
        state_path.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n")
    except OSError as e:
        print(f"Warning: sync state update skipped for {state_path}: {e}", file=sys.stderr)


def _hash_tree(root: Path) -> Dict[str, str]:
    hashes = {}
    for current_root, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        current_dir = Path(current_root)
        for filename in filenames:
            file_path = current_dir / filename
            rel_path = file_path.relative_to(root)
            hashes[str(rel_path)] = _hash_file(file_path)
    return hashes


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _format_sync_command(args, target_name: str) -> str:
    runtime_root = getattr(args, "runtime_root", None)
    script_path = Path(runtime_root) / "sync_worktree.py" if runtime_root else Path("sync_worktree.py")
    parts = ["python3", str(script_path), "sync", target_name]
    source = getattr(args, "source", None)
    if source:
        parts.extend(["--source", str(source)])
    if getattr(args, "apply", False):
        parts.append("--apply")
    if getattr(args, "json", False):
        parts.append("--json")
    if getattr(args, "force", False):
        parts.append("--force")
    return " ".join(parts)
