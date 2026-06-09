"""Tests for bin/workspace-watcher.py — cmux workspace crash detection + auto-resume.

Loaded by file path (the script is a hyphenated CLI, not an importable module).
Everything runs against a tmp HOME so no real ~/.assistant, ~/.claude,
~/.architect, or ~/Library is touched. The module binds path constants AND
calls logging.basicConfig(...) + LOG_PATH.parent.mkdir(...) at import, so HOME
must be set in os.environ BEFORE the module loads — load_module() does this.
After load, per-test we monkeypatch individual module constants onto the tmp
dirs and re-bind the logging handler away from any stale file path.
"""
from __future__ import annotations

import importlib.util
import json
import logging
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

REPO = Path(__file__).resolve().parent.parent

# A single tmp HOME exists for the whole module so the import-time side effects
# (logging.basicConfig + mkdir) land somewhere harmless. Per-test fixtures then
# repoint the module constants at fresh tmp dirs.
_IMPORT_HOME = TemporaryDirectory()
os.environ["HOME"] = _IMPORT_HOME.name


def _load():
    spec = importlib.util.spec_from_file_location(
        "workspace_watcher_mod", str(REPO / "bin" / "workspace-watcher.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


WW = _load()


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Repoint every module path constant at a per-test tmp tree and silence the
    file logger so no stale LOG_PATH handle is touched."""
    h = tmp_path / "home"
    watcher_dir = h / ".assistant" / "workspace-watcher"
    crash_dir = h / ".claude" / "cmux-crash-events"
    orch = h / ".architect" / "orchestrator-ledger"
    diag = h / "Library" / "Logs" / "DiagnosticReports"
    cmux_state = h / "Library" / "Application Support" / "cmux" / "session-com.cmuxterm.app.json"
    cmux_prev = h / "Library" / "Application Support" / "cmux" / "session-com.cmuxterm.app-previous.json"

    monkeypatch.setattr(WW, "HOME", h)
    monkeypatch.setattr(WW, "WATCHER_DIR", watcher_dir)
    monkeypatch.setattr(WW, "CURSOR_FILE", watcher_dir / "cursor.seq")
    monkeypatch.setattr(WW, "RESUME_LEDGER", watcher_dir / "resume-ledger.jsonl")
    monkeypatch.setattr(WW, "WS_CACHE", watcher_dir / "ws-cache.json")
    monkeypatch.setattr(WW, "CRASH_EVENTS_DIR", crash_dir)
    monkeypatch.setattr(WW, "ORCH_LEDGER", orch)
    monkeypatch.setattr(WW, "DIAG_REPORTS", diag)
    monkeypatch.setattr(WW, "CMUX_SESSION_STATE", cmux_state)
    monkeypatch.setattr(WW, "CMUX_PREV_STATE", cmux_prev)

    # Neutralize logging so the module's log.* calls never touch the real fs.
    monkeypatch.setattr(WW, "log", logging.getLogger("ww-test-silent"))
    WW.log.handlers = [logging.NullHandler()]
    WW.log.propagate = False

    h.mkdir(parents=True, exist_ok=True)
    return h


# --------------------------------------------------------------------------- time helpers

def test_utcnow_is_tz_aware_utc():
    dt = WW.utcnow()
    assert dt.tzinfo is timezone.utc


def test_iso_drops_microseconds_and_uses_z():
    dt = datetime(2026, 5, 27, 12, 45, 2, 123456, tzinfo=timezone.utc)
    assert WW.iso(dt) == "2026-05-27T12:45:02Z"


# --------------------------------------------------------------------------- run()

def test_run_success_real_command():
    rc, out, err = WW.run(["echo", "hi"])
    assert rc == 0
    assert out.strip() == "hi"
    assert err == ""


def test_run_file_not_found_returns_minus_one():
    rc, out, err = WW.run(["this-binary-does-not-exist-xyz"])
    assert rc == -1
    assert out == ""
    assert err  # carries the FileNotFoundError text


def test_run_timeout(monkeypatch):
    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="x", timeout=1)
    monkeypatch.setattr(WW.subprocess, "run", boom)
    rc, out, err = WW.run(["sleep", "100"], timeout=1)
    assert rc == -1
    assert out == ""


# --------------------------------------------------------------------------- notify()

def test_notify_escapes_quotes_and_backslashes(monkeypatch):
    captured = {}
    monkeypatch.setattr(WW, "run", lambda cmd, timeout=10: captured.setdefault("argv", cmd))
    WW.notify('ti"tle\\x', 'mes"sage\\y')
    argv = captured["argv"]
    assert argv[0] == "osascript"
    assert argv[1] == "-e"
    script = argv[2]
    # backslash escaped to \\ and double-quote escaped to \" — injection-safe.
    assert 'ti\\"tle\\\\x' in script
    assert 'mes\\"sage\\\\y' in script
    # No raw unescaped user double-quote can break out of the AppleScript string.
    assert 'sound name "Sosumi"' in script


# --------------------------------------------------------------------------- json_quote()

def test_json_quote_escapes_single_quotes():
    assert WW.json_quote("plain") == "'plain'"
    # ' becomes '\'' (close, escaped-quote, reopen)
    assert WW.json_quote("a'b") == "'a'\\''b'"


# --------------------------------------------------------------------------- _strip_number_suffix()

def test_strip_number_suffix():
    assert WW._strip_number_suffix(" Title [12]") == "Title"
    assert WW._strip_number_suffix("Plain") == "Plain"
    assert WW._strip_number_suffix(None) == ""
    assert WW._strip_number_suffix("") == ""


# --------------------------------------------------------------------------- _parse_capture_time()

def test_parse_capture_time_with_explicit_tz():
    # 2026-05-27 12:45:02 -0700 → known epoch.
    expected = datetime(2026, 5, 27, 12, 45, 2,
                        tzinfo=timezone(WW.timedelta(hours=-7))).timestamp()
    got = WW._parse_capture_time("2026-05-27 12:45:02.0269 -0700")
    assert got == pytest.approx(expected)


def test_parse_capture_time_tz_only_no_subseconds():
    expected = datetime(2026, 5, 27, 12, 45, 2,
                        tzinfo=timezone(WW.timedelta(hours=2))).timestamp()
    got = WW._parse_capture_time("2026-05-27 12:45:02 +0200")
    assert got == pytest.approx(expected)


def test_parse_capture_time_no_tz_fallback_local():
    # No tz token → astimezone() local fallback. Just assert it returns a float
    # matching the local-clock interpretation of the same wall time.
    raw = "2026-05-27 12:45:02"
    got = WW._parse_capture_time(raw)
    expected = datetime(2026, 5, 27, 12, 45, 2).astimezone().timestamp()
    assert got == pytest.approx(expected)


def test_parse_capture_time_none_and_garbage():
    assert WW._parse_capture_time(None) is None
    assert WW._parse_capture_time("not a date at all") is None
    assert WW._parse_capture_time("") is None


def test_parse_capture_time_regex_matches_but_strptime_fails():
    # Prefix matches \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} but is an impossible
    # date — strptime raises, the inner except returns None (lines 312-313).
    assert WW._parse_capture_time("2026-13-40 99:99:99 +0000") is None


# --------------------------------------------------------------------------- _read_ips()

def _write_ips(path: Path, body: dict, header: str = '{"app_name":"cmux"}'):
    path.write_text(header + "\n" + json.dumps(body))


def test_read_ips_parses_json_body(tmp_path):
    p = tmp_path / "crash.ips"
    _write_ips(p, {"procName": "node", "coalitionName": "com.cmuxterm.app"})
    doc = WW._read_ips(p)
    assert doc is not None
    assert doc["procName"] == "node"


def test_read_ips_malformed_returns_none(tmp_path):
    p = tmp_path / "bad.ips"
    p.write_text("header line\n{ not valid json")
    assert WW._read_ips(p) is None


def test_read_ips_missing_file_returns_none(tmp_path):
    assert WW._read_ips(tmp_path / "nope.ips") is None


# --------------------------------------------------------------------------- find_recent_cleanup_ledger()

def test_find_cleanup_ledger_nonexistent_dir_returns_none(home):
    # ORCH_LEDGER does not exist yet.
    assert WW.find_recent_cleanup_ledger("u", 5, "Title") is None


def test_find_cleanup_ledger_match_by_ref(home):
    WW.ORCH_LEDGER.mkdir(parents=True)
    p = WW.ORCH_LEDGER / "cleanup-1.json"
    p.write_text(json.dumps({"workspace_ref": "workspace:5", "workspace_title": "Other"}))
    got = WW.find_recent_cleanup_ledger("u", 5, "Title")
    assert got == p


def test_find_cleanup_ledger_match_by_stripped_title(home):
    WW.ORCH_LEDGER.mkdir(parents=True)
    p = WW.ORCH_LEDGER / "cleanup-2.json"
    p.write_text(json.dumps({"workspace_ref": "workspace:99",
                             "workspace_title": "My Work [3]"}))
    # ref 5 won't match workspace:99, but the stripped title "My Work" matches.
    got = WW.find_recent_cleanup_ledger("u", 5, "My Work [7]")
    assert got == p


def test_find_cleanup_ledger_outside_window_no_match(home):
    WW.ORCH_LEDGER.mkdir(parents=True)
    p = WW.ORCH_LEDGER / "cleanup-old.json"
    p.write_text(json.dumps({"workspace_ref": "workspace:5"}))
    old = time.time() - 10000
    os.utime(p, (old, old))
    assert WW.find_recent_cleanup_ledger("u", 5, "Title") is None


def test_find_cleanup_ledger_malformed_skipped(home):
    WW.ORCH_LEDGER.mkdir(parents=True)
    (WW.ORCH_LEDGER / "cleanup-bad.json").write_text("{ not json")
    good = WW.ORCH_LEDGER / "cleanup-good.json"
    good.write_text(json.dumps({"workspace_ref": "workspace:5"}))
    assert WW.find_recent_cleanup_ledger("u", 5, "Title") == good


# --------------------------------------------------------------------------- find_recent_cmux_ips()

def _ips_doc(capture_time, coalition="com.cmuxterm.app", proc_path="", argv=None):
    d = {
        "coalitionName": coalition,
        "captureTime": capture_time,
        "procName": "node",
        "procPath": proc_path,
    }
    if argv is not None:
        d["processByPid"] = {"processList": argv}
    return d


def _cap_string(epoch: float) -> str:
    """Render an epoch as a captureTime string with explicit local-ish tz."""
    dt = datetime.fromtimestamp(epoch, tz=timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M:%S") + ".0000 +0000"


def test_find_ips_nonexistent_dir_returns_none(home):
    assert WW.find_recent_cmux_ips(time.time()) is None


def test_find_ips_inside_window_returned(home):
    WW.DIAG_REPORTS.mkdir(parents=True)
    close = time.time()
    p = WW.DIAG_REPORTS / "a.ips"
    _write_ips(p, _ips_doc(_cap_string(close - 5)))
    got = WW.find_recent_cmux_ips(close)
    assert got == p


def test_find_ips_outside_window_not_returned(home):
    WW.DIAG_REPORTS.mkdir(parents=True)
    close = time.time()
    p = WW.DIAG_REPORTS / "old.ips"
    # captureTime well before the lookback window; bump mtime forward so the
    # coarse mtime pre-filter doesn't drop it for the wrong reason.
    _write_ips(p, _ips_doc(_cap_string(close - 5000)))
    os.utime(p, (close, close))
    assert WW.find_recent_cmux_ips(close) is None


def test_find_ips_non_cmux_coalition_ignored(home):
    WW.DIAG_REPORTS.mkdir(parents=True)
    close = time.time()
    p = WW.DIAG_REPORTS / "other.ips"
    _write_ips(p, _ips_doc(_cap_string(close - 2), coalition="com.apple.Safari"))
    assert WW.find_recent_cmux_ips(close) is None


def test_find_ips_responsible_proc_cmux_accepted(home):
    WW.DIAG_REPORTS.mkdir(parents=True)
    close = time.time()
    p = WW.DIAG_REPORTS / "resp.ips"
    doc = _ips_doc(_cap_string(close - 2), coalition="something.else")
    doc["responsibleProc"] = "cmux"
    _write_ips(p, doc)
    assert WW.find_recent_cmux_ips(close) == p


def test_find_ips_cwd_binding_wins_over_plain_coalition(home):
    WW.DIAG_REPORTS.mkdir(parents=True)
    close = time.time()
    # Plain coalition hit, closer in time.
    plain = WW.DIAG_REPORTS / "plain.ips"
    _write_ips(plain, _ips_doc(_cap_string(close - 1)))
    # cwd-bound hit (argv mentions the cwd leaf), farther in time.
    bound = WW.DIAG_REPORTS / "bound.ips"
    _write_ips(bound, _ips_doc(_cap_string(close - 8), argv=["node", "/x/myrepo/run"]))
    got = WW.find_recent_cmux_ips(close, ws_cwd="/Users/me/myrepo")
    assert got == bound


def test_find_ips_skips_file_with_stale_mtime(home):
    # mtime well before earliest-5 → coarse pre-filter drops it (line 349).
    WW.DIAG_REPORTS.mkdir(parents=True)
    close = time.time()
    stale = WW.DIAG_REPORTS / "stale.ips"
    _write_ips(stale, _ips_doc(_cap_string(close - 2)))  # captureTime would match
    old = close - 5000
    os.utime(stale, (old, old))
    assert WW.find_recent_cmux_ips(close) is None


def test_find_ips_skips_malformed_and_vanished_files(home, monkeypatch):
    # One file vanishes between glob and stat() (FileNotFoundError → continue,
    # lines 349-351); one is malformed so _read_ips returns None (line 354 → continue).
    WW.DIAG_REPORTS.mkdir(parents=True)
    close = time.time()
    vanished = WW.DIAG_REPORTS / "vanished.ips"
    vanished.write_text("placeholder")
    malformed = WW.DIAG_REPORTS / "malformed.ips"
    malformed.write_text("hdr\n{ not json")
    good = WW.DIAG_REPORTS / "good.ips"
    _write_ips(good, _ips_doc(_cap_string(close - 2)))

    real_stat = Path.stat

    def flaky_stat(self, *a, **k):
        if self.name == "vanished.ips":
            raise FileNotFoundError(self.name)
        return real_stat(self, *a, **k)
    monkeypatch.setattr(Path, "stat", flaky_stat)
    assert WW.find_recent_cmux_ips(close) == good


def test_find_ips_closest_in_time_tiebreak(home):
    WW.DIAG_REPORTS.mkdir(parents=True)
    close = time.time()
    far = WW.DIAG_REPORTS / "far.ips"
    _write_ips(far, _ips_doc(_cap_string(close - 40)))
    near = WW.DIAG_REPORTS / "near.ips"
    _write_ips(near, _ips_doc(_cap_string(close - 3)))
    assert WW.find_recent_cmux_ips(close) == near


# --------------------------------------------------------------------------- parse_ips_summary()

def test_parse_ips_summary_signal_and_top_frame(tmp_path):
    p = tmp_path / "s.ips"
    _write_ips(p, {
        "procName": "node",
        "responsibleProc": "cmux",
        "coalitionName": "com.cmuxterm.app",
        "captureTime": "2026-05-27 12:45:02.0269 -0700",
        "exception": {"signal": "SIGSEGV"},
        "threads": [
            {"triggered": False, "frames": [{"symbol": "ignored"}]},
            {"triggered": True, "frames": [{"symbol": "boom_fn"}, {"symbol": "x"}]},
        ],
    })
    out = WW.parse_ips_summary(p)
    assert out["signal"] == "SIGSEGV"
    assert out["top"] == "boom_fn"
    assert out["proc"] == "node"
    assert out["coalition"] == "com.cmuxterm.app"


def test_parse_ips_summary_falls_back_to_exception_type(tmp_path):
    p = tmp_path / "t.ips"
    _write_ips(p, {
        "exception": {"type": "EXC_BAD_ACCESS"},
        "threads": [{"triggered": True, "frames": [{}]}],  # frame has no symbol → "?"
    })
    out = WW.parse_ips_summary(p)
    assert out["signal"] == "EXC_BAD_ACCESS"
    assert out["top"] == "?"


def test_parse_ips_summary_unreadable_returns_empty(tmp_path):
    p = tmp_path / "bad.ips"
    p.write_text("hdr\n{not json")
    assert WW.parse_ips_summary(p) == {}


# --------------------------------------------------------------------------- WorkspaceRegistry

def test_registry_load_from_cache(home):
    WW.WATCHER_DIR.mkdir(parents=True)
    WW.WS_CACHE.write_text(json.dumps({"U1": {"uuid": "U1", "title": "Cached"}}))
    reg = WW.WorkspaceRegistry()
    assert reg.snapshot("U1")["title"] == "Cached"
    assert reg.snapshot("missing") is None


def test_registry_load_corrupt_cache_is_empty(home):
    WW.WATCHER_DIR.mkdir(parents=True)
    WW.WS_CACHE.write_text("{ not json")
    reg = WW.WorkspaceRegistry()
    assert reg.by_uuid == {}


def test_registry_refresh_parses_refs(home, monkeypatch):
    ws_list = {
        "workspaces": [
            {"id": "U-str", "ref": "workspace:5", "title": "A",
             "current_directory": "/a", "selected": True},
            {"id": "U-int", "ref": 7, "title": "B", "current_directory": "/b"},
            {"id": "U-none", "ref": None, "title": "C"},
            {"title": "no-id-skipped"},  # missing id → skipped
        ]
    }
    monkeypatch.setattr(WW, "run",
                        lambda cmd, timeout=10: (0, json.dumps(ws_list), ""))
    reg = WW.WorkspaceRegistry()
    reg.refresh()
    assert reg.snapshot("U-str")["ref"] == 5
    assert reg.snapshot("U-int")["ref"] == 7
    assert reg.snapshot("U-none")["ref"] is None
    # no-id row never created an entry.
    assert all(m.get("title") != "no-id-skipped" for m in reg.by_uuid.values())
    # Persisted to disk.
    assert WW.WS_CACHE.exists()


def test_registry_refresh_rpc_failure_tolerated(home, monkeypatch):
    monkeypatch.setattr(WW, "run", lambda cmd, timeout=10: (1, "", "rpc down"))
    reg = WW.WorkspaceRegistry()
    reg.refresh()  # must not raise
    assert reg.by_uuid == {}


def test_registry_refresh_bad_json_tolerated(home, monkeypatch):
    monkeypatch.setattr(WW, "run", lambda cmd, timeout=10: (0, "{not json", ""))
    reg = WW.WorkspaceRegistry()
    reg.refresh()  # the except clause logs and continues
    assert reg.by_uuid == {}


def test_merge_session_state_joins_by_title_cwd(home, monkeypatch):
    # Live RPC returns one workspace; session-state has a matching panel.
    ws_list = {"workspaces": [
        {"id": "LIVE", "ref": 3, "title": "Repo Work", "current_directory": "/work"},
    ]}
    monkeypatch.setattr(WW, "run",
                        lambda cmd, timeout=10: (0, json.dumps(ws_list), ""))
    session = {
        "windows": [{
            "tabManager": {"workspaces": [{
                "customTitle": "Repo Work",
                "currentDirectory": "/work",
                "processTitle": "claude",
                "panels": [
                    {"terminal": {
                        "resumeBinding": {"command": "claude --resume abc",
                                          "cwd": "/work",
                                          "checkpointId": "ckpt-1"},
                        "wasAgentRunning": True}},
                    {"browser": {"urlString": "https://github.com/o/r/pull/42"}},
                ],
            }]},
        }],
    }
    WW.CMUX_SESSION_STATE.parent.mkdir(parents=True)
    WW.CMUX_SESSION_STATE.write_text(json.dumps(session))
    reg = WW.WorkspaceRegistry()
    reg.refresh()
    meta = reg.snapshot("LIVE")
    assert meta["resume_command"] == "claude --resume abc"
    assert meta["resume_cwd"] == "/work"
    assert meta["checkpoint_id"] == "ckpt-1"
    assert meta["was_agent_running"] is True
    assert meta["last_pr"] == "https://github.com/o/r/pull/42"
    assert meta["process_title"] == "claude"
    # No synthetic by-title entry since the live UUID matched.
    assert "by-title:Repo Work" not in reg.by_uuid


def test_merge_session_state_synthetic_by_title_when_no_live_match(home, monkeypatch):
    # RPC returns no workspaces, so session-state has nothing to join to.
    monkeypatch.setattr(WW, "run",
                        lambda cmd, timeout=10: (0, json.dumps({"workspaces": []}), ""))
    session = {
        "windows": [{
            "tabManager": {"workspaces": [{
                "title": "Ghost WS",
                "currentDirectory": "/ghost",
                "panels": [{"terminal": {
                    "resumeBinding": {"command": "claude", "cwd": "/ghost"}}}],
            }]},
        }],
    }
    WW.CMUX_SESSION_STATE.parent.mkdir(parents=True)
    WW.CMUX_SESSION_STATE.write_text(json.dumps(session))
    reg = WW.WorkspaceRegistry()
    reg.refresh()
    syn = reg.snapshot("by-title:Ghost WS")
    assert syn is not None
    assert syn["resume_command"] == "claude"
    assert syn["cwd"] == "/ghost"


def test_merge_session_state_missing_file_noop(home):
    reg = WW.WorkspaceRegistry()
    # neither session-state file exists; should be a quiet no-op.
    reg._merge_session_state(WW.CMUX_SESSION_STATE)
    assert reg.by_uuid == {}


def test_merge_session_state_bad_json_noop(home):
    WW.CMUX_SESSION_STATE.parent.mkdir(parents=True)
    WW.CMUX_SESSION_STATE.write_text("{ broken")
    reg = WW.WorkspaceRegistry()
    reg._merge_session_state(WW.CMUX_SESSION_STATE)
    assert reg.by_uuid == {}


# --------------------------------------------------------------------------- ResumeGovernor

def _iso(epoch: float) -> str:
    return WW.iso(datetime.fromtimestamp(epoch, tz=timezone.utc))


def test_governor_load_from_ledger(home):
    WW.WATCHER_DIR.mkdir(parents=True)
    now = time.time()
    lines = [
        json.dumps({"ts": _iso(now - 10), "workspace_uuid": "A"}),
        json.dumps({"ts": _iso(now - 20), "workspace_uuid": "B"}),
        "",  # blank line skipped
    ]
    WW.RESUME_LEDGER.write_text("\n".join(lines) + "\n")
    gov = WW.ResumeGovernor()
    assert len(gov.attempts) == 2


def test_governor_load_corrupt_ledger_tolerated(home):
    WW.WATCHER_DIR.mkdir(parents=True)
    WW.RESUME_LEDGER.write_text("not json at all\n")
    gov = WW.ResumeGovernor()
    # the broad except in _load swallows; deque may be partially filled but no raise.
    assert isinstance(gov.attempts, type(gov.attempts))


def test_governor_gc_drops_old(home):
    WW.WATCHER_DIR.mkdir(parents=True)
    now = time.time()
    WW.RESUME_LEDGER.write_text(
        json.dumps({"ts": _iso(now - WW.CAP_WINDOW_SEC - 100), "workspace_uuid": "A"}) + "\n"
        + json.dumps({"ts": _iso(now - 10), "workspace_uuid": "B"}) + "\n")
    gov = WW.ResumeGovernor()
    gov._gc()
    assert len(gov.attempts) == 1
    assert gov.attempts[0][1] == "B"


def test_governor_per_ws_cap(home):
    gov = WW.ResumeGovernor()
    now = time.time()
    for _ in range(WW.PER_WS_CAP):
        gov.attempts.append((now, "U1"))
    ok, why = gov.can_resume("U1")
    assert ok is False
    assert "per-workspace cap" in why
    # A different uuid is still allowed.
    ok2, _ = gov.can_resume("U2")
    assert ok2 is True


def test_governor_daemon_cap(home):
    gov = WW.ResumeGovernor()
    now = time.time()
    # Fill with distinct uuids so the per-ws cap doesn't trip first.
    for i in range(WW.DAEMON_CAP):
        gov.attempts.append((now, f"U{i}"))
    ok, why = gov.can_resume("brand-new")
    assert ok is False
    assert "daemon-wide cap" in why


def test_governor_record_appends_jsonl_and_deque(home):
    gov = WW.ResumeGovernor()
    before = len(gov.attempts)
    gov.record("U1", "Title", "crash", "claude --resume",
               "workspace:9", "NEW-UUID", 1, False, True, None)
    assert len(gov.attempts) == before + 1
    assert WW.RESUME_LEDGER.exists()
    entry = json.loads(WW.RESUME_LEDGER.read_text().splitlines()[-1])
    assert entry["workspace_uuid"] == "U1"
    assert entry["ok"] is True
    assert entry["new_workspace_ref"] == "workspace:9"
    assert entry["command_preview"] == "claude --resume"


# --------------------------------------------------------------------------- resume_workspace()

def test_resume_no_command_returns_not_ok(home):
    gov = WW.ResumeGovernor()
    res = WW.resume_workspace({"uuid": "U1", "title": "T"}, gov)
    assert res == {"ok": False, "error": "no resume_command in cache"}


def test_resume_success_parses_new_ref_and_uuid(home, monkeypatch):
    gov = WW.ResumeGovernor()
    new_uuid = "DEADBEEF-1234-5678-9ABC-0123456789AB"
    stdout = f"created workspace:7 ({new_uuid})\n"
    captured = {}

    def fake_run(cmd, timeout=10):
        captured["argv"] = cmd
        return (0, stdout, "")
    monkeypatch.setattr(WW, "run", fake_run)
    meta = {"uuid": "U1", "title": "My WS", "resume_command": "claude --resume x",
            "cwd": "/work", "cause": "crash"}
    res = WW.resume_workspace(meta, gov, attempt_no=1, used_bash_lc=False)
    assert res["ok"] is True
    assert res["new_ref"] == "workspace:7"
    assert res["new_uuid"] == new_uuid
    # governor recorded an ok entry.
    entry = json.loads(WW.RESUME_LEDGER.read_text().splitlines()[-1])
    assert entry["ok"] is True
    assert entry["cause"] == "crash"
    # new-workspace argv carried the verbatim command (no bash -lc wrap).
    argv = captured["argv"]
    assert "--command" in argv
    assert argv[argv.index("--command") + 1] == "claude --resume x"


def test_resume_failure_records_error(home, monkeypatch):
    gov = WW.ResumeGovernor()
    monkeypatch.setattr(WW, "run",
                        lambda cmd, timeout=10: (1, "", "spawn failed"))
    meta = {"uuid": "U1", "title": "T", "resume_command": "claude"}
    res = WW.resume_workspace(meta, gov)
    assert res["ok"] is False
    assert res["error"] == "spawn failed"
    entry = json.loads(WW.RESUME_LEDGER.read_text().splitlines()[-1])
    assert entry["ok"] is False
    assert entry["error"] == "spawn failed"


def test_resume_bash_lc_wraps_command(home, monkeypatch):
    gov = WW.ResumeGovernor()
    captured = {}

    def fake_run(cmd, timeout=10):
        captured["argv"] = cmd
        return (0, "workspace:1", "")
    monkeypatch.setattr(WW, "run", fake_run)
    meta = {"uuid": "U1", "title": "T", "resume_command": "claude --resume z"}
    WW.resume_workspace(meta, gov, attempt_no=2, used_bash_lc=True)
    argv = captured["argv"]
    cmd_arg = argv[argv.index("--command") + 1]
    assert cmd_arg == "bash -lc 'claude --resume z'"


# --------------------------------------------------------------------------- handle_close()

def _close_evt(uuid="U1"):
    return {"name": "workspace.closed", "workspace_id": uuid, "payload": {}}


def _seed_registry(reg, uuid, meta):
    reg.by_uuid[uuid] = {"uuid": uuid, **meta}


def test_handle_close_intentional_no_resume(home, monkeypatch):
    # No .ips → intentional. A drop is written, no resume attempted.
    monkeypatch.setattr(WW, "find_recent_cmux_ips", lambda *a, **k: None)
    monkeypatch.setattr(WW, "notify", lambda *a, **k: None)
    reg = WW.WorkspaceRegistry()
    monkeypatch.setattr(reg, "refresh", lambda: None)  # don't shell out
    _seed_registry(reg, "U1", {"ref": 4, "title": "Work",
                               "resume_command": "claude", "cwd": "/w"})
    gov = WW.ResumeGovernor()
    WW.handle_close(_close_evt("U1"), reg, gov)
    drops = list(WW.CRASH_EVENTS_DIR.glob("*.json"))
    assert len(drops) == 1
    drop = json.loads(drops[0].read_text())
    assert drop["cause"] == "intentional"
    assert drop["resume"] is None
    assert drop["workspace_ref"] == "workspace:4"
    # No resume was recorded.
    assert not WW.RESUME_LEDGER.exists()


def test_handle_close_crash_resumes(home, monkeypatch):
    fake_ips = WW.DIAG_REPORTS  # any Path; parse_ips_summary is stubbed
    WW.DIAG_REPORTS.mkdir(parents=True)
    ips_file = WW.DIAG_REPORTS / "crash.ips"
    _write_ips(ips_file, {"exception": {"signal": "SIGSEGV"},
                          "threads": [{"triggered": True,
                                       "frames": [{"symbol": "boom"}]}]})
    monkeypatch.setattr(WW, "find_recent_cmux_ips", lambda *a, **k: ips_file)
    notes = []
    monkeypatch.setattr(WW, "notify", lambda title, msg, **k: notes.append((title, msg)))
    monkeypatch.setattr(WW, "resume_workspace",
                        lambda meta, gov, **k: {"ok": True, "new_ref": "workspace:8",
                                                "new_uuid": "NU"})
    reg = WW.WorkspaceRegistry()
    monkeypatch.setattr(reg, "refresh", lambda: None)
    _seed_registry(reg, "U1", {"ref": 4, "title": "Work",
                               "resume_command": "claude", "cwd": "/w"})
    gov = WW.ResumeGovernor()
    WW.handle_close(_close_evt("U1"), reg, gov)
    drop = json.loads(list(WW.CRASH_EVENTS_DIR.glob("*.json"))[0].read_text())
    assert drop["cause"] == "crash"
    assert drop["resume"]["attempted"] is True
    assert drop["resume"]["ok"] is True
    assert drop["resume"]["new_ref"] == "workspace:8"
    assert drop["evidence"]["ips_summary"]["signal"] == "SIGSEGV"
    # crash + resumed notifications fired.
    assert any("crashed" in t for t, _ in notes)
    assert any("resumed" in t for t, _ in notes)


def test_handle_close_crash_no_resume_command_blocked(home, monkeypatch):
    monkeypatch.setattr(WW, "find_recent_cmux_ips", lambda *a, **k: WW.HOME / "x.ips")
    monkeypatch.setattr(WW, "parse_ips_summary", lambda p: {"signal": "SIGSEGV"})
    monkeypatch.setattr(WW, "notify", lambda *a, **k: None)
    reg = WW.WorkspaceRegistry()
    monkeypatch.setattr(reg, "refresh", lambda: None)
    _seed_registry(reg, "U1", {"ref": 4, "title": "Work", "cwd": "/w"})  # no resume_command
    gov = WW.ResumeGovernor()
    WW.handle_close(_close_evt("U1"), reg, gov)
    drop = json.loads(list(WW.CRASH_EVENTS_DIR.glob("*.json"))[0].read_text())
    assert drop["cause"] == "crash"
    assert drop["resume"] == {"attempted": False, "reason": "no resume_command"}


def test_handle_close_crash_governor_blocks(home, monkeypatch):
    monkeypatch.setattr(WW, "find_recent_cmux_ips", lambda *a, **k: WW.HOME / "x.ips")
    monkeypatch.setattr(WW, "parse_ips_summary", lambda p: {"signal": "SIGSEGV"})
    monkeypatch.setattr(WW, "notify", lambda *a, **k: None)
    reg = WW.WorkspaceRegistry()
    monkeypatch.setattr(reg, "refresh", lambda: None)
    _seed_registry(reg, "U1", {"ref": 4, "title": "Work",
                               "resume_command": "claude", "cwd": "/w"})
    gov = WW.ResumeGovernor()
    # Force the per-ws cap so can_resume returns False.
    now = time.time()
    for _ in range(WW.PER_WS_CAP):
        gov.attempts.append((now, "U1"))
    WW.handle_close(_close_evt("U1"), reg, gov)
    drop = json.loads(list(WW.CRASH_EVENTS_DIR.glob("*.json"))[0].read_text())
    assert drop["resume"]["attempted"] is False
    assert "per-workspace cap" in drop["resume"]["reason"]


def test_handle_close_crash_resume_fails(home, monkeypatch):
    monkeypatch.setattr(WW, "find_recent_cmux_ips", lambda *a, **k: WW.HOME / "x.ips")
    monkeypatch.setattr(WW, "parse_ips_summary", lambda p: {"signal": "SIGSEGV"})
    notes = []
    monkeypatch.setattr(WW, "notify", lambda t, m, **k: notes.append((t, m)))
    monkeypatch.setattr(WW, "resume_workspace",
                        lambda meta, gov, **k: {"ok": False, "error": "spawn died"})
    reg = WW.WorkspaceRegistry()
    monkeypatch.setattr(reg, "refresh", lambda: None)
    _seed_registry(reg, "U1", {"ref": 4, "title": "Work",
                               "resume_command": "claude", "cwd": "/w"})
    gov = WW.ResumeGovernor()
    WW.handle_close(_close_evt("U1"), reg, gov)
    drop = json.loads(list(WW.CRASH_EVENTS_DIR.glob("*.json"))[0].read_text())
    assert drop["resume"]["attempted"] is True
    assert drop["resume"]["ok"] is False
    assert any("failed" in t for t, _ in notes)


def test_handle_close_unknown_workspace_fallback(home, monkeypatch):
    monkeypatch.setattr(WW, "find_recent_cmux_ips", lambda *a, **k: None)
    monkeypatch.setattr(WW, "notify", lambda *a, **k: None)
    reg = WW.WorkspaceRegistry()
    monkeypatch.setattr(reg, "refresh", lambda: None)
    gov = WW.ResumeGovernor()
    # uuid not in registry → falls back to {"title": "(unknown)"}.
    WW.handle_close(_close_evt("GHOST"), reg, gov)
    drop = json.loads(list(WW.CRASH_EVENTS_DIR.glob("*.json"))[0].read_text())
    assert drop["name"] == "(unknown)"
    assert drop["workspace_ref"] == "workspace:?"


def test_handle_close_uuid_from_payload_nested(home, monkeypatch):
    monkeypatch.setattr(WW, "find_recent_cmux_ips", lambda *a, **k: None)
    monkeypatch.setattr(WW, "notify", lambda *a, **k: None)
    reg = WW.WorkspaceRegistry()
    monkeypatch.setattr(reg, "refresh", lambda: None)
    _seed_registry(reg, "NESTED", {"ref": 2, "title": "T", "cwd": "/w"})
    gov = WW.ResumeGovernor()
    evt = {"name": "workspace.closed",
           "payload": {"workspace": {"id": "NESTED"}}}
    WW.handle_close(evt, reg, gov)
    drop = json.loads(list(WW.CRASH_EVENTS_DIR.glob("*.json"))[0].read_text())
    assert drop["workspace_id"] == "NESTED"


# --------------------------------------------------------------------------- stream()

class _FakeProc:
    def __init__(self, lines):
        self.stdout = iter(lines)


def test_stream_dispatches_handlers(home, monkeypatch):
    lines = [
        json.dumps({"name": "workspace.created"}) + "\n",
        "\n",                       # blank — skipped
        "{ not json\n",             # malformed — skipped
        json.dumps({"name": "workspace.closed", "workspace_id": "U1"}) + "\n",
        json.dumps({"name": "workspace.unknown"}) + "\n",  # ignored branch
    ]
    monkeypatch.setattr(WW.subprocess, "Popen", lambda *a, **k: _FakeProc(lines))
    refreshes = []
    closes = []
    reg = WW.WorkspaceRegistry()
    monkeypatch.setattr(reg, "refresh", lambda: refreshes.append(1))
    monkeypatch.setattr(WW, "handle_close",
                        lambda evt, r, g: closes.append(evt.get("workspace_id")))
    gov = WW.ResumeGovernor()
    WW.stream(reg, gov)
    assert refreshes == [1]            # workspace.created triggered one refresh
    assert closes == ["U1"]            # workspace.closed routed to handle_close


def test_stream_handle_close_exception_swallowed(home, monkeypatch):
    lines = [json.dumps({"name": "workspace.closed", "workspace_id": "U1"}) + "\n"]
    monkeypatch.setattr(WW.subprocess, "Popen", lambda *a, **k: _FakeProc(lines))
    reg = WW.WorkspaceRegistry()

    def boom(evt, r, g):
        raise ValueError("kaboom")
    monkeypatch.setattr(WW, "handle_close", boom)
    gov = WW.ResumeGovernor()
    # The except in stream() logs and continues — no raise escapes.
    WW.stream(reg, gov)


def test_stream_no_stdout_raises(home, monkeypatch):
    class NoStdout:
        stdout = None
    monkeypatch.setattr(WW.subprocess, "Popen", lambda *a, **k: NoStdout())
    reg = WW.WorkspaceRegistry()
    gov = WW.ResumeGovernor()
    with pytest.raises(RuntimeError, match="no stdout"):
        WW.stream(reg, gov)


# --------------------------------------------------------------------------- main()

def test_main_returns_0_on_keyboard_interrupt(home, monkeypatch):
    monkeypatch.setattr(WW, "WorkspaceRegistry", lambda: type("R", (), {"refresh": lambda self: None})())
    monkeypatch.setattr(WW, "ResumeGovernor", lambda: object())

    def interrupt(reg, gov):
        raise KeyboardInterrupt
    monkeypatch.setattr(WW, "stream", interrupt)
    assert WW.main() == 0


def test_main_retries_on_stream_crash_then_exits(home, monkeypatch):
    # First stream() raises a generic Exception → except logs + sleeps + retries
    # (lines 673-675); second raises KeyboardInterrupt → clean exit.
    monkeypatch.setattr(WW, "WorkspaceRegistry", lambda: type("R", (), {"refresh": lambda self: None})())
    monkeypatch.setattr(WW, "ResumeGovernor", lambda: object())
    slept = []
    monkeypatch.setattr(WW.time, "sleep", lambda s: slept.append(s))
    calls = {"n": 0}

    def flaky(reg, gov):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("socket dropped")
        raise KeyboardInterrupt
    monkeypatch.setattr(WW, "stream", flaky)
    assert WW.main() == 0
    assert slept == [5]            # the retry path slept once for 5s
    assert calls["n"] == 2
