"""Tests for bin/session-context-watcher.py — event-driven transcript watcher.

The script is a hyphenated CLI (not an importable module name), so it's loaded
by file path via importlib. The module binds HOME-derived path constants AT
IMPORT, so we set os.environ["HOME"] to a tmp dir BEFORE the first import — every
constant (PROJECTS_DIR, CMUX_REGISTRY, ORCHESTRATOR_REGISTRY, OUT_PATH, LOG_DIR,
LOCK_FILE) then resolves under that tmp dir. Per-test we monkeypatch individual
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
    projects = home / ".claude/projects"
    cmux_reg = home / ".claude/cmux-registry.json"
    orch_reg = home / ".architect/orchestrator-registry.json"
    out_path = home / ".claude/cache/session-context.json"
    log_dir = home / ".assistant/logs"
    lock_file = home / ".architect/.session-context-watcher.lock"
    monkeypatch.setattr(scw, "HOME", home)
    monkeypatch.setattr(scw, "PROJECTS_DIR", projects)
    monkeypatch.setattr(scw, "CMUX_REGISTRY", cmux_reg)
    monkeypatch.setattr(scw, "ORCHESTRATOR_REGISTRY", orch_reg)
    monkeypatch.setattr(scw, "OUT_PATH", out_path)
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


def test_find_active_transcripts_old_mtime_skipped(tmp_home, tmp_path):
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
        assert w.find_active_transcripts(cutoff) == []
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
    _write_registry(tmp_home, {
        "tab-c": {"claude_pid": os.getpid(), "session_id": "SC",
                  "cwd": "/Users/mukuls/.architect",
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
