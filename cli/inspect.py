"""inspect command: export complete repo state as AI-friendly JSON."""

import json
import sys
from pathlib import Path

import policy  # Ensure policies registered

from core.topology import detect_topology, TopologyError
from core.exceptions import ConfigError
from config.loader import load_all
from reporting.ai_context import export_context


def cmd_inspect(args):
    """
    Export full repo state as structured JSON.
    
    One-call global view for AI agents.
    """
    caller_root = Path(getattr(args, "caller_root", Path.cwd())).resolve()
    runtime_root = Path(getattr(args, "runtime_root", Path(__file__).resolve().parent)).resolve()
    
    # Generate full context
    try:
        ctx = export_context(
            target_name=args.target,
            include_file_hashes=getattr(args, "deep", False),
            cwd=caller_root,
            runtime_root=runtime_root,
        )
    except Exception as e:
        # Return error JSON with proper contract
        from datetime import datetime, timezone
        ctx = {
            "contract": {"version": "2.0", "type": "context"},
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

