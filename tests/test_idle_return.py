"""hooks/idle-return.py runs as Claude runs it: a subprocess fed a UserPromptSubmit
event on stdin, reading a transcript written into the test's tmp dir.

It stays silent unless ASSISTANT_IDLE_RETURN is on and the last human prompt or
real model reply is older than the gap. Resume bookkeeping, hook output, peer
messages, and API error stubs written while you were away don't reset the gap.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
HOOK = REPO / "hooks" / "idle-return.py"


def stamp(ago):
    return (datetime.now(timezone.utc) - ago).isoformat().replace("+00:00", "Z")


def reply(ago, text="Want me to merge PR #12?", model="claude-opus-5-5", **extra):
    return {"type": "assistant", "timestamp": stamp(ago), **extra,
            "message": {"model": model, "content": [{"type": "text", "text": text}]}}


def prompt(ago, text):
    return {"type": "user", "timestamp": stamp(ago), "message": {"content": text}}


def attachment(ago):
    return {"type": "attachment", "timestamp": stamp(ago),
            "attachment": {"type": "hook_success", "hookName": "SessionStart:resume"}}


def run(tmp_path, records, text="status?", env=None):
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("".join(json.dumps(r) + "\n" for r in records))
    environment = {k: v for k, v in os.environ.items() if not k.startswith("ASSISTANT_IDLE")}
    environment.update({"ASSISTANT_IDLE_RETURN": "1", **(env or {})})
    result = subprocess.run(
        [sys.executable, "-I", str(HOOK)],
        input=json.dumps({"transcript_path": str(transcript), "prompt": text}),
        capture_output=True, text=True, env=environment, timeout=30, check=True)
    return json.loads(result.stdout)["hookSpecificOutput"] if result.stdout.strip() else None


def test_a_return_after_three_hours_asks_for_a_catch_up(tmp_path):
    out = run(tmp_path, [prompt(timedelta(hours=3, minutes=5), "fix it"),
                         reply(timedelta(hours=3))])
    assert out["hookEventName"] == "UserPromptSubmit"
    assert "after 3 hours away" in out["additionalContext"]
    assert "catch-up card" in out["additionalContext"]


def test_it_is_off_unless_the_setting_is_on(tmp_path):
    records = [reply(timedelta(days=2))]
    assert run(tmp_path, records, env={"ASSISTANT_IDLE_RETURN": "0"}) is None
    environment = {k: v for k, v in os.environ.items() if not k.startswith("ASSISTANT_IDLE")}
    transcript = tmp_path / "session.jsonl"
    result = subprocess.run(
        [sys.executable, "-I", str(HOOK)],
        input=json.dumps({"transcript_path": str(transcript), "prompt": "status?"}),
        capture_output=True, text=True, env=environment, timeout=30, check=True)
    assert result.stdout == ""


def test_a_short_gap_is_not_a_return(tmp_path):
    assert run(tmp_path, [reply(timedelta(minutes=20))]) is None


def test_the_gap_is_configurable(tmp_path):
    out = run(tmp_path, [reply(timedelta(minutes=20))],
              env={"ASSISTANT_IDLE_RETURN_MINUTES": "10"})
    assert "after 20 minutes away" in out["additionalContext"]


def test_the_prompt_being_answered_is_not_counted_as_activity(tmp_path):
    out = run(tmp_path, [reply(timedelta(hours=5)), prompt(timedelta(0), "yes")], text="yes")
    assert "after 5 hours away" in out["additionalContext"]


def test_resume_bookkeeping_and_error_stubs_do_not_hide_the_gap(tmp_path):
    out = run(tmp_path, [
        reply(timedelta(days=16)),
        prompt(timedelta(hours=4), "Another Claude session sent a message: hands-off"),
        reply(timedelta(hours=4), "API Error: Could not load AWS credentials",
              isApiErrorMessage=True, model="<synthetic>"),
        attachment(timedelta(minutes=13)),
        prompt(timedelta(0), "continue"),
    ], text="continue")
    assert "after 16.0 days away" in out["additionalContext"]


def test_a_reply_after_a_task_notification_counts_as_activity(tmp_path):
    out = run(tmp_path, [
        reply(timedelta(days=1)),
        prompt(timedelta(minutes=30), "<task-notification> agent done </task-notification>"),
        reply(timedelta(minutes=29), "The review is back."),
    ])
    assert out is None


@pytest.mark.parametrize("event", [{}, {"transcript_path": "/nonexistent/session.jsonl"}])
def test_no_transcript_means_no_output(event):
    environment = {**os.environ, "ASSISTANT_IDLE_RETURN": "1"}
    result = subprocess.run([sys.executable, "-I", str(HOOK)], input=json.dumps(event),
                            capture_output=True, text=True, env=environment, timeout=30)
    assert result.returncode == 0
    assert result.stdout == ""
