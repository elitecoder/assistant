"""src/assistant/telemetry.py: the local record of what Assistant did. It never raises, never holds content, never
writes a secret, never leaves the Mac, and loads on the system Python."""
from __future__ import annotations

import ast
import enum
import fcntl
import importlib.util
import json
import logging
import os
import re
import sqlite3
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest import mock

import pytest

from assistant import pointers
from assistant import telemetry as T

REPO = Path(__file__).resolve().parent.parent
MODULE = REPO / "src/assistant/telemetry.py"
TRACE = "tr-0123456789abcdef"
# Made-up secrets are built from parts so GitHub's push protection doesn't take them for real ones.
BODY = "AbCdEfGhIjKlMnOpQrStUv0123456789"
SECRET_WORDS = {
    "slack": "-".join(["xoxb", "111111111", "222222222", BODY]),
    "github": "_".join(["ghp", BODY]),
    "model": "-".join(["sk", "ant", "api03", BODY]),
    "aws": "".join(["AK", "IAIOSFODNN7EXAMPLE"]),
}


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A temporary home. The suite runs telemetry in strict mode (tests/conftest.py); the tests here are about
    what it leaves out, so they clear it."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("ASSISTANT_TELEMETRY_STRICT", raising=False)
    (tmp_path / ".assistant").mkdir()
    return tmp_path


def tdir(home) -> Path:
    return home / ".assistant" / "telemetry"


def config(home, text) -> None:
    (home / ".assistant" / "config.json").write_text(text if isinstance(text, str) else json.dumps(text))


def rows(home, sql="SELECT kind, trace, subject, component, fields FROM events"):
    conn = sqlite3.connect(str(tdir(home) / "telemetry.db"))
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def spool_lines(home):
    path = tdir(home) / "spool.jsonl"
    return path.read_text().splitlines() if path.exists() else []


def counter(home) -> dict:
    return json.loads((tdir(home) / "dropped.json").read_text())


def mode(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def claimed(home):
    return [p for p in tdir(home).iterdir() if p.name.startswith("spool.draining.")]


def copies(home):
    return sorted(p for p in tdir(home).iterdir() if ".corrupt-" in p.name)


@pytest.fixture
def locked_db(home):
    """The database, locked by another connection for the length of the test."""
    assert T.emit("check", name="seed")
    holder = sqlite3.connect(str(tdir(home) / "telemetry.db"), isolation_level=None)
    holder.execute("BEGIN EXCLUSIVE")
    yield holder
    holder.close()


@pytest.fixture
def fresh_clock(monkeypatch):
    """Renamed corrupt copies get a stamp from the clock; give each its own so no test has to wait for one."""
    ticks = iter(range(10 ** 6))
    monkeypatch.setattr(T, "_copy_stamp", lambda: "%015d" % next(ticks))


# ---------------------------------------------------------------- a record


def test_a_record_lands_in_the_database_with_its_pointers_and_fields(home):
    assert T.emit("task", trace=TRACE, subject="row:0123456789abcdef", component="pulse", state="open", attempt=2)
    [(kind, trace, subject, component, fields)] = rows(home)
    assert (kind, trace, subject, component) == ("task", TRACE, "row:0123456789abcdef", "pulse")
    assert json.loads(fields) == {"attempt": 2, "state": "open"}


def test_events_read_back_oldest_first_and_filter(home):
    other = pointers.legacy_trace_id("row", "x")
    for i, (kind, trace, subject, component) in enumerate([("check", TRACE, "a", "x"), ("task", other, "b", "y"),
                                                           ("check", TRACE, "b", "x")]):
        assert T.emit(kind, trace=trace, subject=subject, component=component,
                      **({"count": i} if kind == "check" else {}))
    everything = T.events()
    assert [e["fields"].get("count") for e in everything] == [0, None, 2]
    assert [e["ts"] for e in everything] == sorted(e["ts"] for e in everything)
    assert {"id", "ts", "kind", "trace", "subject", "component", "fields"} == set(everything[0])
    assert len(T.events(trace=TRACE)) == 2
    assert len(T.events(kind="task")) == 1
    assert len(T.events(trace=TRACE, subject="b")) == 1
    assert len(T.events(component="x")) == 2
    assert len(T.events(since=everything[2]["ts"])) >= 1
    assert T.events(since=everything[2]["ts"] + 10) == []
    assert [e["fields"].get("count") for e in T.events(until=everything[0]["ts"])] == [0]
    assert [e["fields"].get("count") for e in T.events(limit=1)] == [0]
    assert T.events(trace=7) == []
    assert len(T.events(limit=0)) == 1  # a limit below one is one


def test_events_can_give_the_newest_first(home):
    for i in range(5):
        T.emit("check", count=i)
    assert [e["fields"]["count"] for e in T.events(newest_first=True, limit=2)] == [4, 3]
    assert [e["fields"]["count"] for e in T.events(limit=2)] == [0, 1]
    assert [e["fields"]["count"] for e in T.events(newest_first=True, kind="check")] == [4, 3, 2, 1, 0]


def test_events_never_creates_the_database(home):
    assert T.events() == []
    assert not tdir(home).exists()


def test_events_is_read_only_and_capped(home, monkeypatch):
    for i in range(5):
        T.emit("check", count=i)
    monkeypatch.setattr(T, "QUERY_LIMIT_MAX", 3)
    assert len(T.events(limit=10 ** 6)) == 3
    log = []
    real = T._connect

    class Spy:
        def __init__(self, conn):
            self.conn = conn

        def execute(self, sql, *args):
            log.append(sql)
            return self.conn.execute(sql, *args)

        def close(self):
            self.conn.close()

    monkeypatch.setattr(T, "_connect", lambda create=True: Spy(real(create)))
    T.events()
    assert log[0] == "PRAGMA query_only=ON" and log[1].startswith("SELECT")


# ---------------------------------------------------------------- pointers, not content

ACCEPTED = ["td-012", "workspace:7", "row:0123456789abcdef", "tr-0123456789abcdef", "ef-0123456789abcdef",
            "anthropic/claude-sonnet-4-5", "claude-sonnet-4-5@20250929", "requests.exceptions.HTTPError",
            "gpt-4.1", "claude-3.5-sonnet", "work-queue", "a", "A_b.c:d/e-9@123456", "x" * 120]
REFUSED = ["ann.lee@example.com", "/Users/ann/project-x/notes", "github.com/acme/secret-repo/pull/42",
           "mailto:a@b.co", "https:/evil.com/a", "file:/etc", "_lead", "https://example.com/a", "file:/etc/passwd",
           "www.example.com/path", "mail.example.com", "~/notes", ".hidden", "a/b/c", "two words", "a//b",
           "x" * 121, 'a"b', "a\nb", "a#b", "a+b", "-lead", "a@12345", "a@123456789", "a@123456x", "a@123456-x",
           "notes.txt", "a.py/b", "ann@example", 5, 1.5, None, b"abc", ["a"]]


@pytest.mark.parametrize("value", ACCEPTED)
def test_an_id_like_word_is_kept_as_a_field_a_subject_and_a_component(home, value):
    assert T.emit("task", subject=value, component=value, state=value)
    [(_, _, subject, component, fields)] = rows(home)
    assert (subject, component, json.loads(fields)) == (value, value, {"state": value})


@pytest.mark.parametrize("value", REFUSED, ids=repr)
def test_anything_else_is_left_out_and_counted(home, value):
    assert T.emit("task", subject=value, component=value, state=value, attempt=1)
    [(_, _, subject, component, fields)] = rows(home)
    assert (subject, component, json.loads(fields)) == ("", "", {"_dropped": 3, "attempt": 1})


def test_an_empty_word_is_no_subject_but_not_a_field_value(home):
    assert T.emit("task", subject="", state="")
    assert json.loads(rows(home)[0][-1]) == {"_dropped": 1}


def test_a_str_that_is_a_subclass_cannot_hand_over_other_text(home):
    class Sneaky(str):
        def __str__(self):
            return "Dear Ana, the quarterly numbers are attached"

    class Colour(str, enum.Enum):
        RED = "red"

    for value in (Sneaky("fine"), Colour.RED):
        assert T.emit("task", subject=value, component=value, state=value, attempt=1)
    assert T.emit(Sneaky("task"), state="x") is False
    assert T.emit("task", trace=Sneaky(TRACE)) is True
    assert T.emit("task", **{Sneaky("state"): "x"}) is True
    stored = rows(home)
    assert len(stored) == 4
    assert all(s == "" and c == "" for _, _, s, c, _ in stored[:2])
    assert json.loads(stored[0][-1]) == {"_dropped": 3, "attempt": 1}
    assert stored[2][1] == "" and json.loads(stored[2][-1]) == {"_dropped": 1}
    assert json.loads(stored[3][-1]) == {"_dropped": 1}
    assert b"Dear Ana" not in (tdir(home) / "telemetry.db").read_bytes()


@pytest.mark.parametrize("value", [True, 1.5, "1", None, [1]])
def test_a_whole_number_field_refuses_anything_else(home, value):
    assert T.emit("check", count=value)
    assert json.loads(rows(home)[0][-1]) == {"_dropped": 1}


@pytest.mark.parametrize("value", [1, 2.5, -3])
def test_a_number_field_keeps_numbers(home, value):
    assert T.emit("model_call", cost_usd=value)
    assert json.loads(rows(home)[0][-1]) == {"cost_usd": value}


@pytest.mark.parametrize("value", [True, float("nan"), float("inf"), "1.5", None])
def test_a_number_field_refuses_booleans_text_and_non_finite_numbers(home, value):
    assert T.emit("model_call", cost_usd=value)
    assert json.loads(rows(home)[0][-1]) == {"_dropped": 1}


@pytest.mark.parametrize("value", [1, 0, "true", None])
def test_a_yes_no_field_takes_only_a_boolean(home, value):
    assert T.emit("check", ok=value)
    assert json.loads(rows(home)[0][-1]) == {"_dropped": 1}


def test_a_whole_number_too_big_for_the_database_is_left_out(home):
    assert T.emit("check", count=2 ** 70)
    assert json.loads(rows(home)[0][-1]) == {"_dropped": 1}


def test_a_field_the_kind_does_not_list_is_left_out_and_counted(home):
    assert T.emit("check", name="x", title="Quarterly plan", body="anything", count=1)
    assert json.loads(rows(home)[0][-1]) == {"_dropped": 2, "count": 1, "name": "x"}


@pytest.mark.parametrize("trace", ["tr-short", "row:abc", "tr-0123456789ABCDEF", 5, None])
def test_a_trace_that_is_not_a_trace_id_is_left_out_and_counted(home, trace):
    assert T.emit("check", trace=trace)
    [(_, got, _, _, fields)] = rows(home)
    assert got == "" and json.loads(fields) == {"_dropped": 1}


def test_a_minted_and_a_legacy_trace_id_are_both_kept(home):
    legacy = pointers.legacy_trace_id("row", "k")
    assert T.emit("check", trace=pointers.new_trace_id()) and T.emit("check", trace=legacy)
    assert legacy in [r[1] for r in rows(home)]


@pytest.mark.parametrize("kind", ["nope", "", None, 5, ["check"]])
def test_an_unknown_kind_stores_nothing_and_is_counted(home, kind):
    assert T.emit(kind, count=1) is False
    assert not (tdir(home) / "telemetry.db").exists()
    assert counter(home)["unknown_kind"] == 1


def test_a_value_of_a_type_that_misbehaves_is_left_out_without_being_looked_at(home):
    class Evil(str):
        def __ne__(self, other):
            raise RuntimeError("boom")

        def __eq__(self, other):
            raise RuntimeError("boom")

        __hash__ = str.__hash__

    assert T.emit("task", state=Evil("ok"), attempt=1)
    assert json.loads(rows(home)[0][-1]) == {"_dropped": 1, "attempt": 1}


def test_a_loss_that_cannot_be_noted_is_not_an_error(home, monkeypatch):
    monkeypatch.setattr(T, "_ensure_dir", lambda: (_ for _ in ()).throw(OSError("no")))
    T._count_loss("spool_full")


def test_a_busy_counter_lock_at_upkeep_still_reports(home):
    T._count_loss("spool_full", 2)
    fd = _hold(home, "dropped.lock")
    try:
        result = T.maintain()
    finally:
        os.close(fd)
    assert any("2 records were lost" in n for n in result["notes"])


def test_every_kind_stores_no_value_with_a_space_whatever_it_is_given(home):
    hostile = ["two words", "https://a.example/b", "a" * 500, "x@y.example then more", 3, 1.5, True, None, "plain"]
    for kind, spec in T.KINDS.items():
        for value in hostile:
            assert T.emit(kind, subject=str(value), **{name: value for name in spec}) in (True, False)
    stored = rows(home)
    assert len(stored) >= len(T.KINDS)
    for kind, trace, subject, component, fields in stored:
        assert " " not in subject + component
        for key, value in json.loads(fields).items():
            if isinstance(value, str):
                assert T._pointer_ok(value), (kind, key)


def test_the_registry_is_well_formed_and_read_only():
    assert set(T.KINDS) == {"source_fetch", "triage_verdict", "model_call", "task", "dispatch", "check", "effect",
                            "feedback", "guard", "job_run", "sign_in"}
    for kind, spec in T.KINDS.items():
        assert spec, kind
        for name, type_ in spec.items():
            assert type_ in (T.INT, T.NUM, T.BOOL, T.PTR), (kind, name)
            assert name not in ("kind", "trace", "subject", "component") and not name.startswith("_"), (kind, name)
            assert name not in ("result", "decision"), (kind, name)
        if "outcome" in spec:
            assert spec["outcome"] == T.PTR
    with pytest.raises(TypeError):
        T.KINDS["invented"] = {}
    with pytest.raises(TypeError):
        T.KINDS["task"]["title"] = T.PTR


def test_a_kind_added_at_run_time_is_refused(home):
    with pytest.raises(TypeError):
        T.KINDS["invented"] = {"x": T.INT}
    assert T.emit("invented", x=1) is False
    assert rows(home) == [] if (tdir(home) / "telemetry.db").exists() else True


def test_a_dict_with_a_kind_key_spread_into_emit_does_not_raise(home):
    assert T.emit("check", **{"kind": "task", "count": 1})
    assert json.loads(rows(home)[0][-1]) == {"_dropped": 1, "count": 1}
    assert T.emit() is False


# ---------------------------------------------------------------- strict mode


def test_strict_mode_raises_for_anything_the_store_would_leave_out(home, monkeypatch):
    monkeypatch.setenv("ASSISTANT_TELEMETRY_STRICT", "1")
    assert T.emit("check", name="fine", count=1) is True
    with pytest.raises(ValueError, match="unknown kind"):
        T.emit("chek")
    with pytest.raises(ValueError, match="bogus"):
        T.emit("check", bogus=1)
    with pytest.raises(ValueError, match="count"):
        T.emit("check", count="1")
    with pytest.raises(ValueError, match="subject"):
        T.emit("check", subject="two words")
    with pytest.raises(ValueError, match="trace"):
        T.emit("check", trace="nope")
    assert len(rows(home)) == 1
    config(home, {"telemetry": {"enabled": False}})
    assert T.emit("chek") is False  # off: nothing looked at


def test_strict_mode_is_off_unless_it_is_exactly_one(home, monkeypatch):
    monkeypatch.setenv("ASSISTANT_TELEMETRY_STRICT", "0")
    assert T.emit("check", bogus=1) is True


def test_the_suite_runs_in_strict_mode():
    assert os.environ.get("ASSISTANT_TELEMETRY_STRICT") == "1"


# ---------------------------------------------------------------- no secret on disk


def test_no_secret_reaches_the_database_its_log_or_the_spool(home):
    words = list(SECRET_WORDS.values())
    for word in words:
        T.emit("task", subject=word, component=word, state=word, mode=word, attempt=1)
    # Keep the write-ahead log from being folded away, and put records in the spool too.
    holder = sqlite3.connect(str(tdir(home) / "telemetry.db"), isolation_level=None)
    reader = sqlite3.connect(str(tdir(home) / "telemetry.db"))
    reader.execute("SELECT count(*) FROM events").fetchall()
    for word in words:
        T.emit("task", subject=word, state=word, attempt=1)
    holder.execute("BEGIN EXCLUSIVE")
    for word in words:
        T.emit("task", subject=word, state=word, attempt=1)
    holder.close()
    reader.close()
    found = [p for p in tdir(home).iterdir() if p.is_file()]
    assert any(p.name == "spool.jsonl" for p in found)
    for path in found:
        data = path.read_bytes()
        for word in words:
            assert word.encode() not in data and BODY.encode() not in data, path.name
    stored = rows(home)
    assert stored and all(json.loads(r[-1]).get("_dropped") for r in stored)


def test_a_secret_shaped_word_is_dropped_not_kept_in_a_redacted_form(home):
    assert T.emit("task", state=SECRET_WORDS["github"], attempt=1)
    assert json.loads(rows(home)[0][-1]) == {"_dropped": 1, "attempt": 1}


def _spool_line(**over):
    line = {"id": "0123456789abcdef", "ts": time.time() - 5, "kind": "task", "trace": "", "subject": "",
            "component": "x", "fields": {"attempt": 1}}
    line.update(over)
    return line


def test_the_spool_is_cleaned_again_when_it_is_read(home):
    secret = SECRET_WORDS["slack"]
    tdir(home).mkdir()
    line = _spool_line(subject=secret, fields={"state": secret, "attempt": 1, "title": "Quarterly plan",
                                               "_dropped": 2})
    (tdir(home) / "spool.jsonl").write_text(json.dumps(line) + "\n")
    assert T.maintain()["drained"] == 1
    [(_, _, subject, _, fields)] = rows(home, "SELECT kind, trace, subject, component, fields FROM events"
                                              " WHERE kind='task'")
    assert subject == "" and json.loads(fields) == {"_dropped": 5, "attempt": 1}


def test_a_time_in_the_future_on_a_spool_line_is_clamped_to_now(home):
    tdir(home).mkdir()
    (tdir(home) / "spool.jsonl").write_text(json.dumps(_spool_line(ts=time.time() + 10 ** 7)) + "\n")
    before = time.time()
    T.maintain()
    [(ts,)] = rows(home, "SELECT ts FROM events WHERE kind='task'")
    assert ts <= time.time() and ts >= before - 1


# ---------------------------------------------------------------- never raises

WRONG = [None, 5, 1.5, True, b"bytes", [], {}, object(), ("a",), float("nan"), "x" * 10000]


def _fault_folder_cannot_be_made(home):
    (home / ".assistant").rmdir()
    (home / ".assistant").write_text("a file where the folder should be")


def _fault_database_is_a_directory(home):
    (tdir(home) / "telemetry.db").mkdir(parents=True)


def _fault_database_is_corrupt(home):
    tdir(home).mkdir()
    (tdir(home) / "telemetry.db").write_bytes(b"not a database at all, " * 500)


def _fault_database_is_locked(home):
    T.emit("check", name="seed")
    holder = sqlite3.connect(str(tdir(home) / "telemetry.db"), isolation_level=None)
    holder.execute("BEGIN EXCLUSIVE")
    FAULT_HOLDERS.append(holder)


def _fault_disk_full(home):
    def full(*a, **k):
        raise sqlite3.OperationalError("database or disk is full")
    FAULT_PATCHES.append((sqlite3, "connect", full))


def _fault_spool_cannot_be_written(home):
    _fault_database_is_locked(home)

    def refuse(*a, **k):
        raise OSError(28, "No space left on device")
    FAULT_PATCHES.append((os, "write", refuse))


def _fault_database_is_someone_elses(home):
    tdir(home).mkdir()
    conn = sqlite3.connect(str(tdir(home) / "telemetry.db"))
    conn.execute("CREATE TABLE other (x)")
    conn.commit()
    conn.close()


def _fault_settings_not_json(home):
    config(home, "{this is not json")


def _fault_settings_wrong_shape(home):
    config(home, '{"telemetry": {"enabled": "yes", "keep_days": "ninety", "max_mb": -1}}')


def _fault_settings_a_list(home):
    config(home, "[1, 2]")


def _fault_telemetry_setting_not_an_object(home):
    config(home, '{"telemetry": 5}')


def _fault_settings_nested_too_deep(home):
    config(home, "[" * 100000 + "]" * 100000)


FAULT_HOLDERS: list = []
FAULT_PATCHES: list = []
FAULTS = [_fault_folder_cannot_be_made, _fault_database_is_a_directory, _fault_database_is_corrupt,
          _fault_database_is_locked, _fault_disk_full, _fault_spool_cannot_be_written,
          _fault_database_is_someone_elses, _fault_settings_not_json, _fault_settings_wrong_shape,
          _fault_settings_a_list, _fault_telemetry_setting_not_an_object, _fault_settings_nested_too_deep]


@pytest.fixture
def fault(home, monkeypatch):
    FAULT_HOLDERS.clear()
    FAULT_PATCHES.clear()
    yield lambda make: (make(home), [monkeypatch.setattr(o, n, v) for o, n, v in FAULT_PATCHES])
    for holder in FAULT_HOLDERS:
        holder.close()


@pytest.mark.parametrize("make", FAULTS, ids=lambda f: f.__name__[7:])
def test_nothing_raises_under_any_fault_and_work_goes_on(fault, make):
    fault(make)
    assert T.emit("check", trace=TRACE, subject="s", component="c", name="n", ok=True) in (True, False)
    assert isinstance(T.events(), list)
    assert isinstance(T.events(trace=TRACE, kind="check", subject="s", since=0, limit=5), list)
    result = T.maintain()
    assert isinstance(result, dict) and "ok" in result
    assert isinstance(T.upkeep(10 ** 12), (dict, type(None)))
    assert T.emit("check", count=1) in (True, False)  # still usable afterwards


@pytest.mark.parametrize("wrong", WRONG, ids=repr)
def test_nothing_raises_for_an_argument_of_the_wrong_type(home, wrong):
    assert T.emit(wrong) is False
    for kind, spec in T.KINDS.items():
        assert T.emit(kind, trace=wrong, subject=wrong, component=wrong, **{name: wrong for name in spec}) in (True, False)
    for arg in ("trace", "kind", "subject", "since", "limit", "until", "component"):
        assert isinstance(T.events(**{arg: wrong}), list)
    assert isinstance(T.events(newest_first=wrong), list)
    assert isinstance(T.maintain(wrong), dict)
    assert isinstance(T.upkeep(wrong), (dict, type(None)))


def test_a_value_that_cannot_be_serialised_is_left_out_and_the_rest_is_kept(home):
    loop = []
    loop.append(loop)
    assert T.emit("check", name=loop, ok=object(), count=float("nan"), ms=1)
    assert json.loads(rows(home)[0][-1]) == {"_dropped": 3, "ms": 1}


def test_emit_survives_a_bug_in_the_module_itself(home, monkeypatch):
    def explode(*a, **k):
        raise RuntimeError("bug")
    monkeypatch.setattr(T, "_build", explode)
    assert T.emit("check", name="x") is False
    monkeypatch.undo()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(T, "_settings", explode)
    assert T.emit("check", name="x") is False
    assert T.upkeep() is None


def test_events_gives_an_empty_list_when_the_database_is_unreadable(home):
    T.emit("check", name="x")
    (tdir(home) / "telemetry.db").write_bytes(b"junk" * 1000)
    assert T.events() == []


def test_maintain_reports_a_failure_by_name_and_records_it(home, monkeypatch):
    def explode(work):
        raise RuntimeError("bug")
    monkeypatch.setattr(T, "_run", explode)
    result = T.maintain()
    assert result["ok"] is False and result["error"] == "RuntimeError"
    assert result["notes"] == ["telemetry upkeep failed: RuntimeError"]
    monkeypatch.undo()
    monkeypatch.setenv("HOME", str(home))
    [line] = spool_lines(home)  # the failure's own record: the database was what failed
    assert json.loads(line)["fields"]["error"] == "RuntimeError" and json.loads(line)["fields"]["ok"] is False


# ---------------------------------------------------------------- the switch


@pytest.mark.parametrize("value", [False, "false", 0, "no", None, "true", 1, [], {}], ids=repr)
def test_with_recording_off_nothing_is_created(home, value):
    config(home, {"telemetry": {"enabled": value}})
    assert T.emit("check", name="x") is False
    assert T.emit("nope") is False
    assert T.maintain() == {"ok": False, "enabled": False, "drained": 0, "bad_lines": 0, "deleted_old": 0,
                            "deleted_size": 0, "skipped": {}, "notes": []}
    assert T.upkeep() is None
    assert T.events() == []
    assert not tdir(home).exists()
    assert sorted(p.name for p in (home / ".assistant").iterdir()) == ["config.json"]


def test_a_settings_file_nested_too_deep_still_keeps_recording(home):
    config(home, "[" * 100000)
    assert T.emit("check", name="still")
    assert len(rows(home)) == 1


def test_recording_is_on_without_the_setting_and_with_true(home):
    assert T.emit("check", name="a")
    config(home, {"telemetry": {"enabled": True}})
    assert T.emit("check", name="b")
    config(home, {"mode": "pro"})
    assert T.emit("check", name="c")
    assert len(rows(home)) == 3


@pytest.mark.parametrize("text", ["{broken", "[1]", '{"telemetry": 5}', '{"telemetry": null}'])
def test_an_unreadable_settings_file_keeps_recording_with_the_defaults(home, text):
    config(home, text)
    assert T.emit("check", name="still")
    assert len(rows(home)) == 1


# ---------------------------------------------------------------- permissions


def _everything_is_owner_only(home):
    assert mode(tdir(home)) == 0o700
    seen = [p for p in tdir(home).iterdir()]
    for path in seen:
        assert mode(path) == 0o600, path.name
    return {p.name for p in seen}


@pytest.fixture
def no_umask():
    """A umask of 0 lets every file through at the mode it asked for, so a file made without asking is open."""
    old = os.umask(0)
    yield
    os.umask(old)


def test_the_folder_and_every_file_are_owner_only(home, locked_db, no_umask):
    T.emit("check", name="spooled")  # database locked: goes to the spool
    T.emit("check", name="x" * 5000, count=1)
    names = _everything_is_owner_only(home)
    assert names >= {"telemetry.db", "spool.jsonl", "spool.lock", "telemetry.db-wal", "telemetry.db-shm"}


def test_files_a_failed_drain_puts_back_are_owner_only(home, no_umask, monkeypatch):
    _spool_of(home, 3)
    monkeypatch.setattr(T, "_spool_rows", lambda claimed: ([("short",)], 0))
    T.maintain()
    assert len(spool_lines(home)) == 3
    _everything_is_owner_only(home)


def test_the_spool_made_by_a_put_back_is_owner_only_even_when_it_was_removed_first(home, no_umask, monkeypatch):
    _spool_of(home, 3)
    (tdir(home) / "spool.jsonl").chmod(0o666)
    monkeypatch.setattr(T, "_spool_rows", lambda claimed: ([("short",)], 0))
    T.maintain()
    assert mode(tdir(home) / "spool.jsonl") == 0o600


def test_files_that_were_left_loose_are_tightened(home, no_umask):
    assert T.emit("check", name="first")
    reader = sqlite3.connect(str(tdir(home) / "telemetry.db"))
    reader.execute("SELECT count(*) FROM events").fetchall()  # keeps the log and its index file around
    assert T.emit("check", name="second")
    (tdir(home) / "spool.jsonl").write_text("")
    T._count_loss("spool_full")
    for path in tdir(home).iterdir():
        path.chmod(0o666)
    tdir(home).chmod(0o777)
    assert T.emit("check", name="third")
    T.maintain()
    names = _everything_is_owner_only(home)
    reader.close()
    assert names >= {"telemetry.db", "telemetry.db-wal", "telemetry.db-shm", "spool.lock", "dropped.json",
                     "dropped.lock"}


def test_a_renamed_corrupt_copy_is_owner_only(home, no_umask):
    tdir(home).mkdir(mode=0o755)
    (tdir(home) / "telemetry.db").write_bytes(b"junk" * 1000)
    os.chmod(tdir(home) / "telemetry.db", 0o644)
    assert T.emit("check", name="x")
    [copy] = copies(home)
    assert mode(copy) == 0o600
    _everything_is_owner_only(home)


def test_a_missing_assistant_folder_is_made_owner_only(tmp_path, monkeypatch, no_umask):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert T.emit("check", name="x")
    assert mode(tmp_path / ".assistant") == 0o700


# ---------------------------------------------------------------- spool and drain


def test_a_locked_database_spools_and_maintain_drains_it(home, locked_db):
    assert T.emit("check", name="late", count=7) is False
    [line] = spool_lines(home)
    assert json.loads(line)["fields"] == {"count": 7, "name": "late"}
    locked_db.close()
    assert len(T.events(kind="check")) == 1  # the spool isn't read
    result = T.maintain()
    assert result["drained"] == 1 and result["ok"]
    assert [e["fields"].get("count") for e in T.events(kind="check")] == [None, 7]
    assert spool_lines(home) == []
    assert not claimed(home)


def test_a_locked_database_costs_the_caller_well_under_a_second(home, locked_db):
    started = time.monotonic()
    T.emit("check", name="x")
    assert time.monotonic() - started < 0.6


def _hold(home, name):
    fd = os.open(T._touch(name), os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def test_the_whole_of_one_emit_has_one_short_deadline(home, locked_db):
    fd = _hold(home, "spool.lock")
    try:
        started = time.monotonic()
        assert T.emit("check", name="x") is False
        took = time.monotonic() - started
    finally:
        os.close(fd)
    assert took < 0.2 + 0.1 + 0.05, took  # the database wait, the rest of the deadline, the note of the loss


def test_a_record_is_dropped_and_counted_when_the_spool_lock_is_not_taken_in_time(home, locked_db):
    fd = _hold(home, "spool.lock")
    try:
        assert T.emit("check", name="x") is False
    finally:
        os.close(fd)
    assert spool_lines(home) == []  # never written without the lock
    assert counter(home)["spool_busy"] == 1


def _writer(home, count, marker):
    code = ("from assistant import telemetry as T\n"
            "for i in range(%d):\n    T.emit('check', component='w%d', count=i)\n" % (count, marker))
    env = {**os.environ, "HOME": str(home), "PYTHONPATH": str(REPO / "src")}
    env.pop("ASSISTANT_TELEMETRY_STRICT", None)
    return subprocess.Popen([sys.executable, "-c", code], env=env)


def test_four_processes_writing_at_once_lose_nothing(home):
    procs = [_writer(home, 200, n) for n in range(4)]
    assert [p.wait(timeout=120) for p in procs] == [0, 0, 0, 0]
    in_db = len(rows(home, "SELECT id FROM events WHERE kind='check'"))
    assert in_db + len(spool_lines(home)) == 800
    T.maintain()
    assert spool_lines(home) == []
    assert len(rows(home, "SELECT id FROM events WHERE kind='check'")) == 800
    for n in range(4):
        assert len(rows(home, "SELECT id FROM events WHERE component='w%d'" % n)) == 200


def _spool_of(home, n, start=0):
    tdir(home).mkdir(exist_ok=True)
    out = [json.dumps({"id": "%016x" % (start + i), "ts": time.time() - 10 + i / 1000, "kind": "check", "trace": "",
                       "subject": "", "component": "s", "fields": {"count": i}}) for i in range(n)]
    (tdir(home) / "spool.jsonl").write_text("\n".join(out) + "\n")


def test_two_drains_at_once_insert_each_line_once(home):
    _spool_of(home, 300)
    gate = threading.Barrier(2)
    results = []

    def drain():
        gate.wait()
        results.append(T.maintain())

    threads = [threading.Thread(target=drain) for _ in range(2)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(rows(home, "SELECT id FROM events WHERE component='s'")) == 300
    assert sum(r["drained"] for r in results) == 300


def test_a_drain_that_died_after_claiming_is_finished_by_the_next_without_doubling(home):
    _spool_of(home, 5)
    T.maintain()
    # The same lines come back as a claimed file a crashed drain left behind.
    _spool_of(home, 5)
    (tdir(home) / "spool.jsonl").rename(tdir(home) / "spool.draining.999.dead")
    _spool_of(home, 3, start=100)
    result = T.maintain()
    assert result["drained"] == 3
    assert len(rows(home, "SELECT id FROM events WHERE component='s'")) == 8
    assert not claimed(home)


def test_a_drain_that_fails_puts_the_lines_back(home, monkeypatch):
    _spool_of(home, 4)
    monkeypatch.setattr(T, "_spool_rows", lambda claimed: ([("short",)], 0))
    result = T.maintain()
    assert result["ok"] is False
    assert len(spool_lines(home)) == 4
    assert not claimed(home)
    assert rows(home, "SELECT id FROM events WHERE component='s'") == []
    monkeypatch.undo()
    monkeypatch.setenv("HOME", str(home))
    assert T.maintain()["drained"] == 4


def test_a_drain_that_fails_part_way_inserts_nothing(home, monkeypatch):
    _spool_of(home, 4)
    good = T._row({"id": "%016x" % 999, "ts": time.time(), "kind": "check", "trace": "", "subject": "",
                   "component": "s", "fields": {}})
    monkeypatch.setattr(T, "_spool_rows", lambda claimed: ([good, ("short",)], 0))
    assert T.maintain()["ok"] is False
    assert rows(home, "SELECT id FROM events WHERE component='s'") == []


def test_a_drain_that_cannot_take_the_spool_lock_leaves_what_it_claimed_for_the_next(home, monkeypatch):
    _spool_of(home, 4)
    real = T._locked
    state = {"n": 0}

    def busy_after_claim(name, wait=T.BUSY_SECONDS):
        state["n"] += 1
        if state["n"] == 2:  # the claim took the first; the put-back asks for the second
            return real(name, 0)
        return real(name, wait)
    monkeypatch.setattr(T, "_spool_rows", lambda claimed: ([("short",)], 0))
    fd = [None]

    class Hold:
        def __enter__(self):
            fd[0] = _hold(home, "spool.lock")
            return False

        def __exit__(self, *a):
            os.close(fd[0])

    calls = {"n": 0}

    def locked(name, wait=T.BUSY_SECONDS):
        calls["n"] += 1
        return real(name, wait) if calls["n"] != 2 else Hold()
    monkeypatch.setattr(T, "_locked", locked)
    assert T.maintain()["ok"] is False
    assert len(claimed(home)) == 1 and spool_lines(home) == []
    monkeypatch.undo()
    monkeypatch.setenv("HOME", str(home))
    assert T.maintain()["drained"] == 4 and not claimed(home)


def test_unusable_spool_lines_are_counted_as_lost_and_the_rest_are_inserted(home):
    _spool_of(home, 2)
    bad = ["not json", "[]", json.dumps(_spool_line(id="zz")),
           json.dumps(_spool_line(id="%016x" % 77, ts=float("nan"))),
           json.dumps(_spool_line(id="%016x" % 78, ts="soon")),
           json.dumps(_spool_line(id="%016x" % 79, kind="unknown"))]
    with open(tdir(home) / "spool.jsonl", "a") as handle:
        handle.write("\n".join(bad) + "\n")
    result = T.maintain()
    assert (result["drained"], result["bad_lines"]) == (2, 6)
    assert counter(home)["bad_line"] == 6
    assert any("6 records were lost" in note for note in result["notes"])


def test_the_spool_has_a_size_limit_past_which_records_are_dropped_and_counted(home, locked_db, monkeypatch):
    monkeypatch.setattr(T, "SPOOL_LIMIT_BYTES", 600)
    for i in range(10):
        T.emit("check", name="n%d" % i)
    kept = spool_lines(home)
    assert 0 < len(kept) < 10
    assert counter(home)["spool_full"] == 10 - len(kept)
    locked_db.close()
    monkeypatch.undo()
    monkeypatch.setenv("HOME", str(home))
    result = T.maintain()
    job = [json.loads(r[-1]) for r in rows(home) if r[0] == "job_run"][-1]
    assert job["lost"] == 10 - len(kept)
    assert any("were lost since the last upkeep" in note for note in result["notes"])
    assert not any("lost" in note for note in T.maintain()["notes"])  # said once


def test_a_record_is_dropped_silently_when_neither_database_nor_spool_can_be_written(home, locked_db, monkeypatch):
    monkeypatch.setattr(os, "write", lambda *a: (_ for _ in ()).throw(OSError(28, "full")))
    assert T.emit("check", name="x") is False
    assert spool_lines(home) == []


def test_a_busy_lock_during_a_claim_leaves_the_spool_for_the_next_hour(home):
    _spool_of(home, 3)
    fd = _hold(home, "spool.lock")
    try:
        assert T.maintain()["drained"] == 0  # couldn't claim
    finally:
        os.close(fd)
    assert T.maintain()["drained"] == 3


def test_a_busy_counter_lock_skips_the_count_without_failing(home, locked_db, monkeypatch):
    monkeypatch.setattr(T, "SPOOL_LIMIT_BYTES", 1)
    fd = _hold(home, "dropped.lock")
    try:
        assert T.emit("check", name="x") is False
    finally:
        os.close(fd)
    assert not (tdir(home) / "dropped.json").exists()


def test_a_damaged_counter_file_starts_again_from_zero(home, locked_db, monkeypatch):
    monkeypatch.setattr(T, "SPOOL_LIMIT_BYTES", 1)
    (tdir(home) / "dropped.json").write_text("garbage")
    T.emit("check", name="x")
    assert counter(home)["spool_full"] == 1
    (tdir(home) / "dropped.json").write_text('{"spool_full": "many", "bad_line": -2, "reported": 1.5}')
    assert T._read_counter() == {}
    (tdir(home) / "dropped.json").write_text("[1]")
    assert T._read_counter() == {}


# ---------------------------------------------------------------- corrupt, newer and foreign databases


def test_a_corrupt_database_is_moved_aside_a_new_one_started_and_the_record_lands(home):
    tdir(home).mkdir()
    (tdir(home) / "telemetry.db").write_bytes(b"this is not sqlite " * 400)
    assert T.emit("check", name="after", count=1)
    assert [json.loads(r[-1]) for r in rows(home)] == [{"count": 1, "name": "after"}]
    [copy] = copies(home)
    assert copy.read_bytes().startswith(b"this is not sqlite")


def test_only_the_two_newest_renamed_copies_are_kept(home, fresh_clock):
    tdir(home).mkdir()
    for n in range(4):
        (tdir(home) / "telemetry.db").write_bytes(b"junk %d " % n * 400)
        assert T.emit("check", count=n)
    assert [c.read_bytes()[:6] for c in copies(home)] == [b"junk 2", b"junk 3"]


def test_only_files_named_exactly_like_a_renamed_copy_are_ever_deleted(home, fresh_clock):
    tdir(home).mkdir()
    decoys = ["telemetry.db.corrupt-notes", "telemetry.db.corrupt-1", "telemetry.db.corrupt-000000000000001.bak",
              "xtelemetry.db.corrupt-000000000000001", "telemetry.db.corrupt-"]
    for name in decoys:
        (tdir(home) / name).write_text("mine")
    for n in range(4):
        (tdir(home) / "telemetry.db").write_bytes(b"junk %d " % n * 400)
        assert T.emit("check", count=n)
    assert all((tdir(home) / name).read_text() == "mine" for name in decoys)
    assert len([c for c in copies(home) if re.fullmatch(r"telemetry\.db\.corrupt-[0-9]{15}", c.name)]) == 2


def _damage_the_middle(path: Path):
    size = path.stat().st_size
    with open(path, "r+b") as handle:
        handle.seek(4096 * 2)
        handle.write(b"\xff" * (size - 4096 * 2))


def _big_store(home, n=3000):
    assert T.emit("check")
    conn = sqlite3.connect(str(tdir(home) / "telemetry.db"), isolation_level=None)
    conn.execute("BEGIN")
    for i in range(n):
        conn.execute(T._INSERT, ("%016x" % (i + 1), time.time() - 100 + i / 100, "check", "", "", "p", '{"count":%d}' % i))
    conn.execute("COMMIT")
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()


def test_damage_in_the_middle_of_the_database_is_healed_by_the_next_write(home):
    _big_store(home)
    _damage_the_middle(tdir(home) / "telemetry.db")
    assert T.emit("check", name="after") is True
    assert len(copies(home)) == 1
    assert [json.loads(r[-1])["name"] for r in rows(home)] == ["after"]


def test_damage_in_the_middle_of_the_database_is_healed_by_upkeep_too(home):
    _big_store(home)
    _damage_the_middle(tdir(home) / "telemetry.db")
    _spool_of(home, 3)
    result = T.maintain()
    assert result["ok"] is True and result["drained"] == 3
    assert len(copies(home)) == 1
    assert len(rows(home, "SELECT id FROM events WHERE component='s'")) == 3


def test_a_database_that_stays_corrupt_after_the_move_spools_instead_of_looping(home, monkeypatch):
    tdir(home).mkdir()
    (tdir(home) / "telemetry.db").write_bytes(b"junk" * 1000)
    monkeypatch.setattr(T, "_quarantine", lambda seen: None)
    assert T.emit("check", name="x") is False
    assert len(spool_lines(home)) == 1


def test_a_second_process_that_saw_the_same_corrupt_file_leaves_the_new_one_alone(home):
    assert T.emit("check", name="fresh")
    T._quarantine(seen_inode=123456789)  # the file it saw is already gone
    assert [json.loads(r[-1])["name"] for r in rows(home)] == ["fresh"]
    assert not copies(home)


def test_the_log_of_a_corrupt_database_is_not_carried_over_to_the_new_one(home):
    assert T.emit("check", name="old")
    holder = sqlite3.connect(str(tdir(home) / "telemetry.db"), isolation_level=None)
    holder.execute("SELECT count(*) FROM events").fetchall()
    for i in range(30):
        assert T.emit("check", name="old", count=i)
    log = (tdir(home) / "telemetry.db-wal").read_bytes()
    assert len(log) > 4096
    holder.close()
    (tdir(home) / "telemetry.db").write_bytes(b"junk" * 2000)
    (tdir(home) / "telemetry.db-wal").write_bytes(log)  # still a valid log of the old database
    assert T.emit("check", name="new") is True
    assert [json.loads(r[-1])["name"] for r in rows(home)] == ["new"]
    assert [e["fields"]["name"] for e in T.events()] == ["new"]


def test_moving_a_corrupt_database_aside_takes_its_log_with_it(home):
    tdir(home).mkdir()
    db = tdir(home) / "telemetry.db"
    for suffix in ("", "-wal", "-shm"):
        (tdir(home) / ("telemetry.db" + suffix)).write_bytes(b"x" * 100)
    T._quarantine(os.stat(db).st_ino)
    assert sorted(p.name for p in tdir(home).iterdir() if "corrupt" not in p.name) == ["recover.lock"]
    assert len(copies(home)) == 1


def test_a_database_that_is_a_directory_is_not_renamed_and_the_record_spools(home):
    (tdir(home) / "telemetry.db").mkdir(parents=True)
    assert T.emit("check", name="x") is False
    assert (tdir(home) / "telemetry.db").is_dir() and len(spool_lines(home)) == 1


def test_a_locked_database_is_not_taken_for_corrupt(home, locked_db):
    assert T.emit("check", name="x") is False
    assert not copies(home)


def _someone_elses_database(home, application_id=0, user_version=0):
    tdir(home).mkdir(exist_ok=True)
    conn = sqlite3.connect(str(tdir(home) / "telemetry.db"))
    conn.execute("CREATE TABLE other (x)")
    conn.execute("PRAGMA application_id=%d" % application_id)
    conn.execute("PRAGMA user_version=%d" % user_version)
    conn.commit()
    conn.close()
    return (tdir(home) / "telemetry.db").read_bytes()


def test_a_database_from_a_newer_version_is_left_untouched_and_records_spool(home):
    before = _someone_elses_database(home, T.APPLICATION_ID, T.SCHEMA_VERSION + 1)
    assert T.emit("check", name="x") is False
    assert T.events() == []
    result = T.maintain()
    assert result["ok"] is False and result["error"] == "NewerSchema"
    assert result["notes"] == ["telemetry.db was written by a newer Assistant, so this one left it alone"]
    assert (tdir(home) / "telemetry.db").read_bytes() == before
    assert not copies(home) and not (tdir(home) / "telemetry.db-wal").exists()
    assert len(spool_lines(home)) >= 2  # the record, and maintain's own record


def test_a_newer_version_still_in_the_log_is_left_alone_too(home):
    assert T.emit("check", name="mine")
    holder = sqlite3.connect(str(tdir(home) / "telemetry.db"), isolation_level=None)
    holder.execute("PRAGMA user_version=%d" % (T.SCHEMA_VERSION + 1))  # in the log; the file's header is stale
    assert int.from_bytes((tdir(home) / "telemetry.db").read_bytes()[60:64], "big") == T.SCHEMA_VERSION
    assert T.emit("check", name="x") is False
    holder.close()
    assert len(spool_lines(home)) == 1


def test_a_database_that_is_someone_elses_is_never_given_tables_or_records(home):
    before = _someone_elses_database(home)
    assert T.emit("check", name="x") is False
    assert T.events() == []
    result = T.maintain()
    assert result["ok"] is False and result["error"] == "ForeignDatabase"
    assert result["notes"] == ["telemetry.db belongs to another program, so Assistant left it alone"]
    assert (tdir(home) / "telemetry.db").read_bytes() == before
    assert not copies(home)
    assert len(spool_lines(home)) >= 1


def test_the_header_is_read_before_sqlite_opens_the_file(home, monkeypatch):
    _someone_elses_database(home, T.APPLICATION_ID, T.SCHEMA_VERSION + 1)
    monkeypatch.setattr(sqlite3, "connect", lambda *a, **k: pytest.fail("opened a database it must not touch"))
    assert T.emit("check", name="x") is False


def test_the_schema_is_recorded_and_indexed(home):
    T.emit("check")
    assert rows(home, "PRAGMA user_version") == [(T.SCHEMA_VERSION,)]
    assert rows(home, "PRAGMA application_id") == [(T.APPLICATION_ID,)]
    assert rows(home, "PRAGMA journal_mode") == [("wal",)]
    assert rows(home, "PRAGMA auto_vacuum") == [(2,)]
    header = (tdir(home) / "telemetry.db").read_bytes()[:100]
    assert int.from_bytes(header[60:64], "big") == T.SCHEMA_VERSION  # in the file, not only in the log
    assert int.from_bytes(header[68:72], "big") == T.APPLICATION_ID
    names = {r[0] for r in rows(home, "SELECT name FROM sqlite_master WHERE type='index'")}
    assert {"events_trace", "events_kind_ts", "events_ts"} <= names
    assert rows(home, "SELECT name, pk FROM pragma_table_info('events') WHERE pk=1") == [("seq", 1)]


# ---------------------------------------------------------------- settings and upkeep


def _event(conn, i, ts):
    conn.execute(T._INSERT, ("%016x" % i, ts, "check", "", "", "t", '{"count":%d}' % i))


def _fill(home, stamps):
    T.emit("check")
    conn = sqlite3.connect(str(tdir(home) / "telemetry.db"), isolation_level=None)
    for i, ts in enumerate(stamps):
        _event(conn, i + 1, ts)
    conn.close()


NOW = 1_000_000_000.0


def test_old_records_are_deleted_and_new_ones_kept(home):
    _fill(home, [NOW - 91 * 86400, NOW - 89 * 86400, NOW - 1])
    config(home, {"telemetry": {"keep_days": 90}})
    result = T.maintain(NOW)  # the clock is later than this: nothing here is clamped
    assert result["deleted_old"] == 1 and result["ok"]


def test_the_default_keeps_ninety_days(home):
    _fill(home, [NOW - 91 * 86400, NOW - 89 * 86400])
    assert T.maintain(NOW)["deleted_old"] == 1


def test_a_time_in_the_future_never_deletes_what_is_recent(home):
    _fill(home, [time.time() - 3600, time.time() - 60])
    config(home, {"telemetry": {"keep_days": 1}})
    assert T.maintain(time.time() + 10 ** 9)["deleted_old"] == 0
    assert T.maintain(float("inf"))["deleted_old"] == 0
    assert T.maintain("later")["deleted_old"] == 0
    assert len(rows(home, "SELECT id FROM events WHERE component='t'")) == 2


def _bulk(home, n, ts, pad=150):
    T.emit("check")
    conn = sqlite3.connect(str(tdir(home) / "telemetry.db"), isolation_level=None)
    conn.execute("BEGIN")
    for i in range(n):
        conn.execute(T._INSERT, (os.urandom(8).hex(), ts(i), "check", "", "", "p", '{"x":"%s"}' % ("y" * pad)))
    conn.execute("COMMIT")
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    return tdir(home) / "telemetry.db"


def test_the_size_limit_removes_the_oldest_first_and_the_file_gets_smaller(home):
    start = time.time() - 12000
    db = _bulk(home, 12000, lambda i: start + i)
    before = db.stat().st_size
    assert before > 2 * 1024 * 1024
    config(home, {"telemetry": {"max_mb": 1}})
    result = T.maintain()
    assert result["deleted_size"] > 0
    assert 0.9 * 1024 <= result["db_kb"] <= 1024  # trimmed to the limit and not much past it
    assert db.stat().st_size <= 1.05 * 1024 * 1024 < before  # the file itself shrank
    survivors = [r[0] for r in rows(home, "SELECT ts FROM events WHERE component='p' ORDER BY ts")]
    assert survivors[-1] == start + 11999
    assert survivors == [start + i for i in range(12000 - len(survivors), 12000)]  # a tail: the oldest went


def test_the_file_gets_smaller_after_an_age_trim(home):
    db = _bulk(home, 12000, lambda i: 1000.0 + i)  # long ago
    before = db.stat().st_size
    result = T.maintain()
    assert result["deleted_old"] == 12000
    assert db.stat().st_size < before / 4


def test_a_limit_the_database_cannot_meet_stops_when_it_is_empty(home):
    _fill(home, [time.time() - 5, time.time() - 4])
    config(home, {"telemetry": {"max_mb": 1}})
    T.emit("check")
    original = T.MAX_MB_RANGE
    T.MAX_MB_RANGE = (0.000001, 100000)
    try:
        config(home, {"telemetry": {"max_mb": 0.000001}})
        result = T.maintain()
    finally:
        T.MAX_MB_RANGE = original
    assert result["ok"] and result["deleted_size"] >= 2


@pytest.mark.parametrize("block", [
    {"keep_days": "ninety"}, {"keep_days": 0}, {"keep_days": 0.5}, {"keep_days": -5}, {"keep_days": True},
    {"keep_days": 3651}, {"keep_days": float("inf")}, {"keep_days": None},
])
def test_an_unreadable_age_setting_deletes_nothing_by_age_and_says_why(home, block):
    _fill(home, [1000.0])
    config(home, {"telemetry": block})
    result = T.maintain(NOW)
    assert result["deleted_old"] == 0 and "keep_days" in result["skipped"]["age"]
    assert "size" not in result["skipped"]
    assert any("keep_days" in note for note in result["notes"])
    assert len(rows(home, "SELECT id FROM events WHERE component='t'")) == 1
    job = [json.loads(r[-1]) for r in rows(home) if r[0] == "job_run"][-1]
    assert job["partial"] is True


@pytest.mark.parametrize("block", [{"max_mb": "big"}, {"max_mb": 0}, {"max_mb": 0.001}, {"max_mb": None},
                                   {"max_mb": 100001}, {"max_mb": False}])
def test_an_unreadable_size_setting_deletes_nothing_by_size_and_says_why(home, block):
    _fill(home, [1000.0, 2000.0])
    config(home, {"telemetry": {"keep_days": 3650, **block}})
    result = T.maintain(NOW)
    assert result["deleted_size"] == 0 and "max_mb" in result["skipped"]["size"]
    assert "age" not in result["skipped"]
    assert len(rows(home, "SELECT id FROM events WHERE component='t'")) == 2 - result["deleted_old"]


def test_the_edges_of_the_ranges_are_accepted(home):
    config(home, {"telemetry": {"keep_days": 1, "max_mb": 1}})
    assert T._settings()["problems"] == {}
    config(home, {"telemetry": {"keep_days": 3650, "max_mb": 100000}})
    assert T._settings()["problems"] == {}
    config(home, {"telemetry": {"keep_days": 1.5, "max_mb": 1.5}})
    assert T._settings()["problems"] == {}


@pytest.mark.parametrize("text", ["{not json", "[1]", '{"telemetry": 5}', "[" * 100000])
def test_an_unreadable_settings_file_deletes_nothing_and_recording_continues(home, text):
    _fill(home, [1000.0])
    config(home, text)
    assert T.emit("check", name="still")
    result = T.maintain(NOW)
    assert result["deleted_old"] == 0 and result["deleted_size"] == 0
    assert set(result["skipped"]) == {"age", "size"}
    assert "can't be read" in result["skipped"]["age"]
    assert len(rows(home, "SELECT id FROM events WHERE component='t'")) == 1


def test_maintain_writes_one_record_about_itself(home):
    _fill(home, [1.0])
    result = T.maintain()
    [job] = [e for e in T.events(kind="job_run")]
    f = job["fields"]
    assert f["job"] == "telemetry-maintain" and f["ok"] is True and f["deleted"] == result["deleted_old"] == 1
    assert f["ms"] >= 0 and f["drained"] == 0 and f["partial"] is False and f["lost"] == 0


def test_the_log_is_checkpointed(home):
    T.emit("check")
    reader = sqlite3.connect(str(tdir(home) / "telemetry.db"))
    reader.execute("SELECT count(*) FROM events").fetchall()
    for i in range(20):
        T.emit("check", count=i)
    reader.close()
    T.maintain()
    wal = tdir(home) / "telemetry.db-wal"
    assert not wal.exists() or wal.stat().st_size == 0


# ---------------------------------------------------------------- the hourly upkeep


def test_upkeep_runs_at_most_once_an_hour_and_keeps_its_stamp_in_the_folder(home, monkeypatch):
    calls = []
    monkeypatch.setattr(T, "maintain", lambda now: calls.append(now) or {"notes": []})
    assert T.upkeep(10_000.0) == {"notes": []}
    assert T.upkeep(10_000.0 + 3599) is None
    assert calls == [10_000.0] or len(calls) == 1
    first = calls[0]
    assert (tdir(home) / "upkeep-at").exists() and mode(tdir(home) / "upkeep-at") == 0o600
    assert T.upkeep(first + 3600) is not None
    assert len(calls) == 2


def test_upkeep_reads_the_clock_when_given_none(home, monkeypatch):
    calls = []
    monkeypatch.setattr(T, "maintain", lambda now: calls.append(now) or {"notes": []})
    (tdir(home)).mkdir()
    (tdir(home) / "upkeep-at").write_text("garbage")
    T.upkeep()
    assert len(calls) == 1 and abs(calls[0] - time.time()) < 60


def test_upkeep_runs_anyway_when_it_cannot_write_its_stamp(home, monkeypatch):
    calls = []
    monkeypatch.setattr(T, "maintain", lambda now: calls.append(now) or {"notes": []})
    (tdir(home) / "upkeep-at").mkdir(parents=True)  # can't be written as a file
    assert T.upkeep() is not None and T.upkeep() is not None
    assert len(calls) == 2


def test_upkeep_goes_on_after_the_clock_goes_back(home, monkeypatch):
    calls = []
    monkeypatch.setattr(T, "maintain", lambda now: calls.append(now) or {"notes": []})
    (tdir(home)).mkdir()
    (tdir(home) / "upkeep-at").write_text(str(time.time() + 10 ** 6))
    T.upkeep()
    assert len(calls) == 1


# ---------------------------------------------------------------- the pulse's call


def _pulse_module(home):
    spec = importlib.util.spec_from_file_location("pulse_telemetry", str(REPO / "bin/pulse.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.self_update_pulse = lambda pulse_idx: None
    return mod


def test_the_pulse_logs_one_plain_line_for_a_problem_and_nothing_otherwise(home, monkeypatch, caplog):
    pulse = _pulse_module(home)
    monkeypatch.setattr(T, "upkeep", lambda now=None: {"notes": []})
    with caplog.at_level(logging.WARNING):
        pulse.run_telemetry_upkeep(5.0)
    assert caplog.records == []
    monkeypatch.setattr(T, "upkeep", lambda now=None: None)
    pulse.run_telemetry_upkeep()
    assert caplog.records == []
    monkeypatch.setattr(T, "upkeep", lambda now=None: {"notes": ["telemetry: keep_days isn't a number from 1 to 3650",
                                                                  "telemetry: 3 records were lost since the last upkeep"]})
    with caplog.at_level(logging.WARNING):
        pulse.run_telemetry_upkeep()
    [record] = caplog.records
    assert record.getMessage() == ("telemetry: keep_days isn't a number from 1 to 3650; "
                                   "telemetry: 3 records were lost since the last upkeep")


def test_the_pulse_survives_the_telemetry_module_failing(home, monkeypatch, caplog):
    pulse = _pulse_module(home)

    def explode(now=None):
        raise RuntimeError("boom")
    monkeypatch.setattr(T, "upkeep", explode)
    with caplog.at_level(logging.WARNING):
        pulse.run_telemetry_upkeep()
    assert "telemetry upkeep failed (ignored): boom" in caplog.text


@pytest.mark.parametrize("source", [None, "def broken(:\n", "raise RuntimeError('at import')\n",
                                    "import no_such_module_anywhere\n"], ids=["missing", "syntax", "raises", "import"])
def test_a_missing_or_broken_telemetry_module_skips_the_upkeep_and_nothing_else(home, monkeypatch, tmp_path, caplog,
                                                                                source):
    import assistant
    package = tmp_path / "fake-assistant"
    package.mkdir()
    if source is not None:
        (package / "telemetry.py").write_text(source)
    monkeypatch.setattr(assistant, "__path__", [str(package)])
    monkeypatch.delitem(sys.modules, "assistant.telemetry")
    monkeypatch.delattr(assistant, "telemetry")
    pulse = _pulse_module(home)
    with caplog.at_level(logging.WARNING):
        assert pulse.run_telemetry_upkeep() is None
    assert "telemetry upkeep failed (ignored)" in caplog.text


def test_the_pulse_does_not_load_telemetry_when_it_starts_and_the_start_up_check_does_not_list_it(home):
    code = ("import sys; sys.path[:0] = [%r, %r]\n"
            "import importlib.util\n"
            "spec = importlib.util.spec_from_file_location('p', %r)\n"
            "mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)\n"
            "print('assistant.telemetry' in sys.modules)\n" % (str(REPO / "src"), str(REPO / "bin"),
                                                              str(REPO / "bin/pulse.py")))
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True,
                         env={**os.environ, "HOME": str(home)})
    assert out.stdout.strip().endswith("False")
    wrapper = (REPO / "bin/run-pulse.py").read_text()
    assert "telemetry" not in wrapper and "pointers" not in wrapper


def _run_main(home, argv):
    (home / ".assistant/inbox").mkdir(parents=True, exist_ok=True)
    (home / ".assistant/observer-summaries").mkdir(parents=True, exist_ok=True)
    (home / ".claude/cache").mkdir(parents=True, exist_ok=True)
    config(home, {"observer": {"enabled": True}})
    pulse = _pulse_module(home)
    ctx = {"ws_ref": "workspace:1", "transcript_path": None, "agent_status": "idle", "last_turn_age_sec": 0,
           "is_protected": False, "title": "t", "cwd": "/"}
    calls = []
    with mock.patch.object(pulse, "run_telemetry_upkeep", lambda *a: calls.append(a)), \
            mock.patch.object(pulse, "scan_busy_ws", return_value=set()), \
            mock.patch.object(pulse, "pick_ws_batch", return_value={
                "to_reclassify": [{"ref": "workspace:1", "title": "t", "cwd": "/"}], "reuse_cached": [],
                "backed_off": [], "total_ws": 1}), \
            mock.patch.object(pulse, "purge_stale_awaiting"), \
            mock.patch.object(pulse, "build_ctx", return_value=ctx), \
            mock.patch.object(pulse, "call_observer_batch", return_value=({"workspace:1": {
                "ws_ref": "workspace:1", "verdict": "active", "summary": "s", "next": "n"}}, {})), \
            mock.patch.object(pulse, "save_summary"), \
            mock.patch.object(pulse, "pick_open_todos", return_value={"bucket_b": []}), \
            mock.patch.object(pulse, "run", return_value=(0, "", "")), \
            mock.patch("sys.stdout"):
        sys.argv = ["pulse.py"] + argv
        assert pulse.main() == 0
    return calls


def test_a_real_pulse_runs_the_upkeep_step_once_and_a_dry_run_does_not(home):
    assert len(_run_main(home, [])) == 1
    assert _run_main(home, ["--dry-run"]) == []


# ---------------------------------------------------------------- local only, and the oldest Python


def _imports(path: Path):
    tree = ast.parse(path.read_text())
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            found.add((node.module or "").split(".")[0] if node.level == 0 else "<relative>")
    return found


def test_the_module_imports_nothing_outside_its_allow_list():
    allowed = {"__future__", "contextlib", "fcntl", "json", "math", "os", "re", "sqlite3", "time", "types",
               "assistant"}
    assert _imports(MODULE) <= allowed
    for name in ("redact.py", "pointers.py"):
        assert _imports(REPO / "src/assistant" / name) <= allowed | {"hashlib", "logging", "secrets", "sys",
                                                                      "threading", "traceback"}


def test_importing_the_module_loads_no_network_library():
    # Compared with what was loaded before: a coverage run starts its subprocesses with its own imports.
    code = ("import sys; sys.path.insert(0, %r)\n"
            "names = ('socket', 'ssl', 'http.client', 'urllib.request', 'ftplib', 'smtplib', 'xmlrpc', 'asyncio')\n"
            "before = {m for m in names if m in sys.modules}\n"
            "from assistant import telemetry\n"
            "print(sorted(m for m in names if m in sys.modules and m not in before))\n" % str(REPO / "src"))
    out = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "[]"


@pytest.mark.parametrize("name", ["telemetry.py", "redact.py", "pointers.py"])
def test_the_module_and_what_it_imports_parse_as_python_3_9(name):
    ast.parse((REPO / "src/assistant" / name).read_text(), feature_version=(3, 9))


@pytest.mark.skipif(not Path("/usr/bin/python3").exists(), reason="no system Python here")
def test_the_system_python_imports_the_module_and_emits_one_record(tmp_path):
    code = ("import sys; sys.path.insert(0, %r)\n"
            "from assistant import telemetry as T\n"
            "assert T.emit('check', name='x', count=1) is True\n"
            "assert T.emit('check', **{'kind': 'x'}) is True\n"
            "print(len(T.events()))\n" % str(REPO / "src"))
    out = subprocess.run(["/usr/bin/python3", "-c", code], capture_output=True, text=True,
                         env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"})
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "2"
    assert (tmp_path / ".assistant/telemetry/telemetry.db").exists()


def test_nothing_is_written_to_the_actions_ledger(home):
    T.emit("check", name="x")
    T.maintain()
    assert not (home / ".assistant" / "actions-ledger.jsonl").exists()
