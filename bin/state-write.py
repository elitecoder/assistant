#!/usr/bin/env python3
"""state-write.py — atomically write ~/.claude/cache/assistant-state.json.

Mechanical only. Reads JSON state from stdin and writes via tmpfile + rename.

Usage:
  cat state.json | state-write.py
"""
import json
import os
import sys


def main():
    state = json.load(sys.stdin)
    path = os.path.expanduser("~/.claude/cache/assistant-state.json")
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, path)


if __name__ == "__main__":
    main()
