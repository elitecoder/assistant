#!/usr/bin/env python3
"""Idempotent patcher for ~/.claude/settings.json.

Ensures:
  - SessionStart contains: cmux-auto-resume.py and cmux-session-ledger.py start
  - SessionEnd contains:   cmux-session-ledger.py end
  - Stop does NOT contain: cmux-session-ledger.py end (legacy bug)

Backs up the original to settings.json.bak-<ts> before writing.
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


def main():
    path = Path(sys.argv[1]).expanduser()
    settings = json.loads(path.read_text()) if path.exists() else {}
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
        return

    # Back up only if the file already exists — on a fresh machine that has
    # never run Claude Code, ~/.claude/settings.json is absent and there is
    # nothing to back up (copying it would FileNotFoundError). Ensure the parent
    # dir exists before the fresh write.
    if path.exists():
        bak = path.with_suffix(path.suffix + f".bak-{int(time.time())}")
        shutil.copy2(path, bak)
        print(f"  backed up to {bak}")
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        print(f"  no existing {path.name} — creating fresh")
    path.write_text(json.dumps(settings, indent=2) + "\n")
    print(f"  wrote {path}")


if __name__ == "__main__":
    main()
