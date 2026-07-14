#!/usr/bin/env python3
"""slack-send — send one Slack message and print {channel, ts, kind}.

Usage:
  slack-send.py --text TEXT --channel TARGET
                [--reply-to THREAD_TS] [--kind reply|action|urgent|info]
                [--dry-run]

TARGET is a Slack user id (U…, opened as a DM) or a channel id (C…/D…/G…).
The bot token comes from $SLACK_BOT_TOKEN (never config.json). Routing target
+ the send-gate allowlist come from ~/.assistant/config.json.

THE SEND-GATE — confines THIS CLI to the one comms channel. A message is sent
ONLY if its target is in config.slack.allowed_targets (the private channel the
bot was invited to, or the operator's DM). Any other target is refused with NO
API call and a nonzero exit. This is the gate for every mechanical daemon send
(comms-listen.py / CommsSubsystem route through here).

SCOPE OF THE GUARANTEE (be honest): the gate confines *callers of this CLI and
of slack.send()*. It does NOT sandbox the warm cmux session — that session runs
--dangerously-skip-permissions with $SLACK_BOT_TOKEN in its env and could, if
prompt-injected, call chat.postMessage directly and bypass this gate. The warm
session is TRUSTED, not confined; the gate is defense-in-depth for the daemon's
own automated sends, not a hard sandbox around the token.

Stdout JSON (one line):
  {"channel": "C…", "message_id": "1699…", "ts": "ISO", "kind": "...", "muted": false}
  message_id is the Slack message ts (its identity, used for threading/replies).

Exit 0 on success (or muted/dry-run); 1 on usage/config/gate errors; 2 if the
send failed at the API.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import comms_lib  # noqa: E402

API_BASE = "https://slack.com/api"


def _api_post(token: str, method: str, payload: dict, http=None) -> dict:
    """POST to a Slack Web API method. Returns the parsed response dict.
    Raises RuntimeError on transport failure or ok=false. `http` is injected in
    tests — it takes (token, method, payload) and returns the response dict."""
    if http is not None:
        return http(token, method, payload)
    url = f"{API_BASE}/{method}"
    body = urllib.parse.urlencode(payload).encode("utf-8")
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
    """A U… user id must be turned into a DM channel via conversations.open.
    Everything else (C…/D…/G…) is already a channel id — passed through."""
    if target.startswith("U"):
        data = _api_post(token, "conversations.open", {"users": target}, http=http)
        return data["channel"]["id"]
    return target


def send_message(token: str, channel: str, text: str,
                 reply_to: str | None, http=None) -> dict:
    """POST chat.postMessage. Returns the parsed response ({ok, channel, ts, …}).
    Raises RuntimeError on API failure."""
    payload: dict = {
        "channel": channel,
        "text": text,
        # mrkdwn is the default; being explicit guards against workspace overrides.
        "mrkdwn": "true",
        # never unfurl link previews for a status ping — keeps it compact.
        "unfurl_links": "false",
        "unfurl_media": "false",
    }
    if reply_to is not None:
        payload["thread_ts"] = str(reply_to)
    return _api_post(token, "chat.postMessage", payload, http=http)


def main(argv: list[str] | None = None, http=None,
         clock=None, paths: comms_lib.Paths | None = None,
         env: dict | None = None) -> int:
    ap = argparse.ArgumentParser(description="send a slack message (gated)")
    ap.add_argument("--text", required=True)
    ap.add_argument("--channel", required=True,
                    help="target: U… user (DMed) or C…/D…/G… channel; "
                         "default comes from config when '-'")
    ap.add_argument("--reply-to", default=None, dest="reply_to",
                    help="thread_ts this message replies into")
    ap.add_argument("--ledger-key", default=None, dest="ledger_key",
                    help="ledger entry this message reports on; recorded in threads.jsonl")
    ap.add_argument("--kind", default="reply",
                    choices=["action", "urgent", "reply", "info"])
    ap.add_argument("--dry-run", action="store_true", dest="dry_run")
    args = ap.parse_args(argv)

    paths = paths or comms_lib.Paths.from_env()
    env = env if env is not None else None
    cfg = comms_lib.Config.load(paths.config, env=env)

    # '-' means "use the configured default target".
    target = cfg.target if args.channel == "-" else args.channel
    if not target:
        print("no target given and none configured", file=sys.stderr)
        return 1

    # ── THE SEND-GATE ────────────────────────────────────────────────────
    # Refuse anything not explicitly allowlisted, BEFORE any network egress.
    if not cfg.is_allowed(target):
        print(json.dumps({
            "channel": target,
            "error": "send-gate: target not in slack.allowed_targets (bot is confined to its comms channel)",
            "ts": comms_lib.now_iso(clock),
            "kind": args.kind,
        }), file=sys.stderr)
        return 1

    token = comms_lib.bot_token(env if env is not None else None)
    if not token and not args.dry_run:
        print("SLACK_BOT_TOKEN not set", file=sys.stderr)
        return 1

    muted = cfg.mute_until_epoch > (clock() if clock else int(time.time()))
    if muted and args.kind not in {"urgent", "reply"}:
        print(json.dumps({
            "channel": target,
            "ts": comms_lib.now_iso(clock),
            "kind": args.kind,
            "muted": True,
        }))
        return 0

    if args.dry_run:
        print(json.dumps({
            "channel": target,
            "message_id": None,
            "ts": comms_lib.now_iso(clock),
            "kind": args.kind,
            "dry_run": True,
        }))
        return 0

    try:
        channel = resolve_channel(token, target, http=http)
        result = send_message(token, channel, args.text, args.reply_to, http=http)
    except RuntimeError as e:
        print(json.dumps({
            "channel": target,
            "error": str(e),
            "ts": comms_lib.now_iso(clock),
            "kind": args.kind,
        }))
        return 2

    msg_ts = str(result["ts"])
    channel_id = str(result.get("channel", channel))
    if args.ledger_key:
        comms_lib.append_thread(paths, args.ledger_key, msg_ts, channel_id,
                                args.kind, clock=clock)
    print(json.dumps({
        "channel": channel_id,
        "message_id": msg_ts,
        "ts": comms_lib.now_iso(clock),
        "kind": args.kind,
        "muted": False,
    }))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
