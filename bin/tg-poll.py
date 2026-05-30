#!/usr/bin/env python3
"""tg-poll — long-poll Telegram for inbound updates and print them as JSON.

Advances ~/.assistant/comms/tg.cursor so the next call only sees newer
updates. Filters out chat_ids not in config (silent drop). Designed to
be called from the comms Claude session each pulse.

Usage:
  tg-poll.py [--timeout SECONDS] [--limit N]
             [--reset-cursor]   # discard any pending updates and exit

Stdout: JSON array of message dicts:
  [
    {
      "update_id": N,
      "chat_id": N,
      "msg_id": N,
      "from_user": "@handle",
      "text": "...",
      "reply_to_msg_id": N | null,
      "ts": "ISO"
    },
    ...
  ]

Each call advances the cursor past the highest update_id received, so
you'll never see the same update twice. If the call fails, the cursor
is unchanged (next call retries).
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import comms_lib  # noqa: E402


def get_updates(token: str, offset: int, timeout: int, limit: int, http=None) -> list[dict]:
    return (http or _real_get_updates)(token, offset, timeout, limit)


def _real_get_updates(token: str, offset: int, timeout: int, limit: int) -> list[dict]:
    qs = urllib.parse.urlencode({
        "offset": offset,
        "timeout": timeout,
        "limit": limit,
        # Only message updates; cuts out edits/inline/etc. we don't handle.
        "allowed_updates": json.dumps(["message"]),
    })
    url = f"https://api.telegram.org/bot{token}/getUpdates?{qs}"
    try:
        with urllib.request.urlopen(url, timeout=timeout + 5) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"telegram HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:500]}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"telegram URL error: {e.reason}")
    if not data.get("ok"):
        raise RuntimeError(f"telegram error: {data}")
    return data.get("result", [])


def project_update(u: dict, clock=None) -> dict | None:
    """Distil a raw getUpdates record into the schema we want to print.
    Returns None if the update has no message we can route."""
    msg = u.get("message")
    if not msg or "chat" not in msg or "message_id" not in msg:
        return None
    chat = msg["chat"]
    sender = msg.get("from", {})
    handle = sender.get("username") or sender.get("first_name") or str(sender.get("id", ""))
    out = {
        "update_id": int(u["update_id"]),
        "chat_id": int(chat["id"]),
        "msg_id": int(msg["message_id"]),
        "from_user": handle,
        "text": msg.get("text", ""),
        "reply_to_msg_id": int(msg["reply_to_message"]["message_id"]) if msg.get("reply_to_message") else None,
        "ts": comms_lib.now_iso(clock),
    }
    return out


def main(argv: list[str] | None = None, http=None, clock=None,
         paths: comms_lib.Paths | None = None) -> int:
    ap = argparse.ArgumentParser(description="long-poll telegram for inbound updates")
    ap.add_argument("--timeout", type=int, default=5,
                    help="long-poll timeout in seconds (default 5)")
    ap.add_argument("--limit", type=int, default=20,
                    help="max updates to fetch this call")
    ap.add_argument("--reset-cursor", action="store_true", dest="reset_cursor",
                    help="drop any pending updates and exit")
    args = ap.parse_args(argv)

    paths = paths or comms_lib.Paths.from_env()
    cfg = comms_lib.Config.load(paths.config)

    if args.reset_cursor:
        # confirmTimeout=0 with a high offset clears outstanding updates.
        try:
            updates = get_updates(cfg.bot_token, offset=-1, timeout=0, limit=1, http=http)
        except RuntimeError as e:
            print(json.dumps({"error": str(e)}), file=sys.stderr)
            return 1
        if updates:
            comms_lib.write_tg_cursor(paths, int(updates[-1]["update_id"]) + 1)
        print(json.dumps([]))
        return 0

    cursor = comms_lib.read_tg_cursor(paths)
    try:
        updates = get_updates(cfg.bot_token, offset=cursor, timeout=args.timeout,
                              limit=args.limit, http=http)
    except RuntimeError as e:
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        return 1

    out: list[dict] = []
    max_seen = cursor - 1
    for u in updates:
        max_seen = max(max_seen, int(u["update_id"]))
        rec = project_update(u, clock=clock)
        if rec is None:
            continue
        if rec["chat_id"] not in cfg.chat_ids:
            continue
        out.append(rec)
    if updates:
        # Always advance past the highest update_id we've seen, even if we
        # filtered the projected output empty — otherwise we'd re-fetch the
        # same noise next call.
        comms_lib.write_tg_cursor(paths, max_seen + 1)
    print(json.dumps(out))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
