"""apply command: execute AI decision from JSON file."""

import json
import subprocess
import sys
import hashlib
import shlex
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

from reporting.ai_context import export_context as inspect_context


def cmd_apply(args):
    """Execute decision JSON."""
    decision_path = Path(args.from_decision)
    caller_root = Path(getattr(args, "caller_root", Path.cwd())).resolve()
    runtime_root = Path(getattr(args, "runtime_root", Path(__file__).resolve().parent)).resolve()
    
    if not decision_path.exists():
        _print_result({
            "contract": {"version": "2.0", "type": "result"},
            "error": f"Decision file not found: {decision_path}"
        })
        return 1
    
    try:
        with open(decision_path) as f:
            decision = json.load(f)
    except json.JSONDecodeError as e:
        _print_result({
            "contract": {"version": "2.0", "type": "result"},
            "error": f"Invalid JSON: {e}"
        })
        return 1
    
    if decision.get("contract", {}).get("type") != "decision":
        _print_result({
            "contract": {"version": "2.0", "type": "result"},
            "error": "Not a decision contract (missing contract.type='decision')"
        })
        return 1
    
    # Dry-run mode: validate against live context and return the inspected plan.
    if getattr(args, 'dry_run', False):
        result = _build_dry_run_result(decision, caller_root, runtime_root)
        _print_result(result)
        return 0 if result["outcome"].get("status") == "dry_run" else 1
    
    # Optional context verification
    if getattr(args, 'verify_context', False):
        try:
            current_ctx = inspect_context(cwd=caller_root, runtime_root=runtime_root)
            current_hash = current_ctx["meta"]["context_hash"]
            decision_hash = decision.get("metadata", {}).get("context_hash")
            if decision_hash and current_hash != decision_hash:
                _print_result({
                    "contract": {"version": "2.0", "type": "result"},
                    "status": "stale_context",
                    "message": "Context hash mismatch",
                    "current_context_hash": current_hash,
                    "decision_context_hash": decision_hash,
                    "suggestion": "Re-run inspect to get fresh context"
                })
                return 1
        except Exception as e:
            _print_result({
                "contract": {"version": "2.0", "type": "result"},
                "status": "failed",
                "message": "Context verification failed",
                "error": str(e),
            })
            return 1
    
    # Dispatch
    act = decision.get("decision", {})
    action = act.get("action")
    target = act.get("target")
    force = act.get("force", False)
    apply_flag = act.get("apply", False)
    
    result = {
        "contract": {"version": "2.0", "type": "result"},
        "metadata": {
            "decision_hash": _decision_hash(decision),
            "executed_at": datetime.now(timezone.utc).isoformat()
        },
        "outcome": {"status": "unknown", "phase": None}
    }
    
    try:
        if action == "sync":
            if not target:
                result["outcome"].update({"status": "error", "error": "Missing target"})
            else:
                sync_res = _run_sync(target, apply=apply_flag, force=force, cwd=caller_root)
                result["outcome"].update(sync_res)
        
        elif action == "fix_then_sync":
            if not target:
                result["outcome"].update({"status": "error", "error": "Missing target"})
            else:
                fix_res = _run_fixes_then_sync(
                    decision,
                    target,
                    apply=apply_flag,
                    force=force,
                    cwd=caller_root,
                )
                result["outcome"].update(fix_res)
        
        elif action == "abort":
            result["outcome"].update({"status": "cancelled", "message": "Decision aborted"})
        
        elif action in ("init_repo", "create_worktree"):
            result["outcome"].update({
                "status": "error",
                "error": f"Action '{action}' not implemented yet"
            })
        
        else:
            result["outcome"].update({
                "status": "error",
                "error": f"Unknown action: {action}"
            })
    
    except Exception as e:
        result["outcome"].update({"status": "failed", "error": str(e)})
        import traceback; traceback.print_exc()
    
    _print_result(result)
    return 0 if result["outcome"]["status"] in ("success", "cancelled") else 1


def _run_sync(target: str, apply: bool, force: bool, cwd: Path) -> Dict[str, Any]:
    """Call sync command via subprocess (no shell=True)."""
    script = Path(sys.argv[0]).resolve()
    cmd = [sys.executable, str(script), "sync", target]
    if apply:
        cmd.append("--apply")
    if force:
        cmd.append("--force")
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd)
    return {
        "phase": "sync",
        "target": target,
        "apply": apply,
        "force": force,
        "returncode": r.returncode,
        "stdout": r.stdout,
        "stderr": r.stderr,
        "success": r.returncode == 0
    }


def _run_fixes_then_sync(decision: dict, target: str, apply: bool, force: bool, cwd: Path) -> Dict[str, Any]:
    """Execute auto-fix commands safely (no shell=True unless necessary)."""
    auto_fixes = decision.get("auto_fixes", [])
    phases = []
    
    for idx, fix in enumerate(auto_fixes, 1):
        # Validate commands: allow only safe git operations
        for raw_cmd in fix.get("commands", []):
            # Parse command to validate
            parts = shlex.split(raw_cmd)
            if not parts:
                continue
            
            # Allowed: git, git-worktree specific commands only
            if parts[0] != "git":
                return {
                    "status": "failed",
                    "stopped_at": f"fix_{idx}",
                    "error": f"Security: only 'git' commands allowed, got: {parts[0]}",
                    "phases": phases
                }
            
            if not _is_safe_auto_fix_command(parts):
                return {
                    "status": "failed",
                    "stopped_at": f"fix_{idx}",
                    "error": f"Security: unsafe auto-fix command rejected: {raw_cmd}",
                    "phases": phases
                }
            
            # Execute without shell=True
            r = subprocess.run(parts, capture_output=True, text=True, cwd=cwd)
            phases.append({
                "phase": f"fix_{idx}",
                "command": raw_cmd,
                "returncode": r.returncode,
                "stdout": r.stdout,
                "stderr": r.stderr,
                "success": r.returncode == 0
            })
            if r.returncode != 0:
                return {
                    "status": "failed",
                    "stopped_at": f"fix_{idx}",
                    "error": f"Fix command failed: {raw_cmd}",
                    "phases": phases
                }
    
    # After fixes, run sync
    sync_res = _run_sync(target, apply=apply, force=force, cwd=cwd)
    phases.append(sync_res)
    
    all_ok = all(p.get("success", False) for p in phases)
    return {"status": "success" if all_ok else "partial", "phases": phases}


def _print_result(result: dict):
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))


def _decision_hash(decision: dict) -> str:
    s = json.dumps(decision, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(s.encode()).hexdigest()[:32]


def _build_dry_run_result(decision: dict, caller_root: Path, runtime_root: Path) -> Dict[str, Any]:
    act = decision.get("decision", {})
    action = act.get("action")
    target = act.get("target")
    inspect_target = target if isinstance(target, str) and target not in ("", "all") else None
    snapshot = inspect_context(cwd=caller_root, runtime_root=runtime_root, target_name=inspect_target)
    if "error" in snapshot:
        return {
            "contract": {"version": "2.0", "type": "result"},
            "metadata": {
                "decision_hash": _decision_hash(decision),
                "executed_at": datetime.now(timezone.utc).isoformat(),
                "dry_run": True,
            },
            "outcome": {
                "status": "failed",
                "message": "Dry-run inspection failed",
                "error": snapshot["error"],
            },
        }

    targets = snapshot.get("targets", [])
    selected_target = targets[0] if inspect_target and targets else None
    if inspect_target and selected_target is None:
        return {
            "contract": {"version": "2.0", "type": "result"},
            "metadata": {
                "decision_hash": _decision_hash(decision),
                "executed_at": datetime.now(timezone.utc).isoformat(),
                "dry_run": True,
            },
            "outcome": {
                "status": "failed",
                "message": f"Target not found in live context: {inspect_target}",
            },
        }

    decision_context_hash = decision.get("metadata", {}).get("context_hash")
    current_context_hash = snapshot.get("meta", {}).get("context_hash")
    outcome: Dict[str, Any] = {
        "status": "dry_run",
        "message": "Dry-run validated against live context",
        "decision_summary": decision.get("decision"),
        "auto_fixes_count": len(decision.get("auto_fixes", [])),
        "would_execute": action,
        "current_context_hash": current_context_hash,
        "decision_context_hash": decision_context_hash,
        "context_match": not decision_context_hash or current_context_hash == decision_context_hash,
        "summary": snapshot.get("summary"),
    }
    if selected_target is not None:
        outcome["target"] = selected_target
    else:
        outcome["targets"] = targets

    return {
        "contract": {"version": "2.0", "type": "result"},
        "metadata": {
            "decision_hash": _decision_hash(decision),
            "executed_at": datetime.now(timezone.utc).isoformat(),
            "dry_run": True,
        },
        "outcome": outcome,
    }


def _is_safe_auto_fix_command(parts: list[str]) -> bool:
    if not parts or parts[0] != "git":
        return False

    banned_tokens = {
        "reset", "clean", "rm", "push", "pull", "merge", "rebase",
        "cherry-pick", "revert", "--hard", "--force", "-f", "-D",
        "-d", "-x", "-fdx",
    }
    if any(token in banned_tokens for token in parts[1:]):
        return False

    safe_prefixes = [
        ("git", "status"),
        ("git", "add"),
        ("git", "commit"),
        ("git", "stash"),
        ("git", "restore"),
        ("git", "branch"),
        ("git", "diff"),
        ("git", "rev-parse"),
        ("git", "symbolic-ref"),
        ("git", "worktree", "list"),
    ]
    return any(tuple(parts[: len(prefix)]) == prefix for prefix in safe_prefixes)
