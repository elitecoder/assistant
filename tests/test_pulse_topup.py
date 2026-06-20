"""Coverage top-up for bin/pulse.py — fills the uncovered branches the existing
tests/test_pulse.py leaves open: self_update_pulse outcome mapping, the TODO
dispatch path (dispatch_todo + _mark_todo_dispatched + _build_dispatch_prompt +
_cmux_rpc + _surface_read_text), the work-receipt gate (pre_cleanup_check),
run_lesson_extractor, and a drain_inbox unlink-failure edge.

Every external boundary is faked: pulse.run / pulse._cmux_rpc /
pulse._surface_read_text are monkeypatched, and time.sleep is neutralized in
the dispatch tests. No real cmux / claude / git / bash / LLM ever runs.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import pytest

REPO = Path(__file__).resolve().parent.parent
PULSE_PATH = REPO / "bin/pulse.py"


def load_pulse(home: Path):
    """Import bin/pulse.py with HOME pointed at a tempdir so every HOME-derived
    path constant lands inside the sandbox. Fresh module per call."""
    os.environ["HOME"] = str(home)
    spec = importlib.util.spec_from_file_location("pulse_mod", str(PULSE_PATH))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def fixture_home(tmp: Path) -> Path:
    (tmp / ".assistant/inbox").mkdir(parents=True)
    (tmp / ".assistant/observer-summaries").mkdir(parents=True)
    (tmp / ".claude/cache").mkdir(parents=True)
    return tmp


@pytest.fixture()
def home(tmp_path):
    return fixture_home(tmp_path)


@pytest.fixture()
def mod(home):
    return load_pulse(home)


def _read_ledger(home: Path) -> list[dict]:
    p = home / ".assistant/actions-ledger.jsonl"
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


# ─── self_update_pulse ────────────────────────────────────────────────────────

class _FakeSelfUpdate:
    """Stand-in for the lazily-imported self_update module. maybe_update returns
    whatever `result` was set to, or raises if `raises` is set."""
    def __init__(self, result, raises: Exception | None = None):
        self._result = result
        self._raises = raises

    def maybe_update(self, repo, **kwargs):
        if self._raises is not None:
            raise self._raises
        return self._result


def _inject_self_update(mod, result, raises=None):
    """self_update_pulse does `import self_update` after inserting BIN on
    sys.path. Pre-seed sys.modules with a fake so the real module is bypassed."""
    sys.modules["self_update"] = _FakeSelfUpdate(result, raises)


def test_self_update_none_throttled_no_ledger(mod, home):
    _inject_self_update(mod, None)
    try:
        mod.self_update_pulse(7)
    finally:
        sys.modules.pop("self_update", None)
    assert _read_ledger(home) == []


def test_self_update_clean_no_change_silent(mod, home):
    _inject_self_update(mod, {"changed": False, "skipped_reason": None, "error": None})
    try:
        mod.self_update_pulse(7)
    finally:
        sys.modules.pop("self_update", None)
    assert _read_ledger(home) == []


def test_self_update_changed_installed_stashed_verified(mod, home):
    _inject_self_update(mod, {
        "changed": True,
        "files_changed": ["bin/pulse.py", "skills/todo/SKILL.md"],
        "installed": True,
        "install_rc": 0,
        "stashed": True,
        "from_sha": "aaaaaaaaaaaa",
        "to_sha": "bbbbbbbbbbbb",
    })
    try:
        mod.self_update_pulse(7)
    finally:
        sys.modules.pop("self_update", None)
    entries = _read_ledger(home)
    assert len(entries) == 1
    e = entries[0]
    assert e["outcome"] == "verified"
    assert e["kind"] == "self-update"
    assert "pulled aaaaaaaaaaaa..bbbbbbbbbbbb" in e["evidence"]
    assert "install=ran" in e["evidence"]
    assert "auto-stashed dirty tree" in e["evidence"]
    assert e["key"] == "self-update-bbbbbbbbbbbb"


def test_self_update_changed_install_failed(mod, home):
    _inject_self_update(mod, {
        "changed": True,
        "files_changed": ["skills/todo/SKILL.md"],
        "installed": True,
        "install_rc": 42,
        "from_sha": "aaaa", "to_sha": "cccc",
    })
    try:
        mod.self_update_pulse(7)
    finally:
        sys.modules.pop("self_update", None)
    e = _read_ledger(home)[0]
    assert e["outcome"] == "failed"
    assert "install_rc=42" in e["evidence"]


def test_self_update_changed_self_plist_reload_deferred(mod, home):
    _inject_self_update(mod, {
        "changed": True,
        "files_changed": ["launchagents/x.plist"],
        "installed": True,
        "install_rc": 0,
        "self_plist_reload_deferred": True,
        "from_sha": "aaaa", "to_sha": "cccc",
    })
    try:
        mod.self_update_pulse(7)
    finally:
        sys.modules.pop("self_update", None)
    e = _read_ledger(home)[0]
    assert e["outcome"] == "verified"
    assert "pulse-plist reload deferred" in e["evidence"]
    assert "install=ran" in e["evidence"]


def test_self_update_changed_install_not_needed(mod, home):
    _inject_self_update(mod, {
        "changed": True,
        "files_changed": ["bin/pulse.py"],
        "installed": False,
        "install_rc": None,
        "from_sha": "aaaa", "to_sha": "dddd",
    })
    try:
        mod.self_update_pulse(7)
    finally:
        sys.modules.pop("self_update", None)
    e = _read_ledger(home)[0]
    assert e["outcome"] == "verified"
    assert "install=not-needed" in e["evidence"]


def test_self_update_reason_ahead_verified(mod, home):
    _inject_self_update(mod, {"changed": False, "skipped_reason": "ahead", "ahead": 3})
    try:
        mod.self_update_pulse(9)
    finally:
        sys.modules.pop("self_update", None)
    e = _read_ledger(home)[0]
    assert e["outcome"] == "verified"
    assert "repo is ahead (3 commit(s)" in e["evidence"]
    assert e["key"] == "self-update-skip-ahead-p9"


def test_self_update_reason_dirty_verified(mod, home):
    _inject_self_update(mod, {
        "changed": False, "skipped_reason": "dirty", "dirty_age_sec": 7200,
    })
    try:
        mod.self_update_pulse(9)
    finally:
        sys.modules.pop("self_update", None)
    e = _read_ledger(home)[0]
    assert e["outcome"] == "verified"
    assert "working tree dirty for 2.0h" in e["evidence"]
    assert e["key"] == "self-update-skip-dirty-p9"


def test_self_update_reason_stash_failed(mod, home):
    _inject_self_update(mod, {
        "changed": False, "skipped_reason": "stash-failed",
        "error": "git stash refused",
    })
    try:
        mod.self_update_pulse(9)
    finally:
        sys.modules.pop("self_update", None)
    e = _read_ledger(home)[0]
    assert e["outcome"] == "failed"
    assert "auto-stash failed: git stash refused" in e["evidence"]
    assert e["key"] == "self-update-stash-failed-p9"


def test_self_update_other_reason_failed(mod, home):
    _inject_self_update(mod, {
        "changed": False, "skipped_reason": "pull-failed",
        "error": "ff-only refused",
    })
    try:
        mod.self_update_pulse(11)
    finally:
        sys.modules.pop("self_update", None)
    e = _read_ledger(home)[0]
    assert e["outcome"] == "failed"
    assert "self-update pull-failed: ff-only refused" in e["evidence"]
    assert e["key"] == "self-update-fail-p11"


def test_self_update_error_no_reason_failed(mod, home):
    # changed False, reason None, but an error present → not silent, falls to
    # the generic failure branch.
    _inject_self_update(mod, {
        "changed": False, "skipped_reason": None, "error": "fetch exploded",
    })
    try:
        mod.self_update_pulse(11)
    finally:
        sys.modules.pop("self_update", None)
    e = _read_ledger(home)[0]
    assert e["outcome"] == "failed"
    assert "fetch exploded" in e["evidence"]


def test_self_update_maybe_update_raises_is_caught(mod, home):
    _inject_self_update(mod, None, raises=RuntimeError("boom"))
    try:
        # Must NOT raise; must NOT write a ledger entry.
        mod.self_update_pulse(3)
    finally:
        sys.modules.pop("self_update", None)
    assert _read_ledger(home) == []


# ─── drain_inbox unlink-failure edge ──────────────────────────────────────────

def test_drain_inbox_tolerates_unlink_failure(mod, home):
    inbox = home / ".assistant/inbox"
    (inbox / "pulse-1.json").write_text("{}")
    (inbox / "pulse-2.json").write_text("{}")
    real_unlink = Path.unlink

    def flaky_unlink(self, *a, **k):
        if self.name == "pulse-1.json":
            raise OSError("permission denied")
        return real_unlink(self, *a, **k)

    with mock.patch.object(Path, "unlink", flaky_unlink):
        n = mod.drain_inbox()
    # Only pulse-2 unlinked; pulse-1 failure swallowed.
    assert n == 1
    assert (inbox / "pulse-1.json").exists()
    assert not (inbox / "pulse-2.json").exists()


# ─── _mark_todo_dispatched ────────────────────────────────────────────────────

def _seed_todo(mod, items: list[dict]):
    mod.TODO_PATH.parent.mkdir(parents=True, exist_ok=True)
    mod.TODO_PATH.write_text(json.dumps({"items": items}))


def test_mark_todo_dispatched_stamps_and_persists(mod):
    _seed_todo(mod, [{"id": "td-7", "title": "thing", "status": "open"}])
    assert mod._mark_todo_dispatched("td-7", "workspace:5") is True
    data = json.loads(mod.TODO_PATH.read_text())
    it = data["items"][0]
    assert it["status"] == "in-progress"
    assert it["dispatchedWs"] == "workspace:5"
    assert it["dispatchedAt"]
    assert it["statusReason"] == "dispatched to workspace:5"
    # No leftover tmp file.
    assert not mod.TODO_PATH.with_suffix(".json.tmp").exists()


def test_mark_todo_dispatched_missing_id_returns_false(mod):
    _seed_todo(mod, [{"id": "td-other"}])
    assert mod._mark_todo_dispatched("td-7", "workspace:5") is False


def test_mark_todo_dispatched_unreadable_returns_false(mod):
    # No file at all → cannot read → False.
    assert mod._mark_todo_dispatched("td-7", "workspace:5") is False


def test_mark_todo_dispatched_write_failure_returns_false(mod):
    _seed_todo(mod, [{"id": "td-7", "title": "x"}])
    # os.replace blows up → write-stamp failure path (862-864).
    with mock.patch.object(mod.os, "replace", side_effect=OSError("disk full")):
        assert mod._mark_todo_dispatched("td-7", "workspace:5") is False


# ─── execute_verdict: receipt-gate block branch (743-758) ────────────────────

def test_execute_verdict_cleanup_blocked_by_receipt_gate(mod, home):
    """ready_for_cleanup that passes the merge gate but fails the work-receipt
    gate → emit an awaiting card, never send /cleanup."""
    # Open the merge gate so we reach the receipt gate.
    mod.record_assistant_merge("workspace:9", ["111"])
    sent = []
    awaiting = []
    with mock.patch.object(mod, "pre_cleanup_check",
                           return_value={"gate": "block", "reason": "no receipt",
                                         "evidence": "no receipt on file"}):
        with mock.patch.object(mod, "cmux_send",
                               lambda *a, **k: sent.append(a) or {"outcome": "sent"}):
            action = mod.execute_verdict(
                {"ref": "workspace:9", "title": "Ruler fix", "cwd": "/"},
                {"verdict": "ready_for_cleanup", "summary": "looks done", "next": "n"},
                awaiting,
            )
    assert sent == [], "/cleanup must NOT fire when the receipt gate blocks"
    assert action["kind"] == "emit-card"
    assert "blocked /cleanup" in action["evidence"]
    assert len(awaiting) == 1
    assert awaiting[0]["key"] == "workspace:9:cleanup-no-receipt"


def test_execute_verdict_cleanup_allowed_when_receipt_present(mod, home):
    """ready_for_cleanup that passes BOTH gates → /cleanup is sent and the
    receipt_path is threaded onto the action base."""
    mod.record_assistant_merge("workspace:9", ["111"])
    seen = {}

    def fake_send(ws_ref, text, **k):
        seen["ws_ref"] = ws_ref
        seen["text"] = text
        return {"outcome": "sent", "transcript_size_delta": 999}

    with mock.patch.object(mod, "pre_cleanup_check",
                           return_value={"gate": "allow", "receipt_path": "/r/x.json"}):
        with mock.patch.object(mod, "previous_send_ingested", lambda *a: True):
            with mock.patch.object(mod, "cmux_send", fake_send):
                action = mod.execute_verdict(
                    {"ref": "workspace:9", "title": "t", "cwd": "/"},
                    {"verdict": "ready_for_cleanup", "summary": "s", "next": "n"},
                    [],
                )
    assert seen == {"ws_ref": "workspace:9", "text": "/cleanup"}
    assert action["outcome"] == "verified"


# ─── _build_dispatch_prompt ───────────────────────────────────────────────────

def test_build_dispatch_prompt_includes_fields_and_rules(mod, home):
    rules_file = home / "dispatch-rules.md"
    rules_file.write_text("CLASSIFY-RULES-SENTINEL: route FFP via archffp")
    mod.DISPATCH_CLASSIFICATION_PROMPT = rules_file
    item = {
        "id": "td-9", "title": "Fix the ruler",
        "detail": "Ruler ticks drift at zoom>3", "url": "https://example/issue/1",
        "tags": ["squirrel", "ruler"],
    }
    p = mod._build_dispatch_prompt(item)
    assert "td-9" in p
    assert "Fix the ruler" in p
    assert "Ruler ticks drift" in p
    assert "https://example/issue/1" in p
    assert "squirrel, ruler" in p
    assert "CLASSIFY-RULES-SENTINEL" in p


def test_build_dispatch_prompt_fallback_when_rules_missing(mod, home):
    mod.DISPATCH_CLASSIFICATION_PROMPT = home / "does-not-exist.md"
    p = mod._build_dispatch_prompt({"id": "td-1", "title": "t"})
    assert "architect-ffp:archffp" in p
    assert "Classification rules file is missing" in p


# ─── _cmux_rpc ────────────────────────────────────────────────────────────────

def test_cmux_rpc_parses_json_on_rc0(mod):
    with mock.patch.object(mod, "run", return_value=(0, '{"ok": true, "n": 3}', "")):
        out = mod._cmux_rpc("surface.read_text", {"surface_id": "surface:1"})
    assert out == {"ok": True, "n": 3}


def test_cmux_rpc_returns_none_on_rc_nonzero(mod):
    with mock.patch.object(mod, "run", return_value=(1, "", "boom")):
        assert mod._cmux_rpc("x", {}) is None


def test_cmux_rpc_returns_none_on_bad_json(mod):
    with mock.patch.object(mod, "run", return_value=(0, "not json", "")):
        assert mod._cmux_rpc("x", {}) is None


# ─── _surface_read_text ───────────────────────────────────────────────────────

def test_surface_read_text_returns_text_on_match(mod):
    with mock.patch.object(mod, "_cmux_rpc",
                           return_value={"surface_ref": "surface:3", "text": "hello"}):
        assert mod._surface_read_text("surface:3") == "hello"


def test_surface_read_text_empty_on_ref_mismatch(mod):
    with mock.patch.object(mod, "_cmux_rpc",
                           return_value={"surface_ref": "surface:9", "text": "hello"}):
        assert mod._surface_read_text("surface:3") == ""


def test_surface_read_text_empty_on_none(mod):
    with mock.patch.object(mod, "_cmux_rpc", return_value=None):
        assert mod._surface_read_text("surface:3") == ""


# ─── pre_cleanup_check ────────────────────────────────────────────────────────

def test_pre_cleanup_check_parses_gate_json(mod):
    gate = {"gate": "allow", "receipt_path": "/x/receipt.json", "ws_ref": "workspace:1"}
    with mock.patch.object(mod, "run", return_value=(0, json.dumps(gate), "")):
        out = mod.pre_cleanup_check("workspace:1")
    assert out == gate


def test_pre_cleanup_check_blocks_on_rc_nonzero(mod):
    with mock.patch.object(mod, "run", return_value=(3, "", "tool crashed")):
        out = mod.pre_cleanup_check("workspace:1")
    assert out["gate"] == "block"
    assert out["reason"] == "gate failed to run"
    assert "tool crashed" in out["evidence"]


def test_pre_cleanup_check_blocks_on_bad_json(mod):
    with mock.patch.object(mod, "run", return_value=(0, "garbage-not-json", "")):
        out = mod.pre_cleanup_check("workspace:1")
    assert out["gate"] == "block"
    assert out["reason"] == "gate bad output"
    assert "garbage-not-json" in out["evidence"]


# ─── run_lesson_extractor ─────────────────────────────────────────────────────

def test_run_lesson_extractor_missing_file_returns_early(mod, home):
    # Point BIN at a dir with no lesson-extractor.py.
    mod.BIN = home / "empty-bin"
    (home / "empty-bin").mkdir()
    called = {"n": 0}
    with mock.patch.object(mod, "run",
                           side_effect=lambda *a, **k: called.__setitem__("n", 1) or (0, "", "")):
        mod.run_lesson_extractor(12)
    assert called["n"] == 0, "run() must not be invoked when extractor is missing"


def test_run_lesson_extractor_success_logs(mod, home):
    extractor = mod.BIN / "lesson-extractor.py"
    # BIN is the real repo bin/ where lesson-extractor.py exists; ensure it does.
    assert extractor.exists()
    out = json.dumps({"n_proposed": 2, "n_candidates": 5, "n_transcript_candidates": 1})
    seen = {}

    def fake_run(cmd, **k):
        seen["cmd"] = cmd
        return (0, out, "")

    with mock.patch.object(mod, "run", side_effect=fake_run):
        # pulse_idx 12 with LESSON_TRANSCRIPT_EVERY default 144 → ledger-only.
        mod.run_lesson_extractor(12)
    assert "--ledger-only" in seen["cmd"]


def test_run_lesson_extractor_transcript_pass_drops_ledger_only(mod, home):
    seen = {}

    def fake_run(cmd, **k):
        seen["cmd"] = cmd
        seen["timeout"] = k.get("timeout")
        return (0, json.dumps({"n_proposed": 0}), "")

    with mock.patch.object(mod, "run", side_effect=fake_run):
        # 144 % 144 == 0 → transcript pass, NO --ledger-only, longer timeout.
        mod.run_lesson_extractor(mod.LESSON_TRANSCRIPT_EVERY)
    assert "--ledger-only" not in seen["cmd"]
    assert seen["timeout"] == mod.LESSON_TRANSCRIPT_TIMEOUT_SEC


def test_run_lesson_extractor_rc_nonzero_warns_no_raise(mod, home):
    with mock.patch.object(mod, "run", return_value=(2, "", "extractor boom")):
        # Must not raise.
        mod.run_lesson_extractor(12)


def test_run_lesson_extractor_bad_json_swallowed(mod, home):
    with mock.patch.object(mod, "run", return_value=(0, "not json", "")):
        mod.run_lesson_extractor(12)  # no raise on unparseable output


# ─── dispatch_todo ────────────────────────────────────────────────────────────

@pytest.fixture()
def no_sleep(mod):
    """Neutralize time.sleep inside pulse so the 30-iteration loops are instant."""
    with mock.patch.object(mod.time, "sleep", lambda *_a, **_k: None):
        yield


def test_dispatch_todo_item_not_found(mod, no_sleep):
    _seed_todo(mod, [{"id": "td-other"}])
    assert mod.dispatch_todo("td-missing") is False


def test_dispatch_todo_cmux_ping_fails(mod, no_sleep):
    _seed_todo(mod, [{"id": "td-1", "title": "t"}])
    with mock.patch.object(mod, "run", return_value=(1, "", "no cmux")):
        assert mod.dispatch_todo("td-1") is False


def test_dispatch_todo_new_workspace_fails(mod, home, no_sleep):
    _seed_todo(mod, [{"id": "td-1", "title": "t"}])
    mod.SPAWN_PROMPT_DIR = home / "spawn-prompts"
    mod.DISPATCH_CLASSIFICATION_PROMPT = home / "missing-rules.md"

    def fake_run(cmd, **k):
        if cmd[1] == "ping":
            return (0, "", "")
        if cmd[1] == "new-workspace":
            return (1, "", "spawn refused")
        return (0, "", "")

    with mock.patch.object(mod, "run", side_effect=fake_run):
        assert mod.dispatch_todo("td-1") is False


def test_dispatch_todo_no_workspace_ref_in_output(mod, home, no_sleep):
    _seed_todo(mod, [{"id": "td-1", "title": "t"}])
    mod.SPAWN_PROMPT_DIR = home / "spawn-prompts"
    mod.DISPATCH_CLASSIFICATION_PROMPT = home / "missing-rules.md"

    def fake_run(cmd, **k):
        if cmd[1] == "ping":
            return (0, "", "")
        if cmd[1] == "new-workspace":
            return (0, "created but no ref here", "")
        return (0, "", "")

    with mock.patch.object(mod, "run", side_effect=fake_run):
        assert mod.dispatch_todo("td-1") is False


def test_dispatch_todo_no_surface(mod, home, no_sleep):
    _seed_todo(mod, [{"id": "td-1", "title": "t"}])
    mod.SPAWN_PROMPT_DIR = home / "spawn-prompts"
    mod.DISPATCH_CLASSIFICATION_PROMPT = home / "missing-rules.md"

    def fake_run(cmd, **k):
        if cmd[1] == "ping":
            return (0, "", "")
        if cmd[1] == "new-workspace":
            return (0, "workspace:7", "")
        if cmd[1] == "list-pane-surfaces":
            return (0, "no surface listed", "")
        return (0, "", "")

    with mock.patch.object(mod, "run", side_effect=fake_run):
        assert mod.dispatch_todo("td-1") is False


def test_dispatch_todo_never_ready(mod, home, no_sleep):
    _seed_todo(mod, [{"id": "td-1", "title": "t"}])
    mod.SPAWN_PROMPT_DIR = home / "spawn-prompts"
    mod.DISPATCH_CLASSIFICATION_PROMPT = home / "missing-rules.md"
    mod.DISPATCH_CWD = home / "dev"
    (home / "dev").mkdir(parents=True, exist_ok=True)

    def fake_run(cmd, **k):
        if cmd[1] == "ping":
            return (0, "", "")
        if cmd[1] == "new-workspace":
            return (0, "workspace:7", "")
        if cmd[1] == "list-pane-surfaces":
            return (0, "surface:3", "")
        return (0, "", "")

    with mock.patch.object(mod, "run", side_effect=fake_run):
        with mock.patch.object(mod, "_cmux_rpc", return_value={}):
            # Screen never shows the banner → readiness loop exhausts → False.
            with mock.patch.object(mod, "_surface_read_text", return_value=""):
                assert mod.dispatch_todo("td-1") is False
    # Prompt was still staged on disk.
    staged = list(mod.SPAWN_PROMPT_DIR.glob("prompt-dispatch-td-1-*.md"))
    assert staged, "prompt file should have been staged before the readiness loop"


def _project_dir_for(mod, cwd: Path) -> Path:
    real = os.path.realpath(str(cwd))
    return mod.HOME / ".claude/projects" / real.replace("/", "-")


def test_dispatch_todo_happy_path(mod, home, no_sleep):
    _seed_todo(mod, [{"id": "td-1", "title": "Build feature"}])
    mod.SPAWN_PROMPT_DIR = home / "spawn-prompts"
    mod.DISPATCH_CLASSIFICATION_PROMPT = home / "missing-rules.md"
    cwd = home / "dev"
    cwd.mkdir(parents=True, exist_ok=True)
    mod.DISPATCH_CWD = cwd
    project_dir = _project_dir_for(mod, cwd)

    rpc_calls = []

    def fake_rpc(method, params, timeout=15):
        rpc_calls.append((method, params))
        return {}

    # First surface read: trust prompt present (covers the trust branch).
    # Subsequent reads: the readiness banner.
    reads = iter([
        "1. Yes, I trust this folder",          # trust-prompt branch
        "Claude Code v1.2.3  ⏵⏵ bypass permissions on",  # ready
    ])

    def fake_surface(ref, lines=200):
        try:
            return next(reads)
        except StopIteration:
            return "Claude Code v1.2.3"

    # The submission-confirmation loop polls for a new *.jsonl carrying the
    # prompt-file path. We materialize that transcript when send_key (Enter) is
    # fired, so the next glob in the loop sees it. We can't know the exact
    # prompt path before dispatch stages it, so we read the staged file name
    # lazily inside fake_rpc.
    state = {"submitted_written": False}

    def fake_rpc_with_transcript(method, params, timeout=15):
        rpc_calls.append((method, params))
        if method == "surface.send_key" and params.get("key") == "enter" and not state["submitted_written"]:
            # Find the staged prompt file (after send_text instruction).
            staged = list(mod.SPAWN_PROMPT_DIR.glob("prompt-dispatch-td-1-*.md"))
            if staged:
                sig = str(staged[0])
                project_dir.mkdir(parents=True, exist_ok=True)
                tp = project_dir / "session-abc.jsonl"
                tp.write_text(json.dumps({
                    "type": "user",
                    "message": {"role": "user",
                                "content": f"Read {sig} in full and execute every instruction in it."},
                }) + "\n")
                state["submitted_written"] = True
        return {}

    def fake_run(cmd, **k):
        if cmd[1] == "ping":
            return (0, "", "")
        if cmd[1] == "new-workspace":
            return (0, "workspace:7", "")
        if cmd[1] == "list-pane-surfaces":
            return (0, "surface:3", "")
        return (0, "", "")

    with mock.patch.object(mod, "run", side_effect=fake_run):
        with mock.patch.object(mod, "_cmux_rpc", side_effect=fake_rpc_with_transcript):
            with mock.patch.object(mod, "_surface_read_text", side_effect=fake_surface):
                ok = mod.dispatch_todo("td-1")

    assert ok is True
    # TODO was stamped.
    it = json.loads(mod.TODO_PATH.read_text())["items"][0]
    assert it["status"] == "in-progress"
    assert it["dispatchedWs"] == "workspace:7"
    # Prompt staged on disk.
    assert list(mod.SPAWN_PROMPT_DIR.glob("prompt-dispatch-td-1-*.md"))
    # Trust prompt was answered ("1" + enter), then the instruction sent.
    sent_texts = [p.get("text") for m, p in rpc_calls if m == "surface.send_text"]
    assert "1" in sent_texts
    assert any("Read " in (t or "") for t in sent_texts)


def test_dispatch_todo_happy_path_droid(mod, home, no_sleep, monkeypatch):
    """ASSISTANT_DISPATCH_AGENT=droid: launches `droid`, confirms against
    ~/.factory/sessions, reads the Droid transcript schema, sends no trust
    answer (droid has no known auto-answerable trust gate)."""
    monkeypatch.setenv("ASSISTANT_DISPATCH_AGENT", "droid")
    _seed_todo(mod, [{"id": "td-1", "title": "Build feature"}])
    mod.SPAWN_PROMPT_DIR = home / "spawn-prompts"
    mod.DISPATCH_CLASSIFICATION_PROMPT = home / "missing-rules.md"
    cwd = home / "dev"
    cwd.mkdir(parents=True, exist_ok=True)
    mod.DISPATCH_CWD = cwd
    real = os.path.realpath(str(cwd))
    project_dir = mod.HOME / ".factory/sessions" / real.replace("/", "-")

    rpc_calls = []
    # Droid readiness banner (no trust prompt) on every read.
    reads = iter(["Auto (High) · allow all commands     Opus 4.8   ? for help"])

    def fake_surface(ref, lines=200):
        try:
            return next(reads)
        except StopIteration:
            return "? for help"

    state = {"written": False}

    def fake_rpc(method, params, timeout=15):
        rpc_calls.append((method, params))
        if method == "surface.send_key" and params.get("key") == "enter" and not state["written"]:
            staged = list(mod.SPAWN_PROMPT_DIR.glob("prompt-dispatch-td-1-*.md"))
            if staged:
                sig = str(staged[0])
                project_dir.mkdir(parents=True, exist_ok=True)
                # DROID schema: top-level type "message", role on message.role.
                (project_dir / "abc.jsonl").write_text(json.dumps({
                    "type": "message",
                    "message": {"role": "user",
                                "content": [{"type": "text",
                                             "text": f"Read {sig} in full and execute every instruction in it."}]},
                }) + "\n")
                state["written"] = True
        return {}

    argv_seen = {}

    def fake_run(cmd, **k):
        if cmd[1] == "ping":
            return (0, "", "")
        if cmd[1] == "new-workspace":
            argv_seen["new"] = cmd
            return (0, "workspace:7", "")
        if cmd[1] == "list-pane-surfaces":
            return (0, "surface:3", "")
        return (0, "", "")

    with mock.patch.object(mod, "run", side_effect=fake_run):
        with mock.patch.object(mod, "_cmux_rpc", side_effect=fake_rpc):
            with mock.patch.object(mod, "_surface_read_text", side_effect=fake_surface):
                ok = mod.dispatch_todo("td-1")

    assert ok is True
    # Launched `droid`, not `claude`.
    new = argv_seen["new"]
    assert new[new.index("--command") + 1] == "droid"
    # Stamped in-progress.
    it = json.loads(mod.TODO_PATH.read_text())["items"][0]
    assert it["status"] == "in-progress"
    # No trust answer was sent (droid has no auto-answerable trust gate).
    sent_texts = [p.get("text") for m, p in rpc_calls if m == "surface.send_text"]
    assert "1" not in sent_texts
    assert any("Read " in (t or "") for t in sent_texts)


def test_dispatch_todo_unconfirmed_still_stamps_no_respawn(mod, home, no_sleep):
    """Regression for td-128: a spawned workspace whose submission can't be
    confirmed via the transcript (e.g. a Claude Code transcript-layout change)
    must STILL be stamped so the next pulse does not re-spawn a duplicate.
    The stamp is decoupled from confirmation; dispatch_todo returns True."""
    _seed_todo(mod, [{"id": "td-1", "title": "t"}])
    mod.SPAWN_PROMPT_DIR = home / "spawn-prompts"
    mod.DISPATCH_CLASSIFICATION_PROMPT = home / "missing-rules.md"
    cwd = home / "dev"
    cwd.mkdir(parents=True, exist_ok=True)
    mod.DISPATCH_CWD = cwd

    def fake_run(cmd, **k):
        if cmd[1] == "ping":
            return (0, "", "")
        if cmd[1] == "new-workspace":
            return (0, "workspace:7", "")
        if cmd[1] == "list-pane-surfaces":
            return (0, "surface:3", "")
        return (0, "", "")

    with mock.patch.object(mod, "run", side_effect=fake_run):
        with mock.patch.object(mod, "_cmux_rpc", return_value={}):
            # Ready immediately, but no transcript ever appears → unconfirmed.
            with mock.patch.object(mod, "_surface_read_text",
                                   return_value="Claude Code v1.0"):
                assert mod.dispatch_todo("td-1") is True
    # The TODO MUST be stamped so the picker drops it from bucket_b.
    it = json.loads(mod.TODO_PATH.read_text())["items"][0]
    assert it["status"] == "in-progress"
    assert it["dispatchedWs"] == "workspace:7"
    assert it["dispatchedAt"]


def test_dispatch_todo_confirms_via_session_subdir_transcript(mod, home, no_sleep):
    """Regression for td-128: newer Claude Code writes the main transcript inside
    a per-session subdirectory (<project>/<session-id>/foo.jsonl), not a flat
    <id>.jsonl. The confirmation glob must be recursive so the dispatched
    session's prompt is still detected as submitted."""
    _seed_todo(mod, [{"id": "td-1", "title": "t"}])
    mod.SPAWN_PROMPT_DIR = home / "spawn-prompts"
    mod.DISPATCH_CLASSIFICATION_PROMPT = home / "missing-rules.md"
    cwd = home / "dev"
    cwd.mkdir(parents=True, exist_ok=True)
    mod.DISPATCH_CWD = cwd
    project_dir = _project_dir_for(mod, cwd)
    state = {"written": False}

    def fake_rpc(method, params, timeout=15):
        if method == "surface.send_key" and params.get("key") == "enter" and not state["written"]:
            staged = list(mod.SPAWN_PROMPT_DIR.glob("prompt-dispatch-td-1-*.md"))
            if staged:
                # Write the transcript ONLY in a per-session subdir, never flat.
                subdir = project_dir / "session-xyz"
                subdir.mkdir(parents=True, exist_ok=True)
                (subdir / "session-xyz.jsonl").write_text(json.dumps({
                    "type": "user",
                    "message": {"role": "user",
                                "content": f"Read {staged[0]} in full and execute every instruction in it."},
                }) + "\n")
                state["written"] = True
        return {}

    def fake_run(cmd, **k):
        if cmd[1] == "ping":
            return (0, "", "")
        if cmd[1] == "new-workspace":
            return (0, "workspace:7", "")
        if cmd[1] == "list-pane-surfaces":
            return (0, "surface:3", "")
        return (0, "", "")

    # dispatch_todo logs an INFO "dispatch … →" line ONLY when submission is
    # confirmed via the transcript, and a WARNING "submission unconfirmed" line
    # otherwise. Spy on the logger to assert confirmation actually fired — this
    # is what makes the recursive-glob fix observable (a flat-only glob would
    # miss the subdir transcript and log the warning instead).
    logs = {"info": [], "warning": []}
    with mock.patch.object(mod, "run", side_effect=fake_run):
        with mock.patch.object(mod, "_cmux_rpc", side_effect=fake_rpc):
            with mock.patch.object(mod, "_surface_read_text",
                                   return_value="Claude Code v1.0"):
                with mock.patch.object(mod.log, "info",
                                       side_effect=lambda m, *a: logs["info"].append(m % a if a else m)):
                    with mock.patch.object(mod.log, "warning",
                                           side_effect=lambda m, *a: logs["warning"].append(m % a if a else m)):
                        assert mod.dispatch_todo("td-1") is True
    it = json.loads(mod.TODO_PATH.read_text())["items"][0]
    assert it["status"] == "in-progress"
    # Confirmation fired (subdir transcript was found by the recursive glob).
    assert any("dispatch td-1 → workspace:7" in m for m in logs["info"])
    assert not any("submission unconfirmed" in m for m in logs["warning"])


def test_dispatch_todo_stamp_failure_after_submit(mod, home, no_sleep):
    _seed_todo(mod, [{"id": "td-1", "title": "t"}])
    mod.SPAWN_PROMPT_DIR = home / "spawn-prompts"
    mod.DISPATCH_CLASSIFICATION_PROMPT = home / "missing-rules.md"
    cwd = home / "dev"
    cwd.mkdir(parents=True, exist_ok=True)
    mod.DISPATCH_CWD = cwd
    project_dir = _project_dir_for(mod, cwd)
    state = {"written": False}

    def fake_rpc(method, params, timeout=15):
        if method == "surface.send_key" and params.get("key") == "enter" and not state["written"]:
            staged = list(mod.SPAWN_PROMPT_DIR.glob("prompt-dispatch-td-1-*.md"))
            if staged:
                project_dir.mkdir(parents=True, exist_ok=True)
                (project_dir / "s.jsonl").write_text(json.dumps({
                    "type": "user",
                    "message": {"role": "user",
                                "content": f"Read {staged[0]} in full and execute every instruction in it."},
                }) + "\n")
                state["written"] = True
        return {}

    def fake_run(cmd, **k):
        if cmd[1] == "ping":
            return (0, "", "")
        if cmd[1] == "new-workspace":
            return (0, "workspace:7", "")
        if cmd[1] == "list-pane-surfaces":
            return (0, "surface:3", "")
        return (0, "", "")

    with mock.patch.object(mod, "run", side_effect=fake_run):
        with mock.patch.object(mod, "_cmux_rpc", side_effect=fake_rpc):
            with mock.patch.object(mod, "_surface_read_text",
                                   return_value="Claude Code v1.0"):
                # Submission confirmed, but the stamp write fails → False.
                with mock.patch.object(mod, "_mark_todo_dispatched", return_value=False):
                    assert mod.dispatch_todo("td-1") is False
