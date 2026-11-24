"""Every model call is counted where it is booked, and only background jobs are paused."""
import json
import sys
import time

import pytest

import metering
from assistant import failwatch

USAGE = {"tokens_in": 1, "tokens_out": 1, "cost_usd": 0.0, "source": "cli"}


def book(tmp_path, caller="lessons", status="failed", **kw):
    metering.append_cost_row(caller=caller, model="m-1", usage=USAGE, wall_ms=1, status=status,
                             provider="droid", path=tmp_path / "cost.jsonl", **kw)


def test_a_failed_call_is_counted_with_provider_model_and_error(tmp_path):
    book(tmp_path, error="Error: no such model")
    book(tmp_path, error="Error: no such model")
    (row,) = failwatch.repeated(min_count=1)
    assert row["component"] == "model:lessons" and row["count"] == 2
    assert row["sample"] == "droid m-1 call failed: Error: no such model"
    assert len((tmp_path / "cost.jsonl").read_text().splitlines()) == 2


def test_a_failed_call_with_no_error_or_names_still_says_which_call_failed(tmp_path):
    metering.append_cost_row(caller="lessons", model=None, usage=USAGE, wall_ms=1, status="failed",
                             path=tmp_path / "cost.jsonl")
    assert failwatch.repeated(min_count=1)[0]["sample"] == "unknown provider unknown model call failed"


def test_an_ok_row_clears_the_back_off_and_keeps_the_history(tmp_path):
    for _ in range(3):
        book(tmp_path)
    assert failwatch.backed_off("model:lessons")
    book(tmp_path, status="ok")
    assert failwatch.backed_off("model:lessons") is None
    assert metering.paused_note("lessons") is None
    assert failwatch.history()[0]["count"] == 3


def test_other_statuses_are_not_counted(tmp_path):
    book(tmp_path, status="skipped")
    assert failwatch.repeated(min_count=1) == []


@pytest.mark.parametrize("caller", metering.BACKGROUND_CALLERS)
def test_background_callers_are_paused_after_three_identical_failures(tmp_path, caller):
    for _ in range(2):
        book(tmp_path, caller=caller)
    assert metering.paused_note(caller) is None
    book(tmp_path, caller=caller)
    assert f"{caller} model calls paused after repeating the same failure; next try after " in metering.paused_note(caller)


@pytest.mark.parametrize("caller", ["work_queue", "slack_focus"])
def test_calls_a_person_asked_for_are_counted_but_never_paused(tmp_path, caller):
    for _ in range(5):
        book(tmp_path, caller=caller)
    assert failwatch.backed_off("model:" + caller) is None
    assert metering.paused_note(caller) is None
    assert failwatch.repeated(min_count=1)[0]["count"] == 5
    for _ in range(3):  # even a pause somehow on record never reaches a person's call
        failwatch.record_failure("model:" + caller, "x", pause=True)
    assert failwatch.backed_off("model:" + caller)
    assert metering.paused_note(caller) is None


def test_failure_reason_in_words():
    assert metering.failure_reason(124) == "timed out"
    assert metering.failure_reason(127, "x") == "not installed"
    assert metering.failure_reason(1, "warming up\nError: bad model\n") == "Error: bad model"
    assert metering.failure_reason(1, "") == "exit code 1"
    assert metering.failure_reason(2) == "exit code 2"
    assert len(metering.failure_reason(1, "x" * 400)) == 150


def test_counting_never_breaks_the_booking(tmp_path, monkeypatch):
    monkeypatch.setattr(failwatch, "record_failure", lambda *a, **k: 1 / 0)
    monkeypatch.setattr(failwatch, "backed_off", lambda *a, **k: 1 / 0)
    book(tmp_path)
    assert (tmp_path / "cost.jsonl").exists()
    assert metering.paused_note("lessons") is None


def test_with_no_counter_available_booking_and_pausing_carry_on(tmp_path, monkeypatch):
    monkeypatch.setattr(metering, "_failwatch", lambda: None)
    book(tmp_path)
    assert metering.paused_note("lessons") is None
    assert (tmp_path / "cost.jsonl").exists()


def test_the_counter_is_loaded_even_when_src_is_not_on_the_path(monkeypatch):
    monkeypatch.setattr(sys, "path", [p for p in sys.path if not p.endswith("/src")])
    assert metering._failwatch() is failwatch
    assert any(p.endswith("/src") for p in sys.path)


def test_an_unloadable_counter_is_none(monkeypatch):
    monkeypatch.setitem(sys.modules, "assistant.failwatch", None)
    monkeypatch.delattr(sys.modules["assistant"], "failwatch")
    assert metering._failwatch() is None
