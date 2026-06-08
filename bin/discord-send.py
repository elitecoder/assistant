#!/usr/bin/env python3
"""discord-send — send one Discord message and print {channel_id, message_id, ts, kind}.

Usage:
  discord-send.py --text TEXT --channel CHANNEL_ID
                  [--reply-to MSG_ID] [--kind reply|action|urgent|info]
                  [--dry-run]

Reads discord.bot_token from ~/.assistant/comms/config.json.

Stdout JSON (one line per send):
  {"channel_id": N, "message_id": N, "ts": "ISO", "kind": "...", "muted": false}

Exit 0 on success; 1 on usage/config errors; 2 if the send failed.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import comms_lib  # noqa: E402

API_BASE = "https://discord.com/api/v10"


def send_message(token: str, channel_id: int, text: str,
                 reply_to: int | None, http=None) -> dict:
    """POST a message to a Discord channel. Returns the parsed message object.
    Raises RuntimeError on API failure. `http` is injected in tests."""
    payload: dict = {"content": text}
    if reply_to is not None:
        payload["message_reference"] = {"message_id": str(reply_to)}
    return (http or _real_post)(token, channel_id, payload)


def _real_post(token: str, channel_id: int, payload: dict) -> dict:
    url = f"{API_BASE}/channels/{channel_id}/messages"
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bot {token}",
            "User-Agent": "DiscordBot (https://github.com/assistant, 1.0)",
        })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(
            f"discord HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:500]}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"discord URL error: {e.reason}")


def main(argv: list[str] | None = None, http=None,
         clock=None, paths: comms_lib.Paths | None = None) -> int:
    ap = argparse.ArgumentParser(description="send a discord message")
    ap.add_argument("--text", required=True)
    ap.add_argument("--channel", type=int, required=True,
                    help="Discord channel (DM or guild channel) ID")
    ap.add_argument("--reply-to", type=int, default=None, dest="reply_to",
                    help="message_id this message replies to")
    ap.add_argument("--kind", default="reply",
                    choices=["action", "urgent", "reply", "info"])
    ap.add_argument("--dry-run", action="store_true", dest="dry_run")
    args = ap.parse_args(argv)

    paths = paths or comms_lib.Paths.from_env()
    cfg = DiscordConfig.load(paths.config)

    muted = cfg.mute_until_epoch > (clock() if clock else int(time.time()))
    if muted and args.kind not in {"urgent", "reply"}:
        print(json.dumps({
            "channel_id": args.channel,
            "ts": comms_lib.now_iso(clock),
            "kind": args.kind,
            "muted": True,
        }))
        return 0

    if args.dry_run:
        print(json.dumps({
            "channel_id": args.channel,
            "message_id": None,
            "ts": comms_lib.now_iso(clock),
            "kind": args.kind,
            "dry_run": True,
        }))
        return 0

    try:
        result = send_message(cfg.bot_token, args.channel, args.text,
                              args.reply_to, http=http)
    except RuntimeError as e:
        print(json.dumps({
            "channel_id": args.channel,
            "error": str(e),
            "ts": comms_lib.now_iso(clock),
            "kind": args.kind,
        }))
        return 2

    msg_id = int(result["id"])
    print(json.dumps({
        "channel_id": args.channel,
        "message_id": msg_id,
        "ts": comms_lib.now_iso(clock),
        "kind": args.kind,
        "muted": False,
    }))
    return 0


# --------------------------------------------------------------------------- config

class DiscordConfig:
    """Discord-specific slice of config.json."""

    def __init__(self, bot_token: str, channel_id: int | None,
                 mute_until_epoch: int = 0):
        self.bot_token = bot_token
        self.channel_id = channel_id
        self.mute_until_epoch = mute_until_epoch

    @classmethod
    def load(cls, path: Path) -> "DiscordConfig":
        if not path.exists():
            raise SystemExit(
                f"missing config at {path}; run assistant-comms-setup.sh first")
        raw = json.loads(path.read_text())
        dc = raw.get("discord", {})
        if not dc.get("bot_token"):
            raise SystemExit(
                "discord.bot_token not set in config.json; "
                "add it under the 'discord' key")
        return cls(
            bot_token=dc["bot_token"],
            channel_id=int(dc["channel_id"]) if dc.get("channel_id") else None,
            mute_until_epoch=int(raw.get("mute_until_epoch", 0)),
        )


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
