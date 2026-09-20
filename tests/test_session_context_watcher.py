"""Tests for bin/session-context-watcher.py — event-driven transcript watcher.

The script is a hyphenated CLI (not an importable module name), so it's loaded
by file path via importlib. The module binds HOME-derived path constants AT
IMPORT, so we set os.environ["HOME"] to a tmp dir BEFORE the first import — every
constant (CMUX_REGISTRY, ORCHESTRATOR_REGISTRY, OUT_PATH, LOG_DIR, LOCK_FILE)
then resolves under that tmp dir. Per-test we monkeypatch individual
module-level constants so nothing ever touches the real ~/.claude or ~/.architect.

kqueue is real here (macOS); we exercise add_watch / drop_watch / handle_event /
discover / flush against real tmp files and a real select.kqueue.
"""
from __future__ import annotations

import importlib.util
import json
import os
import select
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock

import pytest

REPO = Path(__file__).resolve().parent.parent

# A tmp HOME that exists for the whole test session, bound BEFORE the module
# import so its module-level path constants resolve under it. Individual tests
# further isolate themselves with their own tmp dirs via monkeypatch.
_SESSION_TMP = TemporaryDirectory()
os.environ["HOME"] = _SESSION_TMP.name


def _load():
    name = "session_context_watcher_mod"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        name, str(REPO / "bin/session-context-watcher.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


scw = _load()


@pytest.fixture
def tmp_home(tmp_path, monkeypatch):
    """Re-point every HOME-derived module constant at a fresh tmp dir so each
    test is isolated and the real home is never touched. LOG_DIR lives under tmp
    so log() writes are safe."""
    home = tmp_path / "home"
    home.mkdir()
    cmux_reg = home / ".claude/cmux-registry.json"
    orch_reg = home / ".architect/orchestrator-registry.json"
    out_path = home / ".claude/cache/session-context.json"
    world_path = home / ".claude/cache/world.json"
    log_dir = home / ".assistant/logs"
    lock_file = home / ".architect/.session-context-watcher.lock"
    monkeypatch.setattr(scw, "HOME", home)
    monkeypatch.setattr(scw, "CMUX_REGISTRY", cmux_reg)
    monkeypatch.setattr(scw, "ORCHESTRATOR_REGISTRY", orch_reg)
    monkeypatch.setattr(scw, "OUT_PATH", out_path)
    monkeypatch.setattr(scw, "WORLD_PATH", world_path)
    monkeypatch.setattr(scw, "LOG_DIR", log_dir)
    monkeypatch.setattr(scw, "LOCK_FILE", lock_file)
    return home


# ─── time helpers ─────────────────────────────────────────────────────────────

def test_utc_now_zeroes_microseconds():
    now = scw.utc_now()
    assert now.microsecond == 0
    assert now.tzinfo == timezone.utc


def test_iso_uses_z_suffix():
    dt = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    assert scw.iso(dt) == "2026-01-02T03:04:05Z"


def test_parse_iso_roundtrip():
    dt = scw.parse_iso("2026-01-02T03:04:05Z")
    assert dt == datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)


def test_parse_iso_none_and_empty():
    assert scw.parse_iso(None) is None
    assert scw.parse_iso("") is None


def test_parse_iso_garbage():
    assert scw.parse_iso("not-a-date") is None
    assert scw.parse_iso(12345) is None  # AttributeError on .replace


# ─── cwd_from_project_dir ────────────────────────────────────────────────────

def test_cwd_from_project_dir_leading_dash():
    # "-Users-mukuls-dev-assistant" → "/Users/mukuls/dev/assistant"
    assert scw.cwd_from_project_dir("-Users-mukuls-dev-assistant") == \
        "/Users/mukuls/dev/assistant"


def test_cwd_from_project_dir_plain_passthrough():
    assert scw.cwd_from_project_dir("plainname") == "plainname"


# ─── pid_alive ────────────────────────────────────────────────────────────────

def test_pid_alive_none_is_false():
    assert scw.pid_alive(None) is False
    assert scw.pid_alive(0) is False


def test_pid_alive_live_process():
    assert scw.pid_alive(os.getpid()) is True
    assert scw.pid_alive(str(os.getpid())) is True  # int() coercion


def test_pid_alive_bogus_pid():
    # A pid that's almost certainly not running.
    assert scw.pid_alive(2_000_000_000) is False


def test_pid_alive_non_int():
    assert scw.pid_alive("notanumber") is False


# ─── load_live_claude_sessions ───────────────────────────────────────────────

def test_load_live_sessions_missing_registry(tmp_home):
    # No file → empty dict, no crash.
    assert scw.load_live_claude_sessions() == {}


def test_load_live_sessions_filters_dead_keeps_live(tmp_home):
    live_pid = os.getpid()
    dead_pid = 2_000_000_000
    scw.CMUX_REGISTRY.parent.mkdir(parents=True, exist_ok=True)
    scw.CMUX_REGISTRY.write_text(json.dumps({
        "tab-live": {"claude_pid": live_pid, "session_id": "S-LIVE",
                     "cwd": "/x", "transcript_path": "/t/live.jsonl", "ts": 100},
        "tab-dead": {"claude_pid": dead_pid, "session_id": "S-DEAD",
                     "cwd": "/y", "transcript_path": "/t/dead.jsonl", "ts": 100},
        "tab-nosid": {"claude_pid": live_pid, "cwd": "/z", "ts": 100},  # no sid
    }))
    out = scw.load_live_claude_sessions()
    assert set(out.keys()) == {"S-LIVE"}
    assert out["S-LIVE"]["pid"] == live_pid
    assert out["S-LIVE"]["tab_id"] == "tab-live"


def test_load_live_sessions_includes_droid_from_world(tmp_home):
    transcript = tmp_home / ".factory/sessions/-work/droid-session.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text("{}\n")
    scw.WORLD_PATH.parent.mkdir(parents=True, exist_ok=True)
    scw.WORLD_PATH.write_text(json.dumps({"live_sessions": [{
        "session_id": "droid-session",
        "pid": os.getpid(),
        "cwd": "/work",
        "transcript_path": str(transcript),
        "provider": "droid",
        "ts": 200,
    }]}))
    out = scw.load_live_agent_sessions()
    assert out["droid-session"]["provider"] == "droid"
    assert out["droid-session"]["transcript_path"] == str(transcript)


@pytest.mark.parametrize("binding", [
    "unknown", "missing_status", "missing_workspace", "missing_surface", "verified", "dead",
])
def test_load_live_sessions_identity_world_excludes_reused_registry_pids(tmp_home, binding):
    _write_registry(tmp_home, {
        "old": {"claude_pid": os.getpid(), "session_id": "historical",
                "cwd": "/old", "transcript_path": "/history.jsonl", "ts": 100},
    })
    entry = {"session_id": "current", "pid": os.getpid(), "provider": "claude",
             "workspace_id": "workspace", "surface_id": "surface",
             "identity_status": "verified", "transcript_path": "/current.jsonl"}
    if binding == "unknown":
        entry["identity_status"] = "unknown"
    elif binding == "missing_status":
        entry.pop("identity_status")
    elif binding == "missing_workspace":
        entry.pop("workspace_id")
    elif binding == "missing_surface":
        entry.pop("surface_id")
    elif binding == "dead":
        entry["pid"] = 2_000_000_000
    scw.WORLD_PATH.parent.mkdir(parents=True, exist_ok=True)
    scw.WORLD_PATH.write_text(json.dumps({"live_sessions": [
        {"session_id": "historical", "pid": os.getpid(), "identity_status": "unknown"},
        entry,
    ]}))
    assert set(scw.load_live_agent_sessions()) == ({"current"} if binding == "verified" else set())


def test_load_live_sessions_empty_identity_world_does_not_restore_registry(tmp_home):
    _write_registry(tmp_home, {
        "old": {"claude_pid": os.getpid(), "session_id": "historical",
                "transcript_path": "/history.jsonl"},
    })
    scw.WORLD_PATH.parent.mkdir(parents=True, exist_ok=True)
    scw.WORLD_PATH.write_text(json.dumps({
        "live_sessions": [],
        "workspaces": [{"surfaces": [{"identity_status": "unknown"}]}],
    }))
    assert scw.load_live_agent_sessions() == {}


def test_load_live_sessions_dup_keeps_most_recent_ts(tmp_home):
    live_pid = os.getpid()
    scw.CMUX_REGISTRY.parent.mkdir(parents=True, exist_ok=True)
    scw.CMUX_REGISTRY.write_text(json.dumps({
        "tab-old": {"claude_pid": live_pid, "session_id": "S", "cwd": "/old",
                    "transcript_path": "/t/old.jsonl", "ts": 100},
        "tab-new": {"claude_pid": live_pid, "session_id": "S", "cwd": "/new",
                    "transcript_path": "/t/new.jsonl", "ts": 200},
    }))
    out = scw.load_live_claude_sessions()
    # Same session_id; the higher-ts entry wins regardless of iteration order.
    assert out["S"]["cwd"] == "/new"
    assert out["S"]["ts"] == 200


def test_load_live_sessions_dup_reverse_order(tmp_home):
    # newer entry first, older second — the older must NOT clobber the newer.
    live_pid = os.getpid()
    scw.CMUX_REGISTRY.parent.mkdir(parents=True, exist_ok=True)
    scw.CMUX_REGISTRY.write_text(json.dumps({
        "tab-new": {"claude_pid": live_pid, "session_id": "S", "cwd": "/new",
                    "transcript_path": "/t/new.jsonl", "ts": 200},
        "tab-old": {"claude_pid": live_pid, "session_id": "S", "cwd": "/old",
                    "transcript_path": "/t/old.jsonl", "ts": 100},
    }))
    out = scw.load_live_claude_sessions()
    assert out["S"]["cwd"] == "/new"


# ─── load_cron_workers ───────────────────────────────────────────────────────

def test_load_cron_workers_missing(tmp_home):
    assert scw.load_cron_workers() == {}


def test_load_cron_workers_maps_workspace_ref(tmp_home):
    scw.ORCHESTRATOR_REGISTRY.parent.mkdir(parents=True, exist_ok=True)
    scw.ORCHESTRATOR_REGISTRY.write_text(json.dumps({
        "workers": {
            "code-slippage": {"workspace_ref": "workspace:104"},
            "no-ref-worker": {"some": "thing"},  # no workspace_ref → skipped
        }
    }))
    out = scw.load_cron_workers()
    assert out == {"workspace:104": "code-slippage"}


def test_load_cron_workers_no_workers_key(tmp_home):
    scw.ORCHESTRATOR_REGISTRY.parent.mkdir(parents=True, exist_ok=True)
    scw.ORCHESTRATOR_REGISTRY.write_text(json.dumps({"version": 1}))
    assert scw.load_cron_workers() == {}


# ─── text_from_message ───────────────────────────────────────────────────────

def test_text_from_message_str_content():
    assert scw.text_from_message({"content": "hello world"}) == "hello world"


def test_text_from_message_list_content_types():
    msg = {"content": [
        {"type": "text", "text": "first"},
        {"type": "tool_use", "name": "Bash"},
        {"type": "tool_result", "content": "ignored"},
        {"type": "unknown", "x": 1},  # ignored type
        "not-a-dict",                  # skipped non-dict item
    ]}
    assert scw.text_from_message(msg) == "first\n[tool_use:Bash]\n[tool_result]"


def test_text_from_message_tool_use_missing_name():
    msg = {"content": [{"type": "tool_use"}]}
    assert scw.text_from_message(msg) == "[tool_use:?]"


def test_text_from_message_non_dict():
    assert scw.text_from_message("a string") == ""
    assert scw.text_from_message(None) == ""


def test_text_from_message_empty_and_no_content():
    assert scw.text_from_message({}) == ""
    assert scw.text_from_message({"content": []}) == ""
    assert scw.text_from_message({"content": 42}) == ""  # neither str nor list


# ─── truncate ────────────────────────────────────────────────────────────────

def test_truncate_under_limit():
    assert scw.truncate("hello", n=10) == "hello"


def test_truncate_strips_whitespace():
    assert scw.truncate("  hello  ", n=10) == "hello"


def test_truncate_over_limit_adds_ellipsis():
    s = "x" * 50
    out = scw.truncate(s, n=10)
    assert out == "x" * 9 + "…"
    assert len(out) == 10


def test_truncate_empty():
    assert scw.truncate("") == ""
    assert scw.truncate(None) == ""


# ─── acquire_lock / release_lock ─────────────────────────────────────────────

def test_acquire_lock_fresh_writes_pid(tmp_home):
    assert scw.acquire_lock() is True
    assert scw.LOCK_FILE.exists()
    assert scw.LOCK_FILE.read_text().strip() == str(os.getpid())


def test_acquire_lock_blocked_by_live_pid(tmp_home):
    scw.LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    scw.LOCK_FILE.write_text(str(os.getpid()))  # our own pid is alive
    assert scw.acquire_lock() is False


def test_acquire_lock_takes_over_stale(tmp_home):
    scw.LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    scw.LOCK_FILE.write_text("2000000000")  # dead pid
    assert scw.acquire_lock() is True
    assert scw.LOCK_FILE.read_text().strip() == str(os.getpid())


def test_acquire_lock_garbage_contents_taken_over(tmp_home):
    scw.LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    scw.LOCK_FILE.write_text("garbage")  # ValueError on int() → take over
    assert scw.acquire_lock() is True
    assert scw.LOCK_FILE.read_text().strip() == str(os.getpid())


def test_release_lock_unlinks(tmp_home):
    scw.acquire_lock()
    assert scw.LOCK_FILE.exists()
    scw.release_lock()
    assert not scw.LOCK_FILE.exists()


def test_release_lock_missing_is_noop(tmp_home):
    scw.LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    # No file present — must not raise.
    scw.release_lock()


# ─── TranscriptState.read_new ─────────────────────────────────────────────────

def _user_line(text, ts="2026-06-09T10:00:00Z"):
    return json.dumps({"type": "user", "timestamp": ts,
                       "message": {"role": "user", "content": text}})


def _assistant_line(text, ts="2026-06-09T10:00:05Z"):
    return json.dumps({"type": "assistant", "timestamp": ts,
                       "message": {"role": "assistant", "content": text}})


def _tool_turn(role, content, provider="claude", root=False):
    record = {"type": "message" if provider == "droid" else role,
              "message": {"role": role, "content": content}}
    if root:
        record["parentUuid"] = None
    return json.dumps(record) + "\n"


def _append_tool_turn(path, role, content, provider="claude"):
    with path.open("a") as stream:
        stream.write(_tool_turn(role, content, provider))


def _question_call(tool_id="question"):
    return {"type": "tool_use", "id": tool_id, "name": "AskUserQuestion",
            "input": {"questions": [{
                "question": "Which environment should you deploy to?",
                "header": "Environment",
                "options": [{"label": "Staging", "description": "Run checks first."},
                            {"label": "Production", "description": "Release to users."}],
                "multiSelect": False,
            }]}}


@pytest.mark.parametrize("provider", ["claude", "droid"])
def test_guidance_real_human_and_long_response_survive_tool_results(tmp_path, provider):
    path = tmp_path / "guidance.jsonl"
    response = "Summary: " + "checks pass. " * 800 + "Which environment should you deploy to?"
    path.write_text(
        _tool_turn("user", "<system-reminder>Environment details.</system-reminder>",
                   provider, root=True)
        + _tool_turn("user", "Build a release.", provider)
        + _tool_turn("assistant", [{"type": "text", "text": response},
                                  {"type": "tool_use", "id": "check", "name": "Bash"}],
                     provider)
        + _tool_turn("user", [{"type": "tool_result", "tool_use_id": "check",
                               "content": "Success"}], provider))
    state = scw.TranscriptState(path, "/cwd", provider=provider)
    state.read_new()
    guidance = state.to_dict(scw.utc_now())["guidance_context"]
    assert guidance["initial_request"]["text"] == "Build a release."
    assert guidance["last_request"]["text"] == "Build a release."
    assert guidance["last_response"]["text"].startswith("Summary:")
    assert guidance["last_response"]["text"].endswith("Which environment should you deploy to?")
    assert guidance["last_response"]["truncated"] is True
    assert len(guidance["last_response"]["text"]) <= 6000
    assert "[tool_use:" not in guidance["last_response"]["text"]
    assert state.last_user["text"] == "[tool_result]"
    assert len(state.last_assistant["text"]) == scw.TEXT_TRUNCATE


def test_guidance_questions_wait_for_matching_result_and_version_is_stable(tmp_path):
    path = tmp_path / "guidance.jsonl"
    path.write_text(_tool_turn("user", "Release.", root=True)
                    + _tool_turn("assistant", [
                        {"type": "text", "text": "You need to choose a target."},
                        _question_call(),
                        {"type": "tool_use", "id": "check", "name": "Bash"}]))
    state = scw.TranscriptState(path, "/cwd")
    state.read_new()
    initial = state.to_dict(scw.utc_now())["guidance_context"]
    assert initial["pending_questions"] == [
        {**_question_call()["input"]["questions"][0], "tool_use_id": "question",
         "truncated": False}]
    assert initial["last_response"]["text"] == "You need to choose a target."
    reloaded = scw.TranscriptState(path, "/cwd")
    reloaded.read_new()
    assert reloaded.to_dict(scw.utc_now())["guidance_context"] == initial
    assert state.read_new() is False
    assert state.to_dict(scw.utc_now() + timedelta(days=1))["guidance_context"] == initial
    _append_tool_turn(path, "user", [{"type": "tool_result", "tool_use_id": "check"}])
    state.read_new()
    pending = state.to_dict(scw.utc_now())["guidance_context"]
    assert pending["pending_questions"] == initial["pending_questions"]
    assert pending["source_version"] != initial["source_version"]
    _append_tool_turn(path, "user", [{"type": "tool_result", "tool_use_id": "question",
                                    "content": "Staging"}])
    state.read_new()
    answered = state.to_dict(scw.utc_now())["guidance_context"]
    assert answered["pending_questions"] == []
    assert answered["last_request"]["text"] == "Release."
    assert state.pending_tool_use() is False
    _append_tool_turn(path, "user", [{"type": "text", "text": "Deploy staging."},
                                   {"type": "tool_result", "tool_use_id": "other"}])
    state.read_new()
    latest = state.to_dict(scw.utc_now())["guidance_context"]
    assert latest["initial_request"]["text"] == "Release."
    assert latest["last_request"]["text"] == "Deploy staging."
    assert latest["source_version"] != answered["source_version"]


def test_guidance_answered_questions_leave_ordinary_tool_pending(tmp_path):
    path = tmp_path / "guidance.jsonl"
    call = _question_call()
    second = {**call["input"]["questions"][0], "question": "When should you deploy?"}
    call["input"]["questions"].append(second)
    path.write_text(_tool_turn("user", "Release.", root=True)
                    + _tool_turn("assistant", [
                        call, {"type": "tool_use", "id": "check", "name": "Bash"}]))
    state = scw.TranscriptState(path, "/cwd")
    state.read_new()
    initial = state.to_dict(scw.utc_now())["guidance_context"]
    assert [question["tool_use_id"] for question in initial["pending_questions"]] == [
        "question", "question"]
    assert initial["pending_questions"][1]["question"] == "When should you deploy?"
    _append_tool_turn(path, "user", [{"type": "tool_result", "tool_use_id": "question",
                                    "content": "Staging, now."}])
    state.read_new()
    answered = state.to_dict(scw.utc_now())["guidance_context"]
    assert answered["pending_questions"] == []
    assert state.pending_tool_use() is True
    assert state.pending_tools == {"check"}
    assert answered["source_version"] != initial["source_version"]


@pytest.mark.parametrize("secondary_identity", [None, "separate-session-id"])
def test_guidance_preserves_completion_after_human_followup(tmp_path, secondary_identity):
    path = tmp_path / "guidance.jsonl"
    completion = "Completed checks. " * 500 + "Do you want to review the release?"
    records = [
        {"type": "user", "sessionId": "guidance", "parentUuid": None,
         "timestamp": "2026-09-19T10:00:00Z", "message": {"content": "Prepare release."}},
        {"type": "assistant", "sessionId": "guidance",
         "timestamp": "2026-09-19T10:05:00Z",
         "message": {"content": [{"type": "text", "text": completion}, _question_call()]}},
        {"type": "user", "sessionId": "guidance", "timestamp": "2026-09-19T10:06:00Z",
         "message": {"content": [{"type": "tool_result", "tool_use_id": "question",
                                  "content": "Review it."}]}},
    ]
    if secondary_identity is not None:
        for record in records[1:]:
            record["session_id"] = secondary_identity
    path.write_text("".join(json.dumps(record) + "\n" for record in records))
    state = scw.TranscriptState(path, "/cwd")
    state.read_new()
    before = state.guidance_context()
    assert before["last_response"]["text"].endswith("Do you want to review the release?")
    with path.open("a") as stream:
        stream.write(_user_line("Show me the final changes.", "2026-09-19T10:07:00Z") + "\n")
    state.read_new()
    after = state.guidance_context()
    assert after["last_response"] == before["last_response"]
    assert after["last_request"]["text"] == "Show me the final changes."
    assert after["last_request"]["ts"] > after["last_response"]["ts"]
    assert after["source_version"] != before["source_version"]
    assert after["pending_questions"] == []
    assert state.pending_tool_use() is False


def test_guidance_rejects_wrong_primary_identity_even_if_secondary_matches(tmp_path):
    path = tmp_path / "guidance.jsonl"
    path.write_text(_tool_turn("user", "Release.", root=True)
                    + _tool_turn("assistant", [_question_call()])
                    + json.dumps({"type": "assistant", "sessionId": "wrong",
                                  "session_id": "guidance",
                                  "message": {"content": "Wrong session."}}) + "\n")
    state = scw.TranscriptState(path, "/cwd")
    state.read_new()
    assert state.guidance_context()["pending_questions"] == []
    assert state.guidance_context()["last_response"] is None


@pytest.mark.parametrize("replacement", ["truncate", "rotate", "corrupt", "identity"])
def test_guidance_pending_questions_reset_with_lost_history(tmp_path, replacement):
    path = tmp_path / "guidance.jsonl"
    path.write_text(_tool_turn("user", "Release.", root=True)
                    + _tool_turn("assistant", [_question_call()]))
    state = scw.TranscriptState(path, "/cwd")
    state.read_new()
    before = state.to_dict(scw.utc_now())["guidance_context"]["source_version"]
    if replacement == "truncate":
        path.write_text("")
    elif replacement == "rotate":
        path.rename(tmp_path / "old.jsonl")
        path.write_text(_tool_turn("user", "New request.", root=True))
    else:
        with path.open("a") as stream:
            stream.write("{bad\n" if replacement == "corrupt" else json.dumps({
                "type": "assistant", "sessionId": "wrong",
                "message": {"content": [_question_call("wrong")]}}) + "\n")
    assert state.read_new() is True
    guidance = state.to_dict(scw.utc_now())["guidance_context"]
    assert guidance["pending_questions"] == []
    assert guidance["source_version"] != before
    if replacement == "truncate":
        assert guidance["initial_request"] is None
        assert guidance["last_response"] is None
    elif replacement == "rotate":
        assert guidance["initial_request"]["text"] == "New request."


def test_guidance_full_text_hash_changes_even_when_bounded_display_matches(tmp_path):
    path = tmp_path / "guidance.jsonl"
    state = scw.TranscriptState(path, "/cwd")
    snapshots = []
    for middle in ("first", "other"):
        _append_tool_turn(path, "assistant", "H" * 6000 + middle + "T" * 6000)
        state.read_new()
        snapshots.append(state.to_dict(scw.utc_now())["guidance_context"])
    assert snapshots[0]["last_response"] == snapshots[1]["last_response"]
    assert snapshots[0]["source_version"] != snapshots[1]["source_version"]


def test_guidance_question_fields_and_count_are_bounded(tmp_path):
    path = tmp_path / "guidance.jsonl"
    call = _question_call()
    call["input"]["questions"][0]["question"] = "Start " + "Q" * 12000 + " end?"
    path.write_text(_tool_turn("assistant", [call]))
    state = scw.TranscriptState(path, "/cwd")
    state.read_new()
    pending = state.to_dict(scw.utc_now())["guidance_context"]["pending_questions"][0]
    assert pending["truncated"] is True
    assert len(pending["question"]) <= 6000
    assert pending["question"].endswith(" end?")


def test_guidance_ignores_meta_but_preserves_human_text_and_response_timestamp(tmp_path):
    path = tmp_path / "guidance.jsonl"
    path.write_text(json.dumps({
        "type": "user", "isMeta": True,
        "message": {"content": "Injected instructions."}}) + "\n"
        + _user_line("<system-reminder>System facts.</system-reminder>Build it.") + "\n"
        + _assistant_line("Result. " * 130 + "Do you approve?") + "\n")
    state = scw.TranscriptState(path, "/cwd")
    state.read_new()
    original = state.to_dict(scw.utc_now())["guidance_context"]
    assert original["initial_request"]["text"] == "Build it."
    assert original["last_response"]["ts"] == "2026-06-09T10:00:05Z"
    assert original["last_response"]["truncated"] is False
    assert original["last_response"]["text"].endswith("Do you approve?")
    _append_tool_turn(path, "assistant", [_question_call()])
    state.read_new()
    assert state.to_dict(scw.utc_now())["guidance_context"]["last_response"] == original["last_response"]


def test_guidance_question_display_caps_do_not_hide_full_input_changes(tmp_path):
    path = tmp_path / "guidance.jsonl"
    state = scw.TranscriptState(path, "/cwd")
    versions = []
    for middle in ("first", "other"):
        call = _question_call()
        question = call["input"]["questions"][0]
        question["question"] = "H" * 6000 + middle + "T" * 6000
        question["options"] *= 10
        call["input"]["questions"] *= 10
        _append_tool_turn(path, "assistant", [call])
        state.read_new()
        context = state.to_dict(scw.utc_now())["guidance_context"]
        versions.append(context["source_version"])
        assert len(context["pending_questions"]) == scw.GUIDANCE_QUESTION_LIMIT
        assert len(context["pending_questions"][0]["options"]) == scw.GUIDANCE_OPTION_LIMIT
    assert versions[0] != versions[1]
    for index in range(20):
        _append_tool_turn(path, "assistant", [_question_call(str(index))])
    state.read_new()
    assert len(state.pending_questions) == scw.GUIDANCE_PENDING_LIMIT
    assert len(state.question_fingerprints) == scw.GUIDANCE_PENDING_LIMIT


def test_guidance_transcript_state_requires_complete_read_and_refreshes_same_size(tmp_path):
    path = tmp_path / "guidance.jsonl"
    original = _tool_turn("user", "Start.", root=True)
    path.write_text(original)
    state = scw.TranscriptState(path, "/cwd")
    state.read_new()
    stat = path.stat()
    assert state.to_dict(scw.utc_now())["transcript_state"] == {
        "device": stat.st_dev, "inode": stat.st_ino,
        "size_read": stat.st_size, "mtime_ns": stat.st_mtime_ns}
    path.write_text(original.replace("Start.", "Later."))
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1000000))
    assert state.read_new() is True
    assert state.to_dict(scw.utc_now())["guidance_context"]["last_request"]["text"] == "Later."
    result = _tool_turn("assistant", "Done.")
    with path.open("a") as stream:
        stream.write(result[:10])
    state.read_new()
    assert state.to_dict(scw.utc_now())["transcript_state"]["mtime_ns"] is None
    with path.open("a") as stream:
        stream.write(result[10:])
    state.read_new()
    assert state.to_dict(scw.utc_now())["transcript_state"]["mtime_ns"] == path.stat().st_mtime_ns


def test_guidance_transcript_state_rejects_append_during_scan(tmp_path, monkeypatch):
    path = tmp_path / "guidance.jsonl"
    path.write_text(_tool_turn("user", "Start.", root=True))
    original_consume = scw.TranscriptState.consume_line
    appended = False

    def consume_and_append(state, line):
        nonlocal appended
        result = original_consume(state, line)
        if not appended:
            appended = True
            _append_tool_turn(path, "assistant", "Appended while reading.")
        return result

    monkeypatch.setattr(scw.TranscriptState, "consume_line", consume_and_append)
    state = scw.TranscriptState(path, "/cwd")
    state.read_new()
    assert state.to_dict(scw.utc_now())["transcript_state"]["mtime_ns"] is None
    state.read_new()
    assert state.to_dict(scw.utc_now())["transcript_state"]["mtime_ns"] == path.stat().st_mtime_ns


@pytest.mark.parametrize("provider", ["claude", "droid"])
def test_pending_tools_mixed_text_parallel_and_matching_results(tmp_path, provider):
    path = tmp_path / "tools.jsonl"
    path.write_text(_tool_turn("user", "Start.", provider, root=True))
    state = scw.TranscriptState(path, "/cwd", provider=provider)
    state.read_new()
    assert state.to_dict(scw.utc_now())["pending_tool_use"] is False
    _append_tool_turn(path, "assistant", [
        {"type": "text", "text": "Running checks. " * 100},
        {"type": "tool_use", "id": "a", "name": "Bash"},
        {"type": "tool_use", "id": "b", "name": "Bash"},
    ], provider)
    state.read_new()
    assert "[tool_use:" not in state.last_assistant["text"]
    assert state.to_dict(scw.utc_now())["pending_tool_use"] is True
    for tool_id in ("unrelated", "a"):
        _append_tool_turn(path, "user", [
            {"type": "tool_result", "tool_use_id": tool_id, "content": "done"},
        ], provider)
        state.read_new()
        assert state.to_dict(scw.utc_now())["pending_tool_use"] is True
    _append_tool_turn(path, "user", [
        {"type": "tool_result", "tool_use_id": "b", "is_error": True, "content": "failed"},
    ], provider)
    state.read_new()
    assert state.to_dict(scw.utc_now())["pending_tool_use"] is False


def test_pending_tools_survive_display_window_trimming(tmp_path):
    path = tmp_path / "tools.jsonl"
    path.write_text(_tool_turn("user", "Start.", root=True)
                    + _tool_turn("assistant", [{"type": "tool_use", "id": "a", "name": "Bash"}])
                    + "".join(_assistant_line("Still working.") + "\n" for _ in range(40)))
    state = scw.TranscriptState(path, "/cwd")
    state.read_new()
    assert state.to_dict(scw.utc_now())["pending_tool_use"] is True


def test_pending_tools_partial_lines_recover_without_losing_results(tmp_path):
    path = tmp_path / "tools.jsonl"
    path.write_text(_tool_turn("user", "Start.", root=True)
                    + _tool_turn("assistant", [{"type": "tool_use", "id": "a", "name": "Bash"}]))
    state = scw.TranscriptState(path, "/cwd")
    state.read_new()
    result = _tool_turn("user", [{"type": "tool_result", "tool_use_id": "a"}])
    with path.open("a") as stream:
        stream.write(result[:20])
    assert state.read_new() is True
    assert state.to_dict(scw.utc_now())["pending_tool_use"] is None
    with path.open("a") as stream:
        stream.write(result[20:])
    assert state.read_new() is True
    assert state.to_dict(scw.utc_now())["pending_tool_use"] is False


@pytest.mark.parametrize("prefix", ["", "{broken json\n"])
def test_pending_tools_tail_or_corrupt_history_never_claims_no_pending(tmp_path, prefix):
    path = tmp_path / "tools.jsonl"
    path.write_text(prefix + _assistant_line("Wrapping up.") + "\n")
    state = scw.TranscriptState(path, "/cwd")
    state.read_new()
    assert state.to_dict(scw.utc_now())["pending_tool_use"] is None
    _append_tool_turn(path, "assistant", [{"type": "tool_use", "id": "a", "name": "Bash"}])
    state.read_new()
    assert state.to_dict(scw.utc_now())["pending_tool_use"] is True
    _append_tool_turn(path, "user", [{"type": "tool_result", "tool_use_id": "a"}])
    state.read_new()
    assert state.to_dict(scw.utc_now())["pending_tool_use"] is None


@pytest.mark.parametrize("block", [
    {"type": "tool_use", "name": "Bash"},
    {"type": "tool_use", "id": ""},
    {"type": "tool_result"},
])
def test_pending_tools_missing_identifiers_remain_unknown(tmp_path, block):
    path = tmp_path / "tools.jsonl"
    role = "assistant" if block["type"] == "tool_use" else "user"
    path.write_text(_tool_turn("user", "Start.", root=True) + _tool_turn(role, [block]))
    state = scw.TranscriptState(path, "/cwd")
    state.read_new()
    assert state.to_dict(scw.utc_now())["pending_tool_use"] is None


def test_pending_tools_reading_from_offset_does_not_assume_complete_history(tmp_path):
    path = tmp_path / "tools.jsonl"
    prefix = _tool_turn("assistant", [{"type": "tool_use", "id": "a", "name": "Bash"}])
    path.write_text(prefix + _tool_turn("assistant", "Summary.", root=True))
    state = scw.TranscriptState(path, "/cwd")
    state.pos = len(prefix.encode())
    state.read_new()
    assert state.to_dict(scw.utc_now())["pending_tool_use"] is None


def test_pending_tools_session_start_establishes_complete_droid_history(tmp_path):
    path = tmp_path / "tools.jsonl"
    path.write_text(json.dumps({"type": "session_start", "id": "tools"}) + "\n"
                    + _tool_turn("assistant", "Ready.", provider="droid"))
    state = scw.TranscriptState(path, "/cwd", provider="droid")
    state.read_new()
    assert state.to_dict(scw.utc_now())["pending_tool_use"] is False


def test_pending_tools_corruption_after_known_call_loses_certainty(tmp_path):
    path = tmp_path / "tools.jsonl"
    path.write_text(_tool_turn("user", "Start.", root=True)
                    + _tool_turn("assistant", [{"type": "tool_use", "id": "a", "name": "Bash"}]))
    state = scw.TranscriptState(path, "/cwd")
    state.read_new()
    with path.open("a") as stream:
        stream.write("{broken\n")
    assert state.read_new() is True
    assert state.to_dict(scw.utc_now())["pending_tool_use"] is None


def test_pending_tools_partial_first_record_and_rotation_remain_unknown(tmp_path):
    path = tmp_path / "tools.jsonl"
    record = _tool_turn("assistant", [{"type": "tool_use", "id": "a", "name": "Bash"}])
    path.write_text(record[:10])
    state = scw.TranscriptState(path, "/cwd")
    state.read_new()
    assert state.to_dict(scw.utc_now())["pending_tool_use"] is None
    with path.open("a") as stream:
        stream.write(record[10:])
    state.read_new()
    assert state.to_dict(scw.utc_now())["pending_tool_use"] is True
    path.write_text("")
    assert state.read_new() is True
    assert state.to_dict(scw.utc_now())["pending_tool_use"] is None


def test_pending_tools_read_failure_loses_certainty(tmp_path, tmp_home, monkeypatch):
    path = tmp_path / "tools.jsonl"
    path.write_text(_tool_turn("user", "Start.", root=True))
    state = scw.TranscriptState(path, "/cwd")
    state.read_new()
    _append_tool_turn(path, "assistant", "More output.")
    real_open = open

    def fail_transcript_read(filename, *args, **kwargs):
        if filename == path:
            raise OSError("Cannot read transcript.")
        return real_open(filename, *args, **kwargs)

    monkeypatch.setattr("builtins.open", fail_transcript_read)
    assert state.read_new() is True
    assert state.to_dict(scw.utc_now())["pending_tool_use"] is None


def test_pending_tools_full_history_scan_is_streamed_then_incremental(tmp_path, monkeypatch):
    path = tmp_path / "long-session.jsonl"
    path.write_text(
        json.dumps({"type": "file-history-snapshot", "snapshot": {}}) + "\n"
        + _tool_turn("user", "Start.", root=True)
        + _tool_turn("assistant", [{"type": "tool_use", "id": "old", "name": "Bash"}])
        + "".join(_assistant_line("Progress. " * 150) + "\n" for _ in range(200))
        + _tool_turn("user", [{"type": "tool_result", "tool_use_id": "old"}]))
    assert path.stat().st_size > 131072
    real_open = open
    readers = []

    def tracked_open(filename, *args, **kwargs):
        stream = real_open(filename, *args, **kwargs)
        if filename != path or args != ("rb",):
            return stream
        reader = MagicMock(wraps=stream)
        reader.__enter__.return_value = reader
        reader.__exit__.side_effect = lambda *_: stream.close()
        readers.append(reader)
        return reader

    monkeypatch.setattr("builtins.open", tracked_open)
    state = scw.TranscriptState(path, "/cwd")
    state.read_new()
    assert state.to_dict(scw.utc_now())["pending_tool_use"] is False
    assert len(state.turns) <= scw.TURNS_PER_SESSION * 2
    readers[0].read.assert_not_called()
    assert readers[0].readline.call_count > 200
    offset = state.pos
    _append_tool_turn(path, "assistant", [{"type": "tool_use", "id": "new", "name": "Bash"}])
    state.read_new()
    assert state.to_dict(scw.utc_now())["pending_tool_use"] is True
    readers[1].seek.assert_called_once_with(offset)
    assert readers[1].readline.call_count == 2
    _append_tool_turn(path, "user", [{"type": "tool_result", "tool_use_id": "new"}])
    state.read_new()
    assert state.to_dict(scw.utc_now())["pending_tool_use"] is False
    assert readers[2].readline.call_count == 2
    assert state.read_new() is False
    assert len(readers) == 3


def test_pending_tools_root_after_incremental_file_metadata_is_known(tmp_path):
    path = tmp_path / "tools.jsonl"
    path.write_text(json.dumps({"type": "file-history-snapshot"}) + "\n")
    state = scw.TranscriptState(path, "/cwd")
    state.read_new()
    assert state.to_dict(scw.utc_now())["pending_tool_use"] is None
    with path.open("a") as stream:
        stream.write(_tool_turn("user", "Start.", root=True))
    state.read_new()
    assert state.to_dict(scw.utc_now())["pending_tool_use"] is False


@pytest.mark.parametrize("root_type", ["user", "attachment"])
def test_pending_tools_claude_metadata_preamble_and_root_attachment(tmp_path, root_type):
    path = tmp_path / "real-shape.jsonl"
    preamble = [
        {"type": kind} for kind in (
            "last-prompt", "mode", "permission-mode", "atis-latch", "ai-title",
            "pr-link", "file-history-snapshot")
    ]
    root = {"type": root_type, "uuid": "root", "parentUuid": None, "isSidechain": False}
    if root_type == "user":
        root["message"] = {"role": "user", "content": "Start."}
    path.write_text("".join(json.dumps(record) + "\n" for record in [*preamble, root]))
    state = scw.TranscriptState(path, "/cwd")
    state.read_new()
    assert state.to_dict(scw.utc_now())["pending_tool_use"] is False
    for content in (
            [{"type": "thinking", "thinking": "Fixture reasoning."}],
            [{"type": "text", "text": "Checking."}],
            [{"type": "tool_use", "id": "call", "name": "Bash"}]):
        with path.open("a") as stream:
            stream.write(json.dumps({
                "type": "assistant", "uuid": f"block-{content[0]['type']}",
                "parentUuid": "root", "message": {
                    "id": "shared-message-id", "role": "assistant",
                    "stop_reason": "tool_use", "content": content,
                },
            }) + "\n")
    state.read_new()
    assert state.to_dict(scw.utc_now())["pending_tool_use"] is True
    _append_tool_turn(path, "user", [{"type": "tool_result", "tool_use_id": "call"}])
    _append_tool_turn(path, "assistant", [{"type": "text", "text": "Finished."}])
    state.read_new()
    assert state.to_dict(scw.utc_now())["pending_tool_use"] is False


@pytest.mark.parametrize("metadata", [
    {"type": "unknown-metadata"},
    {"type": "mode", "uuid": "not-just-metadata"},
    {"type": "mode", "message": {"content": "Not metadata."}},
])
def test_pending_tools_unknown_prefix_does_not_gain_root_certainty(tmp_path, metadata):
    path = tmp_path / "tools.jsonl"
    path.write_text(json.dumps(metadata) + "\n" + _tool_turn("user", "Start.", root=True))
    state = scw.TranscriptState(path, "/cwd")
    state.read_new()
    assert state.to_dict(scw.utc_now())["pending_tool_use"] is None


def test_pending_tools_replacement_and_truncation_reset_history(tmp_path):
    path = tmp_path / "tools.jsonl"
    path.write_text(_tool_turn("user", "Start.", root=True))
    state = scw.TranscriptState(path, "/cwd")
    state.read_new()
    assert state.to_dict(scw.utc_now())["pending_tool_use"] is False
    replacement = tmp_path / "replacement.jsonl"
    replacement.write_text(
        _tool_turn("assistant", [{"type": "tool_use", "id": "new", "name": "Bash"}])
        + _assistant_line("Still running. " * 100) + "\n")
    assert replacement.stat().st_size > state.pos
    replacement.replace(path)
    state.read_new()
    assert state.to_dict(scw.utc_now())["pending_tool_use"] is True
    path.write_text(_assistant_line("A partial history.") + "\n")
    state.read_new()
    assert state.to_dict(scw.utc_now())["pending_tool_use"] is None


def test_read_new_missing_file_returns_false(tmp_path):
    st = scw.TranscriptState(tmp_path / "nope.jsonl", "/cwd")
    assert st.read_new() is False


def test_read_new_picks_up_turns_and_sets_last(tmp_path):
    f = tmp_path / "sess.jsonl"
    f.write_text(_user_line("hi there") + "\n" + _assistant_line("hello back") + "\n")
    st = scw.TranscriptState(f, "/cwd")
    assert st.read_new() is True
    assert st.last_user["text"] == "hi there"
    assert st.last_user["role"] == "user"
    assert st.last_assistant["text"] == "hello back"
    assert len(st.turns) == 2
    assert st.session_id == "sess"


def test_read_new_parses_droid_message_records(tmp_path):
    f = tmp_path / "droid.jsonl"
    f.write_text(json.dumps({
        "type": "message",
        "timestamp": "2026-07-12T10:00:00Z",
        "message": {"role": "user", "content": "droid input"},
    }) + "\n")
    st = scw.TranscriptState(f, "/cwd", provider="droid")
    assert st.read_new() is True
    assert st.last_user["text"] == "droid input"
    assert st.to_dict(scw.utc_now())["provider"] == "droid"
    assert len(st.turns) == 1
    assert st.session_id == "droid"


def test_read_new_incremental_only_reads_new_bytes(tmp_path):
    f = tmp_path / "sess.jsonl"
    f.write_text(_user_line("first") + "\n")
    st = scw.TranscriptState(f, "/cwd")
    assert st.read_new() is True
    assert len(st.turns) == 1
    # No new bytes → no change.
    assert st.read_new() is False
    # Append a second turn → only the new turn is parsed.
    with open(f, "a") as fh:
        fh.write(_assistant_line("second") + "\n")
    assert st.read_new() is True
    assert len(st.turns) == 2
    assert st.last_assistant["text"] == "second"


def test_read_new_queue_operation_increments_pending(tmp_path):
    f = tmp_path / "sess.jsonl"
    f.write_text(
        _user_line("do a thing") + "\n"
        + json.dumps({"type": "queue-operation"}) + "\n"
        + json.dumps({"type": "queue-operation"}) + "\n"
    )
    st = scw.TranscriptState(f, "/cwd")
    st.read_new()
    assert st.queue_pending == 2


def test_read_new_user_turn_resets_queue_pending(tmp_path):
    f = tmp_path / "sess.jsonl"
    f.write_text(json.dumps({"type": "queue-operation"}) + "\n")
    st = scw.TranscriptState(f, "/cwd")
    st.read_new()
    assert st.queue_pending == 1
    with open(f, "a") as fh:
        fh.write(_user_line("new input") + "\n")
    st.read_new()
    assert st.queue_pending == 0  # a real user turn clears the queue


def test_read_new_skips_blank_and_bad_json(tmp_path):
    f = tmp_path / "sess.jsonl"
    f.write_text(
        "\n"                                # blank
        + "   \n"                           # whitespace
        + "{ not valid json\n"              # bad json
        + _user_line("real") + "\n"
    )
    st = scw.TranscriptState(f, "/cwd")
    assert st.read_new() is True
    assert len(st.turns) == 1
    assert st.last_user["text"] == "real"


def test_read_new_skips_empty_text_turn(tmp_path):
    f = tmp_path / "sess.jsonl"
    # A user turn whose content yields no text → skipped (not counted).
    f.write_text(json.dumps({"type": "user", "timestamp": "t",
                             "message": {"role": "user", "content": []}}) + "\n")
    st = scw.TranscriptState(f, "/cwd")
    assert st.read_new() is False
    assert st.turns == []


def test_read_new_ignores_non_user_assistant_types(tmp_path):
    f = tmp_path / "sess.jsonl"
    f.write_text(json.dumps({"type": "summary", "message": {"content": "x"}}) + "\n")
    st = scw.TranscriptState(f, "/cwd")
    assert st.read_new() is False
    assert st.turns == []


def test_read_new_role_falls_back_to_type(tmp_path):
    f = tmp_path / "sess.jsonl"
    # message has no role → falls back to top-level type "user".
    f.write_text(json.dumps({"type": "user", "timestamp": "t",
                             "message": {"content": "no role here"}}) + "\n")
    st = scw.TranscriptState(f, "/cwd")
    st.read_new()
    assert st.last_user["role"] == "user"


def test_read_new_truncation_rotation_resets(tmp_path):
    f = tmp_path / "sess.jsonl"
    f.write_text(_user_line("first") + "\n" + _assistant_line("second") + "\n")
    st = scw.TranscriptState(f, "/cwd")
    st.read_new()
    assert st.pos > 0
    assert len(st.turns) == 2
    # Rotate: file shrinks below current pos → pos resets, turns clear.
    f.write_text(_user_line("fresh") + "\n")
    assert st.read_new() is True
    assert len(st.turns) == 1
    assert st.last_user["text"] == "fresh"
    assert st.last_assistant is None  # cleared on rotation


def test_read_new_trims_window_when_too_long(tmp_path):
    f = tmp_path / "sess.jsonl"
    n = scw.TURNS_PER_SESSION * 4 + 5
    lines = "".join(_user_line(f"msg{i}") + "\n" for i in range(n))
    f.write_text(lines)
    st = scw.TranscriptState(f, "/cwd")
    st.read_new()
    # Window trimmed down to TURNS_PER_SESSION * 2.
    assert len(st.turns) == scw.TURNS_PER_SESSION * 2
    # Most recent turns are kept.
    assert st.turns[-1]["text"] == f"msg{n - 1}"


def test_read_new_handles_ts_field_alias(tmp_path):
    f = tmp_path / "sess.jsonl"
    # uses "ts" instead of "timestamp"
    f.write_text(json.dumps({"type": "assistant", "ts": "2026-06-09T11:00:00Z",
                             "message": {"role": "assistant", "content": "via ts"}}) + "\n")
    st = scw.TranscriptState(f, "/cwd")
    st.read_new()
    assert st.last_assistant["ts"] == "2026-06-09T11:00:00Z"


def test_read_new_open_oserror_logged(tmp_path, tmp_home, monkeypatch):
    """If the open()/read() on the transcript raises OSError after stat()
    succeeds, read_new logs a warn and returns False (lines 230-232)."""
    f = tmp_path / "sess.jsonl"
    f.write_text(_user_line("first") + "\n")
    st = scw.TranscriptState(f, "/cwd")

    real_open = open

    def boom_open(path, *a, **k):
        # The module reads via builtin open(self.path, "rb"); make that raise,
        # but let the .stat() (which uses Path.stat) succeed normally.
        if str(path) == str(f) and "rb" in a:
            raise OSError("disk gone")
        return real_open(path, *a, **k)

    monkeypatch.setattr("builtins.open", boom_open)
    assert st.read_new() is False
    log_out = scw.LOG_DIR / "session-context-watcher.out"
    assert "read sess.jsonl" in log_out.read_text()


# ─── TranscriptState.to_dict ──────────────────────────────────────────────────

def test_to_dict_user_unanswered_when_last_is_user(tmp_path):
    f = tmp_path / "sess.jsonl"
    f.write_text(_assistant_line("a reply") + "\n" + _user_line("a question") + "\n")
    st = scw.TranscriptState(f, "/cwd")
    st.read_new()
    d = st.to_dict(scw.utc_now())
    assert d["user_unanswered"] is True
    assert d["recent_turns"][-1]["role"] == "user"


def test_to_dict_answered_when_last_is_assistant(tmp_path):
    f = tmp_path / "sess.jsonl"
    f.write_text(_user_line("q") + "\n" + _assistant_line("a") + "\n")
    st = scw.TranscriptState(f, "/cwd")
    st.read_new()
    d = st.to_dict(scw.utc_now())
    assert d["user_unanswered"] is False


def test_to_dict_user_unanswered_when_queue_pending(tmp_path):
    f = tmp_path / "sess.jsonl"
    # last turn is assistant, but a queue-op after it means input is pending.
    f.write_text(
        _user_line("q") + "\n"
        + _assistant_line("a") + "\n"
        + json.dumps({"type": "queue-operation"}) + "\n"
    )
    st = scw.TranscriptState(f, "/cwd")
    st.read_new()
    assert st.queue_pending == 1
    d = st.to_dict(scw.utc_now())
    assert d["user_unanswered"] is True
    assert d["queue_pending"] == 1


def test_to_dict_empty_turns_not_unanswered(tmp_path):
    f = tmp_path / "empty.jsonl"
    f.write_text("")
    st = scw.TranscriptState(f, "/cwd")
    st.read_new()
    d = st.to_dict(scw.utc_now())
    assert d["user_unanswered"] is False
    assert d["recent_turns"] == []


def test_to_dict_age_and_last_modified_shape(tmp_path):
    f = tmp_path / "sess.jsonl"
    f.write_text(_user_line("hi") + "\n")
    st = scw.TranscriptState(f, "/cwd")
    st.read_new()
    now = scw.utc_now()
    d = st.to_dict(now)
    assert isinstance(d["age_sec"], int)
    assert d["age_sec"] >= 0
    # last_modified is an ISO-Z string.
    assert d["last_modified"].endswith("Z")
    assert d["transcript_path"] == str(f)
    assert d["cwd"] == "/cwd"


def test_to_dict_age_none_when_no_mtime(tmp_path):
    # A state that never read a file has mtime 0.0 → age_sec is None.
    st = scw.TranscriptState(tmp_path / "x.jsonl", "/cwd")
    d = st.to_dict(scw.utc_now())
    assert d["age_sec"] is None


def test_to_dict_recent_turns_capped(tmp_path):
    f = tmp_path / "sess.jsonl"
    lines = "".join(_user_line(f"m{i}") + "\n" for i in range(scw.TURNS_PER_SESSION + 3))
    f.write_text(lines)
    st = scw.TranscriptState(f, "/cwd")
    st.read_new()
    d = st.to_dict(scw.utc_now())
    assert len(d["recent_turns"]) == scw.TURNS_PER_SESSION


# ─── Watcher.find_active_transcripts ─────────────────────────────────────────

def _write_registry(tmp_home, entries):
    scw.CMUX_REGISTRY.parent.mkdir(parents=True, exist_ok=True)
    scw.CMUX_REGISTRY.write_text(json.dumps(entries))


def test_find_active_transcripts_recent_returned(tmp_home, tmp_path):
    tfile = tmp_path / "live.jsonl"
    tfile.write_text(_user_line("hi") + "\n")
    _write_registry(tmp_home, {
        "tab-1": {"claude_pid": os.getpid(), "session_id": "S1", "cwd": "/proj",
                  "transcript_path": str(tfile), "ts": 1},
    })
    w = scw.Watcher()
    try:
        cutoff = (scw.utc_now() - timedelta(hours=scw.ACTIVITY_HOURS)).timestamp()
        out = w.find_active_transcripts(cutoff)
        assert len(out) == 1
        assert out[0]["path"] == tfile
        assert out[0]["is_cron"] is False
        assert out[0]["cron_label"] is None
        assert out[0]["pid"] == os.getpid()
    finally:
        w.kq.close()


def test_find_active_transcripts_old_mtime_retained(tmp_home, tmp_path):
    tfile = tmp_path / "stale.jsonl"
    tfile.write_text(_user_line("old") + "\n")
    # Backdate mtime well past the activity window.
    old = time.time() - (scw.ACTIVITY_HOURS + 5) * 3600
    os.utime(tfile, (old, old))
    _write_registry(tmp_home, {
        "tab-1": {"claude_pid": os.getpid(), "session_id": "S1", "cwd": "/proj",
                  "transcript_path": str(tfile), "ts": 1},
    })
    w = scw.Watcher()
    try:
        cutoff = (scw.utc_now() - timedelta(hours=scw.ACTIVITY_HOURS)).timestamp()
        assert [entry["path"] for entry in w.find_active_transcripts(cutoff)] == [tfile]
    finally:
        w.kq.close()


def test_find_active_transcripts_missing_file_skipped(tmp_home, tmp_path):
    _write_registry(tmp_home, {
        "tab-1": {"claude_pid": os.getpid(), "session_id": "S1", "cwd": "/proj",
                  "transcript_path": str(tmp_path / "ghost.jsonl"), "ts": 1},
        "tab-2": {"claude_pid": os.getpid(), "session_id": "S2", "cwd": "/proj"},
    })  # tab-2 has no transcript_path
    w = scw.Watcher()
    try:
        cutoff = (scw.utc_now() - timedelta(hours=scw.ACTIVITY_HOURS)).timestamp()
        assert w.find_active_transcripts(cutoff) == []
    finally:
        w.kq.close()


def test_find_active_transcripts_cron_tagging(tmp_home, tmp_path):
    tfile = tmp_path / "cron.jsonl"
    tfile.write_text(_user_line("cron work") + "\n")
    # cron cwd is derived from the (patched) module HOME — build it portably
    _write_registry(tmp_home, {
        "tab-c": {"claude_pid": os.getpid(), "session_id": "SC",
                  "cwd": str(tmp_home / ".architect"),
                  "transcript_path": str(tfile), "ts": 1},
    })
    w = scw.Watcher()
    try:
        cutoff = (scw.utc_now() - timedelta(hours=scw.ACTIVITY_HOURS)).timestamp()
        out = w.find_active_transcripts(cutoff)
        assert len(out) == 1
        assert out[0]["is_cron"] is True
        assert out[0]["cron_label"] == "orchestrator-worker"
    finally:
        w.kq.close()


def test_find_active_transcripts_stat_oserror_skipped(tmp_home, tmp_path, monkeypatch):
    """If stat() raises OSError between exists() and the mtime check, the entry
    is skipped (lines 325-326)."""
    tfile = tmp_path / "racey.jsonl"
    tfile.write_text(_user_line("hi") + "\n")
    _write_registry(tmp_home, {
        "tab-1": {"claude_pid": os.getpid(), "session_id": "S1", "cwd": "/proj",
                  "transcript_path": str(tfile), "ts": 1},
    })
    real_stat = Path.stat

    def flaky_stat(self, *a, **k):
        if str(self) == str(tfile):
            raise OSError("stat race")
        return real_stat(self, *a, **k)

    monkeypatch.setattr(Path, "stat", flaky_stat)
    w = scw.Watcher()
    try:
        cutoff = (scw.utc_now() - timedelta(hours=scw.ACTIVITY_HOURS)).timestamp()
        assert w.find_active_transcripts(cutoff) == []
    finally:
        w.kq.close()


# ─── Watcher.add_watch / drop_watch ──────────────────────────────────────────

def test_add_watch_registers_fd_and_reads(tmp_home, tmp_path):
    f = tmp_path / "w.jsonl"
    f.write_text(_user_line("watched") + "\n")
    w = scw.Watcher()
    try:
        w.add_watch(f, "/cwd", pid=os.getpid())
        assert str(f) in w.path_to_fd
        fd = w.path_to_fd[str(f)]
        state = w.fd_to_state[fd]
        assert state.last_user["text"] == "watched"  # read_new ran on add
        assert w.dirty is True
    finally:
        w.kq.close()


def test_add_watch_idempotent_refreshes_metadata(tmp_home, tmp_path):
    f = tmp_path / "w.jsonl"
    f.write_text(_user_line("x") + "\n")
    w = scw.Watcher()
    try:
        w.add_watch(f, "/cwd", pid=111, is_cron=False, tab_id="t1")
        fd = w.path_to_fd[str(f)]
        # Second add for the same path must NOT open a new fd; it refreshes meta.
        w.add_watch(f, "/cwd", pid=222, is_cron=True, cron_label="cron", tab_id="t2")
        assert w.path_to_fd[str(f)] == fd  # same fd
        assert len(w.fd_to_state) == 1
        state = w.fd_to_state[fd]
        assert state.pid == 222
        assert state.is_cron is True
        assert state.cron_label == "cron"
        assert state.tab_id == "t2"
    finally:
        w.kq.close()


def test_add_watch_evicts_oldest_when_at_cap(tmp_home, tmp_path, monkeypatch):
    """At MAX_WATCHED_FDS the oldest (lowest mtime) watch is dropped to make
    room for the new one (lines 351-353)."""
    monkeypatch.setattr(scw, "MAX_WATCHED_FDS", 1)
    f1 = tmp_path / "old.jsonl"
    f1.write_text(_user_line("old") + "\n")
    old_mtime = time.time() - 1000
    os.utime(f1, (old_mtime, old_mtime))
    f2 = tmp_path / "new.jsonl"
    f2.write_text(_user_line("new") + "\n")
    w = scw.Watcher()
    try:
        w.add_watch(f1, "/cwd")
        assert str(f1) in w.path_to_fd
        # Adding the second file is at the cap → oldest (f1) gets evicted.
        w.add_watch(f2, "/cwd")
        assert str(f2) in w.path_to_fd
        assert str(f1) not in w.path_to_fd
        assert len(w.fd_to_state) == 1
    finally:
        w.kq.close()


def test_add_watch_kqueue_failure_closes_fd(tmp_home, tmp_path):
    """If kq.control raises while registering the kevent, the opened fd is
    closed and no watch is registered (lines 372-375). The C-level kqueue
    object's .control is read-only, so we swap in a tiny proxy that raises."""
    f = tmp_path / "w.jsonl"
    f.write_text(_user_line("x") + "\n")
    w = scw.Watcher()
    real_kq = w.kq

    class BoomKq:
        def control(self, *a, **k):
            raise OSError("kqueue full")

    try:
        w.kq = BoomKq()
        w.add_watch(f, "/cwd")
        assert str(f) not in w.path_to_fd
        assert len(w.fd_to_state) == 0
        log_out = scw.LOG_DIR / "session-context-watcher.out"
        assert "kqueue add w.jsonl" in log_out.read_text()
    finally:
        real_kq.close()


def test_add_watch_open_failure_logged(tmp_home, tmp_path):
    # A path that cannot be opened (directory does not exist) → open() raises,
    # caught, logged, and no watch registered.
    missing = tmp_path / "nope" / "x.jsonl"
    w = scw.Watcher()
    try:
        w.add_watch(missing, "/cwd")
        assert str(missing) not in w.path_to_fd
        # log() wrote a warn line to LOG_DIR.
        log_out = scw.LOG_DIR / "session-context-watcher.out"
        assert log_out.exists()
    finally:
        w.kq.close()


def test_drop_watch_removes_state_and_closes(tmp_home, tmp_path):
    f = tmp_path / "w.jsonl"
    f.write_text(_user_line("x") + "\n")
    w = scw.Watcher()
    try:
        w.add_watch(f, "/cwd")
        fd = w.path_to_fd[str(f)]
        w.drop_watch(fd)
        assert fd not in w.fd_to_state
        assert str(f) not in w.path_to_fd
        # fd is closed: os.close again raises.
        with pytest.raises(OSError):
            os.close(fd)
    finally:
        w.kq.close()


def test_drop_watch_unknown_fd_is_noop(tmp_home):
    w = scw.Watcher()
    try:
        # An fd we never registered; pop returns None, close swallows OSError.
        w.drop_watch(999999)
    finally:
        w.kq.close()


# ─── Watcher.handle_event ─────────────────────────────────────────────────────

def test_handle_event_write_reads_new_data(tmp_home, tmp_path):
    """Drive a real kqueue event: watch a file, append to it, pull the kevent,
    and confirm handle_event reads the new turn and flips dirty."""
    f = tmp_path / "w.jsonl"
    f.write_text(_user_line("one") + "\n")
    w = scw.Watcher()
    try:
        w.add_watch(f, "/cwd")
        w.dirty = False  # reset after add
        fd = w.path_to_fd[str(f)]
        with open(f, "a") as fh:
            fh.write(_assistant_line("two") + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        events = w.kq.control(None, 8, 1.0)
        assert events, "expected a kqueue write event"
        for ev in events:
            w.handle_event(ev)
        assert w.fd_to_state[fd].last_assistant["text"] == "two"
        assert w.dirty is True
    finally:
        w.kq.close()


def test_handle_event_unknown_ident_noop(tmp_home):
    w = scw.Watcher()
    try:
        fake = select.kevent(123456, filter=select.KQ_FILTER_VNODE,
                             fflags=select.KQ_NOTE_WRITE)
        w.handle_event(fake)  # ident not in fd_to_state → returns early
        assert w.dirty is False
    finally:
        w.kq.close()


def test_handle_event_delete_drops_watch(tmp_home, tmp_path):
    f = tmp_path / "w.jsonl"
    f.write_text(_user_line("x") + "\n")
    w = scw.Watcher()
    try:
        w.add_watch(f, "/cwd")
        fd = w.path_to_fd[str(f)]
        w.dirty = False
        # Synthesize a delete event for that fd.
        ev = select.kevent(fd, filter=select.KQ_FILTER_VNODE,
                           fflags=select.KQ_NOTE_DELETE)
        w.handle_event(ev)
        assert fd not in w.fd_to_state
        assert w.dirty is True
    finally:
        w.kq.close()


# ─── Watcher.discover ─────────────────────────────────────────────────────────

def test_discover_identity_world_keeps_idle_verified_session_not_registry_ghosts(
        tmp_home, tmp_path):
    paths = {}
    old = scw.utc_now() - timedelta(hours=72)
    for name in ("verified", "unknown", "registry-only"):
        path = tmp_path / f"{name}.jsonl"
        path.write_text(_user_line("Resume this work.", scw.iso(old)) + "\n")
        os.utime(path, (old.timestamp(), old.timestamp()))
        paths[name] = path
    _write_registry(tmp_home, {
        name: {"claude_pid": os.getpid(), "session_id": name,
               "transcript_path": str(path), "cwd": "/project"}
        for name, path in paths.items()
    })
    scw.WORLD_PATH.parent.mkdir(parents=True, exist_ok=True)
    scw.WORLD_PATH.write_text(json.dumps({"live_sessions": [
        {"session_id": name, "pid": os.getpid(), "transcript_path": str(paths[name]),
         "workspace_id": "workspace", "surface_id": name,
         "identity_status": name, "provider": "claude", "cwd": "/project"}
        for name in ("verified", "unknown")
    ]}))
    watcher = scw.Watcher()
    try:
        watcher.discover()
        watcher.flush()
        assert set(watcher.path_to_fd) == {str(paths["verified"])}
        assert set(json.loads(scw.OUT_PATH.read_text())["by_session"]) == {"verified"}
    finally:
        for fd in list(watcher.fd_to_state):
            watcher.drop_watch(fd)
        watcher.kq.close()


@pytest.mark.parametrize("drop_reason", ["dead_pid", "missing_identity"])
def test_discover_keeps_days_old_live_guidance_and_scanner_verifies_it(
        tmp_home, tmp_path, monkeypatch, drop_reason):
    path = tmp_path / "long-lived.jsonl"
    old = scw.utc_now() - timedelta(hours=72)
    response = "Completed the checks. " * 100 + "Would you like to review them?"
    path.write_text(_user_line("Finish the checks.", scw.iso(old)) + "\n"
                    + _assistant_line(response, scw.iso(old + timedelta(minutes=1))) + "\n"
                    + _user_line("Show the results.", scw.iso(old + timedelta(minutes=2))) + "\n")
    os.utime(path, (old.timestamp(), old.timestamp()))
    historical = tmp_path / "unregistered-history.jsonl"
    historical.write_text(_user_line("Do not watch historical sessions.") + "\n")
    registration = {"claude_pid": os.getpid(), "session_id": path.stem,
                    "cwd": "/project", "transcript_path": str(path), "ts": 1}
    _write_registry(tmp_home, {"tab-live": registration})
    spec = importlib.util.spec_from_file_location(
        "scanner_for_idle_watcher", str(REPO / "bin/world-scanner.py"))
    scanner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(scanner)
    monkeypatch.setattr(scanner, "SESSION_CTX", scw.OUT_PATH)
    checked = scw.utc_now() + timedelta(days=2)
    monkeypatch.setattr(scanner, "utc_now", lambda: checked)
    watcher = scw.Watcher()
    try:
        watcher.discover()
        assert set(watcher.path_to_fd) == {str(path)}
        watcher.flush()
        cached = json.loads(scw.OUT_PATH.read_text())
        guidance = cached["by_session"][path.stem]["guidance_context"]
        assert guidance["last_response"]["text"] == response
        assert guidance["last_request"]["text"] == "Show the results."
        assert cached["recent_user_inputs"] == []
        watcher.discover()
        assert set(watcher.path_to_fd) == {str(path)}
        live = {path.stem: {"session_id": path.stem, "identity_status": "verified",
                            "transcript_path": str(path)}}
        scanner.merge_session_context(live)
        assert live[path.stem]["guidance_context"] == guidance
        assert live[path.stem]["context_status"] == "verified"
        assert live[path.stem]["context_checked_at"] == scanner.iso(checked)
        assert live[path.stem]["context_built_at"] == cached["_meta"]["built_at"]
        if drop_reason == "dead_pid":
            registration["claude_pid"] = 2_000_000_000
        else:
            registration.pop("session_id")
        _write_registry(tmp_home, {"tab-live": registration})
        watcher.discover()
        watcher.flush()
        assert watcher.path_to_fd == {}
        assert json.loads(scw.OUT_PATH.read_text())["by_session"] == {}
    finally:
        for fd in list(watcher.fd_to_state):
            watcher.drop_watch(fd)
        watcher.kq.close()


def test_discover_adds_live_and_drops_dead(tmp_home, tmp_path):
    live_file = tmp_path / "live.jsonl"
    live_file.write_text(_user_line("active") + "\n")
    _write_registry(tmp_home, {
        "tab-1": {"claude_pid": os.getpid(), "session_id": "S1", "cwd": "/proj",
                  "transcript_path": str(live_file), "ts": 1},
    })
    w = scw.Watcher()
    try:
        w.discover()
        assert str(live_file) in w.path_to_fd
        # Pre-seed a stale watch on another file whose pid is no longer in the
        # live set; the next discover should drop it.
        stale_file = tmp_path / "stale.jsonl"
        stale_file.write_text(_user_line("gone") + "\n")
        w.add_watch(stale_file, "/proj", pid=2_000_000_000)
        assert str(stale_file) in w.path_to_fd
        w.discover()
        # live one stays, stale one dropped (not in live_paths set).
        assert str(live_file) in w.path_to_fd
        assert str(stale_file) not in w.path_to_fd
    finally:
        w.kq.close()


# ─── Watcher.flush ────────────────────────────────────────────────────────────

def test_flush_writes_out_path_with_meta(tmp_home, tmp_path):
    human_file = tmp_path / "human.jsonl"
    human_file.write_text(
        _user_line("a human input", ts=scw.iso(scw.utc_now())) + "\n"
    )
    cron_file = tmp_path / "cron.jsonl"
    cron_file.write_text(_user_line("cron line", ts=scw.iso(scw.utc_now())) + "\n")
    w = scw.Watcher()
    try:
        w.add_watch(human_file, "/proj/human", pid=1, is_cron=False, tab_id="h")
        w.add_watch(cron_file, "/Users/mukuls/.architect", pid=2,
                    is_cron=True, cron_label="orchestrator-worker", tab_id="c")
        w.flush()
        assert scw.OUT_PATH.exists()
        payload = json.loads(scw.OUT_PATH.read_text())
        meta = payload["_meta"]
        assert meta["watched"] == 2
        assert meta["watched_human"] == 1
        assert meta["watched_cron"] == 1
        assert meta["activity_hours"] == scw.ACTIVITY_HOURS
        assert "built_at" in meta
        assert set(payload["by_session"].keys()) == {"human", "cron"}
        # recent_user_inputs only contains the human session, never cron.
        inputs = payload["recent_user_inputs"]
        assert len(inputs) == 1
        assert inputs[0]["session_id"] == "human"
        assert inputs[0]["cwd"] == "/proj/human"
        assert inputs[0]["has_assistant_reply"] is False
        assert w.dirty is False
    finally:
        w.kq.close()


def test_flush_recent_input_has_assistant_reply(tmp_home, tmp_path):
    f = tmp_path / "h.jsonl"
    now = scw.utc_now()
    f.write_text(
        _user_line("question", ts=scw.iso(now - timedelta(seconds=10))) + "\n"
        + _assistant_line("answer", ts=scw.iso(now)) + "\n"
    )
    w = scw.Watcher()
    try:
        w.add_watch(f, "/proj", pid=1, is_cron=False)
        w.flush()
        payload = json.loads(scw.OUT_PATH.read_text())
        inp = payload["recent_user_inputs"][0]
        assert inp["has_assistant_reply"] is True
    finally:
        w.kq.close()


def test_flush_excludes_stale_user_input(tmp_home, tmp_path):
    f = tmp_path / "h.jsonl"
    # last_user ts is older than the activity window → not in recent_user_inputs.
    old_ts = scw.iso(scw.utc_now() - timedelta(hours=scw.ACTIVITY_HOURS + 1))
    f.write_text(_user_line("ancient", ts=old_ts) + "\n")
    w = scw.Watcher()
    try:
        w.add_watch(f, "/proj", pid=1, is_cron=False)
        w.flush()
        payload = json.loads(scw.OUT_PATH.read_text())
        assert payload["recent_user_inputs"] == []
        # but the session is still in by_session.
        assert "h" in payload["by_session"]
    finally:
        w.kq.close()


def test_flush_empty_watcher(tmp_home):
    w = scw.Watcher()
    try:
        w.flush()
        payload = json.loads(scw.OUT_PATH.read_text())
        assert payload["_meta"]["watched"] == 0
        assert payload["by_session"] == {}
        assert payload["recent_user_inputs"] == []
    finally:
        w.kq.close()


def test_flush_recent_inputs_sorted_and_limited(tmp_home, tmp_path):
    now = scw.utc_now()
    w = scw.Watcher()
    try:
        # Create more than RECENT_INPUTS_LIMIT human sessions with descending ts.
        for i in range(scw.RECENT_INPUTS_LIMIT + 5):
            f = tmp_path / f"s{i}.jsonl"
            ts = scw.iso(now - timedelta(seconds=i))
            f.write_text(_user_line(f"input {i}", ts=ts) + "\n")
            w.add_watch(f, f"/proj/{i}", pid=1, is_cron=False)
        w.flush()
        payload = json.loads(scw.OUT_PATH.read_text())
        inputs = payload["recent_user_inputs"]
        assert len(inputs) == scw.RECENT_INPUTS_LIMIT  # capped
        # Sorted newest-first: input 0 (ts=now) is first.
        assert inputs[0]["text"] == "input 0"
    finally:
        w.kq.close()


# ─── main() ───────────────────────────────────────────────────────────────────

def test_main_once_writes_out_path(tmp_home, tmp_path, monkeypatch, capsys):
    tfile = tmp_path / "live.jsonl"
    tfile.write_text(_user_line("hi", ts=scw.iso(scw.utc_now())) + "\n")
    _write_registry(tmp_home, {
        "tab-1": {"claude_pid": os.getpid(), "session_id": "S1", "cwd": "/proj",
                  "transcript_path": str(tfile), "ts": 1},
    })
    monkeypatch.setattr(sys, "argv", ["session-context-watcher.py", "--once"])
    scw.main()
    out = capsys.readouterr().out
    assert "flushed 1 sessions" in out
    assert scw.OUT_PATH.exists()
    payload = json.loads(scw.OUT_PATH.read_text())
    # by_session is keyed by the transcript file stem, not the registry sid.
    assert "live" in payload["by_session"]
    assert payload["by_session"]["live"]["cwd"] == "/proj"


def test_main_daemon_lock_contention_exits(tmp_home, monkeypatch):
    """Daemon mode with the lock already held by a live pid → acquire_lock
    returns False and main returns without ever starting run()."""
    scw.LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    scw.LOCK_FILE.write_text(str(os.getpid()))  # held by a live pid
    monkeypatch.setattr(sys, "argv", ["session-context-watcher.py", "--daemon"])
    # If run() were ever entered it would block forever; guard it.
    def boom(self):
        raise AssertionError("run() must not be called when lock is contended")
    monkeypatch.setattr(scw.Watcher, "run", boom)
    scw.main()  # returns cleanly after the lock-contention branch


def test_main_daemon_acquires_runs_and_releases(tmp_home, monkeypatch):
    """Daemon mode with a free lock: acquire succeeds, run() is invoked, and the
    lock is released in the finally block."""
    monkeypatch.setattr(sys, "argv", ["session-context-watcher.py", "--daemon"])
    called = {"run": False}
    def fake_run(self):
        called["run"] = True
        # Lock should be held while running.
        assert scw.LOCK_FILE.exists()
    monkeypatch.setattr(scw.Watcher, "run", fake_run)
    scw.main()
    assert called["run"] is True
    # finally: release_lock unlinked it.
    assert not scw.LOCK_FILE.exists()


def test_main_daemon_keyboard_interrupt_releases(tmp_home, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["session-context-watcher.py", "--daemon"])
    def fake_run(self):
        raise KeyboardInterrupt
    monkeypatch.setattr(scw.Watcher, "run", fake_run)
    scw.main()  # KeyboardInterrupt is caught, logged, lock released
    assert not scw.LOCK_FILE.exists()


def test_main_daemon_crash_reraises_and_releases(tmp_home, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["session-context-watcher.py", "--daemon"])
    def fake_run(self):
        raise RuntimeError("kaboom")
    monkeypatch.setattr(scw.Watcher, "run", fake_run)
    with pytest.raises(RuntimeError, match="kaboom"):
        scw.main()
    # finally still released the lock even though the exception propagated.
    assert not scw.LOCK_FILE.exists()


# ─── log() ────────────────────────────────────────────────────────────────────

def test_log_writes_out_and_err(tmp_home):
    scw.log("a warning happened", "warn")
    out_file = scw.LOG_DIR / "session-context-watcher.out"
    err_file = scw.LOG_DIR / "session-context-watcher.err"
    assert "a warning happened" in out_file.read_text()
    # warn level also mirrors to .err
    assert "a warning happened" in err_file.read_text()


def test_log_info_only_to_out(tmp_home):
    scw.log("just info")
    out_file = scw.LOG_DIR / "session-context-watcher.out"
    err_file = scw.LOG_DIR / "session-context-watcher.err"
    assert "just info" in out_file.read_text()
    assert not err_file.exists()
