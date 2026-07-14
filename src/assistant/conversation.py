"""conversation — durable chat memory (conversation.jsonl).

Extracted from bin/comms_lib.py (append_conversation_turn / read_conversation_window)
and bin/conversation.py. One JSONL row per turn, both directions. The comms
subsystem mirrors each outbound broadcast here so it is part of the thread for
later replies; the warm session reconstructs context from this file.

Slack schema: the conversation key is the Slack `channel` id and the message
identity is the Slack `msg_ts`. Path unchanged (~/.assistant/comms/conversation.jsonl).
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable

Clock = Callable[[], float]


def _now_iso(clock: Clock | None = None) -> str:
    from datetime import datetime, timezone
    if clock is None:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return datetime.fromtimestamp(clock(), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def append_turn(conversation_path: Path, channel: str, msg_ts: str | None,
                direction: str, text: str, *, reply_to: str | None = None,
                kind: str | None = None, clock: Clock | None = None) -> None:
    """Append one turn (inbound or outbound).

    direction: "in" (from the user) or "out" (from the daemon/assistant).
    Raises ValueError on a bad direction — the contract is strict.
    """
    if direction not in ("in", "out"):
        raise ValueError(f"direction must be 'in' or 'out', got {direction!r}")
    p = Path(conversation_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    epoch = int(clock()) if clock else int(time.time())
    rec = {
        "ts": _now_iso(clock),
        "epoch": epoch,
        "channel": channel,
        "msg_ts": str(msg_ts) if msg_ts is not None else None,
        "reply_to": str(reply_to) if reply_to is not None else None,
        "direction": direction,
        "text": text,
        "kind": kind,
    }
    with open(p, "a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def read_window(conversation_path: Path, channel: str, *, max_turns: int = 20,
                max_age_sec: int = 7200,
                now: Clock | None = None) -> list[dict[str, Any]]:
    """Recent conversation for one channel, oldest-first, bounded by BOTH
    max_turns AND max_age_sec (whichever is tighter wins). Malformed/blank
    lines are skipped."""
    p = Path(conversation_path)
    if not p.exists():
        return []
    now_epoch = int(now()) if now else int(time.time())
    floor = now_epoch - max_age_sec
    rows: list[dict[str, Any]] = []
    with open(p) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if str(rec.get("channel")) != str(channel):
                continue
            if int(rec.get("epoch") or 0) < floor:
                continue
            rows.append(rec)
    return rows[-max_turns:]
