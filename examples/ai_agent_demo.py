#!/usr/bin/env python3
"""
AI Agent Demo: 三步流程自动化 sync (inspect → decide → apply)

用法:
  python3 master/examples/ai_agent_demo.py [--target runner]

原理:
  1. inspect: 讀取完整 repo context (所有 target 狀態)
  2. decide: 基於 health_score/checks 生成決策 JSON
  3. apply: 執行決策 (可選擇 --dry-run 測試)

AI agent 可以重现相同逻辑,替换 decide() 函数即可。
"""

import json
import subprocess
import sys
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "sync_worktree.py"

def run_cmd(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)

def inspect(target=None):
    """Step 1: 取得全局上下文"""
    cmd = [sys.executable, str(SCRIPT_PATH), "inspect"]
    if target:
        cmd += ["--target", target]
    r = run_cmd(cmd)
    if r.returncode != 0:
        print(f"[inspect] 失敗: {r.stderr}", file=sys.stderr)
        sys.exit(1)
    ctx = json.loads(r.stdout)
    if "error" in ctx:
        print(f"[inspect] 錯誤: {ctx['error']}", file=sys.stderr)
        sys.exit(1)
    return ctx

def decide(ctx):
    """
    Step 2: AI 決策引擎 (簡易规则库)
    
    策略:
    - 選 health_score 最低的 target
    - 如果有 auto-fixable checks → fix_then_sync
    - 如果 safe_to_apply → sync
    - 否則 → ask (需要人工)
    """
    targets = ctx["targets"]
    if not targets:
        return {
            "contract": {"version":"2.0","type":"decision"},
            "metadata": {"ai_agent":"demo","context_hash":ctx["meta"]["context_hash"]},
            "decision": {"action":"ask","target":None},
            "questions": [{
                "id":"init_repo","prompt":"No targets configured. Initialize repo?",
                "options":["yes","no"],"default":"no"
            }]
        }
    
    # 選擇最需要的 target (health_score 最小)
    urgent = min(targets, key=lambda t: t.get("health_score", 100))
    print(f"[decide] 選中 target: {urgent['name']} (health={urgent['health']}, score={urgent['health_score']})")
    
    # 分析 blockers/warnings
    blockers = urgent["risk_assessment"]["blockers"]
    safe = urgent["risk_assessment"]["safe_to_apply"]
    
    # 收集可自動修復的 checks
    auto_fixes = []
    for check in urgent["checks"]:
        if not check["passed"] and check.get("remediable"):
            auto_fixes.append({
                "check_id": check["id"],
                "description": check["title"],
                "commands": check["remediation"]["commands"]
            })
    
    if auto_fixes:
        print(f"[decide] 發現 {len(auto_fixes)} 項可自動修復, 觸發 fix_then_sync")
        action = "fix_then_sync"
    elif safe:
        print("[decide] 檢查通過, 执行 sync")
        action = "sync"
    else:
        print("[decide] 需要人工介入, 觸发 ask")
        # 生成問答
        questions = []
        for check in urgent["checks"]:
            if not check["passed"] and not check.get("remediable"):
                questions.append({
                    "id": check["id"],
                    "prompt": f"{check['title']}: {check['description']}. 是否強制執行?",
                    "options": ["abort", "force"],
                    "default": "abort",
                    "risk": "high" if check["severity"]=="error" else "medium"
                })
        return {
            "contract": {"version":"2.0","type":"decision"},
            "metadata": {"ai_agent":"demo","context_hash":ctx["meta"]["context_hash"]},
            "decision": {"action":"ask","target":urgent["name"],"apply":False},
            "questions": questions
        }
    
    return {
        "contract": {"version":"2.0","type":"decision"},
        "metadata": {"ai_agent":"demo","context_hash":ctx["meta"]["context_hash"]},
        "decision": {
            "action": action,
            "target": urgent["name"],
            "apply": True,
            "force": False,
            "confidence": 0.8 if safe else 0.5,
            "rationale": f"Health score {urgent['health_score']}, auto-fixes {len(auto_fixes)}"
        },
        "auto_fixes": auto_fixes
    }

def apply(decision, dry_run=False):
    """Step 3: 執行決策"""
    decision_file = Path("/tmp/ai_decision.json")
    decision_file.write_text(json.dumps(decision, indent=2, ensure_ascii=False))
    
    cmd = [sys.executable, str(SCRIPT_PATH), "apply", "--from-decision", str(decision_file)]
    if dry_run:
        cmd.append("--dry-run")
    
    print(f"\n[apply] 執行: {' '.join(cmd)}")
    r = subprocess.run(cmd, capture_output=True, text=True)
    try:
        result = json.loads(r.stdout) if r.stdout else {}
    except json.JSONDecodeError:
        result = {"raw_stdout": r.stdout, "raw_stderr": r.stderr}
    
    # Pretty print result
    print("\n[apply] 結果:")
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    
    return result

def main():
    import argparse
    parser = argparse.ArgumentParser(description="AI Agent Demo")
    parser.add_argument("--target", help="只處理特定 target (否則全部)")
    parser.add_argument("--dry-run", action="store_true", help="apply 時也用 dry-run")
    args = parser.parse_args()
    
    print("=== AI Agent Demo: inspect → decide → apply ===\n")
    
    # Phase 1: Inspect
    print("🔍 Phase 1: 讀取 repo 狀態...")
    ctx = inspect(target=args.target)
    summary = ctx["summary"]
    print(f"   Repo: {ctx['repository']['name']} ({ctx['repository']['topology']})")
    print(f"   Targets: total={summary['total_targets']}, healthy={summary['healthy']}, warning={summary['warning']}, error={summary['error']}")
    if summary['next_action']:
        print(f"   建議: {summary['next_action']}")
    
    # Phase 2: Decide
    print("\n🧠 Phase 2: AI 決策...")
    decision = decide(ctx)
    dec = decision["decision"]
    print(f"   決策: {dec['action']} (target={dec.get('target')}, apply={dec.get('apply')})")
    if decision.get("questions"):
        print("   ⚠️  產生提問,需要人工回答 (此 demo 停止於此)")
        print("   提問:", decision["questions"])
        sys.exit(0)
    if decision.get("auto_fixes"):
        print(f"   自動修復: {len(decision['auto_fixes'])} 項")
        for fix in decision["auto_fixes"]:
            print(f"     - {fix['check_id']}: {fix['description']}")
            for cmd in fix["commands"][:2]:
                print(f"       $ {cmd}")
    
    # Phase 3: Apply
    print("\n🚀 Phase 3: 執行...")
    result = apply(decision, dry_run=args.dry_run)
    
    status = result.get("outcome", {}).get("status", "unknown")
    if status == "success":
        print("\n✅ 完成!")
    elif status == "failed":
        print("\n❌ 失敗, 請檢查錯誤")
    else:
        print(f"\n⚠️  狀態: {status}")

if __name__ == "__main__":
    main()
