"""hooks/idle-return.py runs as Claude runs it: a subprocess fed a UserPromptSubmit
event on stdin, reading a transcript written into the test's tmp dir.

It stays silent unless ASSISTANT_IDLE_RETURN is on and your last prompt, or
Claude's last reply in a turn you started, is older than the gap. Resume
bookkeeping, hook output, local slash commands, API error stubs, and turns started
by another session, a task notification, or a message automation typed while you
were away don't reset the gap.
"""
from __future__ import annotations

import importlib.util
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


def prompt(ago, text, **extra):
    return {"type": "user", "timestamp": stamp(ago), **extra, "message": {"content": text}}


def typed(ago, text):
    return prompt(ago, text, origin={"kind": "human"}, promptSource="typed")


def agent_report(ago):
    return prompt(ago, "Another Claude session sent a message: <agent-message>done</agent-message>",
                  isMeta=True, origin={"kind": "peer", "from": "a67e9ad3a6489115b"},
                  promptSource="system")


def attachment(ago):
    return {"type": "attachment", "timestamp": stamp(ago),
            "attachment": {"type": "hook_success", "hookName": "SessionStart:resume"}}


def clean_env():
    return {k: v for k, v in os.environ.items() if not k.startswith("ASSISTANT_IDLE")}


def run(tmp_path, records, text="status?", env=None):
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("".join(json.dumps(r) + "\n" for r in records))
    environment = clean_env()
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
    environment = clean_env()
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


def test_a_one_minute_gap_reads_as_singular(tmp_path):
    out = run(tmp_path, [reply(timedelta(seconds=70))], env={"ASSISTANT_IDLE_RETURN_MINUTES": "1"})
    assert "after 1 minute away" in out["additionalContext"]


def test_a_short_decision_after_the_gap_gets_the_catch_up(tmp_path):
    out = run(tmp_path, [reply(timedelta(hours=5))], text="yes")
    assert "after 5 hours away" in out["additionalContext"]


def test_resume_bookkeeping_and_error_stubs_do_not_hide_the_gap(tmp_path):
    out = run(tmp_path, [
        reply(timedelta(days=16)),
        prompt(timedelta(hours=4), "Another Claude session sent a message: hands-off"),
        reply(timedelta(hours=4), "API Error: Could not load AWS credentials",
              isApiErrorMessage=True, model="<synthetic>"),
        attachment(timedelta(minutes=13)),
    ], text="continue")
    assert "after 16.0 days away" in out["additionalContext"]


def test_turns_started_while_you_were_away_do_not_hide_the_gap(tmp_path):
    out = run(tmp_path, [
        prompt(timedelta(hours=6, minutes=1), "run the evals"),
        reply(timedelta(hours=6), "Started; I'll report back."),
        prompt(timedelta(hours=2), "<task-notification> evals done </task-notification>"),
        reply(timedelta(hours=2), "Evals passed. Want me to open the PR?"),
    ])
    assert "after 6 hours away" in out["additionalContext"]


def test_a_reply_in_your_own_turn_counts_as_activity(tmp_path):
    assert run(tmp_path, [
        prompt(timedelta(hours=3), "run the full suite"),
        reply(timedelta(minutes=10), "The suite passed."),
    ]) is None


def test_a_compaction_summary_does_not_split_your_turn(tmp_path):
    assert run(tmp_path, [
        prompt(timedelta(hours=3), "finish the refactor"),
        prompt(timedelta(hours=1), "This session is being continued from a previous conversation."),
        reply(timedelta(minutes=10), "The refactor is done."),
    ]) is None


def test_a_compaction_summary_alone_is_not_you_being_there(tmp_path):
    out = run(tmp_path, [
        prompt(timedelta(hours=3, minutes=1), "finish the refactor"),
        reply(timedelta(hours=3), "Done."),
        prompt(timedelta(minutes=10), "This session is being continued from a previous conversation."),
    ])
    assert "after 3 hours away" in out["additionalContext"]


def test_an_api_error_stub_is_not_a_reply(tmp_path):
    out = run(tmp_path, [
        prompt(timedelta(hours=5, minutes=1), "ship it"),
        reply(timedelta(hours=5), "Shipped."),
        reply(timedelta(minutes=5), "API Error: Could not load AWS credentials",
              isApiErrorMessage=True, model="<synthetic>"),
    ])
    assert "after 5 hours away" in out["additionalContext"]


def test_a_skill_command_after_the_gap_gets_the_catch_up(tmp_path):
    out = run(tmp_path, [
        typed(timedelta(hours=3, minutes=1), "start"),
        reply(timedelta(hours=3)),
    ], text="/wassup")
    assert "after 3 hours away" in out["additionalContext"]


def test_a_local_slash_command_does_not_hide_the_gap(tmp_path):
    out = run(tmp_path, [
        typed(timedelta(days=1, minutes=1), "start"),
        reply(timedelta(days=1)),
        prompt(timedelta(minutes=5), "<command-name>/model</command-name>"),
        prompt(timedelta(minutes=5), "<local-command-stdout>Set model</local-command-stdout>"),
    ], text="where are we?")
    assert "after 24 hours away" in out["additionalContext"]


@pytest.mark.parametrize("text", [
    "Assistant picked your session as one of five that can land fast.",
    "Manager note: read the review and fix it.",
    "Manager: the gate ran on the new head.",
    "Manager answer to your open question: merge it.",
    "hands-off. Read /Users/me/.assistant/spawn-prompts/brief.md and follow it.",
    "This comes from the readiness judge that Mukul Sharma runs on his sessions.",
    "The previous turn appears stalled. Re-check the last failed or incomplete step.",
])
def test_a_message_automation_typed_while_you_were_away_does_not_hide_the_gap(tmp_path, text):
    out = run(tmp_path, [
        typed(timedelta(hours=5, minutes=1), "start"),
        reply(timedelta(hours=5)),
        typed(timedelta(minutes=20), text),
        reply(timedelta(minutes=19), "On it."),
    ], text="where are we?")
    assert "after 5 hours away" in out["additionalContext"]


@pytest.mark.parametrize("text", [
    "<cross-session-message from=dev-f6>go</cross-session-message>",
    "<agent-message>done</agent-message>",
    "Read ~/.assistant/spawn-prompts/prompt-1.md in full and execute every instruction in it.",
    "Read ~/.claude/spawn-prompts/td-12.md in full and execute every instruction in it.",
    "The previous turn appears stalled. Re-check the last failed or incomplete step.",
    "[Cross-session delivery notice] Your message to dev-f6 was delivered.",
    "[Cross-session idle notice] dev-f6 is idle.",
])
def test_an_automation_prompt_arriving_after_the_gap_is_not_a_return(tmp_path, text):
    assert run(tmp_path, [typed(timedelta(hours=3), "start"), reply(timedelta(hours=3))],
               text=text) is None


def test_a_system_prompt_is_not_you_being_there(tmp_path):
    out = run(tmp_path, [
        typed(timedelta(hours=3, minutes=1), "start"),
        reply(timedelta(hours=3)),
        prompt(timedelta(minutes=5), "Scheduled check: CI is green.", promptSource="system"),
        reply(timedelta(minutes=4), "Noted."),
    ])
    assert "after 3 hours away" in out["additionalContext"]


def test_a_prompt_you_sent_seconds_ago_that_claude_answered_counts(tmp_path):
    assert run(tmp_path, [
        typed(timedelta(hours=3, minutes=1), "start"),
        reply(timedelta(hours=3)),
        agent_report(timedelta(hours=1)),
        typed(timedelta(seconds=20), "also check CI"),
        reply(timedelta(seconds=10), "CI is green."),
    ]) is None


def test_a_session_you_never_typed_into_counts_from_claudes_last_reply(tmp_path):
    out = run(tmp_path, [
        typed(timedelta(hours=2, minutes=1),
              "Read ~/.assistant/spawn-prompts/prompt-1.md in full and execute every instruction in it."),
        reply(timedelta(hours=2), "Opened PR #12."),
    ])
    assert "after 2 hours away" in out["additionalContext"]


def test_a_background_agent_report_and_its_reply_do_not_hide_the_gap(tmp_path):
    out = run(tmp_path, [
        typed(timedelta(hours=3, minutes=1), "review the PR in the background"),
        reply(timedelta(hours=3), "Started the reviewer."),
        agent_report(timedelta(minutes=5)),
        reply(timedelta(minutes=4), "The reviewer found two bugs."),
    ])
    assert "after 3 hours away" in out["additionalContext"]


def test_an_sdk_prompt_is_not_you_being_there(tmp_path):
    out = run(tmp_path, [
        typed(timedelta(hours=3, minutes=1), "ship it"),
        reply(timedelta(hours=3), "Shipped."),
        prompt(timedelta(minutes=5), "You are a verification gate.", origin={"kind": "human"},
               promptSource="sdk"),
        reply(timedelta(minutes=4), "PASS"),
    ])
    assert "after 3 hours away" in out["additionalContext"]


def test_resending_the_same_text_after_an_error_is_not_a_return(tmp_path):
    assert run(tmp_path, [
        typed(timedelta(hours=20), "continue"),
        reply(timedelta(hours=20), "Working on it."),
        typed(timedelta(minutes=2), "continue"),
        reply(timedelta(minutes=2), "API Error: overloaded", model="<synthetic>"),
    ], text="continue") is None


@pytest.mark.parametrize("minutes, fires", [("abc", False), ("nan", False), ("-5", False),
                                             ("1.5", True)])
def test_an_odd_gap_setting_does_not_break_prompts(tmp_path, minutes, fires):
    out = run(tmp_path, [reply(timedelta(minutes=30))],
              env={"ASSISTANT_IDLE_RETURN_MINUTES": minutes})
    assert (out is not None) == fires


def test_an_agent_report_arriving_as_the_prompt_is_not_a_return(tmp_path):
    assert run(tmp_path, [typed(timedelta(hours=3), "start"), reply(timedelta(hours=3))],
               text="<task-notification> agent done </task-notification>") is None


def queued(ago, text, origin):
    return {"type": "attachment", "timestamp": stamp(ago),
            "attachment": {"type": "queued_command", "prompt": text, "origin": origin,
                           "commandMode": "prompt"}}


def test_a_message_you_queue_during_a_report_turn_counts_as_you_being_there(tmp_path):
    assert run(tmp_path, [
        typed(timedelta(hours=5), "start"),
        reply(timedelta(hours=5)),
        agent_report(timedelta(minutes=30)),
        queued(timedelta(minutes=28), "also check CI", {"kind": "human"}),
        reply(timedelta(minutes=27), "CI is green."),
    ]) is None


def test_a_queued_agent_report_is_not_you_being_there(tmp_path):
    out = run(tmp_path, [
        typed(timedelta(hours=5), "start"),
        reply(timedelta(hours=5)),
        queued(timedelta(minutes=28), "<agent-message>done</agent-message>",
               {"kind": "peer", "from": "a67e9ad3a6489115b"}),
        reply(timedelta(minutes=27), "The agent is done."),
    ])
    assert "after 5 hours away" in out["additionalContext"]


def test_a_queued_message_automation_typed_is_not_you_being_there(tmp_path):
    out = run(tmp_path, [
        typed(timedelta(hours=5), "start"),
        reply(timedelta(hours=5)),
        queued(timedelta(minutes=28), "Manager note: read the review.", {"kind": "human"}),
        reply(timedelta(minutes=27), "Read it."),
    ])
    assert "after 5 hours away" in out["additionalContext"]


def test_retyping_a_prompt_seconds_after_the_last_one_is_not_a_return(tmp_path):
    assert run(tmp_path, [
        typed(timedelta(hours=3, minutes=1), "start"),
        reply(timedelta(hours=3)),
        typed(timedelta(seconds=15), "continue"),
    ], text="continue") is None


@pytest.mark.parametrize("odd", [
    "[1, 2]",
    '{"type": "user", "timestamp": "yesterday", "message": {"content": "hi"}}',
    '{"type": "user", "timestamp": "2026-11-27T10:00:00", "message": {"content": "hi"}}',
    '{"type": "user", "timestamp": "2026-11-27T10:00:00Z", "message": null}',
    '{"type": "user", "timestamp": "2026-11-27T10:00:00Z", "origin": "human", "message": {"content": "hi"}}',
    '{"type": "attachment", "timestamp": "2026-11-27T10:00:00Z", "attachment": {"type": "queued_command", "origin": "human"}}',
    '{"type": "assistant", "timestamp": "2026-11-27T10:00:00Z", "message": null}',
])
def test_an_odd_record_is_skipped(tmp_path, odd):
    transcript = tmp_path / "session.jsonl"
    run(tmp_path, [typed(timedelta(hours=2, minutes=1), "start"), reply(timedelta(hours=2))])
    with open(transcript, "a") as f:
        f.write(odd + "\n")
    result = subprocess.run(
        [sys.executable, "-I", str(HOOK)],
        input=json.dumps({"transcript_path": str(transcript), "prompt": "status?"}),
        capture_output=True, text=True, env={**clean_env(), "ASSISTANT_IDLE_RETURN": "1"},
        timeout=30, check=True)
    assert "after 2 hours away" in result.stdout


def test_a_prompt_saved_as_text_blocks_counts_as_you_being_there(tmp_path):
    assert run(tmp_path, [
        typed(timedelta(hours=3), "start"),
        reply(timedelta(hours=3)),
        prompt(timedelta(minutes=5), [{"type": "text", "text": "also check CI"},
                                      {"type": "image", "source": {}}]),
    ]) is None


def test_tool_results_and_empty_or_meta_records_are_not_you_being_there(tmp_path):
    out = run(tmp_path, [
        typed(timedelta(hours=3, minutes=1), "start"),
        reply(timedelta(hours=3)),
        prompt(timedelta(minutes=5), [{"type": "tool_result", "content": "ok"}]),
        prompt(timedelta(minutes=5), "   "),
        prompt(timedelta(minutes=5), "Loaded the skill.", isMeta=True),
    ])
    assert "after 3 hours away" in out["additionalContext"]


def test_a_hook_that_cannot_load_agent_session_stays_silent(tmp_path):
    (tmp_path / "hooks").mkdir()
    copy = tmp_path / "hooks" / "idle-return.py"
    copy.write_text(HOOK.read_text())
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(json.dumps(reply(timedelta(hours=3))) + "\n")
    result = subprocess.run(
        [sys.executable, "-I", str(copy)],
        input=json.dumps({"transcript_path": str(transcript), "prompt": "status?"}),
        capture_output=True, text=True, env={**clean_env(), "ASSISTANT_IDLE_RETURN": "1"},
        timeout=30)
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")


def test_the_hook_does_nothing_when_agent_session_cannot_load(monkeypatch):
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setitem(sys.modules, "agent_session", None)
    monkeypatch.setenv("ASSISTANT_IDLE_RETURN", "1")
    spec = importlib.util.spec_from_file_location("idle_return_without_seam", HOOK)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.agent_session is None
    assert module.main() is None


def test_the_hook_can_be_imported_without_running(tmp_path):
    spec = importlib.util.spec_from_file_location("idle_return", HOOK)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.prompt_kind({"type": "user", "timestamp": stamp(timedelta(0)),
                               "message": {"content": "where are we?"}}) == ("human", "where are we?")


def test_a_message_you_type_as_a_notice_starts_a_turn_is_still_a_return(tmp_path):
    out = run(tmp_path, [
        typed(timedelta(hours=3, minutes=1), "start"),
        reply(timedelta(hours=3)),
        prompt(timedelta(seconds=5), "<task-notification> evals done </task-notification>",
               origin={"kind": "task-notification"}, promptSource="system"),
    ])
    assert "after 3 hours away" in out["additionalContext"]


def test_a_second_message_before_claude_replies_gets_no_second_catch_up(tmp_path):
    assert run(tmp_path, [
        typed(timedelta(hours=3, minutes=1), "start"),
        reply(timedelta(hours=3)),
        typed(timedelta(seconds=10), "status?"),
    ], text="also check CI") is None


def test_a_half_written_last_line_does_not_break_prompts(tmp_path):
    transcript = tmp_path / "session.jsonl"
    run(tmp_path, [reply(timedelta(hours=2))])
    with open(transcript, "ab") as f:
        f.write('{"type": "user", "message": {"content": "caf'.encode() + "é".encode()[:1])
    result = subprocess.run(
        [sys.executable, "-I", str(HOOK)],
        input=json.dumps({"transcript_path": str(transcript), "prompt": "status?"}),
        capture_output=True, text=True, env={**clean_env(), "ASSISTANT_IDLE_RETURN": "1"},
        timeout=30, check=True)
    assert "after 2 hours away" in result.stdout


def test_a_record_longer_than_one_read_is_kept_whole(tmp_path):
    report = agent_report(timedelta(minutes=5))
    report["message"]["content"] += "é" * 1_500_000
    out = run(tmp_path, [typed(timedelta(hours=4), "start"), reply(timedelta(hours=4)), report,
                         reply(timedelta(minutes=4), "The agent is done.")])
    assert (tmp_path / "session.jsonl").stat().st_size > 2_000_000
    assert "after 4 hours away" in out["additionalContext"]


@pytest.mark.parametrize("event", [{}, {"transcript_path": "/nonexistent/session.jsonl"}])
def test_no_transcript_means_no_output(event):
    environment = {**clean_env(), "ASSISTANT_IDLE_RETURN": "1"}
    result = subprocess.run([sys.executable, "-I", str(HOOK)], input=json.dumps(event),
                            capture_output=True, text=True, env=environment, timeout=30)
    assert result.returncode == 0
    assert result.stdout == ""
