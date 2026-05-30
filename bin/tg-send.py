#!/usr/bin/env python3
"""tg-send — send one Telegram message and print {message_id, chat_id, ts}.

Usage:
  tg-send.py --text "<body>" [--chat <chat_id>] [--reply-to <msg_id>]
             [--ledger-key <key>] [--kind action|urgent|reply]
             [--parse-mode HTML|MarkdownV2|None] [--silent]
             [--dry-run]

Behavior:
  - If --chat is omitted, sends to every chat_id in config (broadcast).
  - If --ledger-key is given, appends a row to threads.jsonl per chat so
    inbound replies can be traced back to that ledger entry.
  - Honours config.mute_until_epoch unless --kind=urgent or --kind=reply.
  - --dry-run prints what would be sent without hitting the API.

Stdout JSON shape (one line per recipient):
  {"chat_id": N, "message_id": M, "ts": "ISO", "kind": "...", "muted": false}

Exit 0 on full success; 2 if all sends failed; 1 on usage/config errors.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import comms_lib  # noqa: E402

API_BASE = "https://api.telegram.org/bot{token}/{method}"


def send_one(token: str, chat_id: int, text: str, reply_to: int | None,
             parse_mode: str | None, silent: bool, http=None) -> dict:
    """POST sendMessage, return the parsed `result` dict on success.
    Raises RuntimeError on Telegram API failure. Indirection via `http`
    is for tests."""
    payload: dict = {"chat_id": chat_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if reply_to is not None:
        payload["reply_to_message_id"] = reply_to
    if silent:
        payload["disable_notification"] = True
    return (http or _real_post)(token, "sendMessage", payload)


def _real_post(token: str, method: str, payload: dict) -> dict:
    url = API_BASE.format(token=token, method=method)
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"telegram HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:500]}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"telegram URL error: {e.reason}")
    if not data.get("ok"):
        raise RuntimeError(f"telegram error: {data}")
    return data["result"]


def main(argv: list[str] | None = None, http=None,
         clock=None, paths: comms_lib.Paths | None = None) -> int:
    ap = argparse.ArgumentParser(description="send a telegram message")
    ap.add_argument("--text", required=True)
    ap.add_argument("--chat", type=int, default=None,
                    help="recipient chat_id (default: every chat in config)")
    ap.add_argument("--reply-to", type=int, default=None, dest="reply_to")
    ap.add_argument("--ledger-key", default=None, dest="ledger_key",
                    help="ledger entry this message reports on; recorded in threads.jsonl")
    ap.add_argument("--kind", default="reply",
                    choices=["action", "urgent", "reply", "info"])
    ap.add_argument("--parse-mode", default="HTML", dest="parse_mode",
                    choices=["HTML", "MarkdownV2", "None"])
    ap.add_argument("--silent", action="store_true",
                    help="suppress notification on the recipient device")
    ap.add_argument("--dry-run", action="store_true", dest="dry_run")
    args = ap.parse_args(argv)

    paths = paths or comms_lib.Paths.from_env()
    cfg = comms_lib.Config.load(paths.config)

    parse_mode = None if args.parse_mode == "None" else args.parse_mode
    targets = [args.chat] if args.chat is not None else sorted(cfg.chat_ids)
    if not targets:
        print("no chat_ids configured", file=sys.stderr)
        return 1

    muted = cfg.mute_until_epoch > (clock() if clock else int(time.time()))
    if muted and args.kind not in {"urgent", "reply"}:
        for chat_id in targets:
            print(json.dumps({
                "chat_id": chat_id,
                "ts": comms_lib.now_iso(clock),
                "kind": args.kind,
                "muted": True,
            }))
        return 0

    sent_any = False
    for chat_id in targets:
        if args.dry_run:
            print(json.dumps({
                "chat_id": chat_id,
                "message_id": None,
                "ts": comms_lib.now_iso(clock),
                "kind": args.kind,
                "dry_run": True,
            }))
            sent_any = True
            continue
        try:
            result = send_one(cfg.bot_token, chat_id, args.text,
                              args.reply_to, parse_mode, args.silent, http=http)
        except RuntimeError as e:
            print(json.dumps({
                "chat_id": chat_id,
                "error": str(e),
                "ts": comms_lib.now_iso(clock),
                "kind": args.kind,
            }))
            continue
        msg_id = int(result["message_id"])
        if args.ledger_key:
            comms_lib.append_thread(
                paths, args.ledger_key, msg_id, chat_id, args.kind, clock=clock)
        print(json.dumps({
            "chat_id": chat_id,
            "message_id": msg_id,
            "ts": comms_lib.now_iso(clock),
            "kind": args.kind,
            "muted": False,
        }))
        sent_any = True
    return 0 if sent_any else 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
