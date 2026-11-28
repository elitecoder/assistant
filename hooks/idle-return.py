#!/usr/bin/env python3
"""UserPromptSubmit hook: when you return to a Claude session after an idle gap,
tell Claude to rebuild your context from live state before acting.

Off unless ASSISTANT_IDLE_RETURN is 1, true, yes, or on.
ASSISTANT_IDLE_RETURN_MINUTES sets the gap, default 60.
The gap runs from your last prompt, or Claude's last reply in a turn you started.
Resume bookkeeping, hook output, local slash commands, API error stubs, and turns
started by another session, a background task, a `claude -p` call, or a message
automation typed into the terminal don't count as activity, and none of those
prompts counts as your return. A session you never typed into counts from
Claude's last reply.
"""
import json
import math
import os
import sys
from datetime import datetime, timezone

CHUNK_BYTES = 1_000_000
JUST_SENT_SECONDS = 30
MACHINE_ORIGINS = ("task-notification", "peer")
MACHINE_SOURCES = ("system", "sdk")
HUMAN_SOURCES = ("typed", "queued")
MACHINE = ("<task-notification", "Another Claude session", "<agent-message", "<cross-session-message",
           "Manager", "Assistant picked your session", "Relayed by Mukul's session manager",
           "Correction from Mukul's session manager")
STAGED_PROMPTS = "/.assistant/spawn-prompts/"
TRANSPARENT = ("This session is being continued", "Caveat:", "<system-reminder", "<user-memory",
               "<command-name>", "<local-command-stdout>")

INSTRUCTION = """The operator is returning to this session after {idle} away. They don't remember where it stood.
Before acting on their message, check live state (git status, PR state via gh, running jobs), not your memory of it.
- If their message asks for status ("status?", "where are we", "what were we doing", "merged?"): reply with a catch-up card, 6 lines max.
- Card lines: Goal (the why), Done (commit/PR/file refs), Left, Needs from you (the one decision, or "Nothing").
- If their message is a short decision ("yes", "go", "waive", "approved", "proceed"): first restate in one line what it approves, from your last question to them, then act.
- Otherwise: open with one line on where the work stands and what changed while they were away (PR merged, CI red, branch behind main), then do what they asked.
If the work is finished and merged, say so and offer cleanup in the same line."""


def enabled():
    return os.environ.get("ASSISTANT_IDLE_RETURN", "0").strip().lower() in ("1", "true", "yes", "on")


def gap_minutes():
    try:
        minutes = float(os.environ.get("ASSISTANT_IDLE_RETURN_MINUTES", "60"))
    except ValueError:
        return 60.0
    return minutes if math.isfinite(minutes) and minutes >= 0 else 60.0


def typed_by_automation(text):
    return text.startswith(MACHINE) or STAGED_PROMPTS in text


def prompt_kind(record):
    """Who started the turn this user record opens: "human", "machine", "transparent", or None."""
    queued = record.get("attachment") if record.get("type") == "attachment" else None
    if isinstance(queued, dict) and queued.get("type") == "queued_command":
        text = str(queued.get("prompt", "")).strip()
        if (queued.get("origin") or {}).get("kind") == "human" and not typed_by_automation(text):
            return "human", text
        return "machine", text
    if record.get("type") != "user" or record.get("isSidechain"):
        return None, None
    content = record.get("message", {}).get("content")
    if isinstance(content, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return None, None
        content = " ".join(b.get("text", "") for b in content
                           if isinstance(b, dict) and b.get("type") == "text")
    origin = (record.get("origin") or {}).get("kind")
    source = record.get("promptSource")
    text = content.strip() if isinstance(content, str) else ""
    if origin in MACHINE_ORIGINS or source in MACHINE_SOURCES:
        return "machine", text
    if record.get("isMeta") or not text:
        return None, None
    if typed_by_automation(text):
        return "machine", text
    if origin == "human" or source in HUMAN_SOURCES:
        return "human", text
    if text.startswith(TRANSPARENT):
        return "transparent", text
    return "human", text


def model_reply(record):
    return (record.get("type") == "assistant" and not record.get("isSidechain")
            and record.get("message", {}).get("model") != "<synthetic>")


def stamp(record):
    return datetime.fromisoformat(record["timestamp"].replace("Z", "+00:00"))


def last_activity(lines, now):
    latest_reply = None
    newest_reply = None
    newest_prompt = True
    for line in lines:
        try:
            record = json.loads(line.decode("utf-8", "replace"))
        except json.JSONDecodeError:
            continue
        if not record.get("timestamp"):
            continue
        kind, _ = prompt_kind(record)
        just_sent = (now - stamp(record)).total_seconds() < JUST_SENT_SECONDS
        if kind in ("human", "machine") and newest_prompt:
            newest_prompt = False
            if kind == "machine" and just_sent:
                return None
        if kind == "human":
            if latest_reply is None and just_sent:
                continue
            return latest_reply or stamp(record)
        if kind == "machine":
            latest_reply = None
        elif model_reply(record):
            latest_reply = latest_reply or stamp(record)
            newest_reply = newest_reply or stamp(record)
    return newest_reply


def lines_from_end(path):
    with open(path, "rb") as f:
        position = f.seek(0, os.SEEK_END)
        rest = b""
        while position > 0:
            step = min(CHUNK_BYTES, position)
            position -= step
            f.seek(position)
            lines = (f.read(step) + rest).split(b"\n")
            rest = lines.pop(0)
            yield from reversed(lines)
        yield rest


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
    if typed_by_automation(str(event.get("prompt", "")).strip()):
        return
    now = datetime.now(timezone.utc)
    then = last_activity(lines_from_end(path), now)
    if then is None:
        return
    minutes = (now - then).total_seconds() / 60
    if minutes < gap_minutes():
        return
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "UserPromptSubmit",
        "additionalContext": INSTRUCTION.format(idle=humanize(minutes)),
    }}))


if __name__ == "__main__":
    main()
