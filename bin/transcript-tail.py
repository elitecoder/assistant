#!/usr/bin/env python3
"""transcript-tail.py — find a workspace's transcript path and read the tail.

Mechanical only. The Assistant prompt decides what to MAKE OF the tail
(verify a sent message landed, classify a workspace as DONE, detect NAB).
This script just locates and reads.

Usage:
  transcript-tail.py --ws workspace:N [--bytes 12000]
  transcript-tail.py --closed-cwd /path/to/cwd  # for closed workspaces

Output: JSON
  {
    "transcript_path": "/Users/.../*.jsonl",
    "last_user": {"ts": "...", "text": "..."},
    "last_assistant": {"ts": "...", "text": "..."}
  }
"""
import argparse
import json
import os
import sys

WORLD = os.path.expanduser("~/.claude/cache/world.json")
REGISTRY = os.path.expanduser("~/.claude/cmux-registry.json")


def find_via_world(ws_ref):
    try:
        w = json.load(open(WORLD))
    except Exception:
        return None
    for s in w.get("live_sessions", []):
        if s.get("ws_ref") == ws_ref:
            return s.get("transcript_path")
    return None


def find_via_registry(cwd):
    try:
        reg = json.load(open(REGISTRY))
    except Exception:
        return None
    matches = []
    for tab_id, e in reg.items():
        if e.get("cwd") == cwd and e.get("transcript_path"):
            matches.append((e.get("started_at", ""), e["transcript_path"]))
    if not matches:
        return None
    matches.sort(reverse=True)
    return matches[0][1]


def read_tail(path, n_bytes):
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        if size > n_bytes:
            f.seek(size - n_bytes)
            f.readline()
        data = f.read().decode("utf-8", errors="replace")
    last_user = None
    last_assistant = None
    for line in data.splitlines():
        if not line.strip():
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        msg = d.get("message")
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        content = msg.get("content")
        if isinstance(content, list):
            text_parts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
            text = "\n".join(text_parts)
        elif isinstance(content, str):
            text = content
        else:
            text = ""
        if role == "user":
            last_user = {"ts": d.get("timestamp"), "text": text}
        elif role == "assistant":
            last_assistant = {"ts": d.get("timestamp"), "text": text}
    return last_user, last_assistant


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--ws", default="")
    p.add_argument("--closed-cwd", default="")
    p.add_argument("--bytes", type=int, default=12000)
    args = p.parse_args()

    path = None
    if args.ws:
        path = find_via_world(args.ws)
    if not path and args.closed_cwd:
        path = find_via_registry(args.closed_cwd)

    if not path or not os.path.exists(path):
        print(json.dumps({"error": "transcript_not_found", "tried_ws": args.ws,
                          "tried_cwd": args.closed_cwd}))
        sys.exit(1)

    last_user, last_assistant = read_tail(path, args.bytes)
    print(json.dumps({
        "transcript_path": path,
        "last_user": last_user,
        "last_assistant": last_assistant,
    }, indent=2))


if __name__ == "__main__":
    main()
