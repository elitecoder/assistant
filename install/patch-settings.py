#!/usr/bin/env python3
"""Idempotent hook patcher for Claude settings and Factory hooks.

Ensures:
  - SessionStart contains: cmux-auto-resume.py and cmux-session-ledger.py start
  - SessionEnd contains:   cmux-session-ledger.py end
  - Stop does NOT contain: cmux-session-ledger.py end (legacy bug)

Accepts one or more paths, including ``~/.claude/settings.json`` and
``~/.factory/hooks.json``. Existing files are backed up before writing; newly
created files are not.
"""
import json
import shutil
import sys
import time
from pathlib import Path

AUTO_RESUME_CMD = "python3 $HOME/.claude/hooks/cmux-auto-resume.py"
LEDGER_START_CMD = "python3 $HOME/.claude/hooks/cmux-session-ledger.py start"
LEDGER_END_CMD = "python3 $HOME/.claude/hooks/cmux-session-ledger.py end"


def has_command(blocks, cmd):
    for block in blocks or []:
        for h in block.get("hooks", []):
            if h.get("command") == cmd:
                return True
    return False


def add_hook(blocks, cmd, timeout):
    blocks.append({
        "matcher": "",
        "hooks": [{"type": "command", "command": cmd, "timeout": timeout}],
    })


def remove_command(blocks, cmd):
    out = []
    for block in blocks or []:
        kept = [h for h in block.get("hooks", []) if h.get("command") != cmd]
        if kept:
            block["hooks"] = kept
            out.append(block)
    return out


def patch_path(path):
    path = Path(path).expanduser()
    existed = path.exists()
    settings = {}
    if existed:
        try:
            loaded = json.loads(path.read_text())
            settings = loaded if isinstance(loaded, dict) else {}
        except json.JSONDecodeError:
            # A corrupt settings file must be recovered (backed up + rebuilt),
            # not raise — that would abort the installer under `set -e`.
            corrupt = path.with_suffix(path.suffix + f".corrupt-{int(time.time())}")
            shutil.copy2(path, corrupt)
            print(f"  {path} was malformed JSON — backed up to {corrupt}, rebuilding")
            settings = {}
    hooks = settings.setdefault("hooks", {})

    ss = hooks.setdefault("SessionStart", [])
    se = hooks.setdefault("SessionEnd", [])
    stop = hooks.setdefault("Stop", [])

    changed = False

    if not has_command(ss, AUTO_RESUME_CMD):
        add_hook(ss, AUTO_RESUME_CMD, 10)
        print(f"  + SessionStart: {AUTO_RESUME_CMD}")
        changed = True
    else:
        print(f"  = SessionStart already has {AUTO_RESUME_CMD}")

    if not has_command(ss, LEDGER_START_CMD):
        add_hook(ss, LEDGER_START_CMD, 5)
        print(f"  + SessionStart: {LEDGER_START_CMD}")
        changed = True
    else:
        print(f"  = SessionStart already has {LEDGER_START_CMD}")

    if not has_command(se, LEDGER_END_CMD):
        add_hook(se, LEDGER_END_CMD, 5)
        print(f"  + SessionEnd: {LEDGER_END_CMD}")
        changed = True
    else:
        print(f"  = SessionEnd already has {LEDGER_END_CMD}")

    if has_command(stop, LEDGER_END_CMD):
        hooks["Stop"] = remove_command(stop, LEDGER_END_CMD)
        print(f"  - Stop: removed legacy {LEDGER_END_CMD}")
        changed = True

    if not changed:
        print("  no changes needed")
        return False

    if existed:
        bak = path.with_suffix(path.suffix + f".bak-{int(time.time())}")
        shutil.copy2(path, bak)
        print(f"  backed up to {bak}")
    path.parent.mkdir(parents=True, exist_ok=True)
    # Atomic write: a crash mid-write must not truncate settings.json (which
    # would brick every later `--apply` on the same malformed-JSON path).
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(settings, indent=2) + "\n")
    tmp.replace(path)
    print(f"  wrote {path}")
    return True


def main():
    if len(sys.argv) < 2:
        print(
            "usage: patch-settings.py PATH [PATH ...]",
            file=sys.stderr,
        )
        raise SystemExit(2)
    for index, raw_path in enumerate(sys.argv[1:]):
        if index:
            print()
        patch_path(raw_path)


if __name__ == "__main__":
    main()
