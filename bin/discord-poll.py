#!/usr/bin/env python3
"""discord-poll — fetch new Discord messages and print them as JSON.

Advances ~/.assistant/comms/discord.cursor (a Discord snowflake message_id) so
each call only sees messages newer than the last run. Filters to the channel_id
set in config. Designed to be called in a loop from comms-listen.py (same
pattern as tg-poll.py).

Uses Discord REST GET /channels/{id}/messages?after=<last_id>&limit=N.
No external dependencies — stdlib urllib only.

Usage:
  discord-poll.py [--limit N] [--reset-cursor]

Stdout: JSON array of message dicts:
  [
    {
      "channel_id": N,
      "msg_id": N,
      "author": "username",
      "text": "...",
      "reply_to": N | null,
      "ts": "ISO"
    },
    ...
  ]

Each call advances the cursor past the highest message_id received, so
you'll never see the same message twice. If the call fails, the cursor
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

API_BASE = "https://discord.com/api/v10"

# Path component for the discord message cursor (snowflake ID of last seen msg).
# Stored alongside the tg cursor as discord.cursor.
_CURSOR_NAME = "discord.cursor"


# --------------------------------------------------------------------------- cursor helpers

def _cursor_path(paths: comms_lib.Paths) -> Path:
    return paths.comms_dir / _CURSOR_NAME


def read_discord_cursor(paths: comms_lib.Paths) -> int:
    p = _cursor_path(paths)
    if not p.exists():
        return 0
    try:
        return int(p.read_text().strip() or "0")
    except ValueError:
        return 0


def write_discord_cursor(paths: comms_lib.Paths, snowflake: int) -> None:
    _cursor_path(paths).write_text(str(snowflake))


# --------------------------------------------------------------------------- REST helpers

def get_messages(token: str, channel_id: int, after: int, limit: int,
                 http=None) -> list[dict]:
    return (http or _real_get_messages)(token, channel_id, after, limit)


def _real_get_messages(token: str, channel_id: int, after: int,
                       limit: int) -> list[dict]:
    params: dict = {"limit": limit}
    if after:
        params["after"] = after
    qs = urllib.parse.urlencode(params)
    url = f"{API_BASE}/channels/{channel_id}/messages?{qs}"
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bot {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(
            f"discord HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:500]}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"discord URL error: {e.reason}")


# --------------------------------------------------------------------------- projection

def project_message(msg: dict, channel_id: int, clock=None) -> dict | None:
    """Distil a raw Discord message object into our schema.
    Returns None for bot messages and system messages we don't handle."""
    # Skip bot-authored messages (including our own sends).
    author = msg.get("author") or {}
    if author.get("bot"):
        return None
    msg_id_str = msg.get("id")
    if not msg_id_str:
        return None

    # Discord message types: 0=DEFAULT, 19=REPLY. Others (pins, calls, etc.) skipped.
    msg_type = int(msg.get("type", 0))
    if msg_type not in (0, 19):
        return None

    username = author.get("username") or author.get("global_name") or str(author.get("id", ""))
    text = msg.get("content", "")

    reply_to: int | None = None
    ref = msg.get("message_reference")
    if ref and ref.get("message_id"):
        try:
            reply_to = int(ref["message_id"])
        except (ValueError, TypeError):
            pass

    return {
        "channel_id": channel_id,
        "msg_id": int(msg_id_str),
        "author": username,
        "text": text,
        "reply_to": reply_to,
        "ts": comms_lib.now_iso(clock),
    }


# --------------------------------------------------------------------------- main

def main(argv: list[str] | None = None, http=None, clock=None,
         paths: comms_lib.Paths | None = None) -> int:
    ap = argparse.ArgumentParser(description="fetch new discord messages")
    ap.add_argument("--limit", type=int, default=20,
                    help="max messages to fetch this call (max 100 per Discord API)")
    ap.add_argument("--reset-cursor", action="store_true", dest="reset_cursor",
                    help="advance cursor to latest and exit without printing messages")
    args = ap.parse_args(argv)

    paths = paths or comms_lib.Paths.from_env()
    cfg = DiscordPollConfig.load(paths.config)

    if args.reset_cursor:
        # Fetch at most 1 message to find the latest ID, advance cursor, exit.
        try:
            msgs = get_messages(cfg.bot_token, cfg.channel_id, after=0,
                                limit=1, http=http)
        except RuntimeError as e:
            print(json.dumps({"error": str(e)}), file=sys.stderr)
            return 1
        if msgs:
            # Discord returns newest-first when not using `after`; take first.
            latest_id = max(int(m["id"]) for m in msgs if m.get("id"))
            write_discord_cursor(paths, latest_id)
        print(json.dumps([]))
        return 0

    cursor = read_discord_cursor(paths)
    try:
        msgs = get_messages(cfg.bot_token, cfg.channel_id, after=cursor,
                            limit=args.limit, http=http)
    except RuntimeError as e:
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        return 1

    # Discord returns messages in ascending order when `after` is set.
    out: list[dict] = []
    max_seen = cursor
    for msg in msgs:
        raw_id = msg.get("id")
        if not raw_id:
            continue
        msg_id = int(raw_id)
        max_seen = max(max_seen, msg_id)
        rec = project_message(msg, cfg.channel_id, clock=clock)
        if rec is None:
            continue
        out.append(rec)

    if msgs:
        # Always advance past the highest ID seen, even if we filtered everything,
        # so we don't re-fetch the same messages next call.
        write_discord_cursor(paths, max_seen)

    print(json.dumps(out))
    return 0


# --------------------------------------------------------------------------- config

class DiscordPollConfig:
    """Discord-specific slice of config.json for the poll script."""

    def __init__(self, bot_token: str, channel_id: int):
        self.bot_token = bot_token
        self.channel_id = channel_id

    @classmethod
    def load(cls, path: Path) -> "DiscordPollConfig":
        if not path.exists():
            raise SystemExit(
                f"missing config at {path}; run assistant-comms-setup.sh first")
        raw = json.loads(path.read_text())
        dc = raw.get("discord", {})
        if not dc.get("bot_token"):
            raise SystemExit(
                "discord.bot_token not set in config.json; "
                "add it under the 'discord' key")
        if not dc.get("channel_id"):
            raise SystemExit(
                "discord.channel_id not set in config.json; "
                "set the DM channel ID under the 'discord' key")
        return cls(
            bot_token=dc["bot_token"],
            channel_id=int(dc["channel_id"]),
        )


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
