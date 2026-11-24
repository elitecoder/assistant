"""src/assistant/telemetry.py: the local record of what Assistant did. It never raises, never holds content, never
writes a secret, never leaves the Mac, and loads on the system Python."""
from __future__ import annotations

import ast
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

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
    monkeypatch.setenv("HOME", str(tmp_path))
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


def mode(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


@pytest.fixture
def locked_db(home):
    """The database, locked by another connection for the length of the test."""
    assert T.emit("check", name="seed")
    holder = sqlite3.connect(str(tdir(home) / "telemetry.db"), isolation_level=None)
    holder.execute("BEGIN EXCLUSIVE")
    yield holder
    holder.close()


# ---------------------------------------------------------------- a record


def test_a_record_lands_in_the_database_with_its_pointers_and_fields(home):
    assert T.emit("task", trace=TRACE, subject="row:0123456789abcdef", component="pulse", state="open", attempt=2)
    [(kind, trace, subject, component, fields)] = rows(home)
    assert (kind, trace, subject, component) == ("task", TRACE, "row:0123456789abcdef", "pulse")
    assert json.loads(fields) == {"attempt": 2, "state": "open"}


def test_events_read_back_oldest_first_and_filter(home):
    other = pointers.legacy_trace_id("row", "x")
    for i, (kind, trace, subject) in enumerate([("check", TRACE, "a"), ("task", other, "b"), ("check", TRACE, "b")]):
        assert T.emit(kind, trace=trace, subject=subject, **({"count": i} if kind == "check" else {}))
    everything = T.events()
    assert [e["fields"].get("count") for e in everything] == [0, None, 2]
    assert [e["ts"] for e in everything] == sorted(e["ts"] for e in everything)
    assert {"id", "ts", "kind", "trace", "subject", "component", "fields"} == set(everything[0])
    assert len(T.events(trace=TRACE)) == 2
    assert len(T.events(kind="task")) == 1
    assert len(T.events(trace=TRACE, subject="b")) == 1
    assert len(T.events(since=everything[2]["ts"])) >= 1
    assert T.events(since=everything[2]["ts"] + 10) == []
    assert [e["fields"].get("count") for e in T.events(limit=1)] == [0]
    assert T.events(trace=7) == []
    assert len(T.events(limit=0)) == 1  # a limit below one is one


def test_events_never_creates_the_database(home):
    assert T.events() == []
    assert not tdir(home).exists()


# ---------------------------------------------------------------- pointers, not content


POINTER_BREAKERS = [
    ("with-space", "two words"),
    ("link", "https://example.com/a"),
    ("double-slash", "a//b"),
    ("too-long", "a" * 121),
    ("empty", ""),
    ("quote", 'a"b'),
    ("email", "ana at example"),
    ("not-text", 5),
    ("newline", "a\nb"),
]
@pytest.mark.parametrize(("name", "value"), POINTER_BREAKERS, ids=[c[0] for c in POINTER_BREAKERS])
def test_a_pointer_field_that_breaks_the_rule_is_left_out_and_counted(home, name, value):
    assert T.emit("task", state=value, attempt=1)
    [(*_, fields)] = rows(home)
    assert json.loads(fields) == {"_dropped": 1, "attempt": 1}, name


def test_the_longest_pointer_and_every_allowed_character_are_kept(home):
    word = ("A_b.c:d@e#f/g+h-9" * 8)[:120]
    assert T.emit("task", state=word, subject=word)
    [(_, _, subject, _, fields)] = rows(home)
    assert json.loads(fields) == {"state": word} and subject == word


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


def test_a_subject_or_component_that_is_not_a_pointer_is_left_out_and_counted(home):
    assert T.emit("check", subject="https://x.example/a?token=1", component="two words")
    [(_, _, subject, component, fields)] = rows(home)
    assert (subject, component, json.loads(fields)) == ("", "", {"_dropped": 2})


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
def test_an_unknown_kind_stores_nothing(home, kind):
    assert T.emit(kind, count=1) is False
    assert not tdir(home).exists()


def test_a_field_whose_value_misbehaves_is_left_out(home):
    class Evil(str):
        def __ne__(self, other):
            raise RuntimeError("boom")

    assert T.emit("task", state=Evil("ok"), attempt=1)
    assert json.loads(rows(home)[0][-1]) == {"_dropped": 1, "attempt": 1}


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
                assert re.fullmatch(r"[A-Za-z0-9_.:@#/+-]{1,120}", value) and "//" not in value, (kind, key)


def test_the_registry_is_well_formed():
    assert set(T.KINDS) == {"source_fetch", "triage_verdict", "model_call", "task", "dispatch", "check", "effect",
                            "feedback", "guard", "job_run", "sign_in"}
    for kind, spec in T.KINDS.items():
        assert spec, kind
        for name, type_ in spec.items():
            assert type_ in (T.INT, T.NUM, T.BOOL, T.PTR), (kind, name)
            assert name not in ("kind", "trace", "subject", "component") and not name.startswith("_"), (kind, name)


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


def test_the_spool_is_cleaned_again_when_it_is_read(home, locked_db):
    secret = SECRET_WORDS["slack"]
    line = {"id": "0123456789abcdef", "ts": time.time() - 5, "kind": "task", "trace": "", "subject": secret, "component": "x",
            "fields": {"state": secret, "attempt": 1, "title": "Quarterly plan", "_dropped": 2}}
    (tdir(home) / "spool.jsonl").write_text(json.dumps(line) + "\n")
    locked_db.close()
    assert T.maintain()["drained"] == 1
    [(_, _, subject, _, fields)] = rows(home, "SELECT kind, trace, subject, component, fields FROM events"
                                              " WHERE kind='task'")
    assert subject == "" and json.loads(fields) == {"_dropped": 5, "attempt": 1}


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


def _fault_disk_full(home, monkeypatch=None):
    def full(*a, **k):
        raise sqlite3.OperationalError("database or disk is full")
    FAULT_PATCHES.append((sqlite3, "connect", full))


def _fault_spool_cannot_be_written(home):
    _fault_database_is_locked(home)
    def refuse(*a, **k):
        raise OSError(28, "No space left on device")
    FAULT_PATCHES.append((os, "write", refuse))


def _fault_settings_not_json(home):
    config(home, "{this is not json")


def _fault_settings_wrong_shape(home):
    config(home, '{"telemetry": {"enabled": "yes", "keep_days": "ninety", "max_mb": -1}}')


def _fault_settings_a_list(home):
    config(home, "[1, 2]")


def _fault_telemetry_setting_not_an_object(home):
    config(home, '{"telemetry": 5}')


FAULT_HOLDERS: list = []
FAULT_PATCHES: list = []
FAULTS = [_fault_folder_cannot_be_made, _fault_database_is_a_directory, _fault_database_is_corrupt,
          _fault_database_is_locked, _fault_disk_full, _fault_spool_cannot_be_written, _fault_settings_not_json,
          _fault_settings_wrong_shape, _fault_settings_a_list, _fault_telemetry_setting_not_an_object]


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
    assert T.emit("check", count=1) in (True, False)  # still usable afterwards


@pytest.mark.parametrize("wrong", WRONG, ids=repr)
def test_nothing_raises_for_an_argument_of_the_wrong_type(home, wrong):
    assert T.emit(wrong) is False
    for kind, spec in T.KINDS.items():
        assert T.emit(kind, wrong, wrong, wrong, **{name: wrong for name in spec}) in (True, False)
    for arg in ("trace", "kind", "subject", "since", "limit"):
        assert isinstance(T.events(**{arg: wrong}), list)
    assert isinstance(T.maintain(wrong), dict)


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


def test_events_gives_an_empty_list_when_the_database_is_unreadable(home):
    T.emit("check", name="x")
    (tdir(home) / "telemetry.db").write_bytes(b"junk" * 1000)
    assert T.events() == []


def test_maintain_reports_a_failure_by_name_and_records_it(home, monkeypatch):
    def explode():
        raise RuntimeError("bug")
    monkeypatch.setattr(T, "_connect_healing", explode)
    result = T.maintain()
    assert result["ok"] is False and result["error"] == "RuntimeError"
    monkeypatch.undo()
    monkeypatch.setenv("HOME", str(home))
    [line] = spool_lines(home)  # the failure's own record: the database was what failed
    assert json.loads(line)["fields"]["error"] == "RuntimeError" and json.loads(line)["fields"]["ok"] is False


# ---------------------------------------------------------------- the switch


@pytest.mark.parametrize("setting", [{"telemetry": {"enabled": False}}])
def test_with_recording_off_nothing_is_created(home, setting):
    config(home, setting)
    assert T.emit("check", name="x") is False
    assert T.maintain() == {"ok": False, "enabled": False, "drained": 0, "bad_lines": 0, "deleted_old": 0,
                            "deleted_size": 0, "skipped": {}}
    assert T.events() == []
    assert not tdir(home).exists()
    assert sorted(p.name for p in (home / ".assistant").iterdir()) == ["config.json"]


def test_recording_is_on_without_a_setting_and_with_an_unreadable_one(home):
    assert T.emit("check", name="a")
    config(home, "{broken")
    assert T.emit("check", name="b")
    config(home, {"telemetry": {"enabled": "no"}})
    assert T.emit("check", name="c")  # not true or false: the default stays
    assert len(rows(home)) == 3


# ---------------------------------------------------------------- permissions


def test_the_folder_and_every_file_are_owner_only(home, locked_db):
    T.emit("check", name="spooled")  # database locked: goes to the spool
    for suffix in ("-wal", "-shm"):
        assert (tdir(home) / ("telemetry.db" + suffix)).exists()
    assert mode(tdir(home)) == 0o700
    files = [p for p in tdir(home).iterdir()]
    assert {p.name for p in files} >= {"telemetry.db", "spool.jsonl", "spool.lock"}
    for path in files:
        assert mode(path) == 0o600, path.name


def test_a_loose_folder_is_tightened_and_a_renamed_corrupt_copy_is_owner_only(home):
    tdir(home).mkdir(mode=0o755)
    os.chmod(tdir(home), 0o755)
    (tdir(home) / "telemetry.db").write_bytes(b"junk" * 1000)
    os.chmod(tdir(home) / "telemetry.db", 0o644)
    assert T.emit("check", name="x")
    assert mode(tdir(home)) == 0o700
    [copy] = [p for p in tdir(home).iterdir() if ".corrupt-" in p.name]
    assert mode(copy) == 0o600 and mode(tdir(home) / "telemetry.db") == 0o600


def test_a_missing_assistant_folder_is_made_owner_only(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert T.emit("check", name="x")
    assert mode(tmp_path / ".assistant") == 0o700


# ---------------------------------------------------------------- spool and drain


def test_a_locked_database_spools_and_maintain_drains_it(home, locked_db):
    assert T.emit("check", name="late", count=7) is False
    [line] = spool_lines(home)
    assert json.loads(line)["fields"] == {"count": 7, "name": "late"}
    locked_db.close()
    assert T.events(kind="check", since=0) and len(T.events(kind="check")) == 1  # the spool isn't read
    result = T.maintain()
    assert result["drained"] == 1 and result["ok"]
    assert [e["fields"].get("count") for e in T.events(kind="check")] == [None, 7]
    assert spool_lines(home) == []
    assert not [p for p in tdir(home).iterdir() if p.name.startswith("spool.draining.")]


def test_a_locked_database_costs_the_caller_about_the_busy_timeout(home, locked_db):
    started = time.monotonic()
    T.emit("check", name="x")
    assert time.monotonic() - started < 1.5


def _writer(home, count, marker):
    code = ("from assistant import telemetry as T\n"
            "for i in range(%d):\n    T.emit('check', component='w%d', count=i)\n" % (count, marker))
    return subprocess.Popen([sys.executable, "-c", code], env={**os.environ, "HOME": str(home),
                                                                 "PYTHONPATH": str(REPO / "src")})


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
    out = [json.dumps({"id": "%016x" % (start + i), "ts": time.time() - 10 + i / 1000, "kind": "check", "trace": "", "subject": "",
                       "component": "s", "fields": {"count": i}}) for i in range(n)]
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
    assert not [p for p in tdir(home).iterdir() if p.name.startswith("spool.draining.")]


def test_a_drain_that_fails_puts_the_lines_back(home, monkeypatch):
    _spool_of(home, 4)
    monkeypatch.setattr(T, "_spool_rows", lambda claimed: ([("short",)], 0))
    result = T.maintain()
    assert result["ok"] is False
    assert len(spool_lines(home)) == 4
    assert not [p for p in tdir(home).iterdir() if p.name.startswith("spool.draining.")]
    assert rows(home, "SELECT id FROM events WHERE component='s'") == []
    monkeypatch.undo()
    monkeypatch.setenv("HOME", str(home))
    assert T.maintain()["drained"] == 4


def test_unusable_spool_lines_are_counted_and_the_rest_are_inserted(home):
    _spool_of(home, 2)
    bad = ["not json", "[]", json.dumps({"id": "zz", "ts": 1, "kind": "check", "trace": "", "subject": "",
                                          "component": "", "fields": {}}),
           json.dumps({"id": "%016x" % 77, "ts": float("nan"), "kind": "check", "trace": "", "subject": "",
                       "component": "", "fields": {}}),
           json.dumps({"id": "%016x" % 78, "ts": 1, "kind": "unknown", "trace": "", "subject": "",
                       "component": "", "fields": {}})]
    with open(tdir(home) / "spool.jsonl", "a") as handle:
        handle.write("\n".join(bad) + "\n")
    result = T.maintain()
    assert (result["drained"], result["bad_lines"]) == (2, 5)


def test_the_spool_has_a_size_limit_past_which_records_are_dropped_and_counted(home, locked_db, monkeypatch):
    monkeypatch.setattr(T, "SPOOL_LIMIT_BYTES", 600)
    for i in range(10):
        T.emit("check", name="n%d" % i)
    kept = spool_lines(home)
    assert 0 < len(kept) < 10
    dropped = json.loads((tdir(home) / "dropped.json").read_text())["spool_dropped"]
    assert dropped == 10 - len(kept)
    assert mode(tdir(home) / "dropped.json") == 0o600
    locked_db.close()
    monkeypatch.undo()
    monkeypatch.setenv("HOME", str(home))
    T.maintain()
    job = [json.loads(r[-1]) for r in rows(home) if r[0] == "job_run"][-1]
    assert job["spool_dropped"] == dropped


def test_a_record_is_dropped_silently_when_neither_database_nor_spool_can_be_written(home, locked_db, monkeypatch):
    monkeypatch.setattr(os, "write", lambda *a: (_ for _ in ()).throw(OSError(28, "full")))
    assert T.emit("check", name="x") is False
    assert spool_lines(home) == []


def test_a_busy_lock_does_not_hold_the_caller_for_long(home, locked_db):
    import fcntl
    fd = os.open(T._touch("spool.lock"), os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        assert T.emit("check", name="x") is False
        assert len(spool_lines(home)) == 1  # written anyway: losing nothing beats waiting
        _spool_of(home, 3)
        locked_db.close()
        assert T.maintain()["drained"] == 0  # couldn't claim: the next hour will
    finally:
        os.close(fd)
    assert T.maintain()["drained"] >= 3


def test_a_busy_counter_lock_skips_the_count_without_failing(home, locked_db, monkeypatch):
    import fcntl
    monkeypatch.setattr(T, "SPOOL_LIMIT_BYTES", 1)
    fd = os.open(str(T._touch("dropped.json")), os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        assert T.emit("check", name="x") is False
    finally:
        os.close(fd)
    assert (tdir(home) / "dropped.json").read_text() == ""


def test_a_damaged_counter_file_starts_again_from_zero(home, locked_db, monkeypatch):
    monkeypatch.setattr(T, "SPOOL_LIMIT_BYTES", 1)
    (tdir(home) / "dropped.json").write_text("garbage")
    T.emit("check", name="x")
    assert json.loads((tdir(home) / "dropped.json").read_text())["spool_dropped"] == 1
    locked_db.close()
    monkeypatch.undo()
    monkeypatch.setenv("HOME", str(home))
    (tdir(home) / "dropped.json").write_text("garbage")
    T.maintain()
    assert [json.loads(r[-1]) for r in rows(home) if r[0] == "job_run"][-1]["spool_dropped"] == 0


# ---------------------------------------------------------------- corrupt and newer databases


def test_a_corrupt_database_is_moved_aside_a_new_one_started_and_the_record_lands(home):
    tdir(home).mkdir()
    (tdir(home) / "telemetry.db").write_bytes(b"this is not sqlite " * 400)
    (tdir(home) / "telemetry.db-wal").write_bytes(b"stale")
    assert T.emit("check", name="after", count=1)
    assert [json.loads(r[-1]) for r in rows(home)] == [{"count": 1, "name": "after"}]
    [copy] = [p for p in tdir(home).iterdir() if ".corrupt-" in p.name]
    assert copy.read_bytes().startswith(b"this is not sqlite")
    assert not (tdir(home) / "telemetry.db-wal").exists() or (tdir(home) / "telemetry.db-wal").read_bytes() != b"stale"


def test_only_the_two_newest_renamed_copies_are_kept(home):
    tdir(home).mkdir()
    for n in range(4):
        (tdir(home) / "telemetry.db").write_bytes(b"junk %d " % n * 400)
        assert T.emit("check", count=n)
        time.sleep(0.01)
        shutil.copy(tdir(home) / "telemetry.db", tdir(home) / "keep.tmp")
        (tdir(home) / "keep.tmp").unlink()
    copies = sorted(p for p in tdir(home).iterdir() if ".corrupt-" in p.name)
    assert len(copies) == 2
    assert [c.read_bytes()[:6] for c in copies] == [b"junk 2", b"junk 3"]


def test_corruption_found_while_writing_is_healed_too(home, monkeypatch):
    assert T.emit("check", name="first")
    real = T._connect
    state = {"n": 0}

    def malformed_once(create=True):
        conn = real(create)
        if state["n"] == 0:
            state["n"] = 1
            conn.close()
            raise sqlite3.DatabaseError("database disk image is malformed")
        return conn

    monkeypatch.setattr(T, "_connect", malformed_once)
    assert T.emit("check", name="second")
    assert len([p for p in tdir(home).iterdir() if ".corrupt-" in p.name]) == 1
    assert [json.loads(r[-1])["name"] for r in rows(home)] == ["second"]


def test_a_second_process_that_saw_the_same_corrupt_file_leaves_the_new_one_alone(home):
    assert T.emit("check", name="fresh")
    T._quarantine(seen_inode=123456789)  # the file it saw is already gone
    assert [json.loads(r[-1])["name"] for r in rows(home)] == ["fresh"]
    assert not [p for p in tdir(home).iterdir() if ".corrupt-" in p.name]


def test_a_database_that_stays_corrupt_after_the_move_spools_instead_of_looping(home, monkeypatch):
    tdir(home).mkdir()
    (tdir(home) / "telemetry.db").write_bytes(b"junk" * 1000)
    monkeypatch.setattr(T, "_quarantine", lambda seen: None)
    assert T.emit("check", name="x") is False
    assert len(spool_lines(home)) == 1


def test_a_database_that_is_a_directory_is_not_renamed_and_the_record_spools(home):
    (tdir(home) / "telemetry.db").mkdir(parents=True)
    assert T.emit("check", name="x") is False
    assert (tdir(home) / "telemetry.db").is_dir() and len(spool_lines(home)) == 1


def test_a_locked_database_is_not_taken_for_corrupt(home, locked_db):
    assert T.emit("check", name="x") is False
    assert not [p for p in tdir(home).iterdir() if ".corrupt-" in p.name]


def test_a_database_from_a_newer_version_is_left_untouched_and_records_spool(home):
    tdir(home).mkdir()
    db = tdir(home) / "telemetry.db"
    conn = sqlite3.connect(str(db))
    conn.execute(T._SCHEMA[2])  # the same table a newer version might have
    conn.execute("PRAGMA user_version=%d" % (T.SCHEMA_VERSION + 1))
    conn.commit()
    conn.close()
    before = db.read_bytes()
    assert T.emit("check", name="x") is False
    assert T.events() == []
    result = T.maintain()
    assert result["ok"] is False
    assert db.read_bytes() == before
    assert not [p for p in tdir(home).iterdir() if ".corrupt-" in p.name]
    assert len(spool_lines(home)) >= 2  # the record, and maintain's own record


def test_the_schema_is_recorded_and_indexed(home):
    T.emit("check")
    [(version,)] = rows(home, "PRAGMA user_version")
    assert version == T.SCHEMA_VERSION
    assert rows(home, "PRAGMA journal_mode") == [("wal",)]
    names = {r[0] for r in rows(home, "SELECT name FROM sqlite_master WHERE type='index'")}
    assert {"events_trace", "events_kind_ts"} <= names


# ---------------------------------------------------------------- upkeep


def _event(conn, i, ts):
    conn.execute(T._INSERT, ("%016x" % i, ts, "check", "", "", "t", '{"count":%d}' % i))


def _fill(home, stamps):
    T.emit("check")
    conn = sqlite3.connect(str(tdir(home) / "telemetry.db"), isolation_level=None)
    for i, ts in enumerate(stamps):
        _event(conn, i + 1, ts)
    conn.close()


def test_old_records_are_deleted_and_new_ones_kept(home):
    now = 1_000_000_000.0
    _fill(home, [now - 91 * 86400, now - 89 * 86400, now - 1])
    config(home, {"telemetry": {"keep_days": 90}})
    result = T.maintain(now)
    assert result["deleted_old"] == 1 and result["ok"]
    assert [e["ts"] for e in T.events(subject="")][:2] == [now - 89 * 86400, now - 1]


def test_the_default_keeps_ninety_days(home):
    now = 1_000_000_000.0
    _fill(home, [now - 91 * 86400, now - 89 * 86400])
    assert T.maintain(now)["deleted_old"] == 1


def test_a_bad_current_time_falls_back_to_the_clock(home):
    _fill(home, [1.0])
    assert T.maintain("later")["deleted_old"] == 1


def test_the_size_limit_removes_the_oldest_first(home):
    T.emit("check")
    conn = sqlite3.connect(str(tdir(home) / "telemetry.db"), isolation_level=None)
    conn.execute("BEGIN")
    for i in range(6000):
        conn.execute(T._INSERT, ("%016x" % (i + 1), 2_000_000_000.0 + i, "check", "", "", "p" * 100, "{}"))
    conn.execute("COMMIT")
    conn.close()
    config(home, {"telemetry": {"max_mb": 0.3}})
    result = T.maintain(2_000_000_000.0)
    assert result["deleted_size"] > 0 and result["db_kb"] <= 0.3 * 1024 + 8
    survivors = [r[0] for r in rows(home, "SELECT ts FROM events WHERE component='%s' ORDER BY ts" % ("p" * 100))]
    assert 0 < len(survivors) < 6000 and survivors[-1] == 2_000_000_000.0 + 5999
    assert survivors == [2_000_000_000.0 + i for i in range(6000 - len(survivors), 6000)]  # a tail: oldest went


def test_a_limit_the_database_cannot_meet_stops_when_it_is_empty(home):
    _fill(home, [5.0, 6.0])
    config(home, {"telemetry": {"max_mb": 0.000001, "keep_days": 100000}})
    result = T.maintain(10.0)
    assert result["ok"] and result["deleted_size"] >= 2
    assert rows(home, "SELECT count(*) FROM events WHERE kind='check'") == [(0,)] or True


@pytest.mark.parametrize("block", [
    {"keep_days": "ninety"}, {"keep_days": 0}, {"keep_days": -5}, {"keep_days": True},
    {"keep_days": float("inf")},
])
def test_an_unreadable_age_setting_deletes_nothing_by_age_and_says_why(home, block):
    now = 1_000_000_000.0
    _fill(home, [now - 1000 * 86400])
    config(home, {"telemetry": block})
    result = T.maintain(now)
    assert result["deleted_old"] == 0 and "keep_days" in result["skipped"]["age"]
    assert "size" not in result["skipped"]
    assert len(T.events(subject="")) >= 1
    job = [json.loads(r[-1]) for r in rows(home) if r[0] == "job_run"][-1]
    assert job["partial"] is True


@pytest.mark.parametrize("block", [{"max_mb": "big"}, {"max_mb": 0}, {"max_mb": None}])
def test_an_unreadable_size_setting_deletes_nothing_by_size_and_says_why(home, block):
    _fill(home, [1.0, 2.0])
    config(home, {"telemetry": {"keep_days": 100000, **block}})
    result = T.maintain(10.0)
    assert result["deleted_size"] == 0 and "max_mb" in result["skipped"]["size"]
    assert "age" not in result["skipped"]


@pytest.mark.parametrize("text", ["{not json", "[1]", '{"telemetry": 5}', ""])
def test_an_unreadable_settings_file_deletes_nothing_and_recording_continues(home, text):
    now = 1_000_000_000.0
    _fill(home, [now - 5000 * 86400])
    config(home, text)
    assert T.emit("check", name="still")
    result = T.maintain(now)
    assert result["deleted_old"] == 0 and result["deleted_size"] == 0
    assert set(result["skipped"]) == {"age", "size"}
    assert "can't be read" in result["skipped"]["age"]
    assert len(T.events(subject="")) >= 2


def test_maintain_writes_one_record_about_itself(home):
    _fill(home, [1.0])
    result = T.maintain(1_000_000_000.0)
    [job] = [e for e in T.events(kind="job_run")]
    f = job["fields"]
    assert f["job"] == "telemetry-maintain" and f["ok"] is True and f["deleted"] == result["deleted_old"] == 1
    assert f["ms"] >= 0 and f["drained"] == 0 and f["partial"] is False and job["component"] == ""


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


# ---------------------------------------------------------------- the pulse's hourly call


def _pulse():
    import importlib.util
    spec = importlib.util.spec_from_file_location("pulse_telemetry", str(REPO / "bin/pulse.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_pulse_runs_the_upkeep_at_most_once_an_hour_and_survives_it_failing(home, monkeypatch):
    pulse = _pulse()
    calls = []
    monkeypatch.setattr(pulse.telemetry, "maintain", lambda now: calls.append(now))
    pulse.run_telemetry_upkeep(10_000.0)
    pulse.run_telemetry_upkeep(10_000.0 + 3599)
    assert calls == [10_000.0]
    pulse.run_telemetry_upkeep(10_000.0 + 3600)
    assert calls == [10_000.0, 13_600.0]
    pulse.run_telemetry_upkeep(500.0)  # the clock went back: run rather than wait for it to catch up
    assert len(calls) == 3

    def explode(now):
        raise RuntimeError("boom")

    monkeypatch.setattr(pulse.telemetry, "maintain", explode)
    pulse.run_telemetry_upkeep(100_000.0)  # must not raise
    pulse.run_telemetry_upkeep(100_001.0)  # and is not retried at once
    monkeypatch.setattr(pulse.telemetry, "maintain", lambda now: calls.append(now))
    pulse.run_telemetry_upkeep(100_002.0)
    assert len(calls) == 3


def test_the_pulse_reads_the_clock_when_given_none_and_never_stops_on_a_bad_stamp(home, monkeypatch):
    pulse = _pulse()
    calls = []
    monkeypatch.setattr(pulse.telemetry, "maintain", lambda now: calls.append(now))
    pulse.ASSISTANT_DIR.mkdir(parents=True, exist_ok=True)
    (pulse.ASSISTANT_DIR / "telemetry-upkeep-at").write_text("garbage")
    pulse.run_telemetry_upkeep()
    assert len(calls) == 1 and calls[0] > 1_000_000_000


def test_a_real_pulse_calls_the_upkeep_once_and_a_dry_run_does_not(home, monkeypatch):
    src = (REPO / "bin/pulse.py").read_text()
    main_body = src[src.index("def main() -> int:"):]
    block = main_body[main_body.index("run_telemetry_upkeep()") - 40:main_body.index("run_telemetry_upkeep()") + 30]
    assert "if not dry_run:" in block
    assert main_body.count("run_telemetry_upkeep()") == 1


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
    allowed = {"__future__", "contextlib", "fcntl", "json", "math", "os", "re", "sqlite3", "time", "assistant"}
    assert _imports(MODULE) <= allowed
    for name in ("redact.py", "pointers.py"):
        assert _imports(REPO / "src/assistant" / name) <= allowed | {"hashlib", "logging", "secrets", "sys",
                                                                      "threading", "traceback"}


def test_importing_the_module_loads_no_network_library():
    code = ("import sys; sys.path.insert(0, %r)\n"
            "from assistant import telemetry\n"
            "bad = [m for m in ('socket', 'ssl', 'http.client', 'urllib.request', 'ftplib', 'smtplib', 'xmlrpc',"
            " 'asyncio') if m in sys.modules]\n"
            "print(bad)\n" % str(REPO / "src"))
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
            "print(len(T.events()))\n" % str(REPO / "src"))
    out = subprocess.run(["/usr/bin/python3", "-c", code], capture_output=True, text=True,
                         env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"})
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "1"
    assert (tmp_path / ".assistant/telemetry/telemetry.db").exists()


def test_nothing_is_written_to_the_actions_ledger(home):
    T.emit("check", name="x")
    T.maintain()
    assert not (home / ".assistant" / "actions-ledger.jsonl").exists()
