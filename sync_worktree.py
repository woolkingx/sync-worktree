#!/usr/bin/env python3
"""sync-worktree: Policy-Enforcing Git Worktree Synchronizer.
AI-native with inspect/apply contract.
"""

import argparse
import json
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SOURCE_ROOT = SCRIPT_DIR if (SCRIPT_DIR / "config").is_dir() else SCRIPT_DIR.parent / "master"
sys.path.insert(0, str(SOURCE_ROOT))

from config.rule import RuleConfig, TargetBinding, rule_config_path
from config.setting import Settings

from core.topology import detect_topology
from config.loader import load_all
from cli.check import cmd_check as check_cmd
from cli.sync import cmd_sync as sync_cmd
from cli.inspect import cmd_inspect as inspect_cmd
from cli.apply import cmd_apply as apply_cmd
from cli.doctor import cmd_doctor as doctor_cmd
from cli.explain import cmd_explain as explain_cmd


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="sync-worktree",
        description="Policy-enforced sync between Git worktrees"
    )
    p.add_argument("-C", "--config-path", help="Path to project root (auto-detect if omitted)")
    sub = p.add_subparsers(dest="command", required=True)
    
    # inspect
    inspect_parser = sub.add_parser("inspect", help="Export bounded report JSON for AI agents")
    inspect_parser.add_argument("--target", help="Limit to specific target (default: all)")
    inspect_parser.add_argument("--deep", action="store_true", help="Include file hashes (slow)")
    inspect_parser.add_argument("--full", action="store_true", help="Export full context instead of bounded report")
    inspect_parser.add_argument("--output", type=Path, help="Write to file")
    inspect_parser.set_defaults(func=cmd_inspect_wrapper)
    
    # apply
    apply_parser = sub.add_parser("apply", help="Execute AI decision from JSON file")
    apply_parser.add_argument("--from-decision", type=Path, required=True, help="Decision JSON file")
    apply_parser.add_argument("--dry-run", action="store_true", help="Validate but don't execute")
    apply_parser.add_argument("--verify-context", action="store_true", help="Verify context hash before executing")
    apply_parser.add_argument("--verify-report", action="store_true", help="Verify report hash before executing")
    apply_parser.set_defaults(func=cmd_apply_wrapper)

    # doctor
    doctor_parser = sub.add_parser("doctor", help="Run read-only agent preflight diagnosis")
    doctor_parser.add_argument("--json", action="store_true", help="Machine-readable report output")
    doctor_parser.set_defaults(func=cmd_doctor_wrapper)

    # explain
    explain_parser = sub.add_parser("explain", help="Explain the current report decision for a target")
    explain_parser.add_argument("target", help="Target worktree name")
    explain_parser.add_argument("--source", help="Source worktree name")
    explain_parser.add_argument("--json", action="store_true", help="Machine-readable explanation output")
    explain_parser.set_defaults(func=cmd_explain_wrapper)
    
    # check
    check_parser = sub.add_parser("check", help="Compute plan and validate policies (dry-run)")
    check_parser.add_argument("target", help="Target worktree name")
    check_parser.add_argument("--source", help="Source worktree name")
    check_parser.add_argument("--json", action="store_true", help="Machine-readable output")
    check_parser.add_argument("--full", action="store_true", help="Include full trace in JSON output")
    check_parser.set_defaults(func=cmd_check_wrapper)
    
    # sync
    sync_parser = sub.add_parser("sync", help="Check then optionally execute")
    sync_parser.add_argument("target", nargs="?", help="Target worktree name")
    sync_parser.add_argument(
        "--target",
        dest="target_flag",
        help="Legacy alias for target; prefer positional 'sync <target>'",
    )
    sync_parser.add_argument("--source", help="Source worktree name")
    sync_parser.add_argument("--apply", action="store_true", help="Execute after successful validation")
    sync_parser.add_argument("--json", action="store_true", help="Machine-readable output")
    sync_parser.add_argument("--full", action="store_true", help="Include full trace in JSON output")
    sync_parser.add_argument("--force", action="store_true", help="Bypass warnings")
    sync_parser.set_defaults(func=cmd_sync_wrapper)
    
    # init
    init_parser = sub.add_parser("init", help="Initialize a new sync-worktree project")
    init_parser.add_argument("path", help="Project directory")
    init_parser.add_argument("--bare", action="store_true", help="Initialize bare repository")
    init_parser.add_argument("--url", help="Clone from remote URL")
    init_parser.add_argument("--branch", help="Default branch name")
    init_parser.set_defaults(func=cmd_init_wrapper)
    
    # config
    cfg_parser = sub.add_parser("config", help="Manage configuration")
    cfg_sub = cfg_parser.add_subparsers(dest="cfg_cmd", required=True)
    show = cfg_sub.add_parser("show", help="Show effective config")
    show.add_argument("--rule", action="store_true", help="Show rule config only")
    show.add_argument("--setting", action="store_true", help="Show settings only")
    show.set_defaults(func=cmd_config_show_wrapper)
    schema = cfg_sub.add_parser("schema", help="Print config schema reference")
    schema.set_defaults(func=cmd_config_schema_wrapper)

    # target
    target_parser = sub.add_parser("target", help="Manage target bindings")
    target_sub = target_parser.add_subparsers(dest="target_cmd", required=True)
    add = target_sub.add_parser("add", help="Add or update a target binding")
    add.add_argument("name", help="Target name")
    add.add_argument("--role", default="deployment", help="Role name (default: deployment)")
    add.add_argument("--source", help="Source worktree or bundle path")
    add.add_argument("--description", help="Target description")
    add.add_argument("--include", action="append", default=[], help="Include pattern (repeatable)")
    add.add_argument("--exclude", action="append", default=[], help="Exclude pattern (repeatable)")
    add.add_argument("--protect", action="append", default=[], help="Protect pattern (repeatable)")
    add.add_argument(
        "--delete-policy",
        choices=["never", "unlisted", "tracked_only"],
        help="Deletion policy override",
    )
    add.add_argument("--force", action="store_true", help="Overwrite existing target binding")
    add.set_defaults(func=cmd_target_add_wrapper)

    remove = target_sub.add_parser("remove", help="Remove a target binding")
    remove.add_argument("name", help="Target name")
    remove.set_defaults(func=cmd_target_remove_wrapper)

    return p


def _attach_execution_roots(args):
    caller_root = Path(getattr(args, "config_path", None) or Path.cwd()).resolve()
    runtime_root = Path(__file__).resolve().parent
    args.caller_root = caller_root
    args.runtime_root = runtime_root
    return caller_root, runtime_root


def _load_project_context(args):
    caller_root, runtime_root = _attach_execution_roots(args)
    topology = detect_topology(caller_root)
    rule_config, settings = load_all(topology, cli_overrides={})
    return caller_root, runtime_root, topology, rule_config, settings


def _load_rule_config(topology):
    rule_path = rule_config_path(topology)
    if not rule_path.exists():
        rule_path.parent.mkdir(parents=True, exist_ok=True)
        default_rule = RuleConfig.default()
        rule_path.write_text(json.dumps(asdict(default_rule), indent=2, ensure_ascii=False))
    return RuleConfig.from_file(rule_path)


def _write_rule_config(topology, rule_config):
    rule_path = rule_config_path(topology)
    rule_path.write_text(json.dumps(asdict(rule_config), indent=2, ensure_ascii=False))
    return rule_path


def _config_payload(rule_config, settings, show_rule=False, show_setting=False):
    rule_json = asdict(rule_config)
    settings_json = asdict(settings)
    if show_rule and not show_setting:
        return {"rule": rule_json}
    if show_setting and not show_rule:
        return {"settings": settings_json}
    return {"rule": rule_json, "settings": settings_json}


def _config_schema_payload():
    return {
        "rule": asdict(RuleConfig.default()),
        "settings": Settings.DEFAULTS(),
    }


def cmd_config_show_wrapper(args):
    _attach_execution_roots(args)
    return cmd_config_show(args)


def cmd_config_schema_wrapper(args):
    _attach_execution_roots(args)
    return cmd_config_schema(args)


def cmd_target_add_wrapper(args):
    _attach_execution_roots(args)
    return cmd_target_add(args)


def cmd_target_remove_wrapper(args):
    _attach_execution_roots(args)
    return cmd_target_remove(args)


def cmd_inspect_wrapper(args):
    _attach_execution_roots(args)
    return inspect_cmd(args)


def cmd_apply_wrapper(args):
    _attach_execution_roots(args)
    return apply_cmd(args)


def cmd_doctor_wrapper(args):
    _attach_execution_roots(args)
    return doctor_cmd(args)


def cmd_explain_wrapper(args):
    _attach_execution_roots(args)
    return explain_cmd(args)


def cmd_check_wrapper(args):
    _attach_execution_roots(args)
    return check_cmd(args)


def cmd_sync_wrapper(args):
    target_status = _normalize_sync_target(args)
    if target_status is not None:
        return target_status
    _attach_execution_roots(args)
    return sync_cmd(args)


def _normalize_sync_target(args):
    positional = getattr(args, "target", None)
    legacy_flag = getattr(args, "target_flag", None)
    if positional and legacy_flag and positional != legacy_flag:
        print(
            f"Error: conflicting targets: positional '{positional}' and --target '{legacy_flag}'",
            file=sys.stderr,
        )
        return 2
    if legacy_flag and not positional:
        print("Warning: 'sync --target <name>' is deprecated; use 'sync <name>'.", file=sys.stderr)
        args.target = legacy_flag
    if not getattr(args, "target", None):
        print("Error: sync requires a target; use 'sync <target>'.", file=sys.stderr)
        return 2
    return None


def cmd_config_show(args):
    """Show effective configuration."""
    caller_root, _, topology, rule_config, settings = _load_project_context(args)
    payload = _config_payload(
        rule_config,
        settings,
        show_rule=getattr(args, "rule", False),
        show_setting=getattr(args, "setting", False),
    )
    print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
    return 0


def cmd_config_schema(args):
    """Print configuration schema reference."""
    _attach_execution_roots(args)
    print(json.dumps(_config_schema_payload(), indent=2, ensure_ascii=False, default=str))
    return 0


def cmd_target_add(args):
    """Add or update a target binding in rule config."""
    caller_root, _ = _attach_execution_roots(args)
    topology = detect_topology(caller_root)
    rule_config = _load_rule_config(topology)
    rule_path = rule_config_path(topology)
    target_name = args.name
    existing = rule_config.targets.get(target_name)
    if existing and not getattr(args, "force", False):
        print(f"Error: target '{target_name}' already exists in {rule_path}", file=sys.stderr)
        return 1

    if args.role not in rule_config.roles:
        print(
            f"Error: role '{args.role}' not found. Available roles: {', '.join(rule_config.roles.keys())}",
            file=sys.stderr,
        )
        return 1

    overrides = {}
    if args.include:
        overrides["include"] = list(args.include)
    if args.exclude:
        overrides["exclude"] = list(args.exclude)
    if args.protect:
        overrides["protect"] = list(args.protect)
    if args.delete_policy:
        overrides["delete_policy"] = args.delete_policy

    updated_targets = dict(rule_config.targets)
    updated_targets[target_name] = TargetBinding(
        role=args.role,
        description=args.description,
        source=args.source,
        overrides=overrides,
    )
    updated_rule = RuleConfig(
        version=rule_config.version,
        roles=rule_config.roles,
        targets=updated_targets,
        policies=rule_config.policies,
    )
    _write_rule_config(topology, updated_rule)

    print(
        json.dumps(
            {
                "status": "ok",
                "action": "target_add",
                "caller_root": str(caller_root),
                "rule_file": str(rule_path),
                "target": {
                    "name": target_name,
                    "role": args.role,
                    "source": args.source,
                    "description": args.description,
                    "overrides": overrides,
                },
            },
            indent=2,
            ensure_ascii=False,
            default=str,
        )
    )
    return 0


def cmd_target_remove(args):
    """Remove a target binding from rule config."""
    caller_root, _ = _attach_execution_roots(args)
    topology = detect_topology(caller_root)
    rule_config = _load_rule_config(topology)
    rule_path = rule_config_path(topology)
    target_name = args.name
    if target_name not in rule_config.targets:
        print(f"Error: target '{target_name}' not found in {rule_path}", file=sys.stderr)
        return 1

    updated_targets = dict(rule_config.targets)
    del updated_targets[target_name]
    updated_rule = RuleConfig(
        version=rule_config.version,
        roles=rule_config.roles,
        targets=updated_targets,
        policies=rule_config.policies,
    )
    _write_rule_config(topology, updated_rule)

    print(
        json.dumps(
            {
                "status": "ok",
                "action": "target_remove",
                "caller_root": str(caller_root),
                "rule_file": str(rule_path),
                "target": target_name,
            },
            indent=2,
            ensure_ascii=False,
            default=str,
        )
    )
    return 0


def cmd_init_wrapper(args):
    """Initialize a new sync-worktree project (bare repo + master worktree)."""
    cwd = Path(args.path).resolve()
    cwd.mkdir(parents=True, exist_ok=True)
    bare_dir = cwd / ".bare"
    
    if bare_dir.exists():
        print(f"Error: .bare/ already exists in {cwd}", file=sys.stderr)
        return 1
    
    # Step 1: Create bare repository
    try:
        if args.url:
            subprocess.run(["git", "clone", "--bare", args.url, str(bare_dir)], check=True)
        else:
            subprocess.run(["git", "init", "--bare", str(bare_dir)], check=True)
            # Seed empty commit so worktree add works
            result = subprocess.run(
                ["git", "hash-object", "-t", "tree", "/dev/null"],
                cwd=bare_dir, capture_output=True, text=True, check=True
            )
            tree_hash = result.stdout.strip()
            result = subprocess.run(
                ["git", "commit-tree", tree_hash, "-m", "init"],
                cwd=bare_dir, capture_output=True, text=True, check=True
            )
            commit_hash = result.stdout.strip()
            branch = args.branch or "master"
            subprocess.run(["git", "update-ref", f"refs/heads/{branch}", commit_hash], cwd=bare_dir, check=True)
    except subprocess.CalledProcessError as e:
        print(f"Error: git command failed: {e}", file=sys.stderr)
        return 1
    
    # Step 2: Determine branch name
    if args.branch:
        branch_name = args.branch
    elif args.url:
        try:
            head = subprocess.run(
                ["git", "symbolic-ref", "HEAD"],
                cwd=bare_dir, capture_output=True, text=True, check=True
            ).stdout.strip()
            branch_name = head.split("/")[-1] if head.startswith("refs/heads/") else "master"
        except subprocess.CalledProcessError:
            branch_name = "master"
    else:
        branch_name = args.branch or "master"
    
    # Step 3: Create worktree
    worktree_path = cwd / branch_name
    if worktree_path.exists():
        print(f"Error: worktree path {worktree_path} already exists", file=sys.stderr)
        return 1
    
    try:
        # Create worktree linked to the bare repo: git -C <bare_dir> worktree add <path> <branch>
        subprocess.run(
            ["git", "-C", str(bare_dir), "worktree", "add", str(worktree_path), branch_name],
            capture_output=True, text=True, check=True
        )
    except subprocess.CalledProcessError as e:
        print(f"Error: failed to create worktree: {e}", file=sys.stderr)
        print(f"  bare_dir: {bare_dir}", file=sys.stderr)
        print(f"  branch_name: {branch_name}", file=sys.stderr)
        print(f"  worktree_path: {worktree_path}", file=sys.stderr)
        return 1
    
    # Step 4: Write default rule.config.json
    try:
        default_rule = RuleConfig.default()
        rule_path = bare_dir / "rule.config.json"
        rule_path.write_text(json.dumps(asdict(default_rule), indent=2))
    except Exception as e:
        print(f"Warning: failed to write default config: {e}", file=sys.stderr)
    
    print(f"\n✅ Initialized sync-worktree project at {cwd}")
    print(f"   Bare repo:   {bare_dir}")
    print(f"   Worktree:    {worktree_path} (branch: {branch_name})")
    print(f"   Config:      {rule_path}")
    print(f"\nNext steps:")
    print(f"  cd {worktree_path}")
    print(f"  python3 sync_worktree.py config show")
    return 0


def main():
    parser = build_parser()
    args = parser.parse_args()
    
    try:
        result = args.func(args)
        sys.exit(result or 0)
    except KeyboardInterrupt:
        print("\nInterrupted", file=sys.stderr)
        sys.exit(130)
    except Exception as e:
        print(f"Fatal error: {e}", file=sys.stderr)
        import traceback; traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
