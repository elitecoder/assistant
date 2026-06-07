#!/usr/bin/env python3
"""memory-sync-pull — run sync-pull.sh and ping Telegram if new lessons/memories arrived.

Called by com.assistant.memory-sync-pull LaunchAgent hourly.
Reads pull_interval_seconds from ~/.assistant/memory-repo-config.json to decide
whether to skip (if last run was too recent).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

HOME = Path.home()
REPO = Path(__file__).resolve().parent.parent
MEM_CONFIG = HOME / ".assistant" / "memory-repo-config.json"
MEMORY_REPO = HOME / "dev" / "mukul-memory"
SYNC_PULL = MEMORY_REPO / "scripts" / "sync-pull.sh"
TG_SEND = REPO / "bin" / "tg-send.py"
LAST_RUN_PATH = HOME / ".assistant" / "memory-sync-pull-last.json"


def load_config() -> dict:
    if MEM_CONFIG.exists():
        try:
            return json.loads(MEM_CONFIG.read_text())
        except Exception:
            pass
    return {}


def get_chat_id() -> int | None:
    comms_cfg = HOME / ".assistant" / "comms" / "config.json"
    if comms_cfg.exists():
        try:
            cfg = json.loads(comms_cfg.read_text())
            chat_ids = cfg.get("telegram", {}).get("chat_ids") or []
            if chat_ids:
                return chat_ids[0]
            return cfg.get("chat_id") or None
        except Exception:
            pass
    return None


def count_lessons(claude_md: Path) -> int:
    if not claude_md.exists():
        return 0
    return claude_md.read_text().count("<!-- lesson:")


def count_memories(memories_jsonl: Path) -> int:
    if not memories_jsonl.exists():
        return 0
    return sum(1 for l in memories_jsonl.read_text().splitlines() if l.strip())


def send_tg(text: str, chat_id: int) -> None:
    subprocess.run(
        [sys.executable, str(TG_SEND), "--text", text, "--chat", str(chat_id)],
        capture_output=True,
    )


def main() -> int:
    cfg = load_config()
    interval = cfg.get("sync", {}).get("pull_interval_seconds", 3600)

    # Throttle: skip if last run was within the interval.
    now = time.time()
    if LAST_RUN_PATH.exists():
        try:
            last = json.loads(LAST_RUN_PATH.read_text()).get("ts", 0)
            if now - last < interval:
                return 0
        except Exception:
            pass

    if not SYNC_PULL.exists():
        print(f"sync-pull not found at {SYNC_PULL}", file=sys.stderr)
        return 1

    claude_md = Path(cfg.get("stores", {}).get("claude_md", "") or HOME / ".claude/CLAUDE.md")
    claude_md = Path(os.path.expanduser(str(claude_md)))
    memories_jsonl = HOME / ".assistant" / "mem0" / "memories.jsonl"

    lessons_before = count_lessons(claude_md)
    memories_before = count_memories(memories_jsonl)

    result = subprocess.run(
        ["bash", str(SYNC_PULL)],
        capture_output=True, text=True,
        env={**os.environ, "MEMORY_SYNC_IN_PROGRESS": "1"},
    )

    # Record last run regardless of outcome.
    LAST_RUN_PATH.write_text(json.dumps({"ts": now, "rc": result.returncode}))

    if result.returncode != 0:
        print(result.stderr, file=sys.stderr)
        return result.returncode

    lessons_after = count_lessons(claude_md)
    memories_after = count_memories(memories_jsonl)

    new_lessons = lessons_after - lessons_before
    new_memories = memories_after - memories_before

    if new_lessons <= 0 and new_memories <= 0:
        return 0

    # Something new landed — ping Telegram.
    chat_id = get_chat_id()
    if not chat_id:
        return 0

    parts = []
    if new_lessons > 0:
        parts.append(f"{new_lessons} new lesson{'s' if new_lessons > 1 else ''} absorbed into CLAUDE.md")
    if new_memories > 0:
        parts.append(f"{new_memories} new memor{'ies' if new_memories > 1 else 'y'} added to the store")

    msg = "Memory sync pulled from another machine: " + ", ".join(parts) + "."
    send_tg(msg, chat_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
