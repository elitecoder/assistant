#!/usr/bin/env python3
"""pattern-feedback — adjust a cmux-watcher pattern's priority from feedback.

The watcher (bin/cmux-watcher.py) drops an inbox item whenever a pattern fires.
Whether that signal was worth surfacing is feedback the system can learn from:

  relevant — the signal mattered. Increment hit_count and boost priority one rung
             (low → medium → high). A pattern that keeps earning its signals rises.
  noise    — the signal was not worth it. Increment noise_count. Once
             noise_count > hit_count * 2, downgrade to "muted" so the watcher
             stops dropping items for it (PatternBank.match() skips muted) —
             without deleting the pattern, so it can be revived later.

Counts and priority are written back to pattern_bank.json atomically (tmp +
os.replace), preserving every other field. The watcher hot-reloads the bank by
mtime, so a feedback write takes effect on the next match with no restart.

Usage:
  pattern-feedback.py --pattern-id <id> --feedback relevant|noise
  pattern-feedback.py --pattern-id <id> --feedback noise --bank <path>
  pattern-feedback.py --list           # show every pattern + its counts

Exit 0 on success; 1 on usage/IO error; 3 if the pattern id is unknown.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

HOME = Path(os.environ.get("HOME", str(Path.home())))
DEFAULT_BANK = Path(os.environ.get("CMUX_PATTERN_BANK",
                                   str(HOME / ".assistant" / "pattern_bank.json")))

# Priority ladder for boosts. "muted" is terminal-on-the-way-down; a relevant
# vote on a muted pattern revives it to low.
_LADDER = ["muted", "low", "medium", "high"]
# noise_count must exceed hit_count by this factor before we mute.
NOISE_MUTE_FACTOR = 2


def load_bank(path: Path) -> dict:
    if not path.exists():
        raise SystemExit(f"pattern bank not found at {path}")
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise SystemExit(f"pattern bank is not valid JSON: {e}")
    if not isinstance(data, dict) or "patterns" not in data:
        raise SystemExit(f"pattern bank at {path} has no patterns array")
    return data


def save_bank(path: Path, data: dict) -> None:
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, path)


def boost(priority: str) -> str:
    """One rung up the ladder. high stays high; muted revives to low."""
    if priority not in _LADDER:
        return priority  # unknown custom priority — leave it
    i = _LADDER.index(priority)
    return _LADDER[min(i + 1, len(_LADDER) - 1)]


def apply_feedback(pattern: dict, feedback: str) -> dict:
    """Mutate one pattern dict per the feedback rule. Returns it for chaining.

    Pure given the dict — no IO — so it's directly unit-testable."""
    hit = int(pattern.get("hit_count", 0))
    noise = int(pattern.get("noise_count", 0))
    if feedback == "relevant":
        hit += 1
        pattern["hit_count"] = hit
        pattern["priority"] = boost(pattern.get("priority", "low"))
    elif feedback == "noise":
        noise += 1
        pattern["noise_count"] = noise
        if noise > hit * NOISE_MUTE_FACTOR:
            pattern["priority"] = "muted"
    else:
        raise ValueError(f"unknown feedback {feedback!r}")
    return pattern


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Adjust a watcher pattern's priority from feedback.")
    ap.add_argument("--pattern-id", dest="pattern_id",
                    help="id of the pattern in the bank to adjust")
    ap.add_argument("--feedback", choices=["relevant", "noise"],
                    help="relevant boosts priority; noise can mute it")
    ap.add_argument("--bank", default=str(DEFAULT_BANK),
                    help=f"path to pattern_bank.json (default {DEFAULT_BANK})")
    ap.add_argument("--list", action="store_true",
                    help="print every pattern with its counts and priority, then exit")
    args = ap.parse_args(argv)

    bank_path = Path(args.bank)
    data = load_bank(bank_path)
    patterns = data.get("patterns", [])

    if args.list:
        for p in patterns:
            print(json.dumps({
                "id": p.get("id"),
                "priority": p.get("priority"),
                "signal": p.get("signal"),
                "hit_count": p.get("hit_count", 0),
                "noise_count": p.get("noise_count", 0),
            }))
        return 0

    if not args.pattern_id or not args.feedback:
        ap.error("--pattern-id and --feedback are required unless --list")

    target = next((p for p in patterns if p.get("id") == args.pattern_id), None)
    if target is None:
        print(f"unknown pattern id {args.pattern_id!r}", file=sys.stderr)
        return 3

    before = target.get("priority")
    apply_feedback(target, args.feedback)
    save_bank(bank_path, data)
    print(json.dumps({
        "id": target.get("id"),
        "feedback": args.feedback,
        "priority_before": before,
        "priority_after": target.get("priority"),
        "hit_count": target.get("hit_count", 0),
        "noise_count": target.get("noise_count", 0),
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
