import json
from pathlib import Path
from unittest import mock

from assistant import transcript_slice as ts


def _user(text, stamp="t"):
    return {"type": "user", "timestamp": stamp, "message": {"role": "user", "content": text}}


def _assistant(blocks, stamp="t"):
    return {"type": "assistant", "timestamp": stamp, "message": {"role": "assistant", "content": blocks}}


def _write(path: Path, records) -> Path:
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    return path


def test_a_noisy_log_becomes_its_first_request_and_latest_turns(tmp_path):
    noise = [{"type": "attachment", "attachment": {"content": "NOISE" * 2000}}] * 300
    records = ([_user("Fix the login bug")] + noise
               + [_user(f"step {i}") for i in range(100)]
               + [_assistant([{"type": "text", "text": "Done. PR #12 is open."}])])
    path = _write(tmp_path / "s.jsonl", records)
    with mock.patch.object(Path, "read_bytes", side_effect=AssertionError("reads whole file")):
        out = ts.read_conversation(path, max_turns=10)
    lines = out.splitlines()
    assert lines[0].startswith("FIRST REQUEST USER t: Fix the login bug")
    assert lines[1] == "[… earlier turns omitted …]"
    assert lines[-1] == "ASSISTANT t: Done. PR #12 is open."
    assert "step 99" in out and "step 90" not in out
    assert "NOISE" not in out
    assert len(lines) == 12


def test_tool_calls_shrink_to_one_line_and_reminders_and_thinking_drop(tmp_path):
    records = [
        _user("go"),
        _assistant([{"type": "thinking", "thinking": "SECRET PLAN"},
                    {"type": "tool_use", "name": "Bash",
                     "input": {"command": "ls", "description": "List files"}}]),
        {"type": "user", "timestamp": "t", "message": {"role": "user", "content": [
            {"type": "tool_result", "is_error": True, "content": "boom " * 100},
            {"type": "text", "text": "<system-reminder>REMIND</system-reminder>"}]}},
        {"type": "user", "isMeta": True, "message": {"role": "user", "content": "META"}},
    ]
    out = ts.read_conversation(_write(tmp_path / "s.jsonl", records))
    assert "ASSISTANT t: [tool Bash: List files]" in out
    assert "USER t: [tool error: boom" in out
    assert len(out.splitlines()[-1]) < 220
    for hidden in ("SECRET PLAN", "REMIND", "META"):
        assert hidden not in out


def test_droid_messages_read_like_claude_turns(tmp_path):
    records = [{"type": "session_start"},
               {"type": "message", "timestamp": "t", "message": {"role": "user", "content": [{"type": "text", "text": "hi"}]}},
               {"type": "message", "timestamp": "t", "message": {"role": "assistant", "content": [{"type": "text", "text": "hello"}]}}]
    out = ts.read_conversation(_write(tmp_path / "d.jsonl", records))
    assert out.splitlines() == ["USER t: hi", "ASSISTANT t: hello"]


def test_copilot_events_read_like_claude_turns(tmp_path):
    records = [{"type": "session.start", "data": {"sessionId": "s"}},
               {"type": "user.message", "timestamp": "t", "data": {"content": "Fix the build"}},
               {"type": "tool.execution_start", "data": {"toolCallId": "c1", "toolName": "bash"}},
               {"type": "assistant.message", "timestamp": "t", "data": {
                   "content": "", "toolRequests": [{"toolCallId": "c1", "name": "bash",
                                                    "arguments": {"command": "make"}}]}},
               {"type": "assistant.message", "timestamp": "t", "data": {"content": "Fixed."}}]
    out = ts.read_conversation(_write(tmp_path / "events.jsonl", records))
    assert out.splitlines() == ["USER t: Fix the build", "ASSISTANT t: [tool bash: make]",
                                "ASSISTANT t: Fixed."]


def test_end_replays_the_transcript_as_it_was(tmp_path):
    path = _write(tmp_path / "s.jsonl", [_user("first"), _user("second")])
    end = len(json.dumps(_user("first"))) + 1
    _write(path, [_user("first"), _user("second"), _user("later")])
    assert ts.read_conversation(path, end=end) == "USER t: first"


def test_the_budget_keeps_the_newest_turns(tmp_path):
    path = _write(tmp_path / "s.jsonl", [_user("ask")] + [_user("x" * 900 + str(i)) for i in range(20)])
    out = ts.read_conversation(path, max_chars=3000)
    assert out.splitlines()[-1].endswith("19")
    assert len(out) < 3000 + ts.FIRST_REQUEST_CHARS + 100
