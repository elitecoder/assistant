"""Tests for bin/tools/propose-lesson.py: record a lesson proposal, then confirm it.

A proposal needs a rule; its trigger line is optional. The confirm tests run the
real curator in a subprocess against a tmp HOME, so a proposal is followed all
the way into the lesson store.
"""
from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
CURATOR = REPO / "bin" / "assistant-curator.py"


@pytest.fixture
def pl(tmp_path, monkeypatch):
    """propose-lesson.py loaded with HOME under tmp_path, so its proposals file and
    the curator it runs both stay off the real home. The vault mirror is off."""
    monkeypatch.setenv("HOME", str(tmp_path))
    spec = importlib.util.spec_from_file_location(
        "propose_lesson_under_test", str(REPO / "bin" / "tools" / "propose-lesson.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.PROPOSALS_PATH == tmp_path / ".assistant" / "proposals.jsonl"
    monkeypatch.setattr(mod, "OBSIDIAN_WRITE", tmp_path / "no-vault-writer.py")
    return mod


@pytest.fixture
def curator(tmp_path) -> Path:
    """A copy of the real curator outside the repo, so a write neither starts the
    background lesson audit nor syncs to the memory repo."""
    copy = tmp_path / "bin" / "assistant-curator.py"
    copy.parent.mkdir()
    copy.write_text(CURATOR.read_text())
    return copy


def _main(mod, monkeypatch, argv: list[str]) -> tuple[int, dict]:
    monkeypatch.setattr(sys, "argv", ["propose-lesson.py", *argv])
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    rc = mod.main()
    return rc, json.loads(out.getvalue())


def _proposals(mod) -> list[dict]:
    return [json.loads(line) for line in mod.PROPOSALS_PATH.read_text().splitlines()]


def _store(tmp_path: Path) -> str:
    return (tmp_path / ".claude" / "CLAUDE.md").read_text()


# ─── record ──────────────────────────────────────────────────────────────────

def test_record_needs_only_a_rule(pl, monkeypatch):
    rc, result = _main(pl, monkeypatch, ["--rule", "When X, do Y.", "--target", "claude",
                                         "--scope", "global"])
    assert rc == 0 and result["status"] == "recorded"
    [entry] = _proposals(pl)
    assert (entry["trigger"], entry["rule"], entry["status"]) == ("", "When X, do Y.", "pending")
    assert entry["id"] == result["proposal_id"]


def test_record_keeps_a_trigger_when_given(pl, monkeypatch):
    rc, _ = _main(pl, monkeypatch, ["--trigger", "On X", "--rule", "Do Y."])
    assert rc == 0
    [entry] = _proposals(pl)
    assert (entry["trigger"], entry["rule"]) == ("On X", "Do Y.")


def test_record_without_a_rule_is_an_error(pl, monkeypatch):
    rc, result = _main(pl, monkeypatch, ["--trigger", "On X"])
    assert rc == 1
    assert result == {"status": "error", "error": "--rule is required to record a proposal"}
    assert not pl.PROPOSALS_PATH.exists()


# ─── confirm (real curator subprocess, tmp HOME) ─────────────────────────────

def test_confirm_writes_a_rule_with_no_trigger_as_the_rule_alone(pl, curator, tmp_path):
    pid = pl.record_proposal("", "When X happens, do Y.", "claude", "global", "manual",
                             path=pl.PROPOSALS_PATH)["proposal_id"]
    result = pl.confirm_proposal(pid, path=pl.PROPOSALS_PATH, curator=curator)
    assert result["status"] == "confirmed", result
    block = _store(tmp_path).split("<!-- lesson: ", 1)[1]
    assert block.split("-->\n", 1)[1] == "When X happens, do Y.\n\n"
    assert block.startswith("when-x-happens-do-y, scope: global, added: ")
    assert _proposals(pl)[0]["status"] == "confirmed"


def test_confirm_writes_a_trigger_as_the_bold_line(pl, curator, tmp_path):
    pid = pl.record_proposal("About to deploy", "Run the smoke test.", "claude", "global",
                             "manual", path=pl.PROPOSALS_PATH)["proposal_id"]
    result = pl.confirm_proposal(pid, path=pl.PROPOSALS_PATH, curator=curator)
    assert result["status"] == "confirmed", result
    assert _store(tmp_path).split("-->\n", 1)[1] == (
        "**About to deploy**\n\nRun the smoke test.\n\n")


def test_confirm_runs_the_curator_with_a_trigger_only_when_the_proposal_has_one(
        pl, curator, monkeypatch):
    calls: list[list[str]] = []

    class _Done:
        returncode = 0
        stdout = "wrote lesson"
        stderr = ""

    monkeypatch.setattr(pl.subprocess, "run",
                        lambda cmd, **kwargs: calls.append(cmd) or _Done())
    plain = pl.record_proposal("", "When X, do Y.", "assistant", "general", "manual",
                               path=pl.PROPOSALS_PATH)["proposal_id"]
    bold = pl.record_proposal("On X", "Do Y.", "assistant", "general", "manual",
                              path=pl.PROPOSALS_PATH)["proposal_id"]
    pl.confirm_proposal(plain, path=pl.PROPOSALS_PATH, curator=curator)
    pl.confirm_proposal(bold, path=pl.PROPOSALS_PATH, curator=curator)
    assert [cmd[2:] for cmd in calls] == [
        ["write", "--rule", "When X, do Y.", "--target", "assistant", "--scope", "general"],
        ["write", "--rule", "Do Y.", "--target", "assistant", "--trigger", "On X",
         "--scope", "general"],
    ]


# ─── vault mirror ────────────────────────────────────────────────────────────

def _mirror(mod, monkeypatch, tmp_path: Path, entry: dict) -> dict[str, str]:
    """Run mirror_lesson_to_vault with the vault writer faked; return its flags."""
    writer = tmp_path / "obsidian-write.py"
    writer.write_text("")
    monkeypatch.setattr(mod, "OBSIDIAN_WRITE", writer)
    seen: list[list[str]] = []

    class _Done:
        returncode = 0
        stdout = json.dumps({"path": "Lessons/note.md"})

    monkeypatch.setattr(mod.subprocess, "run",
                        lambda cmd, **kwargs: seen.append(cmd) or _Done())
    assert mod.mirror_lesson_to_vault(entry) == "Lessons/note.md"
    [cmd] = seen
    return {flag: cmd[cmd.index(flag) + 1] for flag in ("--title", "--body")}


def test_vault_note_is_titled_by_the_trigger_when_there_is_one(pl, monkeypatch, tmp_path):
    flags = _mirror(pl, monkeypatch, tmp_path,
                    {"trigger": "About to deploy", "rule": "Run the smoke test."})
    assert flags["--title"] == "About to deploy"
    assert flags["--body"].startswith(
        "**Rule:** Run the smoke test.\n\n- **Trigger:** About to deploy\n- **Target store:** ")


def test_vault_note_is_titled_by_the_rule_when_there_is_no_trigger(pl, monkeypatch, tmp_path):
    rule = "Before a deploy, run the smoke test." + " x" * 80 + "\n\nSecond paragraph."
    flags = _mirror(pl, monkeypatch, tmp_path, {"trigger": "", "rule": rule})
    assert flags["--title"] == rule.split("\n", 1)[0][:120]
    assert "**Trigger:**" not in flags["--body"]
    assert flags["--body"].startswith(f"**Rule:** {rule}\n\n- **Target store:** ")


def test_vault_note_with_neither_is_titled_lesson(pl, monkeypatch, tmp_path):
    assert _mirror(pl, monkeypatch, tmp_path, {})["--title"] == "lesson"
