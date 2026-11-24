"""The feedback store (src/assistant/feedback.py): one append-only, flushed, content-free record of what the owner said."""
from __future__ import annotations

import ast
import json
import multiprocessing
import os
import stat
import threading
import time
from pathlib import Path

import pytest

from assistant import feedback, pointers, telemetry

REPO = Path(__file__).resolve().parent.parent
LINK = "https://acme.slack.com/archives/C1/p123"
ROW = pointers.row_pointer(LINK)
TRACE = "tr-" + "ab" * 8


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def lines(home) -> list[dict]:
    return [json.loads(x) for x in (home / ".assistant/feedback/feedback.jsonl").read_text().splitlines()]


def down(**kw):
    return feedback.record("thumbs_down", ROW, "row", "work-queue", trace=TRACE, **kw)


def test_a_record_is_one_line_with_a_new_id_and_only_pointers(home):
    one = feedback.record("done", ROW, "row", "work-queue", trace=TRACE, source="slack",
                          who=feedback.who_pointer("  Ana Smith "))
    two = feedback.record("snooze", ROW, "row", "work-queue")
    assert one != two and one.startswith("fb-") and len(one) == 19
    first, second = lines(home)
    assert first["id"] == one and first["ts"].endswith("Z")
    assert {k: first[k] for k in ("signal", "kind", "surface", "subject", "trace")} == \
        {"signal": "done", "kind": "row", "surface": "work-queue", "subject": ROW, "trace": TRACE}
    assert first["context"] == {"source": "slack", "who": feedback.who_pointer("ana smith")}
    assert "Ana" not in json.dumps(first) and "context" not in second and "trace" not in second


def test_the_folder_and_file_are_owner_only(home):
    feedback.record("done", ROW, "row", "work-queue")
    assert stat.S_IMODE((home / ".assistant/feedback").stat().st_mode) == 0o700
    assert stat.S_IMODE((home / ".assistant/feedback/feedback.jsonl").stat().st_mode) == 0o600
    (home / ".assistant/feedback/feedback.jsonl").chmod(0o644)
    feedback.record("done", ROW, "row", "work-queue")
    assert stat.S_IMODE((home / ".assistant/feedback/feedback.jsonl").stat().st_mode) == 0o600


def test_a_sender_pointer_is_a_hash_of_the_lowercased_trimmed_sender_and_empty_for_none():
    import hashlib
    assert feedback.who_pointer(" Ana@Example.com ") == "who:" + hashlib.sha256(b"ana@example.com").hexdigest()[:16]
    assert feedback.who_pointer("") == feedback.who_pointer("  ") == feedback.who_pointer(None) == ""


BAD = [
    {"signal": "like"}, {"signal": 3}, {"kind": "thing"}, {"surface": "tv"}, {"reason": "boring"}, {"reason": 4},
    {"source": "fax"}, {"who": "Ana"}, {"who": 5}, {"subject": LINK}, {"subject": "Ana Smith"}, {"subject": ""},
    {"subject": "td-001"}, {"subject": "row:XYZ"}, {"trace": "my title"}, {"trace": 4}, {"amends": "nope"},
    {"amends": "fb-" + "0" * 16}, {"words": "hello"}, {"words": 5}, {"signal": "tell"},
    {"signal": "retract"}, {"reason": "not_important"}, {"signal": "done", "reason": "not_important"},
    {"signal": "done", "amends": "fb-" + "0" * 16},
    {"signal": "done", "reason": "not_mine", "amends": "fb-" + "0" * 16},
]


@pytest.mark.parametrize("bad", BAD, ids=lambda b: json.dumps(b))
def test_every_malformed_argument_is_refused_and_nothing_is_written(home, bad):
    args = {"signal": "done", "subject": ROW, "kind": "row", "surface": "work-queue", **bad}
    with pytest.raises(ValueError):
        feedback.record(**args)
    assert not (home / ".assistant/feedback/feedback.jsonl").exists() or lines(home) == []


def test_a_title_a_sender_a_link_or_a_message_cannot_stand_in_for_a_subject_or_a_reason(home):
    for words in ("Fix the Q3 budget", "ana@example.com", LINK, "Can you review this today?"):
        for field in ("subject", "reason", "trace", "source", "who", "amends"):
            with pytest.raises(ValueError):
                feedback.record("done", **{"subject": ROW, "kind": "row", "surface": "work-queue", field: words})
    assert not (home / ".assistant/feedback/feedback.jsonl").exists()


def test_a_result_is_about_a_task_and_a_page_note_about_nothing(home):
    feedback.record("kept_result", "td-051", "result", "work-queue")
    feedback.record("tell", "", "page", "work-queue", words="The order is off")
    with pytest.raises(ValueError):
        feedback.record("tell", ROW, "page", "work-queue", words="x")
    assert [r["subject"] if "subject" in r else "" for r in lines(home)] == ["td-051", ""]


def test_a_reason_and_a_take_back_name_an_earlier_record_about_the_same_subject(home):
    first = down()
    reason = down(reason="not_important", amends=first)
    other = pointers.row_pointer("https://elsewhere.example/x")
    with pytest.raises(ValueError, match="no earlier record"):
        feedback.record("thumbs_down", other, "row", "work-queue", reason="not_mine", amends=first)
    feedback.record("retract", ROW, "row", "work-queue", amends=first)
    assert [r["signal"] for r in lines(home)] == ["thumbs_down", "thumbs_down", "retract"]
    assert lines(home)[1]["amends"] == first and reason != first


def test_words_are_collapsed_cut_and_scrubbed(home):
    secret = "-".join(["xoxb", "1234567890", "abcdefghijklmnop"])
    feedback.record("tell", ROW, "row", "work-queue", words=f"  too   \n  many\tspaces {secret} " + "x" * 3000)
    words = lines(home)[0]["words"]
    assert words.startswith("too many spaces xoxb-<redacted>") and secret not in words
    assert len(" ".join(("a " * 3000).split())[:feedback.WORDS_MAX]) == 2000
    feedback.record("tell", ROW, "row", "work-queue", words="a " * 3000)
    assert len(lines(home)[1]["words"]) == feedback.WORDS_MAX


def test_the_unwritable_store_raises_unrecorded_and_a_failed_flush_is_not_a_record(home, monkeypatch):
    def broken(fd):
        raise OSError(5, "disk")
    monkeypatch.setattr(feedback, "_fsync", broken)
    with pytest.raises(feedback.Unrecorded):
        feedback.record("done", ROW, "row", "work-queue")
    monkeypatch.undo()
    monkeypatch.setenv("HOME", str(home))
    import shutil
    shutil.rmtree(home / ".assistant/feedback")
    (home / ".assistant/feedback").write_text("a file where the folder should be")
    with pytest.raises(feedback.Unrecorded):
        feedback.record("done", ROW, "row", "work-queue")


def test_a_record_waits_for_the_flush(home, monkeypatch):
    real, seen = os.fsync, []

    def slow(fd):
        time.sleep(0.2)
        seen.append(1)
        real(fd)
    monkeypatch.setattr(feedback, "_fsync", slow)
    started = time.monotonic()
    feedback.record("done", ROW, "row", "work-queue")
    assert seen and time.monotonic() - started >= 0.2


def test_a_torn_last_line_is_not_glued_to_the_next_record(home):
    feedback.record("done", ROW, "row", "work-queue")
    path = home / ".assistant/feedback/feedback.jsonl"
    path.write_bytes(path.read_bytes() + b'{"id": "fb-00')
    mine = feedback.record("snooze", ROW, "row", "work-queue")
    assert [r["id"] for r in feedback.recent()][0] == mine and len(feedback.recent()) == 2


def append_many(home, n):
    os.environ["HOME"] = home
    for _ in range(n):
        feedback.record("done", ROW, "row", "work-queue")


def test_four_processes_appending_200_records_each_leave_800_whole_lines(home):
    ctx = multiprocessing.get_context("fork")
    procs = [ctx.Process(target=append_many, args=(str(home), 200)) for _ in range(4)]
    [p.start() for p in procs]
    [p.join(120) for p in procs]
    assert all(p.exitcode == 0 for p in procs)
    rows = lines(home)
    assert len(rows) == 800 and len({r["id"] for r in rows}) == 800


def test_recent_filters_orders_newest_first_limits_and_skips_corrupt_lines(home, monkeypatch):
    clock = iter(range(1000, 2000))
    monkeypatch.setattr(feedback, "_now", lambda: next(clock))
    a = feedback.record("done", ROW, "row", "work-queue", trace=TRACE)
    other = pointers.row_pointer("https://other.example/y")
    b = feedback.record("snooze", other, "row", "work-queue")
    path = home / ".assistant/feedback/feedback.jsonl"
    path.write_bytes(path.read_bytes() + b"not json\n[1]\n" + b'{"id":"bad","signal":"x","ts":"t"}\n')
    c = feedback.record("drop", ROW, "row", "work-queue")
    assert [r["id"] for r in feedback.recent()] == [c, b, a]
    assert [r["id"] for r in feedback.recent(subject=ROW)] == [c, a]
    assert [r["id"] for r in feedback.recent(trace=TRACE)] == [a]
    assert [r["id"] for r in feedback.recent(signal="snooze")] == [b]
    assert [r["id"] for r in feedback.recent(since=1001)] == [c, b]
    assert [r["id"] for r in feedback.recent(limit=1)] == [c]


def test_reads_never_raise(home):
    assert feedback.recent() == [] and feedback.latest_for(ROW) is None and feedback.views() == {}
    (home / ".assistant").mkdir()
    (home / ".assistant/feedback").mkdir()
    (home / ".assistant/feedback/feedback.jsonl").mkdir()
    assert feedback.recent() == [] and feedback.latest_for(ROW) is None and feedback.views() == {}


def test_only_the_newest_part_of_a_big_file_is_read(home, monkeypatch):
    monkeypatch.setattr(feedback, "READ_BYTES", 700)
    ids = [feedback.record("done", ROW, "row", "work-queue") for _ in range(10)]
    found = [r["id"] for r in feedback.recent()]
    assert found and found[0] == ids[-1] and len(found) < 10 and found == ids[::-1][:len(found)]


def test_latest_for_honours_take_backs(home):
    assert feedback.latest_for(ROW) is None
    done = feedback.record("done", ROW, "row", "work-queue")
    first = down()
    reason = down(reason="not_mine", amends=first)
    assert feedback.latest_for(ROW)["id"] == reason
    feedback.record("retract", ROW, "row", "work-queue", amends=first)
    assert feedback.latest_for(ROW)["id"] == done, "a taken-back thumbs-down takes its reason with it"
    feedback.record("retract", ROW, "row", "work-queue", amends=done)
    assert feedback.latest_for(ROW) is None


def test_views_say_what_still_stands_for_each_subject(home):
    first = down()
    down(reason="wrong_order", amends=first)
    feedback.record("tell", ROW, "row", "work-queue", words="x" * 100)
    second = pointers.row_pointer("https://other.example/y")
    gone = feedback.record("thumbs_down", second, "row", "work-queue")
    feedback.record("retract", second, "row", "work-queue", amends=gone)
    feedback.record("tell", "", "page", "work-queue", words="Too long a list")
    feedback.record("done", ROW, "row", "work-queue")
    feedback.record("thumbs_down", ROW, "draft", "work-queue")
    assert feedback.views().pop("draft:" + ROW)["down"].startswith("fb-")
    assert {k: v for k, v in feedback.views().items() if k != "draft:" + ROW} == {ROW: {"kind": "row", "down": first, "reason": "wrong_order", "told": "x" * 80 + "…"},
                                "page": {"kind": "page", "down": "", "reason": "", "told": "Too long a list"}}
    again = down()
    assert feedback.views()[ROW]["down"] == again and feedback.views()[ROW]["reason"] == ""


def test_each_record_writes_one_telemetry_record_without_the_words(home):
    feedback.record("tell", ROW, "row", "work-queue", trace=TRACE, words="secret-ish words of mine")
    first = down()
    feedback.record("thumbs_down", ROW, "row", "work-queue", trace=TRACE, reason="not_mine", amends=first)
    got = telemetry.events(kind="feedback")
    assert [e["fields"] for e in got] == [
        {"signal": "tell", "reason_code": "none", "surface": "work-queue", "subject_kind": "row"},
        {"signal": "thumbs_down", "reason_code": "none", "surface": "work-queue", "subject_kind": "row"},
        {"signal": "thumbs_down", "reason_code": "not_mine", "surface": "work-queue", "subject_kind": "row"}]
    assert got[0]["subject"] == ROW and got[0]["trace"] == TRACE and got[0]["component"] == "work-queue"
    assert "words" not in json.dumps(got)


def test_every_surface_files_its_telemetry_under_a_known_component():
    assert set(feedback.COMPONENTS) == set(feedback.SURFACES)
    assert set(feedback.COMPONENTS.values()) <= set(telemetry.COMPONENTS)


def test_the_lists_match_the_telemetry_registry():
    assert feedback.SIGNALS == telemetry.SIGNAL
    assert ("none",) + feedback.REASONS == telemetry.FEEDBACK_REASON
    assert feedback.SURFACES == telemetry.SURFACE
    assert set(telemetry.SUBJECT_KIND) - set(feedback.KINDS) == {"task"}, "telemetry keeps `task` for later"
    assert set(feedback.KINDS) <= set(telemetry.SUBJECT_KIND)
    assert set(feedback.SOURCES) == set(telemetry.SOURCE)
    spec = telemetry.KINDS["feedback"]
    assert (spec["signal"], spec["surface"], spec["subject_kind"], spec["reason_code"]) == \
        (telemetry.SIGNAL, telemetry.SURFACE, telemetry.SUBJECT_KIND, telemetry.FEEDBACK_REASON)
    assert set(feedback.REASONS_FOR) <= set(feedback.KINDS)
    assert {r for rs in feedback.REASONS_FOR.values() for r in rs} <= set(feedback.REASONS)


def test_the_module_parses_as_python_3_9():
    ast.parse((REPO / "src/assistant/feedback.py").read_text(), feature_version=(3, 9))


def test_threads_appending_together_leave_whole_lines(home):
    threads = [threading.Thread(target=lambda: [feedback.record("done", ROW, "row", "work-queue") for _ in range(20)])
               for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(lines(home)) == 80
