#!/usr/bin/env python3
"""Idempotent cmux session hook patcher for Claude settings and Factory hooks.

Ensures:
  - SessionStart runs hooks/cmux-auto-resume.py (linked into ~/.claude/hooks/).
    It forwards SessionStart to cmux for agents started without cmux's Claude
    wrapper, so cmux still records them. Added when no SessionStart hook runs
    it; an existing registration is left as it is.
  - No event runs cmux-session-ledger.py. cmux records sessions itself, so
    the ledger is retired; its registrations are removed, along with any
    matcher block or event that leaves empty.

Hooks other installers own, such as mukul-memory's cmux-registry.py, are never
touched. Accepts one or more paths, including ``~/.claude/settings.json`` and
``~/.factory/hooks.json``. A file is written only when it changes; an existing
one is backed up first, a newly created one is not. A malformed file is backed
up and rebuilt. A file that can't be read fails the run after the remaining
paths are handled.
"""
import json
import os
import shutil
import sys
import time
from pathlib import Path

AUTO_RESUME_SCRIPT = "cmux-auto-resume.py"
AUTO_RESUME_CMD = f"python3 $HOME/.claude/hooks/{AUTO_RESUME_SCRIPT}"
AUTO_RESUME_TIMEOUT = 10
RETIRED_HOOK_SCRIPTS = ("cmux-session-ledger.py",)


def runs(entry, scripts):
    command = entry.get("command") if isinstance(entry, dict) else None
    return any(os.path.basename(word) in scripts for word in str(command or "").split())


def entries(blocks):
    return [entry for block in blocks if isinstance(block, dict)
            for entry in (block.get("hooks") if isinstance(block.get("hooks"), list) else [])]


def remove_retired(hooks):
    """Drop retired commands from `hooks` in place; return what was removed."""
    removed = []
    for event, blocks in list(hooks.items()):
        if not isinstance(blocks, list):
            continue
        kept_blocks = []
        for block in blocks:
            found = block.get("hooks") if isinstance(block, dict) else None
            if isinstance(found, list) and any(runs(e, RETIRED_HOOK_SCRIPTS) for e in found):
                removed += [f"{event}: {e['command']}" for e in found
                            if runs(e, RETIRED_HOOK_SCRIPTS)]
                block["hooks"] = [e for e in found if not runs(e, RETIRED_HOOK_SCRIPTS)]
                if not block["hooks"]:
                    continue
            kept_blocks.append(block)
        if kept_blocks:
            hooks[event] = kept_blocks
        elif blocks:
            del hooks[event]
    return removed


def ensure_auto_resume(hooks):
    """Register the auto-resume hook on SessionStart unless it already runs there."""
    start = hooks.get("SessionStart")
    if not isinstance(start, list):
        if start is not None:
            return False
        start = hooks["SessionStart"] = []
    if any(runs(entry, (AUTO_RESUME_SCRIPT,)) for entry in entries(start)):
        return False
    start.append({"matcher": "", "hooks": [
        {"type": "command", "command": AUTO_RESUME_CMD, "timeout": AUTO_RESUME_TIMEOUT}]})
    return True


def patch_path(path):
    path = Path(path).expanduser()
    # Atomic replace on a SYMLINK would sever it (swap the link for a regular
    # file), disconnecting an operator/machine-config-managed target. These are
    # functional session hooks that must still apply, so resolve to the real
    # file and write THROUGH the symlink instead of replacing it.
    write_target = Path(os.path.realpath(path)) if path.is_symlink() else path
    existed = path.exists()
    settings = {}
    if existed:
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            settings = loaded if isinstance(loaded, dict) else {}
        except json.JSONDecodeError:
            # A corrupt settings file must be recovered (backed up + rebuilt),
            # not raise — that would abort the installer under `set -e`.
            corrupt = path.with_suffix(path.suffix + f".corrupt-{int(time.time())}")
            shutil.copy2(path, corrupt)
            print(f"  {path} was malformed JSON — backed up to {corrupt}, rebuilding")
            settings = {}
    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        print(f"  {path} has a non-object \"hooks\" — left untouched")
        return False

    removed = remove_retired(hooks)
    for line in removed:
        print(f"  - {line}")
    added = ensure_auto_resume(hooks)
    if added:
        print(f"  + SessionStart: {AUTO_RESUME_CMD}")
    else:
        print(f"  = SessionStart already runs {AUTO_RESUME_SCRIPT}")
    if not (removed or added):
        print("  no changes needed")
        return False

    if write_target.exists():
        bak = write_target.with_suffix(write_target.suffix + f".bak-{int(time.time())}")
        shutil.copy2(write_target, bak)
        print(f"  backed up to {bak}")
    write_target.parent.mkdir(parents=True, exist_ok=True)
    # Atomic write: a crash mid-write must not truncate settings.json (which
    # would brick every later `--apply`). A pid-unique tmp name avoids two
    # concurrent `--apply` runs racing on a shared tmp path.
    tmp = write_target.with_suffix(write_target.suffix + f".tmp-{os.getpid()}")
    tmp.write_text(json.dumps(settings, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(write_target)
    print(f"  wrote {write_target}")
    return True


def main():
    if len(sys.argv) < 2:
        print(
            "usage: patch-settings.py PATH [PATH ...]",
            file=sys.stderr,
        )
        raise SystemExit(2)
    failed = False
    for index, raw_path in enumerate(sys.argv[1:]):
        if index:
            print()
        try:
            patch_path(raw_path)
        except (OSError, ValueError) as exc:
            print(f"  {raw_path} could not be patched: {exc}", file=sys.stderr)
            failed = True
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
