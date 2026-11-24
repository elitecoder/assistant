"""Typing into a session and the work records agree: each typing carries its task's trace, and every way
`dispatch_todo` can end says why in the dispatch record (or that it went out)."""
from __future__ import annotations

import ast
import json
from pathlib import Path
from unittest import mock

import pytest

from assistant import effects, records, session_effects, telemetry, todostore

from test_work_records import OWN, dispatch, of_kind, sent_task, trail, wq  # noqa: F401 - the wq fixture

PULSE = Path(__file__).resolve().parent.parent / "bin/pulse.py"


def ledger_rows(wq) -> list:
    path = wq.home / ".assistant/effects/effects.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]


def the_dispatch_record() -> dict:
    return [e for e in of_kind("dispatch")][-1]


# --- the registry --------------------------------------------------------------------------------------------------

def test_a_dispatch_reason_of_typing_refused_is_accepted_and_free_text_still_is_not(wq):
    assert "typing_refused" in telemetry.DISPATCH_REASON
    now = 0.0
    records.dispatch({"id": "td-1", "trace": OWN}, {"id": "td-1", "trace": OWN}, False, now, "typing_refused")
    assert of_kind("dispatch")[-1]["fields"]["reason"] == "typing_refused"
    # a reason somebody wrote in words is not on the list, so it is not kept (strict mode would raise)
    assert records.one_of("the disk is full", "DISPATCH_REASON") is None
    assert records.one_of("typing_refused", "DISPATCH_REASON") == "typing_refused"


# --- the trace ----------------------------------------------------------------------------------------------------

def test_a_typing_for_a_task_carries_the_tasks_trace_and_shares_it_with_the_dispatch_record(wq, monkeypatch):
    sent_task(wq)
    _, ok, _ = dispatch(monkeypatch, "td-1")
    assert ok is True
    intent = ledger_rows(wq)[0]
    assert intent["trace"] == OWN
    [typing] = [e for e in trail(OWN) if e["kind"] == "effect"]
    dispatched = [e for e in trail(OWN) if e["kind"] == "dispatch"]
    assert len(dispatched) == 1 and typing["trace"] == dispatched[0]["trace"] == OWN
    assert typing["fields"]["action"] == "session.type" and typing["fields"]["target"] == "td-1"


def test_a_task_without_a_trace_of_its_own_shares_its_stand_in(wq, monkeypatch):
    sent_task(wq, trace="")
    stand_in = todostore.trace_of(wq.items()[0])
    _, ok, _ = dispatch(monkeypatch, "td-1")
    assert ok is True and ledger_rows(wq)[0]["trace"] == stand_in
    assert of_kind("dispatch")[-1]["trace"] == stand_in


def test_a_typing_with_no_task_carries_no_trace(wq):
    ef = session_effects.begin("workspace:1000020126", "nudge:stranded", kind="standing", ref="nudge:stranded",
                               component="pulse")
    session_effects.finish(ef, "done")
    assert ledger_rows(wq)[0]["trace"] == ""


def test_each_typing_is_told_to_telemetry_once_and_only_by_the_ledger(wq, monkeypatch):
    sent_task(wq)
    dispatch(monkeypatch, "td-1")
    assert len([e for e in of_kind("effect")]) == 1  # one typing, one record: nothing of ours writes a second


# --- every way dispatch can end says why ----------------------------------------------------------------------------

@pytest.mark.parametrize("how", ["writable", "begin", "invalid"])
def test_a_dispatch_whose_typing_is_refused_says_typing_refused_and_the_task_is_parked_unless_nothing_started(
        wq, monkeypatch, how):
    sent_task(wq)
    ws = "workspace:" + "9" * 19 if how == "invalid" else "workspace:1000020126"
    patches = []
    if how == "writable":
        patches.append(mock.patch.object(session_effects, "writable", return_value=False))
    if how == "begin":
        patches.append(mock.patch.object(effects, "begin", side_effect=effects.Unrecorded("disk full")))
    for patch in patches:
        patch.start()
    try:
        _, ok, _ = dispatch(monkeypatch, "td-1", workspace=ws)
    finally:
        for patch in patches:
            patch.stop()
    assert ok is False
    record = the_dispatch_record()
    assert record["fields"]["reason"] == "typing_refused" and record["fields"]["outcome"] == "failed"
    assert wq.items()[0]["status"] == ("open" if how == "writable" else "in-progress")  # parked when a session exists


def test_a_dispatch_that_typed_the_instruction_says_whether_it_was_seen_to_arrive(wq, monkeypatch):
    sent_task(wq)
    dispatch(monkeypatch, "td-1")
    assert the_dispatch_record()["fields"]["confirmed"] is False and "reason" not in the_dispatch_record()["fields"]


def test_every_return_in_dispatch_todo_says_why_or_says_it_went_out():
    """Each `return` is preceded by `_dispatch_failed(...)` (or is `return _park_refused_dispatch(...)`, which
    says so itself), or by `_dispatch_confirmed(...)` earlier in the function for the one that went out."""
    tree = ast.parse(PULSE.read_text())
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "dispatch_todo")
    park = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_park_refused_dispatch")
    assert any(isinstance(c, ast.Call) and getattr(c.func, "id", "") == "_dispatch_failed" and
               c.args[0].value == "typing_refused" for c in ast.walk(park))

    def calls(node, name):
        return isinstance(node, ast.Expr) and isinstance(node.value, ast.Call) and getattr(node.value.func, "id", "") == name

    bare, seen = [], []

    def walk(body, said_confirmed):
        for index, node in enumerate(body):
            if isinstance(node, ast.Return):
                value = node.value
                if isinstance(value, ast.Call) and getattr(value.func, "id", "") == "_park_refused_dispatch":
                    seen.append(node.lineno)
                elif isinstance(value, ast.Constant) and value.value is True and said_confirmed:
                    seen.append(node.lineno)
                elif index and calls(body[index - 1], "_dispatch_failed") and isinstance(value, ast.Constant) \
                        and value.value is False:
                    seen.append(node.lineno)
                else:
                    bare.append(node.lineno)
            for field in ("body", "orelse", "finalbody"):
                inner = getattr(node, field, None)
                if isinstance(inner, list) and inner and isinstance(inner[0], ast.stmt):
                    walk(inner, said_confirmed)
            said_confirmed = said_confirmed or calls(node, "_dispatch_confirmed")

    walk(fn.body, False)
    assert bare == [] and len(seen) >= 12, (bare, seen)
