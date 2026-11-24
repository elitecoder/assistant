"""src/assistant/effects.py: the ledger of changes Assistant makes outside itself."""
from __future__ import annotations

import ast
import json
import os
import re
import stat
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from assistant import effects, pointers

REPO = Path(__file__).resolve().parent.parent
SOURCE = REPO / "src/assistant/effects.py"
T0 = datetime(2026, 11, 2, 10, 0, 10, tzinfo=timezone.utc).timestamp()
AT = "2026-11-02T10:00:00+00:00"
TARGET = {"account": "me@example.com", "thread": "t1"}
TAP = {"kind": "tap", "ref": "/api/gmail", "at": AT}
DECISION = {"kind": "decision", "ref": "row:0123456789abcdef", "at": AT}
# Made-up secrets are built from parts so GitHub's push protection doesn't take them for real ones.
TOKEN = "-".join(["xoxb", "111111111", "222222222", "AbCdEfGhIjKlMnOpQrStUv0123456789"])


class Clock:
    def __init__(self):
        self.t = T0

    def __call__(self):
        return self.t


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(effects, "_now", c)
    return c


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def ledger(home, clock):
    return home / ".assistant/effects/effects.jsonl"


def rows(ledger) -> list[dict]:
    return [json.loads(line) for line in ledger.read_text().splitlines()]


def archive(target=None, auth=None, **kw) -> str:
    return effects.begin("gmail.archive", target or TARGET, auth or TAP, **kw)


# --- the registry ---------------------------------------------------------------------------------------------

def test_the_registry_is_the_table_in_the_design():
    got = {name: (c.reversible, c.undone_by, c.accepts) for name, c in effects.CLASSES.items()}
    assert got == {
        "gmail.archive": (True, "gmail.unarchive", ("tap",)),
        "gmail.unarchive": (True, "gmail.archive", ("tap",)),
        "gmail.mark_read": (True, "", ("tap",)),
        "gmail.trash": (True, "gmail.untrash", ("decision",)),
        "gmail.untrash": (True, "gmail.trash", ("decision",)),
        "mail.unsubscribe": (False, "", ("decision",)),
        "slack.save.complete": (True, "slack.save.uncomplete", ("tap", "decision")),
        "slack.save.uncomplete": (True, "slack.save.complete", ("decision",)),
        "session.type": (False, "", ("tap", "owner_flag", "standing")),
    }


def test_no_irreversible_class_accepts_nothing_and_every_undo_is_a_registered_class():
    for name, spec in effects.CLASSES.items():
        assert spec.accepts and set(spec.accepts) <= set(effects.KINDS), name
        if not spec.reversible:
            assert spec.undone_by == "", name
        elif spec.undone_by:
            assert spec.undone_by in effects.CLASSES and effects.CLASSES[spec.undone_by].undone_by == name


# --- intent, then result --------------------------------------------------------------------------------------

def test_an_effect_writes_an_intent_row_then_a_result_row(ledger):
    eid = archive(trace=pointers.legacy_trace_id("k", "v"), component="work-queue")
    assert re.fullmatch(r"ef-[0-9a-f]{16}", eid)
    assert [r["kind"] for r in rows(ledger)] == ["intent"]
    assert effects.finish(eid, "done", verified=True)
    intent, result = rows(ledger)
    assert intent == {"v": 1, "kind": "intent", "id": eid, "at": "2026-11-02T10:00:10Z", "t": T0,
                      "class": "gmail.archive", "target": TARGET, "auth": {**TAP, "at": "2026-11-02T10:00:00Z"},
                      "trace": pointers.legacy_trace_id("k", "v"), "component": "work-queue", "undoes": ""}
    assert result == {"v": 1, "kind": "result", "id": eid, "at": "2026-11-02T10:00:10Z", "t": T0, "outcome": "done",
                      "verified": True, "error": ""}


def test_history_merges_each_effect_newest_first_and_filters(ledger, clock):
    a = archive()
    clock.t += 1
    b = effects.begin("gmail.mark_read", {"account": "me@example.com", "thread": "t2"}, TAP, trace="tr-0123456789abcdef")
    effects.finish(a, "failed", error="http 404")
    got = effects.history()
    assert [e["id"] for e in got] == [b, a]
    assert (got[1]["outcome"], got[1]["error"], got[0]["outcome"], got[0]["verified"]) == ("failed", "http 404", None, None)
    assert [e["id"] for e in effects.history("gmail.archive")] == [a]
    assert [e["id"] for e in effects.history(target={"thread": "t2", "account": "me@example.com"})] == [b]
    assert [e["id"] for e in effects.history(trace="tr-0123456789abcdef")] == [b]
    assert [e["id"] for e in effects.history(limit=1)] == [b] and effects.history(limit=0) == []
    assert effects.last("gmail.archive", TARGET)["id"] == a and effects.last("gmail.trash", TARGET) is None


def test_reads_never_raise_on_bad_arguments(ledger):
    assert effects.history(limit="many") == [] and effects.history(target="x") == []
    assert effects.last("gmail.archive", {"subject": "hi"}) is None and effects.unfinished("x") == []
    assert effects.latest_undoable("gmail.unarchive", None) == ""


def test_unfinished_lists_intents_with_no_result_at_least_that_old(ledger, clock):
    done, open_ = archive(), effects.begin("gmail.archive", {**TARGET, "thread": "t2"}, TAP)
    effects.finish(done, "done")
    clock.t += 599
    assert effects.unfinished() == []
    clock.t += 1
    assert [e["id"] for e in effects.unfinished()] == [open_] and effects.unfinished(0)[0]["id"] == open_


# --- approval -------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("auth", [
    None, "tap", {}, {"kind": "tap", "ref": "/api/gmail"}, {**TAP, "extra": 1},
    {**TAP, "kind": "decision"}, {**TAP, "kind": "standing"}, {**TAP, "kind": "nonsense"},
    {**TAP, "ref": ""}, {**TAP, "ref": "has spaces"}, {**TAP, "ref": 7}, {**TAP, "at": "yesterday"}, {**TAP, "at": 5},
])
def test_an_effect_without_an_accepted_authorization_is_refused_and_writes_nothing(ledger, auth):
    with pytest.raises(effects.NotAuthorized):
        effects.begin("gmail.archive", TARGET, auth)
    assert not ledger.exists()


def test_an_irreversible_effect_needs_a_decision_not_a_tap(ledger):
    target = {"row": "row:0123456789abcdef", "host": "news.example.com"}
    for auth in (TAP, {**DECISION, "kind": "owner_flag"}):
        with pytest.raises(effects.NotAuthorized):
            effects.begin("mail.unsubscribe", target, auth)
    assert effects.begin("mail.unsubscribe", target, DECISION).startswith("ef-")


def test_a_time_with_z_or_no_zone_is_accepted(ledger):
    for n, at in enumerate(("2026-11-02T10:00:00Z", "2026-11-02T10:00:00")):
        archive(target={"thread": f"t{n}"}, auth={**TAP, "at": at})
    assert {r["auth"]["at"] for r in rows(ledger)} == {"2026-11-02T10:00:00Z"}


def test_an_unknown_class_trace_component_or_undoes_is_a_value_error(ledger):
    for bad in ("nope", None, ["gmail.archive"]):
        with pytest.raises(ValueError):
            effects.begin(bad, TARGET, TAP)
    for kw in ({"trace": "tr-nothex"}, {"trace": 5}, {"component": "has space"}, {"component": 5}, {"undoes": "x"},
               {"undoes": ["ef-0123456789abcdef"]}):
        with pytest.raises(ValueError):
            archive(**kw)
    assert not ledger.exists()


# --- what a target may hold -----------------------------------------------------------------------------------

@pytest.mark.parametrize("target", [
    None, "t1", {}, [], {"subject": "Your invoice"}, {"thread": "has space"}, {"thread": 5}, {"account": "Ana Lee"},
    {"account": "two@signs@example.com"}, {"account": "tab\t@example.com"}, {"account": "a@" + "x" * 300},
    {"account": "me@example.com", "sender": "Ana"}, {"host": ""}, {"host": 5},
    {"row": "https://mail.google.com/x"}, {"msgs": "m1"}, {"msgs": []}, {"msgs": ["a b"]},
    {"msgs": [5]}, {"session": "my session"}, {"task": "td-x"},
    {"channel": "C1"}, {"ts": "1.2"}, {k: "t1" for k in "abcdefghij"},
])
def test_a_target_that_isnt_a_pointer_is_refused(ledger, target):
    with pytest.raises(ValueError):
        effects.begin("gmail.archive", target, TAP)
    assert not ledger.exists()


def test_each_kind_of_pointer_is_accepted_and_message_ids_are_kept_sorted(ledger):
    target = {"account": "me@example.com", "thread": "t-1_x", "msgs": ["m2", "m1", "m2"], "row": "row:0123456789abcdef",
              "host": "news.example.com", "session": "workspace:12", "task": "td-041", "channel": "C0123456789",
              "ts": "1700000000.000100"}
    archive(target=target)
    assert rows(ledger)[0]["target"] == {**target, "msgs": ["m1", "m2"]}
    assert effects.last("gmail.archive", {**target, "msgs": ["m1", "m2"]}) is not None


def test_every_string_is_scrubbed_on_its_way_to_the_file(ledger):
    archive(auth={**TAP, "ref": TOKEN}, component="work-queue")
    assert "AbCdEfGhIjKlMnOpQrStUv0123456789" not in ledger.read_text()
    assert rows(ledger)[0]["auth"]["ref"] == "xoxb-<redacted>"


def test_an_error_is_a_class_of_error_never_a_servers_message(ledger):
    cases = {"http 404": "http 404", "timeout": "timeout", "tool failed": "tool failed", "http 99": "other",
             "Gmail said: quota exceeded for user ana@example.com": "other", TOKEN: "other", "": "", None: "",
             "x" * 500: "other", "http 404 " + "x" * 500: "other"}
    for given, kept in cases.items():
        eid = archive(target={"thread": "t" + str(abs(hash(str(given))))[:8]})
        assert effects.finish(eid, "failed", error=given)
        assert rows(ledger)[-1]["error"] == kept, given
    assert "ana@example.com" not in ledger.read_text() and "AbCdEf" not in ledger.read_text()


def test_verified_is_true_false_or_unknown(ledger):
    for given, kept in ((True, True), (False, False), (None, None), ("yes", None)):
        eid = archive(target={"thread": f"t{len(rows(ledger)) if ledger.exists() else 0}"})
        effects.finish(eid, "done", verified=given)
        assert rows(ledger)[-1]["verified"] is kept


# --- repeat protection ----------------------------------------------------------------------------------------

def test_a_second_begin_while_the_first_is_unfinished_is_a_duplicate(ledger):
    archive()
    with pytest.raises(effects.Duplicate) as caught:
        archive()
    assert caught.value.done is False and len(rows(ledger)) == 1
    other = effects.begin("gmail.archive", {**TARGET, "thread": "t2"}, TAP)
    assert other and effects.begin("gmail.mark_read", TARGET, TAP)


def test_an_intent_older_than_ten_minutes_with_no_result_no_longer_blocks(ledger, clock):
    archive()
    clock.t += 599
    with pytest.raises(effects.Duplicate):
        archive()
    clock.t += 1
    assert archive().startswith("ef-")


def test_a_reversible_effect_can_repeat_after_it_finishes(ledger):
    for _ in range(3):
        effects.finish(archive(), "done")
    assert len(effects.history("gmail.archive")) == 3


def test_an_irreversible_effect_that_succeeded_is_not_run_again_unless_re_approved(ledger, clock):
    target = {"row": "row:0123456789abcdef", "host": "news.example.com"}
    first = effects.begin("mail.unsubscribe", target, DECISION)
    effects.finish(first, "failed", error="http 500")
    clock.t += 1
    second = effects.begin("mail.unsubscribe", target, DECISION)  # after a failure it may run again
    effects.finish(second, "done", verified=True)
    for auth in (DECISION, {**DECISION, "at": "2026-11-02T10:00:10Z"}):  # not newer than the result
        with pytest.raises(effects.Duplicate) as caught:
            effects.begin("mail.unsubscribe", target, auth)
        assert caught.value.done is True
    clock.t += 1
    assert effects.begin("mail.unsubscribe", target, {**DECISION, "at": "2026-11-02T10:00:12Z"}).startswith("ef-")


# --- undo linkage ---------------------------------------------------------------------------------------------

def test_archive_undo_archive_again_all_run_and_are_linked(ledger):
    a1 = archive()
    effects.finish(a1, "done")
    assert effects.latest_undoable("gmail.unarchive", TARGET) == a1
    u1 = effects.begin("gmail.unarchive", TARGET, TAP, undoes=a1)
    effects.finish(u1, "done")
    assert effects.latest_undoable("gmail.unarchive", TARGET) == ""
    a2 = archive()
    effects.finish(a2, "done")
    assert effects.latest_undoable("gmail.unarchive", TARGET) == a2
    assert [e["undoes"] for e in reversed(effects.history())] == ["", a1, ""]


def test_every_way_an_undo_can_be_invalid_is_refused(ledger):
    done = archive()
    effects.finish(done, "done")
    failed = effects.begin("gmail.archive", {**TARGET, "thread": "t2"}, TAP)
    effects.finish(failed, "failed")
    unfinished = effects.begin("gmail.archive", {**TARGET, "thread": "t3"}, TAP)
    read = effects.begin("gmail.mark_read", {**TARGET, "thread": "t4"}, TAP)
    effects.finish(read, "done")
    unsub_target = {"row": "row:0123456789abcdef", "host": "news.example.com"}
    unsub = effects.begin("mail.unsubscribe", unsub_target, DECISION)
    effects.finish(unsub, "done")
    cases = [("gmail.unarchive", TARGET, "ef-0000000000000000"),              # no such effect
             ("gmail.unarchive", {**TARGET, "thread": "t2"}, failed),         # it didn't succeed
             ("gmail.unarchive", {**TARGET, "thread": "t3"}, unfinished),     # it hasn't finished
             ("gmail.unarchive", {**TARGET, "thread": "t4"}, read),           # another class's undo
             ("gmail.trash", TARGET, done),                                   # not this class's undo
             ("gmail.unarchive", {**TARGET, "thread": "t9"}, done)]           # another target
    for cls, target, undoes in cases:
        auth = DECISION if cls == "gmail.trash" else TAP
        with pytest.raises(effects.NotAuthorized):
            effects.begin(cls, target, auth, undoes=undoes)
    # An effect that can't be undone has no undo class to name it.
    with pytest.raises(effects.NotAuthorized):
        effects.begin("gmail.unarchive", TARGET, TAP, undoes=unsub)
    effects.finish(effects.begin("gmail.unarchive", TARGET, TAP, undoes=done), "done")
    with pytest.raises(effects.NotAuthorized):  # already undone
        effects.begin("gmail.unarchive", TARGET, TAP, undoes=done)


def test_an_undo_that_failed_leaves_the_effect_undoable_and_one_in_flight_blocks_a_second(ledger, clock):
    a1 = archive()
    effects.finish(a1, "done")
    u1 = effects.begin("gmail.unarchive", TARGET, TAP, undoes=a1)
    with pytest.raises(effects.Duplicate):
        effects.begin("gmail.unarchive", TARGET, TAP, undoes=a1)
    effects.finish(u1, "failed", error="tool failed")
    assert effects.latest_undoable("gmail.unarchive", TARGET) == a1
    u2 = effects.begin("gmail.unarchive", TARGET, TAP, undoes=a1)
    assert effects.latest_undoable("gmail.unarchive", TARGET) == ""  # an undo in flight counts as undone
    clock.t += 700  # u2 never finished and is now too old to count
    assert effects.latest_undoable("gmail.unarchive", TARGET) == a1
    assert u2 and effects.begin("gmail.unarchive", TARGET, TAP, undoes=a1)


def test_a_ledger_row_of_a_class_no_longer_registered_is_never_undone(ledger):
    ledger.parent.mkdir(parents=True)
    line = {"v": 1, "kind": "intent", "id": "ef-0123456789abcdef", "class": "gone.class", "target": TARGET, "auth": {}, "t": 1}
    ledger.write_text(json.dumps(line) + "\n" + json.dumps({"kind": "result", "id": "ef-0123456789abcdef", "outcome": "done"}) + "\n")
    with pytest.raises(effects.NotAuthorized):
        effects.begin("gmail.unarchive", TARGET, TAP, undoes="ef-0123456789abcdef")
    assert effects.latest_undoable("gmail.unarchive", TARGET) == ""


# --- no record, no action -------------------------------------------------------------------------------------

def test_an_intent_that_cant_be_written_raises_unrecorded(home, clock):
    (home / ".assistant").mkdir()
    (home / ".assistant/effects").write_text("a file where the folder should be")
    with pytest.raises(effects.Unrecorded):
        archive()
    assert effects.history() == [] and effects.unfinished() == []


def test_an_append_that_fails_or_a_flush_that_fails_raises_unrecorded(ledger, monkeypatch):
    def boom(*a):
        raise OSError(28, "No space left on device")
    for seam in ("_write", "_fsync"):
        monkeypatch.setattr(effects, seam, boom)
        with pytest.raises(effects.Unrecorded) as caught:
            archive()
        assert "No space" not in str(caught.value)
        monkeypatch.undo()
        monkeypatch.setenv("HOME", str(ledger.parents[2]))
        monkeypatch.setattr(effects, "_now", lambda: T0)
        ledger.unlink(missing_ok=True)


def test_a_result_that_cant_be_written_returns_false_and_says_so_once(ledger, monkeypatch, capsys):
    eid = archive()

    def boom(*a):
        raise OSError(5, "I/O error")
    monkeypatch.setattr(effects, "_fsync", boom)
    assert effects.finish(eid, "done") is False
    err = capsys.readouterr().err
    assert err.count("\n") == 1 and eid in err and "I/O" not in err


@pytest.mark.parametrize("args", [("ef-0000000000000000", "done"), (None, "done"), ("x", "maybe"), ([], "done")])
def test_finish_with_an_unknown_effect_or_outcome_returns_false(ledger, capsys, args):
    archive()
    assert effects.finish(*args) is False and capsys.readouterr().err.startswith("effects: couldn't record")
    assert len(rows(ledger)) == 1


def test_a_second_result_for_one_effect_is_refused(ledger):
    eid = archive()
    assert effects.finish(eid, "failed", error="timeout") and effects.finish(eid, "done") is False
    assert effects.history()[0]["outcome"] == "failed"


def test_a_write_that_takes_several_calls_still_lands_whole(ledger, monkeypatch):
    real = os.write
    monkeypatch.setattr(effects, "_write", lambda fd, data: real(fd, bytes(data[:7])))
    eid = archive()
    assert rows(ledger)[0]["id"] == eid


# --- append-only ----------------------------------------------------------------------------------------------

def test_the_file_only_ever_grows_and_earlier_bytes_never_change(ledger, clock):
    seen = b""
    target = {"row": "row:0123456789abcdef", "host": "news.example.com"}
    for step in range(6):
        clock.t += 1
        eid = effects.begin("gmail.archive", {**TARGET, "thread": f"t{step}"}, TAP)
        effects.finish(eid, "done" if step % 2 else "failed", error="" if step % 2 else "http 500")
        effects.history(), effects.unfinished(0)
        if step == 3:
            effects.begin("mail.unsubscribe", target, DECISION)
            effects.finish("ef-0000000000000000", "done")  # refused: writes nothing
        now = ledger.read_bytes()
        assert now.startswith(seen) and len(now) > len(seen)
        seen = now


def test_no_code_in_the_module_can_rewrite_or_delete_a_row():
    text = SOURCE.read_text()
    tree = ast.parse(text)
    for banned in ("truncate", "unlink", "os.remove", "os.rename", "os.replace", "shutil", "write_text", "write_bytes",
                   "rmdir", "seek("):
        assert banned not in text, banned
    opens = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and ast.unparse(n.func) in ("open", "os.open")]
    for call in opens:
        shown = ast.unparse(call)
        if call.func.id if isinstance(call.func, ast.Name) else False:
            raise AssertionError(shown)  # nothing opens the ledger with open()
        else:
            # os.open: the ledger in append mode, its lock, or the ledger only to read it
            assert "O_APPEND" in shown or "_lock_path" in shown or "O_RDONLY" in shown, shown


# --- robustness -----------------------------------------------------------------------------------------------

def test_reads_on_a_missing_folder_and_an_empty_file_are_empty(home, clock):
    assert effects.history() == [] and effects.unfinished() == [] and effects.last("gmail.archive", TARGET) is None
    d = home / ".assistant/effects"
    d.mkdir(parents=True)
    (d / "effects.jsonl").write_bytes(b"")
    assert effects.history() == []
    assert archive().startswith("ef-")


def test_corrupt_lines_are_skipped_and_never_stop_a_read_or_a_write(ledger, clock):
    eid = archive()
    good = ledger.read_bytes()
    junk = [b"not json\n", b"\xff\xfe\n", b"[1, 2]\n", b"5\n", b'{"id": 5}\n', b"\n", b"[" * 100000 + b"\n",
            json.dumps({"kind": "intent", "id": "ef-1111111111111111", "class": 5, "target": {}, "auth": {}}).encode() + b"\n",
            json.dumps({"kind": "result", "id": "ef-2222222222222222", "outcome": "done"}).encode() + b"\n",
            json.dumps({"kind": "result", "id": eid, "outcome": "maybe"}).encode() + b"\n"]
    with open(ledger, "ab") as f:
        f.write(b"".join(junk))
    assert [e["id"] for e in effects.history()] == [eid] and effects.history()[0]["outcome"] is None
    assert effects.finish(eid, "done")
    assert effects.history()[0]["outcome"] == "done"
    assert ledger.read_bytes().startswith(good + b"".join(junk))


def test_a_torn_last_line_is_left_alone_and_the_next_row_starts_on_its_own_line(ledger):
    archive()
    with open(ledger, "ab") as f:
        f.write(b'{"v":1,"kind":"inte')
    before = ledger.read_bytes()
    eid = effects.begin("gmail.archive", {**TARGET, "thread": "t2"}, TAP)
    assert ledger.read_bytes().startswith(before) and [e["id"] for e in effects.history()][0] == eid
    assert len(effects.history()) == 2


def test_an_intent_written_twice_under_one_id_counts_once(ledger):
    eid = archive()
    with open(ledger, "ab") as f:
        f.write(ledger.read_bytes().splitlines()[0] + b"\n")
    assert len(effects.history()) == 1 and effects.history()[0]["id"] == eid


def test_hand_edited_rows_with_odd_field_types_still_read(ledger):
    ledger.parent.mkdir(parents=True)
    ledger.write_text(json.dumps({"kind": "intent", "id": "ef-0123456789abcdef", "class": "gmail.archive", "target": {},
                                  "auth": {}, "t": True, "trace": 5}) + "\n")
    got = effects.history()[0]
    assert (got["t"], got["trace"]) == (0.0, "")


# --- permissions ----------------------------------------------------------------------------------------------

def test_the_folder_is_owner_only_and_so_are_the_ledger_and_its_lock(ledger):
    archive()
    mode = lambda p: stat.S_IMODE(p.stat().st_mode)  # noqa: E731
    assert mode(ledger.parent) == 0o700 and mode(ledger) == 0o600 and mode(ledger.parent / "effects.lock") == 0o600


def test_wider_permissions_on_an_existing_folder_and_files_are_tightened(ledger):
    ledger.parent.mkdir(parents=True)
    ledger.write_text("")
    (ledger.parent / "effects.lock").write_text("")
    for p in (ledger.parent, ledger, ledger.parent / "effects.lock"):
        p.chmod(0o755)
    archive()
    assert stat.S_IMODE(ledger.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(ledger.stat().st_mode) == stat.S_IMODE((ledger.parent / "effects.lock").stat().st_mode) == 0o600


# --- concurrency ----------------------------------------------------------------------------------------------

WORKER = '''
import sys
from assistant import effects
who = sys.argv[1]
for n in range(100):
    eid = effects.begin("gmail.archive", {"account": "me@example.com", "thread": f"{who}x{n}"},
                        {"kind": "tap", "ref": "/api/gmail", "at": "2026-11-02T10:00:00Z"})
    assert effects.finish(eid, "done", verified=True)
'''


def test_four_processes_each_recording_100_effects_leave_800_whole_lines(home):
    env = {**os.environ, "HOME": str(home), "PYTHONPATH": str(REPO / "src")}
    procs = [subprocess.Popen([sys.executable, "-c", WORKER, name], env=env, stderr=subprocess.PIPE) for name in "abcd"]
    for p in procs:
        assert p.wait(120) == 0, p.stderr.read()
    lines = (home / ".assistant/effects/effects.jsonl").read_bytes().split(b"\n")
    assert lines[-1] == b"" and len(lines) == 801
    parsed = [json.loads(line) for line in lines[:-1]]
    assert sorted(r["kind"] for r in parsed).count("intent") == 400
    assert len({e["id"] for e in effects.history(limit=1000)}) == 400
    assert all(e["outcome"] == "done" for e in effects.history(limit=1000))


def test_two_begins_for_one_target_at_the_same_moment_record_one_intent(ledger, monkeypatch):
    import threading
    import time
    real = effects._sync

    def slow_read():
        found = real()
        time.sleep(0.2)  # long enough that, with no lock, every thread has read before any has written
        return found
    monkeypatch.setattr(effects, "_sync", slow_read)
    results = []

    def go():
        try:
            results.append(archive())
        except effects.Duplicate:
            results.append("duplicate")
    threads = [threading.Thread(target=go) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert sorted(r == "duplicate" for r in results) == [False, True, True, True] and len(rows(ledger)) == 1


# --- one stored form of a target ------------------------------------------------------------------------------

UNSUB = "mail.unsubscribe"


@pytest.mark.parametrize("host, secret", [("ya29.unsubscribe-service-mailer.com", "unsubscribe-service"),
                                         ("xoxb-abcdef12.example.com", "abcdef12")])
def test_a_host_that_looks_like_a_token_is_still_the_same_target_after_it_is_scrubbed(ledger, host, secret):
    target = {"row": "row:0123456789abcdef", "host": host}
    effects.finish(effects.begin(UNSUB, target, DECISION), "done", verified=True)
    assert secret not in ledger.read_text()  # the token-shaped part is cut on its way to the file
    assert effects.last(UNSUB, target) is not None
    with pytest.raises(effects.Duplicate) as caught:
        effects.begin(UNSUB, target, DECISION)
    assert caught.value.done is True and len(rows(ledger)) == 2


def test_the_target_stored_is_the_target_compared(ledger):
    stored = effects.clean_target({"row": "row:0123456789abcdef", "host": "ya29.unsubscribe-service-mailer.com"})
    assert stored["host"] == "ya29.<redacted>"
    effects.begin(UNSUB, stored, DECISION)  # the stored form is itself a target, and is the same one
    with pytest.raises(effects.Duplicate):
        effects.begin(UNSUB, {"row": "row:0123456789abcdef", "host": "ya29.unsubscribe-service-mailer.com"}, DECISION)


@pytest.mark.parametrize("given, kept", [
    ("example.com.", "example.com"), ("EXAMPLE.com", "example.com"), ("news_mailer.example.com", "news_mailer.example.com"),
    ("münchen.example", "xn--mnchen-3ya.example"), ("2001:db8::1", "2001:db8::1"),
    ("a..example.com", "a..example.com"),  # not a legal name, but already plain ASCII, so left as it is
])
def test_a_host_is_recorded_in_one_normal_form(given, kept):
    assert effects.clean_target({"host": given}) == {"host": kept}


def test_a_host_that_still_doesnt_fit_is_recorded_as_a_digest_of_itself():
    odd = effects.clean_target({"host": "https://x.example.com/unsub?token=abc"})["host"]
    assert re.fullmatch(r"h:[0-9a-f]{16}", odd) and "unsub" not in odd
    assert odd == effects.clean_target({"host": "https://x.example.com/unsub?token=abc"})["host"]
    assert odd != effects.clean_target({"host": "https://y.example.com/unsub?token=abc"})["host"]


def test_the_same_host_with_and_without_a_trailing_dot_is_one_target(ledger):
    effects.finish(effects.begin(UNSUB, {"row": "row:0123456789abcdef", "host": "example.com"}, DECISION), "done")
    with pytest.raises(effects.Duplicate):
        effects.begin(UNSUB, {"row": "row:0123456789abcdef", "host": "example.com."}, DECISION)


def test_a_conversation_with_more_than_100_messages_is_recorded_as_a_count_and_a_digest(ledger):
    ids = [f"m{n}" for n in range(250)]
    target = {"account": "me@example.com", "msgs": ids, "row": "row:0123456789abcdef"}
    eid = effects.begin("gmail.trash", target, DECISION)
    stored = rows(ledger)[0]["target"]
    assert stored["msg_count"] == "250" and re.fullmatch(r"[0-9a-f]{16}", stored["msg_digest"]) and "msgs" not in stored
    assert "m17" not in ledger.read_text()
    effects.finish(eid, "done")
    assert effects.last("gmail.trash", {**target, "msgs": list(reversed(ids))})["id"] == eid
    assert effects.last("gmail.trash", {**target, "msgs": ids[:-1]}) is None  # a different set of ids
    assert effects.last("gmail.trash", stored)["id"] == eid  # the stored form is itself a target
    exactly = effects.clean_target({"msgs": ids[:100]})
    assert exactly == {"msgs": sorted(ids[:100])}


@pytest.mark.parametrize("account", ["o'brien@example.com", "ana+news@example.co.uk", "first.last@sub.example.com", "x@y"])
def test_any_legal_looking_gmail_address_is_a_target(account):
    assert effects.clean_target({"account": account}) == {"account": account}


def test_a_target_value_that_cant_be_described_gives_a_message_with_no_content():
    with pytest.raises(ValueError) as caught:
        effects.clean_target({"thread": "secret thing here"})
    assert "secret" not in str(caught.value)
    with pytest.raises(ValueError) as caught:
        effects.clean_target({"secret key": "x"})
    assert "secret" not in str(caught.value)


# --- approval dates -------------------------------------------------------------------------------------------

def test_an_approval_dated_in_the_future_is_refused_so_it_cant_outrank_every_result(ledger, clock):
    target = {"row": "row:0123456789abcdef", "host": "news.example.com"}
    with pytest.raises(effects.NotAuthorized):
        effects.begin(UNSUB, target, {**DECISION, "at": "2099-01-01T00:00:00Z"})
    assert not ledger.exists()
    for ahead, ok in ((59, True), (61, False)):
        at = datetime.fromtimestamp(clock.t + ahead, timezone.utc).isoformat()
        if ok:
            effects.finish(effects.begin(UNSUB, target, {**DECISION, "at": at}), "failed")
        else:
            with pytest.raises(effects.NotAuthorized):
                effects.begin(UNSUB, target, {**DECISION, "at": at})


@pytest.mark.parametrize("at", ["9999-99-99", "2026-11-02T25:00:00Z", "0000-00-00T00:00:00", "2026"])
def test_a_time_that_cant_be_read_is_not_authorized_never_a_value_error(ledger, at):
    with pytest.raises(effects.NotAuthorized):
        archive(auth={**TAP, "at": at})


# --- a time the file can't be trusted on ----------------------------------------------------------------------

def put(ledger, *lines):
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with open(ledger, "ab") as f:
        for line in lines:
            f.write((line if isinstance(line, str) else json.dumps(line)).encode() + b"\n")


def intent_line(eid, t, thread="t1"):
    return ('{"v":1,"kind":"intent","id":"%s","at":"x","t":%s,"class":"gmail.archive","target":'
            '{"account":"me@example.com","thread":"%s"},"auth":{},"trace":"","component":"","undoes":""}' % (eid, t, thread))


@pytest.mark.parametrize("t", ["1e999", "-1e999", "NaN", "9" * 400, "1e12", "-5", '"soon"', "null", "true"])
def test_an_intent_with_a_time_that_cant_be_trusted_neither_blocks_nor_hides(ledger, t):
    put(ledger, intent_line("ef-0123456789abcdef", t))
    assert [e["id"] for e in effects.unfinished()] == ["ef-0123456789abcdef"]  # an outcome nobody knows
    assert archive().startswith("ef-")  # and it doesn't hold the action off


def test_one_huge_time_never_stops_a_write_or_a_read(ledger):
    put(ledger, intent_line("ef-0123456789abcdef", "9" * 400), intent_line("ef-1123456789abcdef", "1e999", "t2"))
    assert len(effects.history()) == 2
    assert effects.finish(archive(), "done") and len(effects.history()) == 3


# --- an intent that never got on disk -------------------------------------------------------------------------

def test_a_flush_that_fails_closes_the_intent_so_a_retry_isnt_held_off(ledger, monkeypatch):
    real, calls = effects._fsync, []

    def flaky(fd):
        calls.append(fd)
        if len(calls) == 1:
            raise OSError(5, "I/O error")
        real(fd)
    monkeypatch.setattr(effects, "_fsync", flaky)
    with pytest.raises(effects.Unrecorded):
        archive()
    first, second = rows(ledger)
    assert (first["kind"], second["kind"], second["outcome"], second["error"]) == ("intent", "result", "failed", "not recorded")
    assert effects.unfinished(0) == [] and archive().startswith("ef-")


def test_a_flush_that_keeps_failing_still_raises_unrecorded_and_nothing_else(ledger, monkeypatch):
    def boom(fd):
        raise OSError(5, "I/O error")
    monkeypatch.setattr(effects, "_fsync", boom)
    with pytest.raises(effects.Unrecorded):
        archive()
    assert [r["kind"] for r in rows(ledger)] == ["intent", "result"]  # both landed; neither was flushed


def test_a_write_that_fails_leaves_nothing_that_blocks_a_retry(ledger, monkeypatch):
    real, calls = effects._write, []

    def flaky(fd, data):
        calls.append(fd)
        if len(calls) == 1:
            raise OSError(28, "No space left on device")
        return real(fd, data)
    monkeypatch.setattr(effects, "_write", flaky)
    with pytest.raises(effects.Unrecorded):
        archive()
    assert archive().startswith("ef-")


@pytest.mark.parametrize("block", [64, 300, 700, 5000])
def test_a_ledger_read_in_small_blocks_is_the_same_as_one_read_whole(ledger, monkeypatch, block):
    for n in range(12):
        effects.finish(archive(target={"thread": f"t{n}"}), "done")
    with open(ledger, "ab") as f:
        f.write(b'{"v":1,"kind":"inte')  # a line not yet whole at the end
    whole = full()
    monkeypatch.setattr(effects, "READ_BLOCK", block)
    assert full() == whole and len(whole) == 12


def test_the_ledgers_clock_is_the_systems():
    import time
    assert abs(effects._now() - time.time()) < 5


# --- the registry can't be changed while running --------------------------------------------------------------

def test_the_registry_is_read_only():
    with pytest.raises(TypeError):
        effects.CLASSES["mail.unsubscribe"] = effects.EffectClass(True, "", ("tap",))
    with pytest.raises(TypeError):
        del effects.CLASSES["mail.unsubscribe"]
    with pytest.raises(Exception):
        effects.CLASSES["mail.unsubscribe"].accepts = ("tap",)


# --- reading only what was added ------------------------------------------------------------------------------

def full():
    """Every effect, from a fresh read of the whole file."""
    effects._forget()
    return effects.history(limit=10 ** 6)


def test_a_file_that_cant_be_read_part_way_reads_as_empty_and_is_read_again_later(ledger, monkeypatch):
    archive()
    assert len(effects.history()) == 1

    def boom(*a):
        raise OSError(5, "I/O error")
    monkeypatch.setattr(os, "pread", boom)
    assert effects.history() == []
    monkeypatch.undo()
    monkeypatch.setenv("HOME", str(ledger.parents[2]))
    assert len(effects.history()) == 1


def test_a_second_call_reads_only_the_rows_added_since_the_first(ledger, monkeypatch):
    for n in range(20):
        effects.finish(archive(target={"thread": f"t{n}"}), "done")
    assert len(effects.history()) == 20
    seen = []
    real = effects._State.ingest
    monkeypatch.setattr(effects._State, "ingest", lambda self, raw, where: (seen.append(raw), real(self, raw, where))[1])
    effects.finish(archive(target={"thread": "new"}), "done")
    assert len(seen) <= 4, "begin and finish read their own two rows, not the whole file"
    assert [e["target"]["thread"] for e in effects.history(limit=1)] == ["new"]


def test_what_a_long_lived_reader_sees_equals_a_fresh_read_while_other_processes_append(home):
    env = {**os.environ, "HOME": str(home), "PYTHONPATH": str(REPO / "src")}
    script = WORKER.replace("range(100)", "range(40)")
    procs = [subprocess.Popen([sys.executable, "-c", script, name], env=env, stderr=subprocess.PIPE) for name in "abc"]
    mid = []
    while any(p.poll() is None for p in procs):
        seen = effects.history(limit=10 ** 6)
        mid.append(len(seen))
        later = {e["id"]: e for e in full()}
        assert {e["id"] for e in seen} <= set(later)
    for p in procs:
        assert p.wait(60) == 0, p.stderr.read()
    incremental = effects.history(limit=10 ** 6)
    assert incremental == full() and len(incremental) == 120 and all(e["outcome"] == "done" for e in incremental)
    assert len(set(mid)) > 1, "the reader saw the file grow"


def test_a_ledger_deleted_and_made_again_is_read_again_even_at_the_same_size(ledger, clock):
    first = archive()
    effects.finish(first, "done")
    assert [e["id"] for e in effects.history()] == [first]
    old = ledger.read_bytes()
    ledger.unlink()
    assert effects.history() == []
    ledger.write_bytes(old.replace(first.encode(), b"ef-ffffffffffffffff"))  # same length, new content
    assert [e["id"] for e in effects.history()] == ["ef-ffffffffffffffff"]


def test_a_ledger_replaced_by_a_shorter_one_is_read_again(ledger):
    for n in range(3):
        effects.finish(archive(target={"thread": f"t{n}"}), "done")
    assert len(effects.history()) == 3
    ledger.write_text(ledger.read_text().splitlines()[0] + "\n")
    assert len(effects.history()) == 1 and effects.history() == full()


def test_a_ledger_replaced_by_a_longer_one_with_other_content_is_read_again(ledger):
    first = archive()
    effects.finish(first, "done")
    effects.history()
    other = ledger.read_text().replace(first, "ef-eeeeeeeeeeeeeeee")
    ledger.write_text(other + other.replace("ef-eeeeeeeeeeeeeeee", "ef-dddddddddddddddd"))
    assert [e["id"] for e in effects.history()] == ["ef-dddddddddddddddd", "ef-eeeeeeeeeeeeeeee"]
    assert effects.history() == full()


def test_a_row_still_being_written_is_read_once_it_is_whole(ledger):
    eid = archive()
    line = ledger.read_bytes()
    extra = intent_line("ef-aaaaaaaaaaaaaaaa", T0, "t2").encode()
    with open(ledger, "ab") as f:
        f.write(extra[:30])
    assert [e["id"] for e in effects.history()] == [eid]
    with open(ledger, "ab") as f:
        f.write(extra[30:] + b"\n")
    assert [e["id"] for e in effects.history()][0] == "ef-aaaaaaaaaaaaaaaa" and effects.history() == full()
    assert ledger.read_bytes().startswith(line)


def test_a_torn_line_completed_by_the_next_append_is_the_same_as_a_fresh_read(ledger):
    archive()
    with open(ledger, "ab") as f:
        f.write(b'{"v":1,"kind":"inte')
    assert len(effects.history()) == 1
    eid = effects.begin("gmail.archive", {**TARGET, "thread": "t2"}, TAP)
    assert [e["id"] for e in effects.history()][0] == eid and effects.history() == full()


def test_several_threads_reading_and_writing_never_see_a_half_built_state(ledger):
    import threading
    errors = []

    def writer(name):
        try:
            for n in range(15):
                effects.finish(effects.begin("gmail.archive", {"thread": f"{name}{n}"}, TAP), "done")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    def reader():
        try:
            for _ in range(40):
                effects.unfinished(0), effects.history(limit=50)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
    threads = [threading.Thread(target=writer, args=(n,)) for n in "ab"] + [threading.Thread(target=reader) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert errors == [] and len(effects.history(limit=1000)) == 30 and effects.history(limit=1000) == full()


def test_what_a_caller_changes_in_a_result_never_changes_the_ledger_state(ledger):
    eid = archive()
    effects.history()[0]["target"]["thread"] = "changed"
    effects.unfinished(0)[0]["authorized_by"]["kind"] = "changed"
    assert effects.history()[0]["target"] == TARGET and effects.last("gmail.archive", TARGET)["id"] == eid


RACE = '''
import sys, time
from assistant import effects
real = effects._sync
def slow():
    found = real()
    time.sleep(0.6)  # long enough that, with no lock, every process has read before any has written
    return found
effects._sync = slow
try:
    effects.begin("gmail.archive", {"thread": "t1"}, {"kind": "tap", "ref": "/api/gmail", "at": "2026-11-02T10:00:00Z"})
    print("recorded")
except effects.Duplicate:
    print("duplicate")
'''


def test_two_processes_beginning_one_target_at_the_same_moment_record_one_intent(home):
    env = {**os.environ, "HOME": str(home), "PYTHONPATH": str(REPO / "src")}
    procs = [subprocess.Popen([sys.executable, "-c", RACE], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True) for _ in range(4)]
    said = []
    for p in procs:
        out, err = p.communicate(timeout=60)
        assert p.returncode == 0, err
        said.append(out.strip())
    assert sorted(said) == ["duplicate"] * 3 + ["recorded"]
    assert len((home / ".assistant/effects/effects.jsonl").read_text().splitlines()) == 1


# --- the module loads on the system Python --------------------------------------------------------------------

def test_the_module_parses_as_python_3_9_and_imports_only_the_standard_library_and_assistant():
    tree = ast.parse(SOURCE.read_text(), feature_version=(3, 9))
    imported = {name.name for node in ast.walk(tree) if isinstance(node, ast.Import) for name in node.names} | \
               {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert imported == {"__future__", "contextlib", "fcntl", "functools", "hashlib", "json", "math", "os", "re",
                        "secrets", "sys", "threading", "time", "types", "dataclasses", "datetime", "pathlib", "assistant",
                        "assistant.redact"}


@pytest.mark.skipif(not Path("/usr/bin/python3").exists(), reason="no system Python on this machine")
def test_the_systems_own_python_can_record_an_effect(tmp_path):
    code = ("from assistant import effects as e; i = e.begin('gmail.archive', {'thread': 't1'}, "
            "{'kind': 'tap', 'ref': '/api/gmail', 'at': '2026-11-02T10:00:00Z'}); print(e.finish(i, 'done'))")
    out = subprocess.run(["/usr/bin/python3", "-c", code], capture_output=True, text=True,
                         env={"PYTHONPATH": str(REPO / "src"), "HOME": str(tmp_path)})
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "True"
