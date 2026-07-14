#!/usr/bin/env python3
"""slack-poll — fetch new inbound Slack messages and print them as JSON.

Reads the DM/channel identified by config.slack.target ($SLACK_PING_TARGET
override) via conversations.history, advancing ~/.assistant/comms/slack.cursor
(a Slack message ts) so each call only sees messages newer than the last run.
Skips the bot's own messages (so our sends never loop back as inbound). Same
role as the removed discord-poll.py / tg-poll.py — called in a loop from
comms-listen.py.

The bot token comes from $SLACK_BOT_TOKEN (never config.json).

Usage:
  slack-poll.py [--limit N] [--reset-cursor]

Stdout: JSON array of message dicts (oldest-first):
  [
    {
      "channel": "D…",
      "msg_ts": "1699999999.000200",
      "author": "U…",
      "text": "...",
      "reply_to": "1699…" | null,   # thread_ts, if this was a threaded reply
      "ts": "ISO"
    },
    ...
  ]

Each call advances the cursor past the highest ts seen. On failure the cursor is
unchanged (next call retries).
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

API_BASE = "https://slack.com/api"


# --------------------------------------------------------------------------- REST helpers

def _api_get(token: str, method: str, params: dict, http=None) -> dict:
    if http is not None:
        return http(token, method, params)
    qs = urllib.parse.urlencode(params)
    url = f"{API_BASE}/{method}?{qs}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"slack HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:500]}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"slack URL error: {e.reason}")
    if not data.get("ok"):
        raise RuntimeError(f"slack error: {data.get('error', data)}")
    return data


def _api_post(token: str, method: str, payload: dict, http=None) -> dict:
    if http is not None:
        return http(token, method, payload)
    body = urllib.parse.urlencode(payload).encode("utf-8")
    url = f"{API_BASE}/{method}"
    req = urllib.request.Request(
        url, data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
            "Authorization": f"Bearer {token}",
        })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"slack HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:500]}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"slack URL error: {e.reason}")
    if not data.get("ok"):
        raise RuntimeError(f"slack error: {data.get('error', data)}")
    return data


def resolve_channel(token: str, target: str, http=None) -> str:
    """A U… user id resolves to its DM channel via conversations.open; a channel
    id is passed through."""
    if target.startswith("U"):
        data = _api_post(token, "conversations.open", {"users": target}, http=http)
        return data["channel"]["id"]
    return target


def get_history(token: str, channel: str, oldest: str, limit: int, http=None) -> list[dict]:
    """conversations.history returns newest-first. `oldest` is exclusive when
    inclusive=false, so we only get messages strictly after our cursor."""
    params = {
        "channel": channel,
        "limit": limit,
        "inclusive": "false",
    }
    if oldest and oldest != "0":
        params["oldest"] = oldest
    data = _api_get(token, "conversations.history", params, http=http)
    return data.get("messages", [])


# --------------------------------------------------------------------------- projection

def project_message(msg: dict, channel: str, bot_user_id: str | None,
                    clock=None) -> dict | None:
    """Distil a raw Slack message into our schema. Returns None for the bot's own
    messages, bot_message subtypes, and non-user system messages (joins, etc.)."""
    subtype = msg.get("subtype")
    # Real user messages have no subtype; thread broadcasts carry
    # subtype=thread_broadcast which we still want. Everything else (channel_join,
    # bot_message, message_changed, …) is noise.
    if subtype not in (None, "thread_broadcast"):
        return None
    if msg.get("bot_id"):
        return None  # our own bot sends, and any other bot
    author = msg.get("user")
    if bot_user_id and author == bot_user_id:
        return None  # defensive: our own messages if they ever lack bot_id
    ts = msg.get("ts")
    if not ts:
        return None
    thread_ts = msg.get("thread_ts")
    reply_to = str(thread_ts) if thread_ts and str(thread_ts) != str(ts) else None
    return {
        "channel": channel,
        "msg_ts": str(ts),
        "author": author or "",
        "text": msg.get("text", ""),
        "reply_to": reply_to,
        "ts": comms_lib.now_iso(clock),
    }


# --------------------------------------------------------------------------- main

def main(argv: list[str] | None = None, http=None, clock=None,
         paths: comms_lib.Paths | None = None, env: dict | None = None) -> int:
    ap = argparse.ArgumentParser(description="fetch new inbound slack messages")
    ap.add_argument("--limit", type=int, default=50,
                    help="max messages to fetch this call (Slack allows up to 1000)")
    ap.add_argument("--reset-cursor", action="store_true", dest="reset_cursor",
                    help="advance cursor to latest and exit without printing")
    ap.add_argument("--bot-user-id", default=None, dest="bot_user_id",
                    help="the bot's own U… id, to filter self-messages defensively")
    args = ap.parse_args(argv)

    paths = paths or comms_lib.Paths.from_env()
    cfg = comms_lib.Config.load(paths.config, env=env)
    token = comms_lib.bot_token(env if env is not None else None)
    if not token:
        print(json.dumps({"error": "SLACK_BOT_TOKEN not set"}), file=sys.stderr)
        return 1
    if not cfg.target:
        print(json.dumps({"error": "no slack.target configured"}), file=sys.stderr)
        return 1

    try:
        channel = resolve_channel(token, cfg.target, http=http)
    except RuntimeError as e:
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        return 1

    if args.reset_cursor:
        try:
            msgs = get_history(token, channel, oldest="0", limit=1, http=http)
        except RuntimeError as e:
            print(json.dumps({"error": str(e)}), file=sys.stderr)
            return 1
        if msgs:
            latest = max(str(m["ts"]) for m in msgs if m.get("ts"))
            comms_lib.write_slack_cursor(paths, latest)
        print(json.dumps([]))
        return 0

    out: list[dict] = []

    # This is a 1:1 channel: the assistant replies at top level (no threading),
    # so every human message is a top-level message that conversations.history
    # returns. That's the whole inbound surface — no per-thread replies polling
    # is needed (and a shared-channel thread model would only add friction).
    cursor = comms_lib.read_slack_cursor(paths)
    try:
        msgs = get_history(token, channel, oldest=cursor, limit=args.limit, http=http)
    except RuntimeError as e:
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        return 1

    # Slack returns newest-first; reverse to oldest-first so messages arrive in order.
    msgs = list(reversed(msgs))
    max_seen = cursor
    for msg in msgs:
        ts = msg.get("ts")
        if not ts:
            continue
        # Defensive floor: don't re-deliver anything <= the cursor (Slack's
        # oldest+inclusive=false should exclude it, but don't depend on it).
        if comms_lib._ts_float(ts) <= comms_lib._ts_float(cursor):
            continue
        if comms_lib._ts_float(ts) > comms_lib._ts_float(max_seen):
            max_seen = str(ts)
        rec = project_message(msg, channel, args.bot_user_id, clock=clock)
        if rec is None:
            continue
        out.append(rec)
    if msgs:
        comms_lib.write_slack_cursor(paths, max_seen)

    print(json.dumps(out))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
