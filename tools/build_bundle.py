"""Build the deployable runtime bundle under master/dist/."""

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


RUNTIME_PATHS = (
    "sync_worktree.py",
    "cli",
    "config",
    "core",
    "executor",
    "planner",
    "policy",
    "reporting",
    "LICENSE",
)


def build_bundle(source_root: Path, output_root: Path, bundle_name: str = "sync-worktree") -> Path:
    source_root = Path(source_root).resolve()
    bundle_root = Path(output_root).resolve() / bundle_name
    if bundle_root.exists():
        shutil.rmtree(bundle_root)
    bundle_root.mkdir(parents=True, exist_ok=True)

    for rel_path in RUNTIME_PATHS:
        _copy_runtime_item(source_root / rel_path, bundle_root / rel_path)

    manifest = {
        "name": "sync-worktree",
        "bundle": bundle_name,
        "built_at": datetime.now(timezone.utc).isoformat(),
        "entry_point": "sync_worktree.py",
        "runtime_paths": list(RUNTIME_PATHS),
    }
    (bundle_root / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    return bundle_root


def _copy_runtime_item(source: Path, dest: Path) -> None:
    if source.is_dir():
        shutil.copytree(
            source,
            dest,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache"),
        )
        return

    if source.is_file():
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, dest)
        return

    raise FileNotFoundError(f"Runtime item missing: {source}")


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    build_root = repo_root / "dist"
    bundle_root = build_bundle(repo_root, build_root)
    print(bundle_root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
