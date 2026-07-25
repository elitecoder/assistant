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


# ─── droid provider/agent checks (G7) ───────────────────────────────────────

def _select_droid(monkeypatch):
    monkeypatch.setattr(doctor.agent_session, "warm_agent", lambda: doctor.DROID)
    monkeypatch.setattr(doctor.agent_session, "dispatch_agent", lambda: doctor.DROID)


def _select_claude(monkeypatch):
    monkeypatch.setattr(doctor.agent_session, "warm_agent", lambda: "claude")
    monkeypatch.setattr(doctor.agent_session, "dispatch_agent", lambda: "claude")


def _write_droid_settings(home, body='{"model": "glm-5.2"}'):
    s = home / ".assistant" / "droid-glm-settings.json"
    s.parent.mkdir(parents=True, exist_ok=True)
    s.write_text(body)
    return s


def test_droid_binary_pass_when_selected_and_present(monkeypatch, tmp_path):
    _select_droid(monkeypatch)
    droid = tmp_path / "droid"
    droid.write_text("#!/bin/sh\n")
    droid.chmod(0o755)
    monkeypatch.setattr(doctor.shutil, "which",
                        lambda b: str(droid) if b == "droid" else None)
    c = doctor.check_droid_binary()
    assert c.status == doctor.PASS
    assert c.detail == str(droid)


def test_droid_binary_found_in_local_bin_off_path(monkeypatch, tmp_path):
    # Factory's default ~/.local/bin — invisible to launchd's pinned PATH
    # (shutil.which misses) but found by the known-location probe.
    _select_droid(monkeypatch)
    monkeypatch.setattr(doctor.shutil, "which", lambda _b: None)
    monkeypatch.setattr(doctor, "HOME", tmp_path)
    d = tmp_path / ".local" / "bin"
    d.mkdir(parents=True)
    droid = d / "droid"
    droid.write_text("#!/bin/sh\n")
    droid.chmod(0o755)
    c = doctor.check_droid_binary()
    assert c.status == doctor.PASS and c.detail == str(droid)


def test_droid_binary_fail_when_missing(monkeypatch, tmp_path):
    _select_droid(monkeypatch)
    monkeypatch.setattr(doctor.shutil, "which", lambda _b: None)
    monkeypatch.setattr(doctor, "HOME", tmp_path)   # no ~/.local/bin/droid
    c = doctor.check_droid_binary()
    assert c.status == doctor.FAIL
    assert c.remedy and "droid" in c.remedy


def test_droid_binary_nonexecutable_local_bin_fails(monkeypatch, tmp_path):
    _select_droid(monkeypatch)
    monkeypatch.setattr(doctor.shutil, "which", lambda _b: None)
    monkeypatch.setattr(doctor, "HOME", tmp_path)
    d = tmp_path / ".local" / "bin"
    d.mkdir(parents=True)
    droid = d / "droid"
    droid.write_text("#!/bin/sh\n")
    droid.chmod(0o644)   # present but not executable
    assert doctor.check_droid_binary().status == doctor.FAIL


def test_droid_settings_pass_when_valid(monkeypatch, tmp_path):
    _select_droid(monkeypatch)
    monkeypatch.setattr(doctor, "HOME", tmp_path)
    settings = _write_droid_settings(tmp_path)
    c = doctor.check_droid_settings()
    assert c.status == doctor.PASS and c.detail == str(settings)


def test_droid_settings_fail_when_missing(monkeypatch, tmp_path):
    _select_droid(monkeypatch)
    monkeypatch.setattr(doctor, "HOME", tmp_path)   # no settings file written
    c = doctor.check_droid_settings()
    assert c.status == doctor.FAIL
    assert "missing" in c.detail and c.remedy


def test_droid_settings_fail_when_corrupt_json(monkeypatch, tmp_path):
    _select_droid(monkeypatch)
    monkeypatch.setattr(doctor, "HOME", tmp_path)
    _write_droid_settings(tmp_path, body="{not json")
    c = doctor.check_droid_settings()
    assert c.status == doctor.FAIL
    assert "valid JSON" in c.detail and c.remedy


def test_droid_settings_fail_when_not_object(monkeypatch, tmp_path):
    _select_droid(monkeypatch)
    monkeypatch.setattr(doctor, "HOME", tmp_path)
    _write_droid_settings(tmp_path, body='["a", "list"]')
    assert doctor.check_droid_settings().status == doctor.FAIL


def test_droid_checks_skip_when_claude_selected(monkeypatch, tmp_path):
    # A claude-only box must NOT spuriously FAIL the droid checks even with no
    # droid binary and no settings file present.
    _select_claude(monkeypatch)
    monkeypatch.setattr(doctor.shutil, "which", lambda _b: None)
    monkeypatch.setattr(doctor, "HOME", tmp_path)
    assert doctor.check_droid_binary().status == doctor.SKIP
    assert doctor.check_droid_settings().status == doctor.SKIP


def test_droid_selected_if_only_dispatch_agent_is_droid(monkeypatch):
    # warm_agent claude but dispatch_agent droid → still runs droid checks.
    monkeypatch.setattr(doctor.agent_session, "warm_agent", lambda: "claude")
    monkeypatch.setattr(doctor.agent_session, "dispatch_agent", lambda: doctor.DROID)
    assert doctor._droid_selected() is True


def test_agent_checks_run_by_default_not_under_only_core_or_slack(monkeypatch):
    _select_claude(monkeypatch)
    names_all = {c.name for c in doctor.run_checks("all")}
    assert "droid binary" in names_all and "droid settings" in names_all
    names_core = {c.name for c in doctor.run_checks("core")}
    assert "droid binary" not in names_core
    names_slack = {c.name for c in doctor.run_checks("slack")}
    assert "droid binary" not in names_slack


def test_claude_box_agent_checks_do_not_fail_exit(monkeypatch, tmp_path):
    # A claude-only box: droid checks SKIP, so --strict all must not exit nonzero
    # on their account.
    _select_claude(monkeypatch)
    monkeypatch.setattr(doctor.shutil, "which", lambda _b: None)
    monkeypatch.setattr(doctor, "HOME", tmp_path)
    monkeypatch.setattr(doctor, "CORE_CHECKS",
                        [lambda: doctor.Check("core", doctor.PASS, core=True)])
    monkeypatch.setattr(doctor, "SLACK_CHECKS",
                        [lambda: doctor.Check("opt", doctor.SKIP, core=False)])
    assert doctor.main(["--only", "all", "--strict"]) == 0
