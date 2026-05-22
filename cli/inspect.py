"""inspect command: export complete repo state as AI-friendly JSON."""

import json
from pathlib import Path

import policy  # noqa: F401  Ensure policies registered

from reporting.ai_context import export_context, export_report


def cmd_inspect(args):
    """
    Export full repo state as structured JSON.
    
    One-call global view for AI agents.
    """
    caller_root = Path(getattr(args, "caller_root", Path.cwd())).resolve()
    runtime_root = Path(getattr(args, "runtime_root", Path(__file__).resolve().parent)).resolve()
    
    # Generate bounded report by default; full context is explicit.
    try:
        if getattr(args, "full", False):
            ctx = export_context(
                target_name=args.target,
                include_file_hashes=getattr(args, "deep", False),
                cwd=caller_root,
                runtime_root=runtime_root,
            )
        else:
            ctx = export_report(
                target_name=args.target,
                cwd=caller_root,
                runtime_root=runtime_root,
            )
    except Exception as e:
        # Return error JSON with proper contract
        from datetime import datetime, timezone
        ctx = {
            "contract": {
                "version": "2.0",
                "type": "context" if getattr(args, "full", False) else "report",
            },
            "error": str(e),
            "meta": {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "context_hash": None
            }
        }
    
    # Output
    if getattr(args, 'output', None):
        Path(args.output).write_text(json.dumps(ctx, indent=2, ensure_ascii=False, default=str))
    else:
        print(json.dumps(ctx, indent=2, ensure_ascii=False, default=str))
    
    return 0 if "error" not in ctx else 1
