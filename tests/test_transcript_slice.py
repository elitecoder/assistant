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


def test_turns_the_person_did_not_type_come_out_as_system(tmp_path):
    records = [
        _user("Fix the build"),
        {**_user("<task-notification><task-id>a1</task-id></task-notification>"),
         "origin": {"kind": "task-notification"}, "promptSource": "system"},
        {**_user("Another Claude session sent a message: hi"), "origin": {"kind": "peer"}},
        {**_user("This session is being continued from a previous conversation"),
         "isCompactSummary": True},
        _user("<local-command-stdout>ok</local-command-stdout>"),
        _user("Caveat: the messages below were generated by the user"),
        _user("[System: context from Assistant] workspace 4 is idle"),
    ]
    lines = ts.read_conversation(_write(tmp_path / "s.jsonl", records)).splitlines()
    assert lines[0] == "USER t: Fix the build"
    assert [line.split(" ", 1)[0] for line in lines[1:]] == ["SYSTEM"] * 6


def test_slash_commands_read_as_commands_and_only_ones_with_arguments_are_requests(tmp_path):
    noise = [_user(f"step {i}") for i in range(5)]
    records = [
        _user("<command-message>model</command-message>\n<command-name>/model</command-name>"),
        _user("<command-name>/archffp</command-name>\n<command-args>fix the trim bug</command-args>"),
        *noise,
    ]
    out = ts.read_conversation(_write(tmp_path / "s.jsonl", records), max_turns=2)
    assert out.splitlines()[0] == "FIRST REQUEST USER t: /archffp fix the trim bug"


def test_a_long_only_request_is_shown_once(tmp_path):
    path = _write(tmp_path / "s.jsonl", [_user("x" * 3000)])
    out = ts.read_conversation(path)
    assert out.splitlines() == ["USER t: " + "x" * 3000]


def test_malformed_records_are_skipped(tmp_path):
    records = [
        _user("ok"),
        {"type": "user", "message": {"role": "user", "content": 5}},
        {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": 7}]}},
        {"type": "system", "message": {"role": "system", "content": "skip"}},
        {"type": "assistant", "message": "not a dict"},
        {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "thinking"}]}},
    ]
    path = tmp_path / "s.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in records) + "{not json\n[1, 2]\n")
    assert ts.read_conversation(path) == "USER t: ok"


def test_every_block_kind_renders(tmp_path):
    records = [_user("go"), _assistant([
        "plain",
        7,
        {"type": "tool_use", "name": "Edit", "input": {"old_string": "a"}},
        {"type": "tool_use", "name": "Odd", "input": "raw"},
        {"type": "image"},
    ]), {"type": "user", "timestamp": "t", "message": {"role": "user", "content": [
        {"type": "tool_result", "content": [{"type": "text", "text": "listed"}]}]}}]
    out = ts.read_conversation(_write(tmp_path / "s.jsonl", records))
    assert 'ASSISTANT t: plain [tool Edit: {"old_string": "a"}] [tool Odd: ] [image]' in out
    assert "USER t: [tool result: listed]" in out


def test_the_tail_grows_until_it_holds_enough_turns(tmp_path, monkeypatch):
    noise = [{"type": "attachment", "attachment": {"content": "N" * 500}}] * 20
    records = [_user("ask")] + [_user(f"turn {i}") for i in range(6)] + noise
    path = _write(tmp_path / "s.jsonl", records)
    monkeypatch.setattr(ts, "TAIL_STEPS", (2000, 6000, 1 << 20))
    out = ts.read_conversation(path, max_turns=5)
    assert "turn 5" in out and "turn 1" in out
    monkeypatch.setattr(ts, "TAIL_STEPS", (2000,))
    assert "turn 5" not in ts.read_conversation(path, max_turns=5)


def test_the_first_request_skips_assistant_and_tool_turns(tmp_path):
    records = [
        _assistant([{"type": "text", "text": "Ready."}]),
        {"type": "user", "timestamp": "t", "message": {"role": "user", "content": [
            {"type": "tool_result", "content": "done"}]}},
        _user("Ship the fix"),
        *[_user(f"step {i}") for i in range(5)],
    ]
    out = ts.read_conversation(_write(tmp_path / "s.jsonl", records), max_turns=2)
    assert out.splitlines()[0] == "FIRST REQUEST USER t: Ship the fix"


def test_the_away_recap_shows_as_a_system_turn(tmp_path):
    records = [_user("Make the table"),
               {"type": "system", "subtype": "away_summary", "timestamp": "t",
                "content": "Next, I can update the chart page if you want."},
               {"type": "system", "subtype": "away_summary", "timestamp": "t", "content": ""},
               {"type": "system", "subtype": "turn_duration", "timestamp": "t", "content": "12s"}]
    lines = ts.read_conversation(_write(tmp_path / "s.jsonl", records)).splitlines()
    assert lines == ["USER t: Make the table",
                     "SYSTEM t: Recap: Next, I can update the chart page if you want."]
