#!/usr/bin/env python3
import json
import shutil
import sys
import time
from pathlib import Path


def patch_settings(target: Path, source: Path) -> bool:
    current = json.loads(target.read_text()) if target.exists() else {}
    desired = json.loads(source.read_text())
    defaults = desired.get("sessionDefaultSettings")
    if not isinstance(defaults, dict):
        raise ValueError("source has no sessionDefaultSettings object")
    merged = dict(current)
    merged["sessionDefaultSettings"] = {
        **(current.get("sessionDefaultSettings") or {}),
        **defaults,
    }
    if merged == current:
        return False
    if target.exists():
        backup = target.with_suffix(
            target.suffix + f".bak-{int(time.time())}")
        shutil.copy2(target, backup)
        print(f"  backed up to {backup}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(merged, indent=2) + "\n")
    print(f"  wrote {target}")
    return True


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: patch-factory-settings.py TARGET SOURCE",
              file=sys.stderr)
        return 2
    patch_settings(Path(sys.argv[1]).expanduser(),
                   Path(sys.argv[2]).expanduser())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
