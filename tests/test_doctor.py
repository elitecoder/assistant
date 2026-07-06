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


# ─── required-scope logic (the H2 fix — minimal, not over-broad) ────────────

def test_required_scopes_channel_minimal():
    # A C… channel target must NOT require groups:read (only history + write + users)
    need = doctor._required_scopes("C0ABC")
    assert need == {"chat:write", "users:read"}
    assert "groups:read" not in need


def test_required_scopes_private_group():
    assert doctor._required_scopes("G0XYZ") == {"chat:write", "users:read", "groups:history"}


def test_required_scopes_dm():
    assert doctor._required_scopes("U0MUKUL") == {"chat:write", "users:read", "im:write", "im:history"}


# ─── scope check accepts EITHER history scope for a C… target ───────────────

def _patch_scopes(monkeypatch, scopes: set[str], target: str = "C0ABC"):
    monkeypatch.setattr(doctor.comms_lib, "bot_token", lambda env=None: "xoxb-fake")
    monkeypatch.setattr(doctor, "_slack_config", lambda: (target, "config"))
    monkeypatch.setattr(doctor, "_fetch_scopes", lambda tok: (scopes, ""))


def test_channel_scopes_pass_with_groups_history(monkeypatch):
    _patch_scopes(monkeypatch, {"chat:write", "users:read", "groups:history"})
    c = doctor.check_slack_scopes()
    assert c.status == doctor.PASS and c.core is False


def test_channel_scopes_pass_with_channels_history(monkeypatch):
    _patch_scopes(monkeypatch, {"chat:write", "users:read", "channels:history"})
    assert doctor.check_slack_scopes().status == doctor.PASS


def test_channel_scopes_fail_without_any_history(monkeypatch):
    _patch_scopes(monkeypatch, {"chat:write", "users:read"})
    c = doctor.check_slack_scopes()
    assert c.status == doctor.FAIL
    assert "history" in c.detail and c.remedy


def test_dm_scopes_fail_without_im_write(monkeypatch):
    _patch_scopes(monkeypatch, {"chat:write", "users:read", "im:history"}, target="U0X")
    c = doctor.check_slack_scopes()
    assert c.status == doctor.FAIL and "im:write" in c.detail


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
