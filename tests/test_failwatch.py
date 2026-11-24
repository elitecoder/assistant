"""failwatch: one counted line for a repeating failure, and a back-off for background work."""
import json
import os
import stat

import pytest

from assistant import failwatch

HOUR = 3600
NOW = 1_700_000_000.0
ERR = "fetch failed: fatal: couldn't find remote ref feat/x"


@pytest.fixture
def store(tmp_path, monkeypatch):
    path = tmp_path / "home" / "failures.json"
    monkeypatch.setenv("ASSISTANT_FAILURES_FILE", str(path))
    return path


def fail(n, text=ERR, component="self-update", start=NOW, step=60):
    for i in range(n):
        failwatch.record_failure(component, text, now=start + i * step)


# fingerprints

def test_numbers_ids_and_paths_collapse_to_one_fingerprint():
    a = failwatch.fingerprint("c", "td-123 failed on workspace:7 at /Users/ann/dev/x/y.py line 42 'abc' sha deadbeef01 "
                                   "uuid 123e4567-e89b-12d3-a456-426614174000")
    b = failwatch.fingerprint("c", "td-9 failed on workspace:12 at /Users/bob/other/z.py line 7 'q' sha 0123abcd99 "
                                   "uuid 00000000-0000-0000-0000-000000000000")
    assert a == b


def test_different_errors_and_components_get_different_fingerprints():
    assert failwatch.fingerprint("c", "disk full") != failwatch.fingerprint("c", "no route to host")
    assert failwatch.fingerprint("a", "disk full") != failwatch.fingerprint("b", "disk full")


def test_fingerprint_keeps_apostrophes_and_cuts_at_100_characters():
    text = failwatch.fingerprint("c", "couldn't find remote ref 'feat/x' now")
    assert text == "c: couldn't find remote ref now"
    assert len(failwatch.fingerprint("c", "x" * 500)) == len("c: ") + 100
    assert failwatch.fingerprint("c", None) == "c: None"
    assert failwatch.fingerprint("c", "home ~/dev/a and /tmp/a/b and https://h.io/a/b") == "c: home and and https://h.io/a/b"


def test_sample_is_one_line_cut_and_redacted(store):
    secret = ("xoxb-123-abc Bearer abc.def gho_ABC123 token=hunter2 " + "y" * 400)
    row = failwatch.record_failure("c", "line one\nline  two " + secret, now=NOW)
    sample = row["sample"]
    assert len(sample) <= 200 and "\n" not in sample
    for leaked in ("xoxb", "abc.def", "gho_", "hunter2"):
        assert leaked not in sample
    assert sample.count("<redacted>") == 4
    assert "hunter2" not in store.read_text()


# back-off schedule

def test_no_back_off_after_two_failures_then_one_hour_after_the_third(store):
    fail(2)
    assert failwatch.backed_off("self-update", now=NOW + 120) is None
    fail(1, start=NOW + 120)
    assert failwatch.backed_off("self-update", now=NOW + 121) == NOW + 120 + HOUR
    assert failwatch.backed_off("self-update", now=NOW + 120 + HOUR) is None
    assert failwatch.backed_off("other", now=NOW + 121) is None


def test_back_off_doubles_and_caps_at_24_hours(store):
    got = [(failwatch.record_failure("c", ERR, now=NOW)["backoff_until"] or NOW) - NOW for _ in range(9)]
    assert got == [0, 0, HOUR, 2 * HOUR, 4 * HOUR, 8 * HOUR, 16 * HOUR, 24 * HOUR, 24 * HOUR]


def test_a_different_error_resets_the_back_off(store):
    fail(3)
    assert failwatch.backed_off("self-update", now=NOW + 200)
    failwatch.record_failure("self-update", "disk full", now=NOW + 200)
    assert failwatch.backed_off("self-update", now=NOW + 201) is None
    fail(1, start=NOW + 300)  # the old error again starts a new streak of one
    assert failwatch.backed_off("self-update", now=NOW + 301) is None


def test_a_success_resets_the_back_off_but_keeps_the_history(store):
    fail(4)
    failwatch.record_ok("self-update")
    assert failwatch.backed_off("self-update", now=NOW + 100) is None
    (row,) = failwatch.repeated(now=NOW + 100)
    assert row["count"] == 4 and row["streak"] == 0
    failwatch.record_failure("self-update", ERR, now=NOW + 200)
    assert failwatch.backed_off("self-update", now=NOW + 201) is None  # streak restarted at 1


def test_record_ok_for_another_component_or_with_no_file_changes_nothing(store):
    failwatch.record_ok("self-update")
    assert not store.exists()
    fail(3)
    before = store.read_text()
    failwatch.record_ok("other")
    assert store.read_text() == before
    failwatch.record_ok("self-update")
    failwatch.record_ok("self-update")  # nothing left to clear: no rewrite needed


# repeated()

def test_repeated_needs_three_inside_the_window_and_sorts_by_count(store):
    fail(2, "two")
    fail(3, "three", start=NOW + 10)
    fail(5, "five", start=NOW + 20)
    fail(4, "old", start=NOW - 3 * 86400)
    rows = failwatch.repeated(now=NOW + 1000)
    assert [r["sample"] for r in rows] == ["five", "three"]
    assert [r["count"] for r in failwatch.repeated(now=NOW + 1000, min_count=2)] == [5, 3, 2]
    assert failwatch.repeated(now=NOW + 3 * 86400 + 10 ** 6) == []


def test_repeated_returns_at_most_five(store):
    for i in range(8):
        fail(3, f"error kind {chr(97 + i)}", component=f"c{i}")
    assert len(failwatch.repeated(now=NOW + 500)) == 5


# the store

def test_store_is_owner_only_and_holds_the_documented_fields(store):
    fail(3)
    assert stat.S_IMODE(store.stat().st_mode) == 0o600
    (row,) = json.loads(store.read_text()).values()
    assert set(row) == {"component", "count", "streak", "first_ts", "last_ts", "sample", "backoff_until"}
    assert row["first_ts"] == NOW and row["last_ts"] == NOW + 120 and row["sample"] == ERR
    assert not list(store.parent.glob("*.tmp"))


def test_store_keeps_the_200_most_recently_seen(store):
    for i in range(205):
        failwatch.record_failure(f"c{i}", "boom", now=NOW + i)
    data = json.loads(store.read_text())
    assert len(data) == 200
    assert "c4: boom" not in data and "c5: boom" in data and "c204: boom" in data


def test_a_crash_between_the_temp_write_and_the_replace_keeps_the_old_file(store, monkeypatch):
    fail(2)
    before = store.read_text()

    def crash(*_):
        raise OSError("power cut")

    monkeypatch.setattr(os, "replace", crash)
    assert failwatch.record_failure("self-update", ERR, now=NOW + 999) is None
    monkeypatch.undo()
    assert store.read_text() == before
    assert not list(store.parent.glob("*.tmp"))


@pytest.mark.parametrize("junk", ["{not json", "[1, 2]", '{"a": 5, "b": {"component": "c", "count": 4, "last_ts": 1e12}}', ""])
def test_a_corrupt_file_is_treated_as_empty_or_filtered(store, junk):
    store.parent.mkdir(parents=True)
    store.write_text(junk)
    assert failwatch.backed_off("c", now=NOW) is None
    assert all(isinstance(r, dict) for r in failwatch.repeated(now=NOW))
    assert failwatch.record_failure("c", "boom", now=NOW)["count"] == 1


def test_a_missing_file_is_empty(store):
    assert failwatch.repeated(now=NOW) == [] and failwatch.backed_off("c", now=NOW) is None


def test_internal_errors_return_quietly(store, monkeypatch):
    fail(3)

    def boom(*_, **__):
        raise RuntimeError("bug")

    monkeypatch.setattr(failwatch, "_read", boom)
    assert failwatch.record_failure("c", "x") is None
    assert failwatch.backed_off("self-update") is None
    assert failwatch.repeated() == []
    failwatch.record_ok("self-update")


def test_a_lock_held_too_long_drops_the_record_instead_of_hanging(store, monkeypatch):
    sleeps = []
    monkeypatch.setattr(failwatch.fcntl, "flock", lambda *_: (_ for _ in ()).throw(BlockingIOError()))
    monkeypatch.setattr(failwatch.time, "sleep", sleeps.append)
    ticks = iter(range(0, 100, 1))
    monkeypatch.setattr(failwatch.time, "monotonic", lambda: next(ticks))
    assert failwatch.record_failure("c", "boom") is None
    assert sleeps and not store.exists()


def test_default_path_is_under_home(monkeypatch, tmp_path):
    monkeypatch.delenv("ASSISTANT_FAILURES_FILE")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert failwatch._store_path() == tmp_path / ".assistant" / "failures.json"
    failwatch.record_failure("c", "boom", now=NOW)
    assert (tmp_path / ".assistant" / "failures.json").exists()


def test_default_time_is_now(store):
    row = failwatch.record_failure("c", "boom")
    assert abs(row["last_ts"] - __import__("time").time()) < 5
    failwatch.record_failure("c", "boom")
    failwatch.record_failure("c", "boom")
    assert failwatch.backed_off("c") and len(failwatch.repeated()) == 1
