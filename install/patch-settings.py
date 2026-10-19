#!/usr/bin/env python3
"""Idempotent removal of retired cmux session hooks from agent hook settings.

cmux records agent sessions itself (`cmux sessions list --json`), so two
hand-written hooks that kept their own copy of that mapping are retired:

  - cmux-session-ledger.py appended start/end events to a JSONL ledger
  - cmux-registry.py       mapped each cmux workspace to its latest session

hooks/cmux-auto-resume.py stays: it is what makes cmux record agents started
without cmux's Claude wrapper.

This drops every hook whose command runs a retired script, under every event,
plus any matcher block or event it leaves empty. Accepts one or more paths,
including ``~/.claude/settings.json`` and ``~/.factory/hooks.json``. A file is
rewritten only when something was removed, and is backed up first. A missing,
malformed, or unreadable file is left alone; an unreadable one fails the run
after the remaining paths are handled.
"""
import json
import os
import shutil
import sys
import time
from pathlib import Path

RETIRED_HOOK_SCRIPTS = (
    "cmux-session-ledger.py",
    "cmux-registry.py",
)


def is_retired(entry):
    command = entry.get("command") if isinstance(entry, dict) else None
    return any(os.path.basename(word) in RETIRED_HOOK_SCRIPTS
               for word in str(command or "").split())


def remove_retired(hooks):
    """Drop retired commands from `hooks` in place; return what was removed."""
    removed = []
    for event, blocks in list(hooks.items()):
        if not isinstance(blocks, list):
            continue
        kept_blocks = []
        for block in blocks:
            entries = block.get("hooks") if isinstance(block, dict) else None
            if isinstance(entries, list) and any(map(is_retired, entries)):
                removed += [f"{event}: {entry['command']}" for entry in entries if is_retired(entry)]
                block["hooks"] = [entry for entry in entries if not is_retired(entry)]
                if not block["hooks"]:
                    continue
            kept_blocks.append(block)
        if kept_blocks:
            hooks[event] = kept_blocks
        elif blocks:
            del hooks[event]
    return removed


def patch_path(path):
    path = Path(path).expanduser()
    # Atomic replace on a SYMLINK would sever it (swap the link for a regular
    # file), disconnecting an operator/machine-config-managed target, so
    # resolve to the real file and write THROUGH the symlink instead.
    write_target = Path(os.path.realpath(path)) if path.is_symlink() else path
    if not path.exists():
        print(f"  {path} does not exist — nothing to remove")
        return False
    try:
        settings = json.loads(path.read_text())
    except json.JSONDecodeError:
        print(f"  {path} is malformed JSON — left untouched")
        return False
    hooks = settings.get("hooks") if isinstance(settings, dict) else None
    if not isinstance(hooks, dict):
        print("  no changes needed")
        return False
    removed = remove_retired(hooks)
    if not removed:
        print("  no changes needed")
        return False
    for line in removed:
        print(f"  - {line}")

    bak = write_target.with_suffix(write_target.suffix + f".bak-{int(time.time())}")
    shutil.copy2(write_target, bak)
    print(f"  backed up to {bak}")
    # Atomic write: a crash mid-write must not truncate settings.json (which
    # would brick every later `--apply`). A pid-unique tmp name avoids two
    # concurrent `--apply` runs racing on a shared tmp path.
    tmp = write_target.with_suffix(write_target.suffix + f".tmp-{os.getpid()}")
    tmp.write_text(json.dumps(settings, indent=2) + "\n")
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
            print(f"  {raw_path} could not be cleaned: {exc}", file=sys.stderr)
            failed = True
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
