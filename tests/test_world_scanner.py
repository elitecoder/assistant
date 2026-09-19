"""Tests for bin/world-scanner.py — the single observer that builds world.json.

Loaded by file path (hyphenated CLI, not an importable module). HOME is pointed
at a tmp dir BEFORE import so every module-level path constant (OUT_PATH,
CMUX_REGISTRY, ORCH_REGISTRY, SESSION_CTX, ...) binds under the tmp HOME. No real
~/.claude or ~/.architect is touched, and cmux/ps/vm_stat are never shelled out:
every test monkeypatches the module's cmux_tree / ps_tty / read_mem_pct /
subprocess.run.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCANNER_PATH = REPO / "bin/world-scanner.py"


def load_scanner(home: Path):
    """Import bin/world-scanner.py with HOME pointed at `home`. Every
    HOME-derived constant is computed at import, so each call gives clean
    paths under the tmp HOME."""
    os.environ["HOME"] = str(home)
    spec = importlib.util.spec_from_file_location("world_scanner_mod", str(SCANNER_PATH))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def ws(tmp_path, monkeypatch):
    """Fresh module bound to a tmp HOME."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return load_scanner(home)


def fake_completed(stdout="", returncode=0):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


# ─── time helpers ─────────────────────────────────────────────────────────────

def test_utc_now_no_microseconds(ws):
    now = ws.utc_now()
    assert now.microsecond == 0
    assert now.tzinfo is not None


def test_iso_uses_z_suffix(ws):
    now = ws.utc_now()
    s = ws.iso(now)
    assert s.endswith("Z")
    assert "+00:00" not in s


def test_parse_iso_valid(ws):
    dt = ws.parse_iso("2026-06-09T12:00:00Z")
    assert dt is not None
    assert dt.year == 2026 and dt.hour == 12
    # Round-trips through iso().
    assert ws.iso(dt) == "2026-06-09T12:00:00Z"


def test_parse_iso_none(ws):
    assert ws.parse_iso(None) is None
    assert ws.parse_iso("") is None


def test_parse_iso_garbage(ws):
    assert ws.parse_iso("not-a-date") is None
    assert ws.parse_iso("2026-13-99") is None


# ─── load_json ────────────────────────────────────────────────────────────────

def test_load_json_valid(ws, tmp_path):
    p = tmp_path / "x.json"
    p.write_text(json.dumps({"a": 1}))
    assert ws.load_json(p) == {"a": 1}


def test_load_json_missing_default_none_returns_empty_dict(ws, tmp_path):
    # default=None means "no caller default" → {}.
    assert ws.load_json(tmp_path / "nope.json") == {}


def test_load_json_missing_explicit_default(ws, tmp_path):
    sentinel = {"items": [], "completed": []}
    assert ws.load_json(tmp_path / "nope.json", sentinel) == sentinel


def test_load_json_malformed_returns_default(ws, tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{ not json")
    assert ws.load_json(p, ["fallback"]) == ["fallback"]
    assert ws.load_json(p) == {}


# ─── load_json_dir ──────────────────────────────────────────────────────────

def test_load_json_dir_reads_all_and_skips_malformed(ws, tmp_path):
    d = tmp_path / "jsons"
    d.mkdir()
    (d / "a.json").write_text(json.dumps({"id": "a"}))
    (d / "b.json").write_text(json.dumps({"id": "b"}))
    (d / "c.json").write_text("{ broken")  # skipped
    (d / "ignore.txt").write_text("not picked up")
    out = ws.load_json_dir(d)
    ids = sorted(e["id"] for e in out)
    assert ids == ["a", "b"]


def test_load_json_dir_missing_returns_empty(ws, tmp_path):
    assert ws.load_json_dir(tmp_path / "no-such-dir") == []


# ─── pid_alive ──────────────────────────────────────────────────────────────

def test_pid_alive_none(ws):
    assert ws.pid_alive(None) is False
    assert ws.pid_alive(0) is False


def test_pid_alive_self(ws):
    assert ws.pid_alive(os.getpid()) is True
    assert ws.pid_alive(str(os.getpid())) is True


def test_pid_alive_bogus(ws):
    # A very high pid that almost certainly does not exist.
    assert ws.pid_alive(99999999) is False


def test_pid_alive_non_int(ws):
    assert ws.pid_alive("not-a-pid") is False


# ─── ps_tty ───────────────────────────────────────────────────────────────────

def test_ps_tty_returns_stripped_tty(ws, monkeypatch):
    monkeypatch.setattr(ws.subprocess, "run", lambda *a, **k: fake_completed(stdout="ttys003\n"))
    assert ws.ps_tty(1234) == "ttys003"


def test_ps_tty_empty_returns_none(ws, monkeypatch):
    monkeypatch.setattr(ws.subprocess, "run", lambda *a, **k: fake_completed(stdout="  \n"))
    assert ws.ps_tty(1234) is None


def test_ps_tty_exception_returns_none(ws, monkeypatch):
    def boom(*a, **k):
        raise OSError("no ps")
    monkeypatch.setattr(ws.subprocess, "run", boom)
    assert ws.ps_tty(1234) is None


# ─── cmux_tree ──────────────────────────────────────────────────────────────

def test_cmux_tree_success(ws, monkeypatch):
    tree = {"windows": [{"workspaces": []}]}
    monkeypatch.setattr(ws.subprocess, "run",
                        lambda *a, **k: fake_completed(stdout=json.dumps(tree), returncode=0))
    assert ws.cmux_tree() == tree


def test_cmux_tree_nonzero_rc_returns_none(ws, monkeypatch):
    monkeypatch.setattr(ws.subprocess, "run",
                        lambda *a, **k: fake_completed(stdout="garbage", returncode=2))
    assert ws.cmux_tree() is None


def test_cmux_tree_exception_logs_and_returns_none(ws, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("cmux gone")
    monkeypatch.setattr(ws.subprocess, "run", boom)
    assert ws.cmux_tree() is None
    # log() wrote to LOG_DIR/world-scanner.out under tmp HOME.
    logfile = ws.LOG_DIR / "world-scanner.out"
    assert logfile.exists()
    assert "cmux tree failed" in logfile.read_text()


# ─── read_mem_pct ─────────────────────────────────────────────────────────────

_VM_STAT = """\
Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                              100000.
Pages active:                            300000.
Pages inactive:                          150000.
Pages speculative:                        50000.
Pages wired down:                        200000.
"""


def test_read_mem_pct_plausible(ws, monkeypatch):
    monkeypatch.setattr(ws.subprocess, "run", lambda *a, **k: fake_completed(stdout=_VM_STAT))
    pct = ws.read_mem_pct()
    # free = 100000+150000+50000 = 300000; total = 800000 → used = 100*(1-300000/800000) = 62.5
    assert isinstance(pct, float)
    assert 0.0 <= pct <= 100.0
    assert pct == 62.5


def test_read_mem_pct_total_zero_returns_none(ws, monkeypatch):
    # No "Pages " lines at all → total == 0 → None.
    monkeypatch.setattr(ws.subprocess, "run",
                        lambda *a, **k: fake_completed(stdout="Mach Virtual Memory Statistics:\n"))
    assert ws.read_mem_pct() is None


def test_read_mem_pct_exception_returns_none(ws, monkeypatch):
    def boom(*a, **k):
        raise OSError("no vm_stat")
    monkeypatch.setattr(ws.subprocess, "run", boom)
    assert ws.read_mem_pct() is None


# ─── build_workspace_index ───────────────────────────────────────────────────

def _tree_fixture():
    return {
        "windows": [
            {
                "workspaces": [
                    {
                        "ref": "workspace:1",
                        "title": "Alpha",
                        "index": 0,
                        "panes": [
                            {"surfaces": [
                                {"ref": "surface:1a", "tty": "ttys001",
                                 "type": "terminal", "title": "term"},
                                {"ref": "surface:1b", "tty": None,
                                 "type": "browser", "title": "web"},
                            ]},
                        ],
                    },
                    {
                        "ref": "workspace:2",
                        "title": None,  # falls back to ""
                        "index": 1,
                        "panes": [
                            {"surfaces": [
                                {"ref": "surface:2a", "tty": "ttys002",
                                 "type": "terminal", "title": None},
                            ]},
                        ],
                    },
                ]
            }
        ]
    }


def test_build_workspace_index(ws):
    out = ws.build_workspace_index(_tree_fixture())
    assert len(out) == 2
    a, b = out
    assert a["ws_ref"] == "workspace:1"
    assert a["title"] == "Alpha"
    assert a["index"] == 0
    assert len(a["surfaces"]) == 2
    s0 = a["surfaces"][0]
    assert s0 == {
        "ref": "surface:1a", "surface_id": None, "tty": "ttys001",
        "type": "terminal", "title": "term", "session_id": None,
        "provider": None, "identity_status": "unknown", "agent_status": "unknown",
    }
    # None title coerced to "".
    assert b["title"] == ""
    assert b["surfaces"][0]["title"] == ""


def test_build_workspace_index_none_tree(ws):
    assert ws.build_workspace_index(None) == []


# ─── build_live_sessions ──────────────────────────────────────────────────────

def test_build_live_sessions_filters_dead_and_dedups(ws):
    live = os.getpid()
    reg = {
        "tab-live-old": {"claude_pid": live, "session_id": "sess-A",
                         "cwd": "/work/a", "transcript_path": "/t/a-old.jsonl", "ts": 100},
        "tab-live-new": {"claude_pid": live, "session_id": "sess-A",
                         "cwd": "/work/a2", "transcript_path": "/t/a-new.jsonl", "ts": 200},
        "tab-dead": {"claude_pid": 99999999, "session_id": "sess-B",
                     "cwd": "/work/b", "transcript_path": "/t/b.jsonl", "ts": 300},
        "tab-no-sid": {"claude_pid": live, "session_id": None,
                       "cwd": "/work/c", "ts": 400},
    }
    ws.CMUX_REGISTRY.parent.mkdir(parents=True, exist_ok=True)
    ws.CMUX_REGISTRY.write_text(json.dumps(reg))

    out = ws.build_live_sessions()
    # Dead pid filtered, sid-less filtered → only sess-A remains.
    assert set(out.keys()) == {"sess-A"}
    a = out["sess-A"]
    # Dedup keeps the most-recent ts (200, cwd /work/a2).
    assert a["ts"] == 200
    assert a["cwd"] == "/work/a2"
    assert a["pid"] == live
    assert a["tab_id"] == "tab-live-new"
    assert a["transcript_path"] == "/t/a-new.jsonl"


def test_build_live_sessions_dedup_keeps_first_when_second_is_older(ws):
    # When the newer entry is encountered FIRST, the older one that follows must
    # be skipped (hits the prev.ts > e.ts guard).
    live = os.getpid()
    reg = {
        "tab-new": {"claude_pid": live, "session_id": "sess-A",
                    "cwd": "/work/new", "ts": 200},
        "tab-old": {"claude_pid": live, "session_id": "sess-A",
                    "cwd": "/work/old", "ts": 100},
    }
    ws.CMUX_REGISTRY.parent.mkdir(parents=True, exist_ok=True)
    ws.CMUX_REGISTRY.write_text(json.dumps(reg))
    out = ws.build_live_sessions()
    assert out["sess-A"]["ts"] == 200
    assert out["sess-A"]["cwd"] == "/work/new"


def test_build_live_sessions_adds_bound_droid_session(ws, monkeypatch):
    session_id = "d0d01234-aaaa-bbbb-cccc-dddddddddddd"
    transcript = ws.HOME / ".factory/sessions/-work-repo" / f"{session_id}.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text("{}\n")
    workspaces = [{
        "ws_ref": "workspace:7", "title": "Droid task",
        "surfaces": [{
            "ref": "surface:9", "tty": "ttys009", "type": "terminal",
            "title": "droid",
        }],
    }]
    monkeypatch.setattr(ws, "surface_resume_binding", lambda *_: {
        "session_id": session_id, "provider": "droid", "cwd": "/work/repo",
    })
    monkeypatch.setattr(ws, "agent_pid_on_tty", lambda *_: 1234)
    out = ws.build_live_sessions(workspaces)
    assert out[session_id]["provider"] == "droid"
    assert out[session_id]["transcript_path"] == str(transcript)
    assert out[session_id]["ws_ref"] == "workspace:7"


def _seed_identity_world(ws, monkeypatch, kinds=("claude", "factory")):
    now = ws.parse_iso("2026-09-19T09:00:00Z")
    first = ws.parse_iso("2026-09-01T09:00:00Z")
    workspaces, records, contexts, starts, calls = [], [], {}, [], []
    for index, kind in enumerate(kinds, 1):
        provider = "droid" if kind in {"factory", "droid"} else kind
        sid = f"session-{index}"
        workspace_id = f"aaaaaaaa-0000-0000-0000-{index:012d}"
        surface_id = f"bbbbbbbb-0000-0000-0000-{index:012d}"
        workspaces.append({
            "id": workspace_id, "ref": f"workspace:{index}", "title": sid,
            "panes": [{"surfaces": [{
                "id": surface_id, "ref": f"surface:{index}",
                "tty": f"ttys{index:03d}", "type": "terminal", "title": kind,
            }]}],
        })
        records.append({
            "session_id": sid, "workspace_id": workspace_id,
            "surface_id": surface_id, "provider": provider,
        })
        contexts[sid] = {
            "session_id": sid, "provider": provider,
            "last_user": {"text": f"Context for {sid}", "ts": ws.iso(now - timedelta(minutes=1))},
            "queue_pending": 0, "user_unanswered": True, "recent_turns": [],
        }
        starts.append({
            "event": "start", "ts": first.timestamp(), "session_id": sid,
            "workspace_id": workspace_id.upper(), "provider": kind,
        })
        if kind in {"claude", "factory", "droid"}:
            root = ".claude/projects" if provider == "claude" else ".factory/sessions"
            path = ws.HOME / root / "same-cwd" / f"{sid}.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{}\n")

    def run(args, **kwargs):
        calls.append(args)
        if "tree" in args:
            return fake_completed(json.dumps({"windows": [{"workspaces": workspaces}]}))
        if args[1:4] == ["surface", "resume", "show"]:
            target = args[args.index("--surface") + 1]
            index = next(
                i for i, record in enumerate(records, 1)
                if target in {f"surface:{i}", record["surface_id"]}
            )
            kind = kinds[index - 1]
            binding = None if kind is None else {
                "kind": kind, "checkpoint_id": f"session-{index}", "cwd": "/work/shared",
            }
            return fake_completed(json.dumps({"binding": binding}))
        if args[:2] == ["ps", "-t"]:
            index = int(args[2][-3:])
            return fake_completed(f"{100 + index} {records[index - 1]['provider']}\n")
        if args[:2] == ["ps", "-p"]:
            return fake_completed("ttys001\n")
        raise AssertionError(f"Unexpected external command: {args}")

    monkeypatch.setattr(ws.subprocess, "run", run)
    monkeypatch.setattr(ws, "utc_now", lambda: now)
    monkeypatch.setattr(ws, "read_mem_pct", lambda: 42.0)
    monkeypatch.setattr(ws, "pid_alive", lambda pid: bool(pid))
    ws.SESSION_CTX.parent.mkdir(parents=True)
    ws.SESSION_CTX.write_text(json.dumps({"by_session": contexts}))
    ledger = ws.HOME / ".cmux-session-ledger.jsonl"
    ledger.write_text("".join(json.dumps(row) + "\n" for row in starts))
    return records, contexts, ledger, calls


def test_build_identity_keeps_same_cwd_sessions_separate(ws, monkeypatch):
    records, _, _, calls = _seed_identity_world(ws, monkeypatch)
    ws.build()
    world = json.loads(ws.OUT_PATH.read_text())
    assert any(args[1:4] == ["--id-format", "both", "tree"] for args in calls)
    binding_calls = [args for args in calls if args[1:4] == ["surface", "resume", "show"]]
    assert len(binding_calls) == 2
    for args, expected in zip(binding_calls, records):
        assert args[args.index("--surface") + 1] == expected["surface_id"]
        assert args[args.index("--workspace") + 1] == expected["workspace_id"]
    assert len(world["live_sessions"]) == 2
    for workspace, session, expected in zip(world["workspaces"], world["live_sessions"], records):
        assert workspace["workspace_id"] == expected["workspace_id"]
        assert workspace["surfaces"][0]["surface_id"] == expected["surface_id"]
        assert workspace["surfaces"][0]["session_id"] == expected["session_id"]
        assert workspace["surfaces"][0]["identity_status"] == "verified"
        assert workspace["session_ids"] == [expected["session_id"]]
        assert session["workspace_id"] == expected["workspace_id"]
        assert session["surface_id"] == expected["surface_id"]
        assert session["provider"] == expected["provider"]
        assert session["context_status"] == "verified"
        assert session["last_user"]["text"] == f"Context for {expected['session_id']}"


def test_build_recycled_ref_rejects_old_registry_identity(ws, monkeypatch):
    _, contexts, ledger, _ = _seed_identity_world(ws, monkeypatch, ("claude",))
    ws.CMUX_REGISTRY.write_text(json.dumps({
        "old-tab": {
            "claude_pid": 999, "session_id": "old-session", "ts": 10,
            "cwd": "/work/shared", "workspace_id": "old-workspace",
            "surface_id": "old-surface",
        },
    }))
    contexts["old-session"] = {
        "provider": "claude", "last_user": {"text": "Old task", "ts": "2026-01-01T00:00:00Z"},
    }
    ws.SESSION_CTX.write_text(json.dumps({"by_session": contexts}))
    with ledger.open("a") as f:
        f.write(json.dumps({
            "event": "start", "ts": 1, "workspace_id": "old-workspace",
            "provider": "claude", "session_id": "session-1",
        }) + "\n")
    ws.build()
    world = json.loads(ws.OUT_PATH.read_text())
    assert world["workspaces"][0]["session_ids"] == ["session-1"]
    old = next(s for s in world["live_sessions"] if s["session_id"] == "old-session")
    assert old.get("ws_ref") is None
    assert old["identity_status"] == "unknown"
    assert old["context_status"] == "unknown"
    assert old["first_recorded_at"] is None
    current = next(s for s in world["live_sessions"] if s["session_id"] == "session-1")
    assert current["first_recorded_at"] == "2026-09-01T09:00:00Z"


def test_first_recorded_is_not_last_activity(ws, monkeypatch):
    _, contexts, ledger, _ = _seed_identity_world(ws, monkeypatch, ("claude",))
    with ledger.open("a") as f:
        f.write(json.dumps({
            "event": "start", "ts": ws.parse_iso("2026-09-18T09:00:00Z").timestamp(),
            "workspace_id": "aaaaaaaa-0000-0000-0000-000000000001",
            "provider": "claude", "session_id": "session-1",
        }) + "\n")
    for timestamp in ["2026-09-19T08:50:00Z", "2026-09-19T08:59:00Z"]:
        contexts["session-1"]["last_user"]["ts"] = timestamp
        ws.SESSION_CTX.write_text(json.dumps({"by_session": contexts}))
        ws.build()
        session = json.loads(ws.OUT_PATH.read_text())["live_sessions"][0]
        assert session["first_recorded_at"] == "2026-09-01T09:00:00Z"
        assert session["last_turn_ts"] == timestamp


def test_context_and_ledger_accept_uuid_casing_differences(ws, monkeypatch):
    records, contexts, _, _ = _seed_identity_world(ws, monkeypatch, ("claude",))
    contexts["session-1"].update({
        "workspace_id": records[0]["workspace_id"].upper(),
        "surface_id": records[0]["surface_id"].upper(),
    })
    ws.SESSION_CTX.write_text(json.dumps({"by_session": contexts}))
    ws.build()
    world = json.loads(ws.OUT_PATH.read_text())
    session = world["live_sessions"][0]
    assert session["identity_status"] == "verified"
    assert session["context_status"] == "verified"
    assert session["first_recorded_at"] == "2026-09-01T09:00:00Z"
    assert world["workspaces"][0]["session_ids"] == ["session-1"]


@pytest.mark.parametrize("kind,provider,root", [
    ("claude", "claude", ".claude/projects"),
    ("factory", "droid", ".factory/sessions"),
    ("droid", "droid", ".factory/sessions"),
])
def test_identity_preserves_supported_provider_routes(ws, monkeypatch, kind, provider, root):
    _seed_identity_world(ws, monkeypatch, (kind,))
    ws.build()
    session = json.loads(ws.OUT_PATH.read_text())["live_sessions"][0]
    assert session["provider"] == provider
    assert session["transcript_path"] == str(ws.HOME / root / "same-cwd/session-1.jsonl")
    assert session["identity_status"] == "verified"
    assert session["context_status"] == "verified"
    assert session["first_recorded_at"] == "2026-09-01T09:00:00Z"


@pytest.mark.parametrize("kind", ["copilot", None])
def test_unsupported_or_missing_binding_stays_unknown(ws, monkeypatch, kind):
    _seed_identity_world(ws, monkeypatch, (kind,))
    ws.build()
    world = json.loads(ws.OUT_PATH.read_text())
    surface = world["workspaces"][0]["surfaces"][0]
    assert surface["surface_id"] == "bbbbbbbb-0000-0000-0000-000000000001"
    assert surface["session_id"] is None
    assert surface["provider"] is None
    assert surface["identity_status"] == "unknown"
    assert surface["agent_status"] == "unknown"
    assert world["live_sessions"] == []
    assert world["workspaces"][0]["session_ids"] == []


def test_registry_without_live_binding_is_not_authoritative(ws, monkeypatch):
    records, _, _, _ = _seed_identity_world(ws, monkeypatch, ("claude",))
    monkeypatch.setattr(ws, "surface_resume_binding", lambda *_: None)
    ws.CMUX_REGISTRY.write_text(json.dumps({
        "tab": {**records[0], "claude_pid": 101, "ts": 1, "cwd": "/work/shared"},
    }))
    ws.build()
    world = json.loads(ws.OUT_PATH.read_text())
    session = world["live_sessions"][0]
    assert session["identity_status"] == "unknown"
    assert session["context_status"] == "unknown"
    assert session["first_recorded_at"] is None
    assert world["workspaces"][0]["session_ids"] == []
    assert world["workspaces"][0]["surfaces"][0]["session_id"] is None


def _seed_current_hook(ws, monkeypatch, *, changes=None, process_change=None, native=None):
    records, _, _, _ = _seed_identity_world(ws, monkeypatch, ("claude",))
    registered = ws.utc_now().timestamp() - 60
    row = {
        "session_id": "session-1",
        "surface_id": records[0]["surface_id"].upper(),
        "claude_pid": "",
        "ts": registered,
        "cwd": "/work/shared",
    }
    row.update(changes or {})
    ws.CMUX_REGISTRY.write_text(json.dumps({records[0]["workspace_id"].upper(): row}))
    process = {
        "pid": 101, "pgid": 101, "tpgid": 101,
        "started": registered - 2, "command": "/Users/fixture/.local/bin/claude",
    }
    process.update(process_change or {})
    original_run = ws.subprocess.run
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        if args[1:4] == ["surface", "resume", "show"]:
            return fake_completed(json.dumps({"binding": native}))
        if args[:2] == ["ps", "-t"] and "lstart" in args[-1]:
            assert kwargs["env"]["TZ"] == "UTC"
            started = datetime.fromtimestamp(
                process["started"], tz=timezone.utc,
            ).strftime("%a %b %d %H:%M:%S %Y")
            return fake_completed(
                f"{process['pid']} {process['pgid']} {process['tpgid']} "
                f"{started} {process['command']}\n"
            )
        return original_run(args, **kwargs)

    monkeypatch.setattr(ws.subprocess, "run", run)
    return calls


@pytest.mark.parametrize("recorded_pid", ["", "101"])
def test_current_session_start_hook_verifies_missing_native_binding(ws, monkeypatch, recorded_pid):
    calls = _seed_current_hook(ws, monkeypatch, changes={"claude_pid": recorded_pid})
    ws.build()
    world = json.loads(ws.OUT_PATH.read_text())
    session = world["live_sessions"][0]
    assert session["session_id"] == "session-1"
    assert session["pid"] == 101
    assert session["identity_status"] == "verified"
    assert session["identity_source"] == "session_start_registry"
    assert session["context_status"] == "verified"
    assert session["first_recorded_at"] == "2026-09-01T09:00:00Z"
    assert world["workspaces"][0]["session_ids"] == ["session-1"]
    assert world["workspaces"][0]["surfaces"][0]["session_id"] == "session-1"
    assert any(args[:3] == ["ps", "-t", "ttys001"] for args in calls)


@pytest.mark.parametrize("changes,process_change", [
    ({"surface_id": "different-surface"}, {}),
    ({"workspace_id": "different-workspace"}, {}),
    ({"claude_pid": "999"}, {}),
    ({"ts": 1}, {}),
    ({"ts": "not-a-timestamp"}, {}),
    ({"provider": "copilot"}, {}),
    ({}, {"started": datetime.fromisoformat("2026-09-19T08:59:30+00:00").timestamp()}),
    ({"claude_pid": "101"}, {"started": datetime.fromisoformat("2026-09-19T08:59:30+00:00").timestamp()}),
    ({}, {"tpgid": 202}),
    ({}, {"command": "/usr/bin/login"}),
])
def test_hook_fallback_rejects_stale_or_mismatched_process(ws, monkeypatch, changes, process_change):
    _seed_current_hook(ws, monkeypatch, changes=changes, process_change=process_change)
    ws.build()
    world = json.loads(ws.OUT_PATH.read_text())
    assert world["workspaces"][0]["session_ids"] == []
    assert world["workspaces"][0]["surfaces"][0]["identity_status"] == "unknown"
    assert not any(s["identity_status"] == "verified" for s in world["live_sessions"])


@pytest.mark.parametrize("kind", ["claude", "copilot"])
def test_native_binding_conflict_never_uses_hook_fallback(ws, monkeypatch, kind):
    calls = _seed_current_hook(ws, monkeypatch, native={
        "kind": kind, "checkpoint_id": "different-session", "cwd": "/work/shared",
    })
    ws.build()
    world = json.loads(ws.OUT_PATH.read_text())
    assert "session-1" not in world["workspaces"][0]["session_ids"]
    assert not any("lstart" in args[-1] for args in calls)


def test_failed_native_lookup_does_not_fall_back_to_hook(ws, monkeypatch):
    _seed_current_hook(ws, monkeypatch)
    original_run = ws.subprocess.run

    def run(args, **kwargs):
        if args[1:4] == ["surface", "resume", "show"]:
            return fake_completed(returncode=1)
        assert "lstart" not in args[-1]
        return original_run(args, **kwargs)

    monkeypatch.setattr(ws.subprocess, "run", run)
    ws.build()
    world = json.loads(ws.OUT_PATH.read_text())
    assert world["workspaces"][0]["session_ids"] == []
    assert world["workspaces"][0]["surfaces"][0]["identity_status"] == "unknown"


@pytest.mark.parametrize("override", [
    {"provider": "droid"},
    {"session_id": "different-session"},
    {"session_id": "SESSION-1"},
    {"workspace_id": "different-workspace"},
    {"surface_id": "different-surface"},
])
def test_context_with_wrong_identity_is_not_attached(ws, monkeypatch, override):
    _, contexts, _, _ = _seed_identity_world(ws, monkeypatch, ("claude",))
    contexts["session-1"].update(override)
    ws.SESSION_CTX.write_text(json.dumps({"by_session": contexts}))
    ws.build()
    session = json.loads(ws.OUT_PATH.read_text())["live_sessions"][0]
    assert session["identity_status"] == "verified"
    assert session["context_status"] == "unknown"
    assert "last_user" not in session


def test_missing_ledger_keeps_first_recorded_unknown(ws, monkeypatch):
    _seed_identity_world(ws, monkeypatch, ("claude",))
    monkeypatch.setattr(ws, "SESSION_LEDGER", ws.HOME / "missing-ledger.jsonl")
    ws.build()
    session = json.loads(ws.OUT_PATH.read_text())["live_sessions"][0]
    assert session["first_recorded_at"] is None
    assert session["identity_status"] == "verified"


@pytest.mark.parametrize("override", [
    {"workspace_id": "different-workspace"},
    {"session_id": "different-session"},
    {"session_id": "SESSION-1"},
    {"provider": "droid"},
    {"provider": None},
    {"event": "end"},
    {"ts": "not-a-time"},
    {"ts": 999999999999999},
])
def test_unmatched_recorded_start_stays_unknown(ws, monkeypatch, override):
    _, _, ledger, _ = _seed_identity_world(ws, monkeypatch, ("claude",))
    record = json.loads(ledger.read_text())
    record.update(override)
    ledger.write_text("invalid\n[]\n" + json.dumps(record) + "\n")
    ws.build()
    session = json.loads(ws.OUT_PATH.read_text())["live_sessions"][0]
    assert session["first_recorded_at"] is None


# ─── join_workspaces_to_sessions ─────────────────────────────────────────────

def test_join_workspaces_to_sessions(ws, monkeypatch):
    workspaces = ws.build_workspace_index(_tree_fixture())
    live = {
        "sess-A": {"session_id": "sess-A", "pid": 111, "cwd": "/work/a"},
        "sess-B": {"session_id": "sess-B", "pid": 222, "cwd": "/work/b"},
    }
    # sess-A's pid maps to ttys001 (workspace:1, surface:1a); sess-B → no tty.
    tty_map = {111: "ttys001", 222: None}
    monkeypatch.setattr(ws, "ps_tty", lambda pid: tty_map.get(pid))

    ws.join_workspaces_to_sessions(workspaces, live)

    a = live["sess-A"]
    assert a["tty"] == "ttys001"
    assert a["ws_ref"] == "workspace:1"
    assert a["surface_ref"] == "surface:1a"
    assert a["ws_title"] == "Alpha"
    assert a["surface_title"] == "term"

    b = live["sess-B"]
    assert b["tty"] is None
    assert "ws_ref" not in b  # no join

    # Back-reference: workspace:1 lists sess-A; workspace:2 lists nobody.
    ws1 = next(w for w in workspaces if w["ws_ref"] == "workspace:1")
    ws2 = next(w for w in workspaces if w["ws_ref"] == "workspace:2")
    assert ws1["session_ids"] == ["sess-A"]
    assert ws2["session_ids"] == []


# ─── tag_cron_workers ─────────────────────────────────────────────────────────

def test_tag_cron_workers(ws):
    oreg = {"workers": {
        "probe-runner": {"workspace_ref": "workspace:9"},
        "no-ref": {},
    }}
    ws.ORCH_REGISTRY.parent.mkdir(parents=True, exist_ok=True)
    ws.ORCH_REGISTRY.write_text(json.dumps(oreg))

    # cron cwd is derived from the module's HOME (portable), not a literal
    architect_cwd = f"{ws.HOME}/.architect/"
    live = {
        "by-ws": {"session_id": "by-ws", "ws_ref": "workspace:9", "cwd": "/somewhere"},
        "by-cwd": {"session_id": "by-cwd", "ws_ref": "workspace:5",
                   "cwd": architect_cwd},
        "human": {"session_id": "human", "ws_ref": "workspace:5", "cwd": "/work/repo"},
    }
    ws.tag_cron_workers(live)
    assert live["by-ws"]["is_cron"] is True
    assert live["by-cwd"]["is_cron"] is True
    assert live["human"]["is_cron"] is False


# ─── merge_session_context ────────────────────────────────────────────────────

def test_merge_session_context(ws):
    ctx = {"by_session": {
        "sess-A": {
            "last_user": {"ts": "2026-06-09T11:00:00Z", "text": "hi"},
            "last_assistant": {"ts": "2026-06-09T11:01:00Z", "text": "yo"},
            "queue_pending": 2,
            "user_unanswered": True,
            "recent_turns": [{"role": "user", "text": "hi"}],
        }
    }}
    ws.SESSION_CTX.parent.mkdir(parents=True, exist_ok=True)
    ws.SESSION_CTX.write_text(json.dumps(ctx))

    live = {
        "sess-A": {"session_id": "sess-A"},
        "sess-Z": {"session_id": "sess-Z"},  # no ctx → untouched
    }
    ws.merge_session_context(live)
    a = live["sess-A"]
    assert a["last_user"]["text"] == "hi"
    assert a["last_assistant"]["ts"] == "2026-06-09T11:01:00Z"
    assert a["queue_pending"] == 2
    assert a["user_unanswered"] is True
    assert a["recent_turns"] == [{"role": "user", "text": "hi"}]
    assert "last_user" not in live["sess-Z"]


# ─── compute_session_age ──────────────────────────────────────────────────────

def test_compute_session_age_picks_latest(ws):
    now = ws.parse_iso("2026-06-09T12:00:00Z")
    sess = {
        "last_user": {"ts": "2026-06-09T11:00:00Z"},
        "last_assistant": {"ts": "2026-06-09T11:30:00Z"},  # newer
    }
    age, last_iso = ws.compute_session_age(sess, now)
    # max ts is 11:30 → 30 min = 1800s ago.
    assert age == 1800
    assert last_iso == "2026-06-09T11:30:00Z"


def test_compute_session_age_none(ws):
    now = ws.utc_now()
    assert ws.compute_session_age({}, now) == (None, None)
    # Present keys but no ts.
    assert ws.compute_session_age({"last_user": {}}, now) == (None, None)


# ─── load_inbox_recent ────────────────────────────────────────────────────────

def test_load_inbox_recent(ws):
    now = ws.parse_iso("2026-06-09T12:00:00Z")
    today = "2026-06-09"
    yesterday = "2026-06-08"

    today_dir = ws.INBOX_ARCHIVE / today
    yest_dir = ws.INBOX_ARCHIVE / yesterday
    today_dir.mkdir(parents=True)
    yest_dir.mkdir(parents=True)

    # Recent (within 24h).
    (today_dir / "recent.json").write_text(json.dumps(
        {"id": "recent", "ts": "2026-06-09T11:00:00Z"}))
    # Yesterday but within the 24h cutoff (cutoff = 2026-06-08T12:00:00Z).
    (yest_dir / "edge-in.json").write_text(json.dumps(
        {"id": "edge-in", "ts": "2026-06-08T13:00:00Z"}))
    # Yesterday but stale (before cutoff) → excluded.
    (yest_dir / "stale.json").write_text(json.dumps(
        {"id": "stale", "ts": "2026-06-08T06:00:00Z"}))
    # Malformed → skipped, no crash.
    (today_dir / "bad.json").write_text("{ broken")

    # Current (unarchived) inbox.
    inbox = ws.HOME / ".architect/orchestrator-inbox"
    inbox.mkdir(parents=True)
    (inbox / "live.json").write_text(json.dumps(
        {"id": "live", "ts": "2026-06-09T11:59:00Z"}))
    (inbox / "old-live.json").write_text(json.dumps(
        {"id": "old-live", "ts": "2026-06-01T00:00:00Z"}))  # stale

    out = ws.load_inbox_recent(now)
    ids = sorted(e["id"] for e in out)
    assert ids == ["edge-in", "live", "recent"]


def test_load_inbox_recent_skips_malformed_current_inbox(ws):
    # A malformed file in the current (unarchived) inbox is skipped, not fatal.
    now = ws.parse_iso("2026-06-09T12:00:00Z")
    inbox = ws.HOME / ".architect/orchestrator-inbox"
    inbox.mkdir(parents=True)
    (inbox / "bad.json").write_text("{ not json")
    (inbox / "good.json").write_text(json.dumps({"id": "good", "ts": "2026-06-09T11:00:00Z"}))
    out = ws.load_inbox_recent(now)
    assert [e["id"] for e in out] == ["good"]


def test_load_inbox_recent_missing_dirs(ws):
    # No archive dir, no current inbox → [].
    now = ws.utc_now()
    assert ws.load_inbox_recent(now) == []


# ─── build() integrator ──────────────────────────────────────────────────────

def _seed_world(ws, monkeypatch, now):
    """Seed every input file under the tmp HOME and stub the shell-outs."""
    live = os.getpid()
    iso_now = ws.iso(now)
    recent = ws.iso(now - timedelta(minutes=5))
    stale = ws.iso(now - timedelta(hours=48))

    # cmux tree → one workspace with a terminal surface on ttys010.
    tree = {"windows": [{"workspaces": [{
        "ref": "workspace:1", "title": "Repo", "index": 0,
        "panes": [{"surfaces": [
            {"ref": "surface:1a", "tty": "ttys010", "type": "terminal", "title": "claude"},
        ]}],
    }]}]}
    monkeypatch.setattr(ws, "cmux_tree", lambda: tree)
    monkeypatch.setattr(ws, "surface_resume_binding", lambda *_: None)
    monkeypatch.setattr(ws, "ps_tty", lambda pid: "ttys010")
    monkeypatch.setattr(ws, "read_mem_pct", lambda: 42.0)

    # cmux-registry: one live human session.
    ws.CMUX_REGISTRY.parent.mkdir(parents=True, exist_ok=True)
    ws.CMUX_REGISTRY.write_text(json.dumps({
        "tab-1": {"claude_pid": live, "session_id": "sess-A",
                  "cwd": "/work/repo", "transcript_path": "/t/a.jsonl", "ts": 100},
        "tab-dead": {"claude_pid": 99999999, "session_id": "sess-dead",
                     "cwd": "/work/x", "ts": 50},
    }))

    # orch-registry: a cron worker bound to workspace:9 (no live session there).
    ws.ORCH_REGISTRY.parent.mkdir(parents=True, exist_ok=True)
    ws.ORCH_REGISTRY.write_text(json.dumps({
        "workers": {"probe-runner": {"workspace_ref": "workspace:9"}}}))

    # session-context: sess-A active 5 min ago (truly active < 30m).
    ws.SESSION_CTX.parent.mkdir(parents=True, exist_ok=True)
    ws.SESSION_CTX.write_text(json.dumps({"by_session": {
        "sess-A": {
            "last_user": {"ts": recent},
            "last_assistant": {"ts": recent},
            "queue_pending": 0,
            "user_unanswered": False,
            "recent_turns": [],
        }
    }}))

    # dashboard-state with a _meta block.
    ws.DASHBOARD_STATE.parent.mkdir(parents=True, exist_ok=True)
    ws.DASHBOARD_STATE.write_text(json.dumps({"_meta": {"version": 7}}))

    # todo board: 3 items, two are P0/P1.
    ws.TODO_PATH.parent.mkdir(parents=True, exist_ok=True)
    ws.TODO_PATH.write_text(json.dumps({
        "items": [
            {"id": "td-1", "priority": "P0"},
            {"id": "td-2", "priority": "P1"},
            {"id": "td-3", "priority": "P3"},
        ],
        "completed": [{"id": "td-old"}],
    }))

    # proposals: exercise included + excluded states.
    ws.PROPOSALS_DIR.mkdir(parents=True, exist_ok=True)
    # awaiting + open: needs_you flag, status open.
    (ws.PROPOSALS_DIR / "p-needs-you.json").write_text(json.dumps(
        {"id": "p1", "needs_you": True, "status": "open"}))
    # awaiting + open: status == "needs_you".
    (ws.PROPOSALS_DIR / "p-status-needs.json").write_text(json.dumps(
        {"id": "p2", "status": "needs_you"}))
    # awaiting + open: held=True.
    (ws.PROPOSALS_DIR / "p-held.json").write_text(json.dumps(
        {"id": "p3", "held": True, "status": "open"}))
    # held but DONE → open=no, awaiting=no (status in excluded set).
    (ws.PROPOSALS_DIR / "p-held-done.json").write_text(json.dumps(
        {"id": "p4", "held": True, "status": "done"}))
    # plain open, not awaiting → counts toward open only.
    (ws.PROPOSALS_DIR / "p-open-plain.json").write_text(json.dumps(
        {"id": "p5", "status": "open"}))
    # vetoed → excluded from both.
    (ws.PROPOSALS_DIR / "p-vetoed.json").write_text(json.dumps(
        {"id": "p6", "needs_you": True, "status": "vetoed"}))

    # ledger: one recent (within 24h), one stale (48h ago → excluded).
    ws.LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    (ws.LEDGER_DIR / "fire-recent.json").write_text(json.dumps(
        {"id": "f1", "ts": recent}))
    (ws.LEDGER_DIR / "fire-stale.json").write_text(json.dumps(
        {"id": "f2", "ts": stale}))

    # inbox archive: one recent event today.
    today = now.strftime("%Y-%m-%d")
    arch = ws.INBOX_ARCHIVE / today
    arch.mkdir(parents=True)
    (arch / "evt.json").write_text(json.dumps({"id": "e1", "ts": recent}))

    return iso_now


def test_build_writes_world_and_counts(ws, monkeypatch):
    now = ws.utc_now()
    monkeypatch.setattr(ws, "utc_now", lambda: now)
    iso_now = _seed_world(ws, monkeypatch, now)

    ws.build()

    assert ws.OUT_PATH.exists()
    payload = json.loads(ws.OUT_PATH.read_text())

    # _meta
    assert payload["_meta"]["built_at"] == iso_now
    assert payload["_meta"]["scanner_version"] == 1
    assert payload["_meta"]["memory_pct"] == 42.0

    counts = payload["counts"]
    assert counts["workspaces"] == 1
    assert counts["live_sessions"] == 1            # dead pid filtered
    assert counts["human_sessions"] == 1           # sess-A is human
    assert counts["cron_sessions"] == 0
    assert counts["truly_active_30m"] == 1         # last turn 5m ago, human
    # proposals_open = p1,p2,p3,p5 (open-ish) — done/vetoed excluded = 4.
    assert counts["proposals_open"] == 4
    # awaiting = p1 (needs_you), p2 (status needs_you), p3 (held) = 3.
    assert counts["proposals_awaiting"] == 3
    assert counts["ledger_24h"] == 1               # stale excluded
    assert counts["todo_open"] == 3
    assert counts["todo_p0_p1"] == 2

    # dashboard_state_meta carried through.
    assert payload["dashboard_state_meta"] == {"version": 7}

    # workspaces back-reference the live session.
    ws1 = payload["workspaces"][0]
    assert ws1["ws_ref"] == "workspace:1"
    assert ws1["session_ids"] == ["sess-A"]

    # live session enriched with ws + turn-age fields.
    sess = payload["live_sessions"][0]
    assert sess["session_id"] == "sess-A"
    assert sess["ws_ref"] == "workspace:1"
    assert sess["is_cron"] is False
    assert sess["last_turn_age_sec"] is not None and sess["last_turn_age_sec"] < 1800
    assert sess["last_turn_ts"] is not None

    # ledger sorted newest-first and only the recent one present.
    assert [e["id"] for e in payload["ledger_recent"]] == ["f1"]
    # inbox events recent.
    assert [e["id"] for e in payload["inbox_events_recent"]] == ["e1"]

    # The awaiting set contains exactly the three awaiting proposal ids.
    awaiting_ids = sorted(
        p["id"] for p in payload["proposals"]
        if (p.get("needs_you") or p.get("status") == "needs_you" or p.get("held"))
        and p.get("status") not in {"done", "expired", "vetoed"}
    )
    assert awaiting_ids == ["p1", "p2", "p3"]

    # A log line was written under the tmp HOME.
    logfile = ws.LOG_DIR / "world-scanner.out"
    assert logfile.exists()
    assert "scan: ws=1 live=1" in logfile.read_text()


def test_build_empty_world(ws, monkeypatch):
    # No tree, no registries, no files — build() still writes a valid world.json.
    now = ws.utc_now()
    monkeypatch.setattr(ws, "utc_now", lambda: now)
    monkeypatch.setattr(ws, "cmux_tree", lambda: None)
    monkeypatch.setattr(ws, "ps_tty", lambda pid: None)
    monkeypatch.setattr(ws, "read_mem_pct", lambda: None)

    ws.build()

    payload = json.loads(ws.OUT_PATH.read_text())
    c = payload["counts"]
    assert c["workspaces"] == 0
    assert c["live_sessions"] == 0
    assert c["human_sessions"] == 0
    assert c["cron_sessions"] == 0
    assert c["truly_active_30m"] == 0
    assert c["proposals_open"] == 0
    assert c["proposals_awaiting"] == 0
    assert c["ledger_24h"] == 0
    assert c["todo_open"] == 0
    assert c["todo_p0_p1"] == 0
    assert payload["_meta"]["memory_pct"] is None
    assert payload["workspaces"] == []
    assert payload["live_sessions"] == []
