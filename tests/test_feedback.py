"""The feedback store (src/assistant/feedback.py): one append-only, flushed, content-free record of what the owner said."""
from __future__ import annotations

import ast
import json
import multiprocessing
import os
import stat
import threading
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


def test_a_sender_pointer_is_a_keyed_hash_of_the_lowercased_trimmed_sender_and_empty_for_none(home):
    import hashlib
    import hmac
    one = feedback.who_pointer(" Ana@Example.com ")
    key = (home / ".assistant/feedback/sender.key").read_bytes()
    assert one == "who:" + hmac.new(key, b"ana@example.com", hashlib.sha256).hexdigest()[:16]
    assert one != "who:" + hashlib.sha256(b"ana@example.com").hexdigest()[:16], "not a plain hash a list of names could match"
    assert one == feedback.who_pointer("ana@example.com")
    assert stat.S_IMODE((home / ".assistant/feedback/sender.key").stat().st_mode) == 0o600
    assert feedback.who_pointer("") == feedback.who_pointer("  ") == feedback.who_pointer(None) == ""


def test_deleting_the_key_makes_old_and_new_sender_pointers_stop_matching_and_a_missing_one_is_made_once(home):
    one = feedback.who_pointer("ana")
    (home / ".assistant/feedback/sender.key").unlink()
    assert feedback.who_pointer("ana") != one
    keys = {(home / ".assistant/feedback/sender.key").read_bytes() for _ in range(3) if feedback.who_pointer("bob")}
    assert len(keys) == 1


def test_a_sender_pointer_is_empty_when_the_key_cannot_be_had_and_never_an_error(home):
    (home / ".assistant").mkdir()
    (home / ".assistant/feedback").write_text("in the way")
    assert feedback.who_pointer("ana") == ""


def test_two_processes_making_the_key_at_once_end_with_one(home):
    ctx = multiprocessing.get_context("fork")
    out = ctx.Queue()

    def make(n):
        os.environ["HOME"] = str(home)
        out.put(feedback.who_pointer("ana"))

    procs = [ctx.Process(target=make, args=(n,)) for n in range(4)]
    [p.start() for p in procs]
    [p.join(60) for p in procs]
    assert len({out.get(timeout=10) for _ in procs}) == 1


BAD = [
    {"signal": "like"}, {"signal": 3}, {"kind": "thing"}, {"surface": "tv"}, {"reason": "boring"}, {"reason": 4},
    {"source": "fax"}, {"who": "Ana"}, {"who": 5}, {"subject": LINK}, {"subject": "Ana Smith"}, {"subject": ""},
    {"subject": "td-001"}, {"subject": "row:XYZ"}, {"trace": "my title"}, {"trace": 4}, {"amends": "nope"},
    {"amends": "fb-" + "0" * 16}, {"words": "hello"}, {"words": 5}, {"signal": "tell"},
    {"signal": "retract"}, {"reason": "not_important"}, {"signal": "done", "reason": "not_important"},
    {"signal": "done", "amends": "fb-" + "0" * 16},
    {"signal": "done", "reason": "not_mine", "amends": "fb-" + "0" * 16}, {"kind": "result"},
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
    with pytest.raises(ValueError):
        feedback.record("kept_result", ROW, "result", "work-queue")
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
    with monkeypatch.context() as broke:
        broke.setattr(feedback, "_fsync", broken)
        with pytest.raises(feedback.Unrecorded):
            feedback.record("done", ROW, "row", "work-queue")
    import shutil
    shutil.rmtree(home / ".assistant/feedback")
    (home / ".assistant/feedback").write_text("a file where the folder should be")
    with pytest.raises(feedback.Unrecorded):
        feedback.record("done", ROW, "row", "work-queue")


def test_a_record_is_written_and_then_flushed_before_record_returns(home, monkeypatch):
    real, seen = os.fsync, []

    def watching(fd):
        seen.append(os.pread(fd, 1000, 0))  # what is on the file at the moment of the flush
        real(fd)
    monkeypatch.setattr(feedback, "_fsync", watching)
    rec_id = feedback.record("done", ROW, "row", "work-queue")
    assert len(seen) == 1 and rec_id.encode() in seen[0], "the line was written, then flushed, before the answer"


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


def test_the_whole_file_is_known_however_big_and_a_take_back_of_the_oldest_record_works(home, monkeypatch):
    monkeypatch.setattr(feedback, "READ_BLOCK", 400)
    first = down()
    others = [feedback.record("done", pointers.row_pointer(f"https://x.example/{n}"), "row", "work-queue") for n in range(60)]
    feedback.forget()
    assert feedback.standing(first)["signal"] == "thumbs_down" and feedback.latest_for(ROW)["id"] == first
    assert len(feedback.recent(limit=1000)) == 61 and feedback.views()[ROW]["down"] == first
    feedback.record("retract", ROW, "row", "work-queue", amends=first)
    assert feedback.views() == {} and feedback.standing(first) is None and others


def test_a_record_longer_than_a_block_is_read_whole(home, monkeypatch):
    monkeypatch.setattr(feedback, "READ_BLOCK", 100)
    one = feedback.record("tell", ROW, "row", "work-queue", words="word " * 300)
    feedback.forget()
    assert feedback.views()[ROW]["told_id"] == one


def test_only_what_was_added_is_read_and_a_file_that_changed_is_read_again(home):
    first = down()
    assert feedback.views()[ROW]["down"] == first
    state = feedback._STATE
    offset = state.offset
    path = home / ".assistant/feedback/feedback.jsonl"
    other = '{"id":"fb-' + "1" * 16 + '","ts":"2026-11-24T10:00:00Z","signal":"tell","kind":"row","subject":"' + ROW + '","words":"from elsewhere"}\n'
    with open(path, "a") as f:
        f.write(other)
    assert feedback.views()[ROW]["told"] == "from elsewhere" and state.offset > offset and len(state.recs) == 2
    with open(path, "a") as f:
        f.write(other[:40])  # a line still being written isn't read until it is whole
    assert len(feedback.recent()) == 2
    path.unlink()
    assert feedback.views() == {} and feedback.recent() == []
    mine = down()
    path2 = home / ".assistant/feedback/feedback.jsonl"
    data = path2.read_bytes()
    path2.write_bytes(data.replace(b'"thumbs_down"', b'"thumbs_dowm"'))  # rewritten in place, same size
    assert feedback.views() == {} and mine


def test_a_file_replaced_by_another_is_read_from_its_start(home):
    first = down()
    assert feedback.views()[ROW]["down"] == first
    path = home / ".assistant/feedback/feedback.jsonl"
    old = path.read_bytes()
    path.unlink()
    path.write_bytes(old.replace(first.encode(), b"fb-" + b"2" * 16))
    assert feedback.views()[ROW]["down"] == "fb-" + "2" * 16


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
    mine = feedback.record("tell", ROW, "row", "work-queue", words="x" * 100)
    second = pointers.row_pointer("https://other.example/y")
    gone = feedback.record("thumbs_down", second, "row", "work-queue")
    feedback.record("retract", second, "row", "work-queue", amends=gone)
    page_note = feedback.record("tell", "", "page", "work-queue", words="Too long a list")
    feedback.record("done", ROW, "row", "work-queue")
    feedback.record("thumbs_down", ROW, "draft", "work-queue")
    assert feedback.views().pop("draft:" + ROW)["down"].startswith("fb-")
    assert {k: v for k, v in feedback.views().items() if k != "draft:" + ROW} == {ROW: {"kind": "row", "down": first, "reason": "wrong_order", "told": "x" * 80 + "…", "told_id": mine},
                                "page": {"kind": "page", "down": "", "reason": "", "told": "Too long a list", "told_id": page_note}}
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


# ─── the lock, telemetry, the cut, the rules, the cancel line ────────────────

class Clock:
    """A clock the lock wait runs on: sleeping moves it, and nothing really waits."""

    def __init__(self, monkeypatch):
        self.now, self.slept = 1000.0, 0.0
        monkeypatch.setattr(feedback, "_monotonic", lambda: self.now)
        monkeypatch.setattr(feedback, "_sleep", self.sleep)
        self.at_sleep = None

    def sleep(self, seconds):
        self.now += seconds
        self.slept += seconds
        if self.at_sleep:
            self.at_sleep(self)


def hold_the_lock(home):
    """Another process holds feedback.lock, the way a stuck writer would. Returns the function that lets go."""
    import fcntl
    (home / ".assistant/feedback").mkdir(parents=True, exist_ok=True)
    fd = os.open(str(home / ".assistant/feedback/feedback.lock"), os.O_CREAT | os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX)
    return lambda: os.close(fd)


def test_a_stuck_lock_holds_a_record_for_half_a_second_not_for_as_long_as_it_is_stuck(home, monkeypatch):
    clock = Clock(monkeypatch)
    release = hold_the_lock(home)
    with pytest.raises(feedback.Unrecorded, match="busy"):
        feedback.record("done", ROW, "row", "work-queue")
    release()
    assert feedback.LOCK_WAIT <= clock.slept < feedback.LOCK_WAIT + 0.1 and lines_or_none(home) == []


def lines_or_none(home):
    path = home / ".assistant/feedback/feedback.jsonl"
    return lines(home) if path.exists() else []


def test_a_batch_shares_one_deadline_and_a_lock_freed_in_time_is_taken(home, monkeypatch):
    clock = Clock(monkeypatch)
    release = hold_the_lock(home)
    deadline = feedback.lock_deadline()
    for _ in range(40):
        with pytest.raises(feedback.Unrecorded):
            feedback.record("done", ROW, "row", "work-queue", deadline=deadline)
    assert clock.slept < feedback.LOCK_WAIT + 0.1, "forty records waited once, not forty times"
    clock.slept = 0
    clock.at_sleep = lambda c: (release(), setattr(c, "at_sleep", None)) if c.slept >= 0.2 else None  # let go after 0.2 s
    feedback.record("done", ROW, "row", "work-queue")
    assert 0.2 <= clock.slept < feedback.LOCK_WAIT and len(lines(home)) == 1


def test_a_deferred_telemetry_record_waits_for_emit_pending_and_is_sent_in_one_write(home, monkeypatch):
    sent = []
    monkeypatch.setattr(feedback.telemetry, "emit_many", lambda batch: sent.append(list(batch)))
    feedback.record("done", ROW, "row", "work-queue", trace=TRACE, defer=True)
    feedback.record("snooze", ROW, "row", "work-queue", defer=True)
    assert sent == [] and len(lines(home)) == 2, "the records are on disk and nothing was sent to telemetry yet"
    feedback.emit_pending()
    assert len(sent) == 1 and [f["signal"] for _, f in sent[0]] == ["done", "snooze"]
    assert sent[0][0][0] == "feedback" and (sent[0][0][1]["trace"], sent[0][0][1]["subject"]) == (TRACE, ROW)
    feedback.emit_pending()
    assert len(sent) == 1, "each is sent once"


def test_pending_telemetry_is_bounded(home, monkeypatch):
    monkeypatch.setattr(feedback, "PENDING_MAX", 2)
    for _ in range(5):
        feedback.record("done", ROW, "row", "work-queue", defer=True)
    sent = []
    monkeypatch.setattr(feedback.telemetry, "emit_many", lambda batch: sent.extend(batch))
    feedback.emit_pending()
    assert len(sent) == 2 and len(lines(home)) == 5


def test_a_secret_across_the_cut_is_scrubbed_before_it_is_cut(home):
    secret = "-".join(["xoxb", "1234567890", "abcdefghijklmnop"])
    text = "a" * (feedback.WORDS_MAX - 12) + " " + secret + " tail"
    feedback.record("tell", ROW, "row", "work-queue", words=text)
    words = lines(home)[0]["words"]
    assert "xoxb-12" not in words and "1234567890" not in words and len(words) <= feedback.WORDS_MAX
    assert words.endswith("xoxb-<redacted>"[:len(words) - (feedback.WORDS_MAX - 11)]) or "<redacted>" in words


def test_only_a_thumbs_down_has_a_reason(home):
    first = down()
    with pytest.raises(ValueError, match="only a thumbs-down"):
        feedback.record("done", ROW, "row", "work-queue", reason="not_mine", amends=first)


def item_rules(home):
    first = down()
    tell = feedback.record("tell", ROW, "row", "work-queue", words="hi")
    reason = down(reason="not_mine", amends=first)
    draft = feedback.record("thumbs_down", ROW, "draft", "work-queue")
    done = feedback.record("done", ROW, "row", "work-queue")
    return first, tell, reason, draft, done


def test_a_take_back_or_a_reason_must_be_about_the_same_item(home):
    first, tell, reason, draft, done = item_rules(home)
    with pytest.raises(ValueError, match="no earlier record about this item"):
        feedback.record("retract", ROW, "draft", "work-queue", amends=first)  # a draft is not its row
    with pytest.raises(ValueError, match="no earlier record about this item"):
        feedback.record("retract", ROW, "row", "work-queue", amends=draft)
    feedback.record("retract", ROW, "start_here", "work-queue", amends=first)  # a row and its Start here item are one item
    assert feedback.views()[ROW]["down"] == ""


def test_a_reason_belongs_to_a_standing_thumbs_down_only(home):
    first, tell, reason, draft, done = item_rules(home)
    for target in (tell, reason, done):
        with pytest.raises(ValueError, match="belongs to a thumbs-down"):
            feedback.record("thumbs_down", ROW, "row", "work-queue", reason="not_mine", amends=target)
    feedback.record("retract", ROW, "row", "work-queue", amends=first)
    with pytest.raises(ValueError, match="already taken back"):
        feedback.record("thumbs_down", ROW, "row", "work-queue", reason="not_mine", amends=first)


def test_a_take_back_is_never_itself_taken_back_or_made_twice(home):
    first, *_ = item_rules(home)
    gone = feedback.record("retract", ROW, "row", "work-queue", amends=first)
    with pytest.raises(ValueError, match="already taken back"):
        feedback.record("retract", ROW, "row", "work-queue", amends=first)
    with pytest.raises(ValueError):
        feedback.record("retract", ROW, "row", "work-queue", amends=gone)


def test_a_reason_can_replace_an_earlier_one_by_a_newer_record(home):
    first = down()
    down(reason="not_mine", amends=first)
    down(reason="already_done", amends=first)
    assert feedback.views()[ROW]["reason"] == "already_done" and len(lines(home)) == 3


def test_two_take_backs_of_the_same_record_at_once_leave_one(home):
    for _ in range(15):
        target = down()
        results = []

        def take():
            try:
                feedback.record("retract", ROW, "row", "work-queue", amends=target)
                results.append("ok")
            except ValueError:
                results.append("no")
        threads = [threading.Thread(target=take) for _ in range(6)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        assert results.count("ok") == 1, results


def test_a_line_whose_flush_failed_is_cancelled_so_a_retry_is_not_a_twin(home, monkeypatch):
    calls = []
    real = os.fsync

    def once(fd):
        calls.append(1)
        if len(calls) == 1:
            raise OSError(5, "disk")
        real(fd)
    monkeypatch.setattr(feedback, "_fsync", once)
    with pytest.raises(feedback.Unrecorded):
        down()
    written = lines(home)
    assert [r["signal"] for r in written] == ["thumbs_down", "retract"] and written[1]["amends"] == written[0]["id"]
    assert feedback.views() == {}, "the failed line doesn't stand"
    retry = down()
    assert feedback.views()[ROW]["down"] == retry


def test_a_cancel_that_cannot_be_written_is_ignored(home, monkeypatch):
    def broken(fd):
        raise OSError(5, "disk")
    monkeypatch.setattr(feedback, "_fsync", broken)
    with pytest.raises(feedback.Unrecorded):
        down()


def test_a_reason_written_after_its_thumbs_down_was_taken_back_does_not_stand(home):
    first = down()
    feedback.record("retract", ROW, "row", "work-queue", amends=first)
    late = '{"id":"fb-' + "3" * 16 + '","ts":"2026-11-24T10:00:00Z","signal":"thumbs_down","kind":"row","subject":"' + ROW \
        + '","reason":"not_mine","amends":"' + first + '"}\n'
    with open(home / ".assistant/feedback/feedback.jsonl", "a") as f:
        f.write(late)  # another writer, such as a later version, wrote it
    assert feedback.views() == {} and feedback.standing("fb-" + "3" * 16) is None and feedback.standing(first) is None


def test_standing_never_raises(home):
    (home / ".assistant/feedback/feedback.jsonl").mkdir(parents=True)
    assert feedback.standing("fb-" + "0" * 16) is None


def test_a_key_made_by_another_process_a_moment_ago_is_used(home, monkeypatch):
    real = os.open

    def racing(path, flags, *a):
        if str(path).endswith("sender.key") and flags & os.O_EXCL:
            (home / ".assistant/feedback").mkdir(parents=True, exist_ok=True)
            (home / ".assistant/feedback/sender.key").write_bytes(b"theirs")
            raise FileExistsError()
        return real(path, flags, *a)
    monkeypatch.setattr(feedback.os, "open", racing)
    assert feedback.who_pointer("ana").startswith("who:")
    assert (home / ".assistant/feedback/sender.key").read_bytes() == b"theirs"
