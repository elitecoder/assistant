"""cmux-restore-sessions records each retyped resume command before it types it."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from assistant import effects

REPO = Path(__file__).resolve().parent.parent
SURFACE = "3F2504E0-4F89-41D3-9A0C-0305E82C3301"
PANEL = "9A2504E0-4F89-41D3-9A0C-0305E82C3302"
MARKER = "cd /RESUME-MARKER && claude --resume abc"


@pytest.fixture
def restore(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    effects._forget()
    spec = importlib.util.spec_from_file_location("restore_effects_test", REPO / "bin/cmux-restore-sessions.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def ledger(home) -> list:
    path = home / ".assistant/effects/effects.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


class Cmux:
    def __init__(self, home, answers=()):
        self.home, self.answers, self.calls, self.first = home, list(answers), [], None

    def __call__(self, *args, timeout=5):
        if self.first is None:
            self.first = ledger(self.home)
        self.calls.append((args[1], json.loads(args[2])))
        rc = self.answers.pop(0) if self.answers else 0
        return rc, "{}" if rc == 0 else "", "" if rc == 0 else "boom"


def run(restore, cmux, surface=SURFACE, panel_id=PANEL):
    restore.run_cmux = cmux
    return restore.respawn_panel({"title": "t"}, {"id": panel_id, "terminal": {"surfaceId": surface}}, MARKER,
                                 False, False)


def test_the_intent_is_on_disk_before_the_first_keystroke_and_is_a_persons_tap(restore, tmp_path):
    cmux = Cmux(tmp_path)
    assert run(restore, cmux) is True
    assert [r["kind"] for r in cmux.first] == ["intent"]
    rows = ledger(tmp_path)
    assert rows[0]["target"] == {"source": "restore", "surface": SURFACE}
    assert rows[0]["auth"]["kind"] == "tap" and rows[0]["auth"]["ref"] == "cli:restore"
    assert rows[0]["component"] == "restore" and rows[1]["outcome"] == "done"
    assert [(m, p.get("key", p.get("text"))) for m, p in cmux.calls] == [("surface.send_text", MARKER),
                                                                         ("surface.send_key", "enter")]
    assert "RESUME-MARKER" not in (tmp_path / ".assistant/effects/effects.jsonl").read_text()


def test_a_refused_first_try_is_retried_by_the_panels_id_and_is_still_one_clean_effect(restore, tmp_path):
    cmux = Cmux(tmp_path, answers=[1, 0, 0])
    assert run(restore, cmux) is True
    assert [p["surface_id"] for _, p in cmux.calls] == [SURFACE, PANEL, SURFACE]
    assert len([r for r in ledger(tmp_path) if r["kind"] == "intent"]) == 1
    assert ledger(tmp_path)[1]["outcome"] == "done"


def test_text_typed_but_enter_refused_is_unknown_but_still_counts_as_restored(restore, tmp_path):
    assert run(restore, Cmux(tmp_path, answers=[0, 1])) is True
    assert ledger(tmp_path)[1]["outcome"] == "unknown"


def test_nothing_taken_is_failed_and_reported(restore, tmp_path, capsys):
    assert run(restore, Cmux(tmp_path, answers=[1, 1])) is False
    assert ledger(tmp_path)[1]["outcome"] == "failed"


def test_with_the_ledger_unwritable_nothing_is_typed_and_the_reason_is_printed(restore, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(effects, "_write", lambda fd, data: (_ for _ in ()).throw(OSError(28, "full")))
    cmux = Cmux(tmp_path)
    assert run(restore, cmux) is False
    assert cmux.calls == [] and "NOT TYPED: Assistant couldn't record what it types" in capsys.readouterr().out


def test_a_terminal_that_is_not_an_id_is_not_typed_into(restore, tmp_path, capsys):
    cmux = Cmux(tmp_path)
    assert run(restore, cmux, surface="surface-1", panel_id="panel-1") is False
    assert cmux.calls == [] and "NOT TYPED" in capsys.readouterr().out


def test_a_dry_run_types_and_records_nothing(restore, tmp_path):
    cmux = Cmux(tmp_path)
    restore.run_cmux = cmux
    assert restore.respawn_panel({}, {"id": PANEL, "terminal": {"surfaceId": SURFACE}}, MARKER, True, False)
    assert cmux.calls == [] and ledger(tmp_path) == []


def test_a_verbose_run_says_why_nothing_was_typed(restore, tmp_path, capsys):
    restore.run_cmux = Cmux(tmp_path, answers=[1, 1])
    assert not restore.respawn_panel({"title": "t"}, {"id": PANEL, "terminal": {"surfaceId": SURFACE}}, MARKER,
                                     False, True)
    assert "send_text failed" in capsys.readouterr().out


def test_a_timeout_on_the_text_is_unknown_and_is_not_tried_again_by_the_other_id(restore, tmp_path):
    cmux = Cmux(tmp_path, answers=[124])
    assert run(restore, cmux) is False
    assert [p["surface_id"] for _, p in cmux.calls] == [SURFACE]
    assert ledger(tmp_path)[1]["outcome"] == "unknown" and ledger(tmp_path)[1]["error"] == "timeout"


def test_a_timeout_on_enter_still_counts_as_restored_and_is_unknown(restore, tmp_path):
    assert run(restore, Cmux(tmp_path, answers=[0, 124])) is True
    assert ledger(tmp_path)[1]["outcome"] == "unknown"


def test_run_cmux_reports_a_timeout_as_one(restore, monkeypatch):
    def slow(*a, **k):
        raise restore.subprocess.TimeoutExpired(cmd="cmux", timeout=5)
    monkeypatch.setattr(restore.subprocess, "run", slow)
    assert restore.run_cmux("rpc", "x")[:2] == (124, "")
    monkeypatch.setattr(restore.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(OSError("gone")))
    assert restore.run_cmux("rpc", "x")[0] == 1


def test_the_restore_rpc_treats_exit_status_zero_as_an_answer_whatever_the_body(restore, tmp_path):
    class Silent(Cmux):
        def __call__(self, *args, timeout=5):
            super().__call__(*args, timeout=timeout)
            return 0, "", ""
    cmux = Silent(tmp_path)
    assert run(restore, cmux) is True and [m for m, _ in cmux.calls] == ["surface.send_text", "surface.send_key"]


def test_keys_that_cant_be_made_close_the_intent_as_failed(restore, tmp_path, monkeypatch, capsys):
    from assistant import session_effects
    monkeypatch.setattr(session_effects, "Keys", lambda *a, **k: (_ for _ in ()).throw(
        session_effects.Refused("invalid", "no")))
    cmux = Cmux(tmp_path)
    assert run(restore, cmux) is False and cmux.calls == []
    assert ledger(tmp_path)[1]["outcome"] == "failed" and "NOT TYPED: no" in capsys.readouterr().out


@pytest.fixture(autouse=True)
def pro_mode(monkeypatch):
    """Everything this file tests is Pro mode's; the test says so, instead of leaning on the suite's default."""
    from assistant import mode
    monkeypatch.setattr(mode, "current", lambda: "pro")
