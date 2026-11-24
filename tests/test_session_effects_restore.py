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
