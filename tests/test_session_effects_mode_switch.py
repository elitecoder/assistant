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


def test_a_typing_begun_in_the_default_mode_writes_nothing_even_if_pro_is_on_before_it_ends(current, tmp_path, capsys):
    current["mode"] = "default"
    ef = se.begin(WS, "nudge:stranded", kind="standing", ref="nudge:stranded", component="pulse")
    assert ef == se.PASSTHROUGH
    rpc = Rpc()
    keys = se.Keys(ef, rpc, "s", WS)
    current["mode"] = "pro"
    keys.line("hi")
    keys.finish()
    se.finish(ef, "done")
    assert capsys.readouterr().err == ""  # not even a complaint about a result with no intent
    assert [m for m, _ in rpc.calls] == ["surface.send_text", "surface.send_key"]
    assert ledger(tmp_path) == [] and not (tmp_path / ".assistant/effects").exists()


def test_a_mode_that_cant_be_read_is_the_default_mode(monkeypatch):
    monkeypatch.setattr(mode, "is_pro", lambda: (_ for _ in ()).throw(OSError("unreadable")))
    assert se.active() is False


def test_a_scope_begun_in_pro_keeps_its_records_when_the_mode_changes_before_the_text_goes_out(current, tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(cs, "_cmux_rpc", lambda p, m, params, timeout=15: sent.append(m) or {})
    paths = cl.Paths.from_env({"HOME": str(tmp_path), "COMMS_HOME": str(tmp_path)})
    with cs.typing_scope(paths, WS, "surface:3", "clear", kind="standing", ref="slack-comms") as keys:
        current["mode"] = "default"
        keys.text("/clear")
    assert sent == ["surface.send_text"] and [r["kind"] for r in ledger(tmp_path)] == ["intent", "result"]


def test_a_batch_whose_mode_changed_part_way_closes_what_it_had_begun_and_types_unrecorded(current, tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(cs, "_cmux_rpc", lambda p, m, params, timeout=15: sent.append(m) or {})
    paths = cl.Paths.from_env({"HOME": str(tmp_path), "COMMS_HOME": str(tmp_path)})
    real = se.begin
    answers = iter([None, se.PASSTHROUGH])  # the first begin is real; by the second the mode is default

    def begin(*a, **k):
        return real(*a, **k) if next(answers) is None else se.PASSTHROUGH

    monkeypatch.setattr(se, "begin", begin)
    older = [{"ref": "slack:1793613610.000100", "anchor": 1793613610.0, "slack_ts": "1793613610.000100"}]
    with cs.typing_scope(paths, WS, "surface:3", "slack-message", kind="tap", batch=older, ref="slack:1793613611.000100",
                         anchor=1793613611.0, slack_ts="1793613611.000100") as keys:
        keys.text("x")
    assert sent == ["surface.send_text"]
    assert [(r["kind"], r.get("outcome")) for r in ledger(tmp_path)] == [("intent", None), ("result", "failed")]


def test_the_doctors_owner_check_runs_in_pro_and_not_in_the_default_mode(current, tmp_path, monkeypatch):
    from test_doctor import _load as load_doctor
    doctor = load_doctor()
    (tmp_path / ".assistant").mkdir(parents=True)
    (tmp_path / ".assistant/config.json").write_text(json.dumps({"slack": {"target": "C0CHAN", "allowed_targets": []}}))
    monkeypatch.setattr(doctor, "SLACK_CHECKS", [doctor.check_slack_owner])
    assert [c.name for c in doctor.run_checks(only="slack")] == ["slack owner"]
    current["mode"] = "default"
    assert doctor.run_checks(only="slack") == []


def test_a_time_out_after_the_mode_changed_to_default_is_a_refusal_not_an_unknown(current, tmp_path, monkeypatch):
    def rpc(p, method, params, timeout=15):
        cl._last.result = (-1, "timeout after 15s")
        return None

    monkeypatch.setattr(cs, "_cmux_rpc", rpc)
    paths = cl.Paths.from_env({"HOME": str(tmp_path), "COMMS_HOME": str(tmp_path)})
    with cs.typing_scope(paths, WS, "surface:3", "clear", kind="standing", ref="slack-comms") as keys:
        current["mode"] = "default"
        keys.text("/clear")
    assert [r.get("outcome") for r in ledger(tmp_path)] == [None, "failed"]
