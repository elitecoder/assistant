#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["slack_sdk>=3.27"]
# ///
"""
Slack reaction → local /todo capture.

Watches a Slack workspace over Socket Mode. When a specific emoji is added to a
message, the message's WHOLE THREAD is captured and turned into a TODO item in
this machine's ~/.claude/assistant-todo.json — the same store the `/todo` skill
and the Assistant dashboard read from.

MACHINE ROUTING
  Each machine owns one (or more) emoji via $TODO_EMOJI. A reaction whose name
  is not in this machine's set is ignored, so the same emoji on another machine's
  watcher routes the capture there instead. No shared state:

    # this machine (Mukuls-MacBook-Pro)
    export TODO_EMOJI=inbox_tray

    # some other machine
    export TODO_EMOJI=bookmark

Usage:
  export SLACK_BOT_TOKEN=xoxb-...      # needs scopes: reactions:read, channels:history, chat:write
  export SLACK_APP_TOKEN=xapp-...      # Socket Mode app-level token (connections:write)
  export TODO_EMOJI=inbox_tray         # the emoji THIS machine claims (comma-separated for several)
  export SLACK_USER_ID=U03K30XQUS2     # optional: only YOUR reactions create todos
  uv run slack_reaction_todo.py

  # test without writing todos
  uv run slack_reaction_todo.py --dry-run

Environment variables:
  SLACK_BOT_TOKEN   Bot token (xoxb-...). Required.
  SLACK_APP_TOKEN   App-level token (xapp-...) for Socket Mode. Required.
  TODO_EMOJI        Emoji name(s) this machine captures (no colons). Default: inbox_tray.
                    Comma-separated to claim several (e.g. "inbox_tray,memo").
  SLACK_USER_ID     If set, only reactions added by this user create todos.
  TODO_PRIORITY     Priority for captured todos (P0..P4). Default: P2.
  TODO_AUTODISPATCH "1" (default) marks todos autoDispatch=true; "0" for manual-only.
"""

import argparse
import json
import os
import re
import sys
import socket
import threading
from datetime import datetime, timezone
from pathlib import Path

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from slack_sdk.socket_mode import SocketModeClient
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN")
APP_TOKEN = os.environ.get("SLACK_APP_TOKEN")
ONLY_REACTOR = os.environ.get("SLACK_USER_ID")  # optional gate
TODO_PRIORITY = os.environ.get("TODO_PRIORITY", "P2")
TODO_AUTODISPATCH = os.environ.get("TODO_AUTODISPATCH", "1") != "0"

# Emoji(s) this machine claims. Reactions outside this set route to other machines.
TODO_EMOJIS = {
    e.strip().strip(":")
    for e in os.environ.get("TODO_EMOJI", "inbox_tray").split(",")
    if e.strip()
}

MACHINE = socket.gethostname()
TODO_PATH = Path(os.path.expanduser("~/.claude/assistant-todo.json"))


# --------------------------------------------------------------------------- #
# TODO store — mirrors the /todo skill's schema + atomic write
# --------------------------------------------------------------------------- #


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _next_id(data: dict) -> str:
    used = set()
    for bucket in ("items", "completed", "removed"):
        for it in data.get(bucket, []):
            m = re.match(r"td-(\d+)", it.get("id", ""))
            if m:
                used.add(int(m.group(1)))
    n = max(used) + 1 if used else 1
    return f"td-{n:03d}"


def _write_atomic(data: dict) -> None:
    data["_lastUpdated"] = _now_iso()
    tmp = TODO_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(TODO_PATH)


def add_todo(title: str, detail: str, source: str, url: str, dry_run: bool = False) -> str | None:
    """Append a captured-thread TODO. De-dups on `source` (the thread identity).

    Returns the new td-id, the existing id if this thread was already captured,
    or None on dry-run.
    """
    data = json.loads(TODO_PATH.read_text())
    items = data.setdefault("items", [])

    # De-dup by source: re-reacting the same thread must not create a duplicate.
    for it in items:
        if it.get("source") == source and it.get("status") not in ("done", "deferred"):
            return it.get("id")

    if dry_run:
        print(f"[dry-run] would add: {title!r}\n  source={source}\n  url={url}", file=sys.stderr)
        return None

    tid = _next_id(data)
    item = {
        "id": tid,
        "priority": TODO_PRIORITY if TODO_PRIORITY in ("P0", "P1", "P2", "P3", "P4") else "P2",
        "title": title,
        "detail": detail,
        "url": url,
        "source": source,
        "createdAt": datetime.now(timezone.utc).date().isoformat(),
        "status": "open",
        "autoDispatch": TODO_AUTODISPATCH,
        "capturedBy": MACHINE,
    }
    items.append(item)
    _write_atomic(data)
    return tid


# --------------------------------------------------------------------------- #
# Slack helpers
# --------------------------------------------------------------------------- #

_user_cache: dict = {}


def user_name(web: WebClient, user_id: str) -> str:
    if not user_id:
        return "unknown"
    if user_id in _user_cache:
        return _user_cache[user_id]
    try:
        info = web.users_info(user=user_id)
        u = info["user"]
        name = u.get("profile", {}).get("display_name") or u.get("real_name") or u.get("name") or user_id
    except SlackApiError:
        name = user_id
    _user_cache[user_id] = name
    return name


def resolve_thread_root(web: WebClient, channel: str, ts: str) -> str:
    """Return the thread parent ts for a reacted message (the message itself if standalone)."""
    try:
        resp = web.conversations_history(channel=channel, latest=ts, oldest=ts, inclusive=True, limit=1)
        msgs = resp.get("messages", [])
        if msgs:
            return msgs[0].get("thread_ts") or ts
    except SlackApiError as e:
        print(f"WARNING: history lookup failed: {e.response['error']}", file=sys.stderr)
    return ts


def fetch_thread(web: WebClient, channel: str, thread_ts: str) -> list[dict]:
    try:
        resp = web.conversations_replies(channel=channel, ts=thread_ts, limit=200)
        return resp.get("messages", [])
    except SlackApiError as e:
        print(f"WARNING: replies fetch failed: {e.response['error']}", file=sys.stderr)
        return []


def permalink(web: WebClient, channel: str, ts: str) -> str:
    try:
        return web.chat_getPermalink(channel=channel, message_ts=ts)["permalink"]
    except SlackApiError:
        return ""


def build_todo_text(web: WebClient, channel: str, messages: list[dict], link: str) -> tuple[str, str]:
    """Render (title, detail) from a thread's messages."""
    def clean(t: str) -> str:
        return re.sub(r"\s+", " ", (t or "").strip())

    root_text = clean(messages[0].get("text", "")) if messages else ""
    title = (root_text[:78] + "…") if len(root_text) > 80 else (root_text or "(no text)")

    lines = []
    for m in messages:
        author = user_name(web, m.get("user", ""))
        body = clean(m.get("text", ""))
        if body:
            lines.append(f"*{author}:* {body}")
    transcript = "\n".join(lines)
    detail = f"Captured from Slack thread on {MACHINE}.\n\n{transcript}\n\n{link}".strip()
    return title, detail


# --------------------------------------------------------------------------- #
# Event handling
# --------------------------------------------------------------------------- #


def handle_reaction(web: WebClient, event: dict, dry_run: bool) -> None:
    # Match emoji (strip skin-tone variant like "thumbsup::skin-tone-3").
    reaction = event.get("reaction", "").split("::")[0]
    if reaction not in TODO_EMOJIS:
        return  # routed to a different machine / not ours

    if ONLY_REACTOR and event.get("user") != ONLY_REACTOR:
        return  # someone else reacted; ignore

    item = event.get("item", {})
    if item.get("type") != "message":
        return

    channel = item["channel"]
    ts = item["ts"]
    thread_ts = resolve_thread_root(web, channel, ts)
    messages = fetch_thread(web, channel, thread_ts)
    if not messages:
        print(f"WARNING: no messages for {channel}/{thread_ts}", file=sys.stderr)
        return

    link = permalink(web, channel, thread_ts)
    title, detail = build_todo_text(web, channel, messages, link)
    source = f"slack-react:{channel}:{thread_ts}"

    tid = add_todo(title, detail, source, link, dry_run=dry_run)
    if dry_run:
        return

    print(f"[{_now_iso()}] :{reaction}: → {tid} ({MACHINE}): {title}", file=sys.stderr)

    # Feedback so it's visible across devices: react + a one-line thread reply.
    try:
        web.reactions_add(channel=channel, timestamp=ts, name="white_check_mark")
    except SlackApiError:
        pass
    try:
        web.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=f":inbox_tray: Captured as `{tid}` on *{MACHINE}* — added to TODO list.",
        )
    except SlackApiError:
        pass


def make_listener(dry_run: bool):
    def listener(client: SocketModeClient, req: SocketModeRequest) -> None:
        if req.type != "events_api":
            return
        # Ack immediately — Slack retries un-acked envelopes.
        client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))
        event = req.payload.get("event", {})
        if event.get("type") == "reaction_added":
            try:
                handle_reaction(client.web_client, event, dry_run)
            except Exception as e:  # never let one bad event kill the socket
                print(f"ERROR handling reaction: {e}", file=sys.stderr)

    return listener


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #


def main() -> None:
    parser = argparse.ArgumentParser(description="Slack reaction → local /todo capture")
    parser.add_argument("--dry-run", action="store_true", help="Log captures but don't write todos or post back")
    parser.add_argument("--emoji", help="Override TODO_EMOJI (comma-separated emoji names)")
    args = parser.parse_args()

    if args.emoji:
        global TODO_EMOJIS
        TODO_EMOJIS = {e.strip().strip(":") for e in args.emoji.split(",") if e.strip()}

    if not BOT_TOKEN:
        sys.exit("ERROR: SLACK_BOT_TOKEN is not set")
    if not APP_TOKEN:
        sys.exit("ERROR: SLACK_APP_TOKEN is not set (Socket Mode app-level token, xapp-...)")
    if not TODO_PATH.exists():
        sys.exit(f"ERROR: todo store not found: {TODO_PATH}")

    web = WebClient(token=BOT_TOKEN)
    auth = web.auth_test()
    print(
        f"Slack reaction → todo watcher\n"
        f"  Machine:  {MACHINE}\n"
        f"  Team:     {auth.get('team')}\n"
        f"  Bot user: {auth.get('user_id')}\n"
        f"  Emoji(s): {', '.join(sorted(TODO_EMOJIS)) or '(none!)'}\n"
        f"  Reactor:  {ONLY_REACTOR or 'anyone'}\n"
        f"  Priority: {TODO_PRIORITY}  autoDispatch={TODO_AUTODISPATCH}\n"
        f"  TODO:     {TODO_PATH}\n"
        f"  Dry-run:  {args.dry_run}\n"
        f"  Ctrl+C to stop\n",
        file=sys.stderr,
    )

    sm = SocketModeClient(app_token=APP_TOKEN, web_client=web)
    sm.socket_mode_request_listeners.append(make_listener(args.dry_run))
    sm.connect()
    print("Connected. Waiting for reactions…", file=sys.stderr)
    threading.Event().wait()


if __name__ == "__main__":
    main()
