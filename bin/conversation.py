#!/usr/bin/env python3
"""conversation — durable chat memory for assistant-comms.

The comms Claude session reconstructs context from this file every pulse,
so its context window can be thrown away (crash, /clear, compact) without
losing the thread.

Usage:
  # record one inbound turn (from the user)
  conversation.py append --chat 42 --msg-id 4012 --direction in \
      --text "was that the right PR?" --reply-to 4008

  # record one outbound turn (from comms)
  conversation.py append --chat 42 --msg-id 4013 --direction out \
      --text "yes — PR #10604, merged" --kind reply

  # rebuild the recent thread for a chat (oldest-first JSON array)
  conversation.py window --chat 42 [--max-turns 20] [--max-age-sec 7200]

`window` is what the session calls at the start of each turn before
replying. It bounds by BOTH max-turns and max-age — whichever is tighter.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import comms_lib  # noqa: E402


def cmd_append(args, paths: comms_lib.Paths, clock=None) -> int:
    comms_lib.append_conversation_turn(
        paths,
        chat_id=args.chat,
        msg_id=args.msg_id,
        direction=args.direction,
        text=args.text,
        reply_to=args.reply_to,
        kind=args.kind,
        clock=clock,
    )
    print("appended")
    return 0


def cmd_window(args, paths: comms_lib.Paths, now=None) -> int:
    rows = comms_lib.read_conversation_window(
        paths,
        chat_id=args.chat,
        max_turns=args.max_turns,
        max_age_sec=args.max_age_sec,
        now=now,
    )
    print(json.dumps(rows, ensure_ascii=False))
    return 0


def main(argv: list[str] | None = None, paths: comms_lib.Paths | None = None,
         clock=None, now=None) -> int:
    ap = argparse.ArgumentParser(description="durable chat memory for assistant-comms")
    sub = ap.add_subparsers(dest="cmd", required=True)

    pa = sub.add_parser("append", help="record one conversation turn")
    pa.add_argument("--chat", type=int, required=True)
    pa.add_argument("--msg-id", type=int, default=None, dest="msg_id")
    pa.add_argument("--reply-to", type=int, default=None, dest="reply_to")
    pa.add_argument("--direction", required=True, choices=["in", "out"])
    pa.add_argument("--text", required=True)
    pa.add_argument("--kind", default=None)

    pw = sub.add_parser("window", help="rebuild recent thread (oldest-first JSON)")
    pw.add_argument("--chat", type=int, required=True)
    pw.add_argument("--max-turns", type=int, default=20, dest="max_turns")
    pw.add_argument("--max-age-sec", type=int, default=7200, dest="max_age_sec")

    args = ap.parse_args(argv)
    paths = paths or comms_lib.Paths.from_env()

    if args.cmd == "append":
        return cmd_append(args, paths, clock=clock)
    return cmd_window(args, paths, now=now)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
