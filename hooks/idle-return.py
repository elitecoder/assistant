#!/usr/bin/env python3
"""UserPromptSubmit hook: when you return to a Claude session after an idle gap,
tell Claude to rebuild your context from live state before acting.

Off unless ASSISTANT_IDLE_RETURN is 1, true, yes, or on.
ASSISTANT_IDLE_RETURN_MINUTES sets the gap, default 60.
The gap runs from your last prompt, or Claude's last reply in a turn you started.
Resume bookkeeping, hook output, API error stubs, and turns started by another
session or a background task notification don't count as activity.
"""
import json
import os
import sys
from datetime import datetime, timezone

TAIL_BYTES = 1_000_000
MACHINE = ("<task-notification", "Another Claude session")
TRANSPARENT = ("This session is being continued", "Caveat:", "<system-reminder", "<user-memory")

INSTRUCTION = """The operator is returning to this session after {idle} away. They don't remember where it stood.
Before acting on their message, check live state (git status, PR state via gh, running jobs), not your memory of it.
- If their message asks for status ("status?", "where are we", "what were we doing", "merged?"): reply with a catch-up card, 6 lines max.
- Card lines: Goal (the why), Done (commit/PR/file refs), Left, Needs from you (the one decision, or "Nothing").
- If their message is a short decision ("yes", "go", "waive", "approved", "proceed"): first restate in one line what it approves, from your last question to them, then act.
- Otherwise: open with one line on where the work stands and what changed while they were away (PR merged, CI red, branch behind main), then do what they asked.
If the work is finished and merged, say so and offer cleanup in the same line."""


def enabled():
    return os.environ.get("ASSISTANT_IDLE_RETURN", "0").strip().lower() in ("1", "true", "yes", "on")


def prompt_kind(record):
    """Who started the turn this user record opens: "human", "machine", "transparent", or None."""
    if record.get("type") != "user" or record.get("isMeta") or record.get("isSidechain"):
        return None, None
    content = record.get("message", {}).get("content")
    if isinstance(content, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return None, None
        content = " ".join(b.get("text", "") for b in content
                           if isinstance(b, dict) and b.get("type") == "text")
    if not isinstance(content, str) or not content.strip():
        return None, None
    text = content.strip()
    if text.startswith(MACHINE):
        return "machine", text
    if text.startswith(TRANSPARENT):
        return "transparent", text
    return "human", text


def model_reply(record):
    return (record.get("type") == "assistant" and not record.get("isSidechain")
            and record.get("message", {}).get("model") != "<synthetic>")


def stamp(record):
    return datetime.fromisoformat(record["timestamp"].replace("Z", "+00:00"))


def last_activity(lines, prompt):
    latest_reply = None
    for line in reversed(lines):
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not record.get("timestamp"):
            continue
        kind, text = prompt_kind(record)
        if kind == "human" and text == prompt.strip() and latest_reply is None:
            continue
        if kind == "human":
            return latest_reply or stamp(record)
        if kind == "machine":
            latest_reply = None
        elif model_reply(record) and latest_reply is None:
            latest_reply = stamp(record)
    return latest_reply


def read_lines(path, limit):
    with open(path, "rb") as f:
        f.seek(max(0, os.path.getsize(path) - limit))
        return f.read().decode("utf-8", "replace").splitlines()


def humanize(minutes):
    if minutes < 120:
        return f"{int(minutes)} minute{'' if int(minutes) == 1 else 's'}"
    hours = minutes / 60
    return f"{hours:.0f} hours" if hours < 48 else f"{hours / 24:.1f} days"


def main():
    if not enabled():
        return
    event = json.load(sys.stdin)
    path = event.get("transcript_path")
    if not path or not os.path.exists(path):
        return
    prompt = event.get("prompt", "")
    then = last_activity(read_lines(path, TAIL_BYTES), prompt)
    if then is None and os.path.getsize(path) > TAIL_BYTES:
        then = last_activity(read_lines(path, os.path.getsize(path)), prompt)
    if then is None:
        return
    minutes = (datetime.now(timezone.utc) - then).total_seconds() / 60
    if minutes < int(os.environ.get("ASSISTANT_IDLE_RETURN_MINUTES", "60")):
        return
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "UserPromptSubmit",
        "additionalContext": INSTRUCTION.format(idle=humanize(minutes)),
    }}))


if __name__ == "__main__":
    main()
