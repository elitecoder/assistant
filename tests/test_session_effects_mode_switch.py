"""The mode is asked once per typing, when it begins: a switch part-way leaves the typing as it began."""
from __future__ import annotations

import json

import comms_lib as cl
import comms_session as cs
import pytest

from assistant import effects, mode, session_effects as se

WS = "workspace:1000010005"


@pytest.fixture
def current(monkeypatch, tmp_path):
    state = {"mode": "pro"}
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(mode, "current", lambda: state["mode"])
    effects._forget()
    return state


def ledger(tmp_path) -> list:
    path = tmp_path / ".assistant/effects/effects.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


class Rpc:
    def __init__(self):
        self.calls = []

    def __call__(self, method, params):
        self.calls.append((method, params))
        return {}


def test_a_typing_begun_in_pro_still_gets_its_result_after_the_mode_changes(current, tmp_path):
    ef = se.begin(WS, "nudge:stranded", kind="standing", ref="nudge:stranded", component="pulse")
    keys = se.Keys(ef, Rpc(), "s", WS)
    current["mode"] = "default"
    keys.line("hi")
    keys.finish()
    assert [r["kind"] for r in ledger(tmp_path)] == ["intent", "result"] and ledger(tmp_path)[1]["outcome"] == "done"


def test_a_mode_that_cant_be_read_is_the_default_mode(monkeypatch):
    from assistant import session_gate
    monkeypatch.setattr(mode, "is_pro", lambda: (_ for _ in ()).throw(OSError("unreadable")))
    assert session_gate.active() is False


def test_a_scope_begun_in_pro_keeps_its_records_when_the_mode_changes_before_the_text_goes_out(current, tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(cs, "_cmux_rpc", lambda p, m, params, timeout=15: sent.append(m) or {})
    paths = cl.Paths.from_env({"HOME": str(tmp_path), "COMMS_HOME": str(tmp_path)})
    with cs.typing_scope(paths, WS, "surface:3", "clear", kind="standing", ref="slack-comms") as keys:
        current["mode"] = "default"
        keys.text("/clear")
    assert sent == ["surface.send_text"] and [r["kind"] for r in ledger(tmp_path)] == ["intent", "result"]


def test_the_doctors_owner_check_runs_in_pro_and_not_in_the_default_mode(current, tmp_path, monkeypatch):
    from test_doctor import _load as load_doctor
    doctor = load_doctor()
    (tmp_path / ".assistant").mkdir(parents=True)
    (tmp_path / ".assistant/config.json").write_text(json.dumps({"slack": {"target": "C0CHAN", "allowed_targets": []}}))
    monkeypatch.setattr(doctor, "SLACK_CHECKS", [doctor.check_slack_owner])
    assert [c.name for c in doctor.run_checks(only="slack")] == ["slack owner"]
    current["mode"] = "default"
    assert doctor.run_checks(only="slack") == []




def timing_out(monkeypatch):
    def rpc(p, method, params, timeout=15):
        cl._last.result = (-1, "timeout after 15s")
        return None

    monkeypatch.setattr(cs, "_cmux_rpc", rpc)


def test_a_time_out_after_the_mode_changed_to_default_is_still_unknown_for_a_typing_begun_in_pro(
        current, tmp_path, monkeypatch):
    timing_out(monkeypatch)
    paths = cl.Paths.from_env({"HOME": str(tmp_path), "COMMS_HOME": str(tmp_path)})
    with cs.typing_scope(paths, WS, "surface:3", "clear", kind="standing", ref="slack-comms") as keys:
        current["mode"] = "default"
        keys.text("/clear")
    assert [r.get("outcome") for r in ledger(tmp_path)] == [None, "unknown"]


def test_a_time_out_after_pro_was_turned_on_is_still_a_refusal_for_a_typing_begun_in_the_default_mode(
        current, tmp_path, monkeypatch):
    current["mode"] = "default"
    sent = []

    def rpc(p, method, params, timeout=15):
        sent.append(method)
        if method.endswith("text"):
            cl._last.result = (-1, "timeout after 15s")
        return None

    monkeypatch.setattr(cs, "_cmux_rpc", rpc)
    paths = cl.Paths.from_env({"HOME": str(tmp_path), "COMMS_HOME": str(tmp_path)})
    with cs.typing_scope(paths, WS, "surface:3", "clear", kind="standing", ref="slack-comms") as keys:
        current["mode"] = "pro"
        keys.line("/clear")  # as it always did: the Enter follows, nothing is raised or recorded
    assert sent == ["surface.send_text", "surface.send_key"] and ledger(tmp_path) == []
    assert not (tmp_path / ".assistant/effects").exists()


def test_a_dispatch_begun_in_the_default_mode_carries_on_when_pro_is_turned_on_before_its_text_times_out(
        current, tmp_path):
    from test_default_mode_is_main import Dispatch
    current["mode"] = "default"
    run = Dispatch(tmp_path, timeout_text=True)
    original = run.rpc

    def rpc(method, params, timeout=15):
        current["mode"] = "pro"  # turned on while the typing is under way
        return original(method, params, timeout)

    run.rpc = rpc
    assert run.go() is True and [c[0] for c in run.calls] == ["surface.send_text", "surface.send_key"]
    assert not (tmp_path / ".assistant/effects").exists()


def test_a_restore_begun_in_the_default_mode_reads_a_time_out_as_a_refusal_even_when_pro_is_turned_on(
        current, tmp_path):
    from test_default_mode_is_main import PANEL, restore_module
    current["mode"] = "default"
    restore = restore_module()
    seen = []

    def run_cmux(*args, timeout=5):
        seen.append(args[1])
        current["mode"] = "pro"
        return 124, "", "t"

    restore.run_cmux = run_cmux
    assert restore.respawn_panel({"title": "t"}, PANEL, "cd /x", False, False) is False
    assert len(seen) == 2 and not (tmp_path / ".assistant/effects").exists()


def results(tmp_path):
    return [r.get("outcome") for r in ledger(tmp_path) if r["kind"] == "result"]


def test_a_dispatch_begun_in_pro_keeps_a_time_out_unknown_when_the_mode_changes_to_default_before_it(
        current, tmp_path):
    from test_default_mode_is_main import Dispatch
    run = Dispatch(tmp_path, timeout_text=True, source="manual:x")
    original = run.rpc

    def rpc(method, params, timeout=15):
        current["mode"] = "default"  # turned off while the typing is under way
        return original(method, params, timeout)

    run.rpc = rpc
    run.go()
    assert results(tmp_path)[-1] == "unknown"


def test_a_restore_begun_in_pro_keeps_a_time_out_unknown_when_the_mode_changes_to_default_before_it(
        current, tmp_path):
    from test_default_mode_is_main import restore_module
    restore = restore_module()
    panel = {"id": "panel-1", "terminal": {"surfaceId": "0123ABCD-0123-0123-0123-0123456789AB"}}

    def run_cmux(*args, timeout=5):
        current["mode"] = "default"
        return 124, "", "t"

    restore.run_cmux = run_cmux
    assert restore.respawn_panel({"title": "t"}, panel, "cd /x", False, False) is False
    assert results(tmp_path) == ["unknown"]
