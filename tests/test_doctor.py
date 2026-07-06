"""Tests for bin/assistant-doctor.py — the preflight. Core vs optional
classification, Slack scope logic (the H2 fix), and exit-code semantics. HTTP is
never hit — scope tests drive the pure _required_scopes logic and monkeypatch
the fetch."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def _load():
    if "assistant_doctor" in sys.modules:
        return sys.modules["assistant_doctor"]
    sys.path.insert(0, str(REPO / "bin"))
    spec = importlib.util.spec_from_file_location(
        "assistant_doctor", str(REPO / "bin" / "assistant-doctor.py"))
    m = importlib.util.module_from_spec(spec)
    sys.modules["assistant_doctor"] = m
    spec.loader.exec_module(m)
    return m


doctor = _load()


# ─── required-scope logic (minimal, not over-broad; no users:read/groups:read) ──

def test_required_scopes_channel_minimal():
    # A C… channel target requires only chat:write (history is an OR-set checked
    # separately) — NOT users:read, NOT groups:read.
    need = doctor._required_scopes("C0ABC")
    assert need == {"chat:write"}
    assert "groups:read" not in need and "users:read" not in need


def test_required_scopes_dm_needs_im_write():
    # A U… user target opens a DM → im:write. Still no users:read.
    assert doctor._required_scopes("U0MUKUL") == {"chat:write", "im:write"}


def test_history_scopes_by_target_type():
    assert doctor._history_scopes("C0ABC") == {"channels:history", "groups:history"}
    assert doctor._history_scopes("G0XYZ") == {"groups:history"}
    assert doctor._history_scopes("D0DM") == {"im:history"}
    assert doctor._history_scopes("U0USER") == {"im:history"}


# ─── scope check: history is an OR-set for EVERY target type ────────────────

def _patch_scopes(monkeypatch, scopes: set[str], target: str = "C0ABC"):
    monkeypatch.setattr(doctor.comms_lib, "bot_token", lambda env=None: "xoxb-fake")
    monkeypatch.setattr(doctor, "_slack_config", lambda: (target, "config"))
    monkeypatch.setattr(doctor, "_fetch_scopes", lambda tok: (scopes, ""))


def test_channel_scopes_pass_with_groups_history(monkeypatch):
    _patch_scopes(monkeypatch, {"chat:write", "groups:history"})
    c = doctor.check_slack_scopes()
    assert c.status == doctor.PASS and c.core is False


def test_channel_scopes_pass_with_channels_history(monkeypatch):
    _patch_scopes(monkeypatch, {"chat:write", "channels:history"})
    assert doctor.check_slack_scopes().status == doctor.PASS


def test_channel_scopes_fail_without_any_history(monkeypatch):
    _patch_scopes(monkeypatch, {"chat:write"})
    c = doctor.check_slack_scopes()
    assert c.status == doctor.FAIL
    assert "history" in c.detail and c.remedy


def test_dm_scopes_fail_without_im_write(monkeypatch):
    # U… target with history but no im:write → FAIL (can't open the DM)
    _patch_scopes(monkeypatch, {"chat:write", "im:history"}, target="U0X")
    c = doctor.check_slack_scopes()
    assert c.status == doctor.FAIL and "im:write" in c.detail


def test_dm_channel_id_fails_without_im_history(monkeypatch):
    # D… DM-channel-id with chat:write but NO im:history → must FAIL (this is the
    # D1 gap: history was previously enforced only for C… targets).
    _patch_scopes(monkeypatch, {"chat:write"}, target="D0DM")
    c = doctor.check_slack_scopes()
    assert c.status == doctor.FAIL and "im:history" in c.detail


def test_dm_channel_id_passes_with_im_history(monkeypatch):
    _patch_scopes(monkeypatch, {"chat:write", "im:history"}, target="D0DM")
    assert doctor.check_slack_scopes().status == doctor.PASS


def test_users_read_not_required(monkeypatch):
    # A channel target with chat:write + a history scope PASSes even with NO
    # users:read (no daemon users.info call).
    _patch_scopes(monkeypatch, {"chat:write", "groups:history"})
    assert doctor.check_slack_scopes().status == doctor.PASS


def test_scopes_skip_when_no_token(monkeypatch):
    monkeypatch.setattr(doctor.comms_lib, "bot_token", lambda env=None: "")
    assert doctor.check_slack_scopes().status == doctor.SKIP


# ─── core/optional classification + exit semantics ──────────────────────────

def test_core_failed_only_counts_core():
    checks = [
        doctor.Check("a", doctor.FAIL, core=False),   # optional fail
        doctor.Check("b", doctor.PASS, core=True),
    ]
    assert doctor.core_failed(checks) is False
    assert doctor.any_failed(checks) is True


def test_core_failed_true_on_core_fail():
    checks = [doctor.Check("a", doctor.FAIL, core=True)]
    assert doctor.core_failed(checks) is True


def test_main_only_core_exit_zero_when_core_ok(monkeypatch, capsys):
    # force all core checks to PASS regardless of the host
    monkeypatch.setattr(doctor, "CORE_CHECKS",
                        [lambda: doctor.Check("stub", doctor.PASS, core=True)])
    rc = doctor.main(["--only", "core"])
    assert rc == 0


def test_main_only_core_exit_one_when_core_fails(monkeypatch):
    monkeypatch.setattr(doctor, "CORE_CHECKS",
                        [lambda: doctor.Check("stub", doctor.FAIL, core=True,
                                              remedy="do the thing")])
    assert doctor.main(["--only", "core"]) == 1


def test_optional_fail_does_not_set_exit_without_strict(monkeypatch):
    monkeypatch.setattr(doctor, "CORE_CHECKS",
                        [lambda: doctor.Check("core", doctor.PASS, core=True)])
    monkeypatch.setattr(doctor, "SLACK_CHECKS",
                        [lambda: doctor.Check("opt", doctor.FAIL, core=False)])
    assert doctor.main(["--only", "all"]) == 0          # optional fail → still 0
    assert doctor.main(["--only", "all", "--strict"]) == 1  # strict → 1


def test_json_output_shape(monkeypatch, capsys):
    monkeypatch.setattr(doctor, "CORE_CHECKS",
                        [lambda: doctor.Check("stub", doctor.PASS, core=True, detail="d")])
    doctor.main(["--only", "core", "--json"])
    import json
    out = json.loads(capsys.readouterr().out)
    assert out[0]["name"] == "stub" and out[0]["core"] is True
