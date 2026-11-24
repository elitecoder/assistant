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


def fail(n, text=ERR, component="self-update", start=NOW, step=60, **kw):
    for i in range(n):
        failwatch.record_failure(component, text, now=start + i * step, **kw)


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


def test_work_that_cannot_be_held_back_is_counted_but_never_paused(store):
    for i in range(5):
        row = failwatch.record_failure("render", "boom", now=NOW + i, pause=False)
    assert row["count"] == 5 and row["backoff_until"] == 0
    assert failwatch.backed_off("render", now=NOW + 10) is None


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
    (row,) = failwatch.history()
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


# history() and repeated()

def test_history_lists_everything_most_recent_first_and_survives_a_broken_store(store):
    fail(1, "first", component="a")
    fail(1, "second", component="b", start=NOW + 500)
    failwatch.record_ok("a")
    assert [r["sample"] for r in failwatch.history()] == ["second", "first"]
    store.write_text("not json")
    assert failwatch.history() == []


def test_history_never_raises(monkeypatch):
    monkeypatch.setattr(failwatch, "_read", lambda path: 1 / 0)
    assert failwatch.history() == []



def test_repeated_needs_three_inside_the_window_and_sorts_by_count(store):
    fail(2, "two", component="a")
    fail(3, "three", component="b", start=NOW + 10)
    fail(5, "five", component="c", start=NOW + 20)
    fail(4, "old", component="d", start=NOW - 3 * 86400)
    rows = failwatch.repeated(now=NOW + 1000)
    assert [r["sample"] for r in rows] == ["five", "three"]
    assert [r["count"] for r in failwatch.repeated(now=NOW + 1000, min_count=2)] == [5, 3, 2]
    assert failwatch.repeated(now=NOW + 3 * 86400 + 10 ** 6) == []


def test_a_failure_that_has_since_worked_is_no_longer_listed(store):
    """Self-update failing three times and then succeeding must not stay on the dashboard for a day."""
    for i in range(4):
        failwatch.record_failure("self-update", "fetch failed", now=NOW + i)
    assert len(failwatch.repeated(now=NOW + 10)) == 1
    failwatch.record_ok("self-update", now=NOW + 20)
    assert failwatch.repeated(now=NOW + 30) == []
    failwatch.record_failure("self-update", "fetch failed", now=NOW + 40)
    assert failwatch.repeated(now=NOW + 50) == [], "one new failure after a recovery is not a repeat"
    (row,) = failwatch.history()
    assert row["count"] == 5 and row["streak"] == 1, "the history keeps the old episode"


def test_an_error_replaced_by_a_different_one_is_no_longer_listed(store):
    for i in range(3):
        failwatch.record_failure("self-update", "fetch failed", now=NOW + i)
    for i in range(3):
        failwatch.record_failure("self-update", "install script missing", now=NOW + 10 + i)
    assert [r["sample"] for r in failwatch.repeated(now=NOW + 20)] == ["install script missing"]


def test_repeated_returns_at_most_five(store):
    for i in range(8):
        fail(3, f"error kind {chr(97 + i)}", component=f"c{i}")
    assert len(failwatch.repeated(now=NOW + 500)) == 5


# the store

def test_store_is_owner_only_and_holds_the_documented_fields(store):
    fail(3)
    assert stat.S_IMODE(store.stat().st_mode) == 0o600
    (row,) = json.loads(store.read_text()).values()
    assert set(row) == {"component", "count", "streak", "first_ts", "streak_first_ts", "last_ts", "sample",
                        "backoff_until", "cap", "context"}
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
    ticks = iter(i * 0.05 for i in range(100))
    monkeypatch.setattr(failwatch.time, "monotonic", lambda: next(ticks))
    assert failwatch.record_failure("c", "boom") is None
    assert 0 < len(sleeps) <= 6, "a contended store costs a fraction of a second, not seconds"
    assert not store.exists()


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


# the current run, not all time

def test_repeated_lists_the_current_streak_and_when_it_began(store):
    fail(5, "boom", start=NOW)
    failwatch.record_ok("self-update")
    failwatch.record_failure("self-update", "boom", now=NOW + 1000)
    failwatch.record_failure("self-update", "boom", now=NOW + 1100)
    assert failwatch.repeated(now=NOW + 1200) == [], "two in a row since the recovery is not a repeat"
    failwatch.record_failure("self-update", "boom", now=NOW + 1200)
    (row,) = failwatch.repeated(now=NOW + 1300)
    assert (row["streak"], row["count"]) == (3, 8)
    assert row["streak_first_ts"] == NOW + 1000 and row["first_ts"] == NOW


def test_a_row_still_paused_stays_listed_whatever_the_window_says(store):
    fail(8, "boom", start=NOW, step=1)  # a 24-hour pause from NOW + 7
    assert failwatch.repeated(now=NOW + 7200, window_sec=3600)
    assert failwatch.repeated(now=NOW + 7200, window_sec=3600, min_count=99)
    assert failwatch.repeated(now=NOW + 86400 + 10) == [], "the pause is over and the window has passed"


def test_a_streak_outside_the_window_with_no_pause_is_not_listed(store):
    fail(3, "boom", pause=False, start=NOW, step=1)
    assert failwatch.repeated(now=NOW + 7200, window_sec=3600) == []


def test_repeated_limit_can_be_raised(store):
    for i in range(8):
        fail(3, f"error kind {chr(97 + i)}", component=f"c{i}")
    assert len(failwatch.repeated(now=NOW + 500, limit=50)) == 8


# a pause that should not outlive its cause

def test_the_longest_pause_can_be_capped(store):
    got = [(failwatch.record_failure("c", "boom", now=NOW, cap=4 * HOUR)["backoff_until"] or NOW) - NOW
           for _ in range(7)]
    assert got == [0, 0, HOUR, 2 * HOUR, 4 * HOUR, 4 * HOUR, 4 * HOUR]


def test_a_pause_set_for_another_context_does_not_hold(store):
    for i in range(3):
        failwatch.record_failure("c", "boom", now=NOW + i, context="origin/feat-x")
    assert failwatch.backed_off("c", now=NOW + 10)
    assert failwatch.backed_off("c", now=NOW + 10, context="origin/feat-x")
    assert failwatch.backed_off("c", now=NOW + 10, context="origin/main") is None


def test_failing_on_something_else_starts_the_streak_over(store):
    for i in range(3):
        failwatch.record_failure("c", "boom", now=NOW + i, context="origin/feat-x")
    row = failwatch.record_failure("c", "boom", now=NOW + 10, context="origin/main")
    assert (row["streak"], row["backoff_until"], row["streak_first_ts"]) == (1, 0, NOW + 10)


def test_a_pause_further_away_than_its_cap_is_a_clock_error_and_counts_as_over(store):
    fail(3, "boom", component="c")
    path = store
    data = json.loads(path.read_text())
    (row,) = data.values()
    row["backoff_until"] = NOW + 365 * 86400
    path.write_text(json.dumps(data))
    assert failwatch.backed_off("c", now=NOW + 200) is None
    row["backoff_until"] = NOW + 200 + 5 * HOUR
    row["cap"] = 4 * HOUR
    path.write_text(json.dumps(data))
    assert failwatch.backed_off("c", now=NOW + 200) is None
    assert failwatch.repeated(now=NOW + 200)[0]["streak"] == 3


def test_record_ok_except_clears_rows_whose_subject_is_gone(store):
    for name in ("workspace:1", "workspace:2"):
        fail(3, "boom", component=f"ready-for-merge:{name}")
    fail(3, "boom", component="self-update")
    failwatch.record_ok_except("ready-for-merge:", {"workspace:1"})
    assert sorted(r["component"] for r in failwatch.repeated(now=NOW + 300)) == [
        "ready-for-merge:workspace:1", "self-update"]
    failwatch.record_ok_except("ready-for-merge:", {"workspace:1"})  # nothing left to clear


def test_record_ok_except_is_quiet_with_no_file_or_on_an_error(store, monkeypatch):
    failwatch.record_ok_except("x:", set())
    assert not store.exists()
    fail(3, "boom", component="x:1")
    monkeypatch.setattr(failwatch, "_read", lambda path: 1 / 0)
    failwatch.record_ok_except("x:", set())


# what counts as the same failure, and what a sample may hold

def test_status_401_403_404_429_stay_exact_and_other_5xx_fold_together():
    def fp(text):
        return failwatch.fingerprint("m", text)
    kept = [fp(f"HTTP {code} from host") for code in (401, 403, 404, 429)]
    assert len(set(kept)) == 4
    assert len({fp(f"HTTP {code} from host") for code in (500, 502, 503, 529)}) == 1
    assert fp("Error code: 529 overloaded") == fp("Error code: 500 overloaded")
    assert fp("HTTP 429 from host") != fp("HTTP 503 from host")
    assert fp("HTTP 401 from host") != fp("HTTP 503 from host")
    assert fp("failed at line 404 of the file") == fp("failed at line 7 of the file")
    assert fp("failed with code 400") == fp("failed with code 418")


def test_an_outage_that_alternates_5xx_codes_is_one_failure_that_pauses(store):
    for i, code in enumerate((500, 502, 503, 529)):
        failwatch.record_failure("model:x", f"Error code: {code} upstream down request_id req_0{i}AbC", now=NOW + i)
    (row,) = failwatch.repeated(now=NOW + 10)
    assert row["streak"] == 4 and failwatch.backed_off("model:x", now=NOW + 10)


def test_the_error_type_alone_tells_two_errors_with_one_status_apart():
    a = failwatch.fingerprint("m", 'Error code: 529 {"error": {"type": "overloaded_error"}}')
    b = failwatch.fingerprint("m", 'Error code: 529 {"error": {"type": "api_error"}}')
    assert a != b


def test_a_request_id_does_not_make_each_call_its_own_failure():
    a = failwatch.fingerprint("m", "Error code: 529 request_id req_011CVabcDEF123")
    b = failwatch.fingerprint("m", "Error code: 529 request_id req_011CVzzzXYZ999")
    c = failwatch.fingerprint("m", "overloaded msg_01AbCdEf toolu_01XyZ")
    d = failwatch.fingerprint("m", "overloaded msg_01ZzZzZz toolu_09QqQ")
    assert a == b and c == d


def test_more_secret_shapes_never_reach_the_sample(store):
    secrets = ["-".join(["sk", "ant", "api03", "AbCdEf123456"]), "hunter" + "2" * 3, "p4" + "ssw0rd", "s3" + "cretkey"]
    text = (f"key {secrets[0]} api_key={secrets[3]} \"password\": \"{secrets[1]}\" "
            f"https://bob:{secrets[2]}@example.com/x risk-averse")
    sample = failwatch.record_failure("c", text, now=NOW)["sample"]
    for leaked in secrets:
        assert leaked not in sample
    assert "https://<redacted>@example.com/x" in sample and "risk-averse" in sample
    assert "bob" not in sample


def test_a_row_stamped_while_the_clock_was_wrong_is_not_listed_and_goes_first(store):
    import time as _t
    fail(3, "boom", component="old", start=_t.time() - 60, step=1)
    data = json.loads(store.read_text())
    (row,) = data.values()
    row["last_ts"] = _t.time() + 365 * 86400
    store.write_text(json.dumps(data))
    assert failwatch.repeated() == []
    data["x"] = {"component": "ghost", "count": 9, "streak": 9, "last_ts": _t.time() + 365 * 86400}
    for i in range(205):
        data[f"c{i}"] = {"component": f"c{i}", "streak": 1, "count": 1, "last_ts": _t.time() - 1000 + i}
    store.write_text(json.dumps(data))
    failwatch.record_failure("fresh", "boom")
    kept = json.loads(store.read_text())
    assert len(kept) == 200 and "x" not in kept and "fresh: boom" in kept
    assert not any(r["component"] == "old" for r in kept.values())


def test_one_corrupt_row_does_not_hide_the_others(store):
    fail(3, "boom", component="good")
    data = json.loads(store.read_text())
    data["bad1"] = {"component": "bad1", "streak": 5, "count": 5, "last_ts": NOW, "cap": "x"}
    data["bad2"] = {"component": "bad2", "streak": "many", "count": 5, "last_ts": NOW}
    data["bad3"] = {"component": "bad3", "streak": 4, "count": 4, "last_ts": None, "backoff_until": "soon"}
    store.write_text(json.dumps(data))
    assert [r["component"] for r in failwatch.repeated(now=NOW + 200)] == ["good"]
