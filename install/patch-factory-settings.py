#!/usr/bin/env python3
import json
import shutil
import sys
import time
from pathlib import Path


def patch_settings(target: Path, source: Path) -> bool:
    # Never write THROUGH a symlink: a machine-config / operator-managed target is
    # authoritative and must not be silently rewritten under the operator.
    if target.is_symlink():
        print(f"  {target} is a symlink (operator/machine-config managed) — "
              f"leaving it authoritative, not patching")
        return False

    desired = json.loads(source.read_text())
    defaults = desired.get("sessionDefaultSettings")
    if not isinstance(defaults, dict):
        raise ValueError("source has no sessionDefaultSettings object")

    # Tolerate a malformed existing settings file: back it up and treat as empty
    # rather than raising, which would abort the whole installer under pipefail.
    current: dict = {}
    if target.exists():
        try:
            loaded = json.loads(target.read_text())
            current = loaded if isinstance(loaded, dict) else {}
        except json.JSONDecodeError:
            backup = target.with_suffix(
                target.suffix + f".corrupt-{int(time.time())}")
            shutil.copy2(target, backup)
            print(f"  {target} was malformed JSON — backed up to {backup}, "
                  f"treating as empty")
            current = {}

    merged = dict(current)
    # OPERATOR-WINS: seed the repo defaults but NEVER override the operator's
    # hand-set values (e.g. a deliberately-lowered autonomyLevel). Only missing
    # keys are filled. This runs on every self-update `install.sh --apply`, so a
    # defaults-win merge would silently re-raise a lowered droid autonomy behind
    # the operator's back.
    merged["sessionDefaultSettings"] = {
        **defaults,
        **(current.get("sessionDefaultSettings") or {}),
    }
    if merged == current:
        return False
    if target.exists():
        backup = target.with_suffix(
            target.suffix + f".bak-{int(time.time())}")
        shutil.copy2(target, backup)
        print(f"  backed up to {backup}")
    target.parent.mkdir(parents=True, exist_ok=True)
    # Atomic write: a crash/SIGTERM mid-write must not leave a truncated
    # settings.json that bricks every subsequent `--apply`.
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(merged, indent=2) + "\n")
    tmp.replace(target)
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
