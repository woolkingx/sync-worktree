"""AI-friendly context reporter for sync-worktree.

Exports complete repository state as structured JSON for AI agents.
Single entry point: export_context()
"""

import json
import hashlib
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from dataclasses import asdict

from core.topology import detect_topology
from config.loader import load_all
from planner.compute import compute_plan
from policy.base import load_policies, PolicyEngine
from core.git import git_status_porcelain


def _git_try(*args, cwd=None, timeout=30) -> Tuple[bool, str]:
    result = subprocess.run(
        ["git"] + list(args), cwd=cwd,
        capture_output=True, text=True, timeout=timeout
    )
    return result.returncode == 0, result.stdout.strip()


def _parse_worktree_porcelain(cwd: Path) -> List[Dict]:
    ok, out = _git_try("worktree", "list", "--porcelain", cwd=cwd)
    if not ok:
        return []
    worktrees, current = [], {}
    for line in out.splitlines():
        if not line:
            if current and not current.get("bare"):
                worktrees.append(current)
            current = {}
        elif line.startswith("worktree "):
            current["path"] = Path(line.split(" ", 1)[1])
        elif line.startswith("branch "):
            current["branch"] = line.split(" ", 1)[1]
        elif line.startswith("HEAD "):
            current["head"] = line.split(" ", 1)[1]
    if current and not current.get("bare"):
        worktrees.append(current)
    return worktrees


def _simplify_name(branch: Optional[str], path: Path) -> str:
    if branch:
        if branch.startswith("refs/heads/"):
            return branch[11:]
        if branch.startswith("refs/tags/"):
            return branch[10:]
        if branch.startswith("refs/remotes/"):
            return branch[13:]
        return branch
    return path.name


def _simplify_ref(ref: Optional[str]) -> Optional[str]:
    if not ref:
        return None
    if ref.startswith("refs/heads/"):
        return ref[11:]
    if ref.startswith("refs/tags/"):
        return ref[10:]
    if ref.startswith("refs/remotes/"):
        return ref[13:]
    return ref


def _load_sync_state(git_internal: Path) -> Dict[str, Any]:
    state_path = git_internal / "sync-worktree.state.json"
    if not state_path.exists():
        return {}
    try:
        data = json.loads(state_path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _extract_last_sync(state: Dict[str, Any], target_name: str) -> Optional[Dict[str, Any]]:
    last_sync = state.get("last_sync", {})
    if isinstance(last_sync, dict):
        entry = last_sync.get(target_name)
        if isinstance(entry, dict):
            return entry

    targets = state.get("targets", {})
    if isinstance(targets, dict):
        entry = targets.get(target_name)
        if isinstance(entry, dict):
            nested = entry.get("last_sync")
            if isinstance(nested, dict):
                return nested
    return None


def _compute_ahead_behind(path: Path) -> Dict[str, Dict[str, int]]:
    ok, upstream = _git_try("rev-parse", "--abbrev-ref", "@{u}", cwd=path)
    upstream = _simplify_ref(upstream) if ok else None
    if not upstream:
        return {}

    ok, counts = _git_try("rev-list", "--left-right", "--count", f"{upstream}...HEAD", cwd=path)
    if not ok:
        return {}

    parts = counts.split()
    if len(parts) != 2:
        return {}

    behind_raw, ahead_raw = parts
    try:
        behind = int(behind_raw)
        ahead = int(ahead_raw)
    except ValueError:
        return {}

    return {
        "ahead_of": {upstream: ahead} if ahead else {},
        "behind_of": {upstream: behind} if behind else {},
    }


def export_context(
    target_name: Optional[str] = None,
    include_file_hashes: bool = False,
    cwd: Optional[Path] = None,
    runtime_root: Optional[Path] = None,
) -> Dict[str, Any]:
    caller_root = Path(cwd).resolve() if cwd else Path.cwd().resolve()
    runtime_root = Path(runtime_root).resolve() if runtime_root else None
    
    try:
        topology = detect_topology(caller_root)
    except Exception as e:
        return {
            "contract": {"version": "2.0", "type": "context"},
            "error": f"Topology detection failed: {e}",
            "meta": {"generated_at": datetime.now(timezone.utc).isoformat()}
        }
    
    try:
        rule_config, settings = load_all(topology, cli_overrides={})
    except Exception as e:
        return {
            "contract": {"version": "2.0", "type": "context"},
            "error": f"Config load failed: {e}",
            "meta": {"generated_at": datetime.now(timezone.utc).isoformat()}
        }
    
    worktrees = _collect_worktree_info(topology)
    git_status = _collect_git_status(topology)
    
    targets_data = []
    target_names = [target_name] if target_name else list(rule_config.targets.keys())
    
    for tname in target_names:
        if tname not in rule_config.targets:
            continue
        
        try:
            plan = compute_plan(topology, rule_config, settings, tname)
            if plan is None:
                targets_data.append({
                    "name": tname, "error": "Failed to compute plan (worktree missing?)",
                    "health": "critical", "health_score": 0
                })
                continue
            
            validation = _validate_plan(plan, rule_config, settings)
            target_entry = _serialize_target(plan, validation, include_file_hashes, topology.git_internal)
            targets_data.append(target_entry)
            
        except Exception as e:
            import traceback; traceback.print_exc()
            targets_data.append({
                "name": tname, "error": str(e), "health": "critical", "health_score": 0
            })
    
    dependencies = _analyze_dependencies(targets_data)
    summary = _summarize_health(targets_data)
    
    context = {
        "contract": {"version": "2.0", "type": "context"},
        "meta": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
        "tool_version": "0.5.9-ai",
            "context_hash": None
        },
        "repository": {
            "name": topology.project_root.name,
            "topology": topology.mode,
            "bare_path": str(topology.git_internal.relative_to(topology.project_root))
                if topology.git_internal else None,
            "worktrees": worktrees
        },
        "global_state": git_status,
        "config": {
            "file": str(rule_config.path.relative_to(topology.project_root))
                if hasattr(rule_config, 'path') and rule_config.path else None,
            "version": rule_config.version,
            "targets": {n: asdict(t) for n, t in rule_config.targets.items()}
        },
        "targets": targets_data,
        "dependencies": dependencies,
        "decision_engine": {
            "auto_fixable_checks": ["POL-TOP-001", "POL-CON-001"],
            "requires_human": ["POL-TOP-002"],
            "blocking_checks": ["POL-TOP-001", "POL-TOP-002"],
            "suggested_workflow": _generate_workflow_suggestions(targets_data)
        },
        "summary": summary
    }
    
    context_json = json.dumps(context, sort_keys=True, ensure_ascii=False, default=str)
    context["meta"]["context_hash"] = f"sha256:{_sha256(context_json)}"
    context["runtime"] = {
        "caller_root": str(caller_root),
        "runtime_root": str(runtime_root) if runtime_root else None,
        "log_dir": str(runtime_root / "logs") if runtime_root else None,
    }
    return context


def _collect_worktree_info(topology) -> List[Dict]:
    worktrees_raw = _parse_worktree_porcelain(topology.git_internal)
    result = []
    for wt in worktrees_raw:
        name = _simplify_name(wt.get("branch"), Path(wt["path"]))
        result.append({
            "name": name,
            "path": str(wt["path"]),
            "branch": wt.get("branch"),
            "head": wt.get("head"),
            "is_current": (wt["path"] == topology.cwd_worktree)
        })
    for wt in result:
        if wt["path"] == str(topology.cwd_worktree):
            wt["is_current"] = True
    return result


def _collect_git_status(topology) -> Dict[str, Any]:
    status = {}
    for wt in _collect_worktree_info(topology):
        name, path = wt["name"], Path(wt["path"])
        try:
            lines = git_status_porcelain(path)
            uncommitted, staged, untracked = [], [], []
            for line in lines:
                if len(line) < 3:
                    continue
                code, fname = line[:2], line[3:]
                idx_status, wt_status = code[0], code[1]
                if idx_status not in (' ', '?'):
                    staged.append(fname)
                if wt_status not in (' ', '?'):
                    uncommitted.append(fname)
                if wt_status == '?':
                    untracked.append(fname)
            status[name] = {
                "dirty": bool(uncommitted or staged),
                "uncommitted": uncommitted,
                "staged": staged,
                "untracked": untracked,
                **{
                    "ahead_of": {},
                    "behind_of": {},
                    **_compute_ahead_behind(path),
                },
            }
        except Exception:
            status[name] = {
                "dirty": False,
                "uncommitted": [],
                "staged": [],
                "untracked": [],
                "ahead_of": {},
                "behind_of": {},
            }
    return status



def _validate_plan(plan, rule_config, settings):
    from policy.base import PolicyContext
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
        policy_params={}
    )
    return engine.validate(ctx, strict=False)


def _serialize_target(plan, validation, include_hashes: bool, git_internal: Path) -> Dict[str, Any]:
    error_count = sum(1 for r in validation.failed if r.severity == "error")
    warn_count = sum(1 for r in validation.failed if r.severity == "warn")
    health_score = max(0, 100 - error_count * 50 - warn_count * 10)
    health = "critical" if error_count > 0 and health_score < 50 else \
             "error" if error_count > 0 else \
             "warning" if warn_count > 0 else "ok"
    
    action_details = []
    for rel_path in plan.sync_files:
        status = _get_file_action(rel_path, plan.actions)
        protected = _is_protected(rel_path, plan.target_config)
        size = _get_file_size(plan.source / rel_path) if (plan.source / rel_path).exists() else 0
        entry = {"path": str(rel_path), "status": status, "protected": protected, "size": size}
        if include_hashes:
            entry["hash"] = _file_hash(plan.source / rel_path) if (plan.source / rel_path).exists() else None
        action_details.append(entry)
    
    checks = []
    for r in validation.results:
        checks.append({
            "id": r.code, "passed": r.valid, "severity": r.severity,
            "title": r.title or r.message.split('.')[0],
            "description": r.message, "evidence": r.evidence,
            "remediable": bool(r.fix_commands) if not r.valid else False,
            "remediation": {"description": r.suggestion, "commands": r.fix_commands}
                if r.fix_commands and not r.valid else None
        })
    
    blockers = [c for c in checks if not c["passed"] and c["severity"] == "error"]
    warnings = [c for c in checks if not c["passed"] and c["severity"] == "warn"]
    state = _load_sync_state(git_internal)

    risk = {
        "overall_score": health_score,
        "blockers": [b["id"] for b in blockers],
        "warnings": [w["id"] for w in warnings],
        "safe_to_apply": len(blockers) == 0,
        "requires_force": False,
        "recommended_action": _recommend_action(blockers, warnings, plan)
    }
    
    return {
        "name": plan.target_name, "exists": True, "health": health, "health_score": health_score,
        "source_worktree": plan.source_branch or str(plan.source.name),
        "dest_path": str(plan.dest), "last_sync": _extract_last_sync(state, plan.target_name),
        "sync_plan": {
            "total_source_files": len(plan.sync_files),
            "matched_files": len(plan.sync_files),
            "excluded_files": len(plan.excluded_files),
            "actions": {k: len(v) for k, v in plan.actions.items()},
            "action_details": action_details,
            "protected_hits": [a["path"] for a in action_details if a["protected"]],
            "delete_candidates": [str(p) for p in plan.actions.get('D', [])]
        },
        "checks": checks,
        "risk_assessment": risk
    }


def _get_file_action(rel_path: Path, actions: Dict[str, List[Path]]) -> str:
    for st, files in actions.items():
        if rel_path in files:
            return st
    return "X"


def _is_protected(rel_path: Path, target_config: Dict) -> bool:
    patterns = target_config.get("protect", [])
    if not patterns:
        return False
    from core.filter import filter_match
    for pat in patterns:
        if filter_match(str(rel_path), pat):
            return True
    return False


def _get_file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except Exception:
        return 0


def _file_hash(path: Path) -> str:
    import hashlib
    try:
        h = hashlib.sha256()
        with open(path, 'rb') as f:
            while chunk := f.read(8192):
                h.update(chunk)
        return f"sha256:{h.hexdigest()}"
    except Exception:
        return "hash:error"


def _recommend_action(blockers, warnings, plan):
    if blockers:
        return "fix_blockers_first"
    if warnings:
        return "review_warnings_then_sync"
    return "ready_to_sync"


def _analyze_dependencies(targets_data: List[Dict]) -> Dict[str, Any]:
    overlaps = []
    for i, t1 in enumerate(targets_data):
        for t2 in targets_data[i+1:]:
            files1 = {f["path"] for f in t1["sync_plan"]["action_details"]}
            files2 = {f["path"] for f in t2["sync_plan"]["action_details"]}
            common = files1 & files2
            if common:
                overlaps.append({
                    "targets": [t1["name"], t2["name"]],
                    "shared_files": list(common)[:10],
                    "count": len(common)
                })
    return {"path_overlaps": overlaps, "conflicts": []}


def _summarize_health(targets_data: List[Dict]) -> Dict[str, Any]:
    total = len(targets_data)
    healthy = sum(1 for t in targets_data if t.get("health") == "ok")
    warning = sum(1 for t in targets_data if t.get("health") == "warning")
    error = sum(1 for t in targets_data if t.get("health") in ("error", "critical"))
    urgent = None
    min_score = 100
    for t in targets_data:
        score = t.get("health_score", 100)
        if score < min_score:
            min_score = score
            urgent = t["name"]
    return {
        "total_targets": total, "healthy": healthy, "warning": warning,
        "error": error,
        "critical": sum(1 for t in targets_data if t.get("health") == "critical"),
        "next_action": f"sync {urgent}" if urgent else "all_clear"
    }


def _generate_workflow_suggestions(targets_data: List[Dict]) -> List[str]:
    steps = []
    critical = [t for t in targets_data if t.get("health") == "critical"]
    errors = [t for t in targets_data if t.get("health") == "error"]
    warnings = [t for t in targets_data if t.get("health") == "warning"]
    if critical:
        for t in critical:
            steps.append(f"Fix critical issues in {t['name']} before syncing")
    elif errors:
        for t in errors:
            for check in t.get("checks", []):
                if not check["passed"] and check["severity"] == "error":
                    steps.append(f"{t['name']}: resolve {check['id']} - {check['title']}")
    elif warnings:
        for t in warnings:
            steps.append(f"Review warnings for {t['name']} or use --force")
    else:
        steps.append("All targets healthy. Ready to sync all.")
    return steps


def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode('utf-8')).hexdigest()
