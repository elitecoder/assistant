"""The Work queue records every Slack save it marks done or reopens in the effects ledger before it acts."""
from __future__ import annotations

import json
import threading

import pytest

from assistant import effects, pointers
from test_work_queue_effects import HOWS, LATER, NOON, break_the_ledger, fixed_clock, ledger_path, recorded  # noqa: F401
from test_work_queue_server import AT, SAVE, SAVE_LINK, applied, slack_answers, wait_for, worker_done
from work_queue import config, refresh, server
from work_queue.inputs import slack
from work_queue_fakes import calls, fake_tool, wq  # noqa: F401

CID, TS1, TS2 = "C0123456789", "1700000000.000100", "1700000001.000100"
DONE = {"status": "done", "verified": True, "at": "now"}
UNDONE = {"status": "undone", "verified": True, "at": "now"}


def job(key=SAVE, ts=TS1, undo=False, at=AT, extra=None):
    return (key, {"kind": "slack", "cid": CID, "ts": ts, "decided_at": at, **(extra or {})}, undo)


def sent_job(key=SAVE, ts=TS1, at=AT):
    return (key, {"cid": CID, "ts": ts, "sent_at": at}, False)


def browser_runs(wq) -> int:
    return len(calls(wq.bin, "uv"))


def link(wq):
    wq.sources({"slack": {"team_id": "T1", "workspace_url": "https://x.slack.com"}})


def target(key=SAVE, ts=TS1):
    return {"channel": CID, "ts": ts, "row": pointers.row_pointer(key)}


def key2(ts):
    return f"{CID}:{ts}"


# --- what is recorded ---------------------------------------------------------------------------------------

def test_a_save_marked_from_apply_records_its_intent_then_a_verified_result(wq):
    slack_answers(wq, {SAVE: DONE})
    out = server.run_slack([job()])
    assert out == {SAVE: DONE}
    [e] = recorded(wq)
    assert (e["class"], e["target"], e["outcome"], e["verified"], e["error"]) == (
        "slack.save.complete", target(), "done", True, "")
    assert e["authorized_by"] == {"kind": "decision", "ref": pointers.row_pointer(SAVE), "at": "2026-11-02T10:00:00Z"}
    assert (e["component"], e["trace"], e["undoes"]) == ("work-queue", "", "")


def test_a_save_marked_from_a_send_records_a_tap_on_the_send_route_at_the_time_of_the_send(wq, fixed_clock):
    slack_answers(wq, {SAVE: DONE})
    with slack.session_lock():  # the worker runs after the clock has moved on
        server.queue_todo({"key": SAVE_LINK, "title": "A save", "url": SAVE_LINK})
        fixed_clock.t += 3600
    wait_for(lambda: applied(wq).get(SAVE))
    wait_for(worker_done)
    [e] = recorded(wq)
    assert e["authorized_by"] == {"kind": "tap", "ref": "/api/todo", "at": "2026-11-02T11:30:00Z"}
    assert e["t"] > NOON and e["target"] == target() and e["outcome"] == "done"


def test_the_time_of_the_send_travels_with_the_save_not_the_time_the_worker_ran(wq, fixed_clock, monkeypatch):
    seen = []
    monkeypatch.setattr(server, "run_slack", lambda jobs: seen.append([c["sent_at"] for _, c, _ in jobs]) or {})
    release = threading.Event()
    real = server.slack.session_lock

    def late(*args):
        release.wait(10)
        return real(*args)
    monkeypatch.setattr(server.slack, "session_lock", late)
    server.queue_save(CID, TS1, "2026-11-02T10:00:00+00:00")
    fixed_clock.t += 3600
    release.set()
    wait_for(worker_done)
    assert seen == [["2026-11-02T10:00:00+00:00"]]


def test_a_send_hands_the_worker_the_time_it_was_made(wq, monkeypatch):
    queued = []
    monkeypatch.setattr(server, "queue_save", lambda cid, ts, at="", link="": queued.append((cid, ts, at, link)))
    server.queue_todo({"key": SAVE_LINK, "title": "A save"})
    assert queued == [(CID, TS1, "2026-11-02T11:30:00+00:00", SAVE_LINK)]


def test_a_save_queued_without_a_time_is_dated_now(wq, monkeypatch):
    seen = []
    monkeypatch.setattr(server, "run_slack", lambda jobs: seen.append(jobs[0][1]["sent_at"]) or {})
    server.queue_save(CID, TS1)
    wait_for(worker_done)
    assert seen == [server.now()]


# --- each outcome ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("reported, outcome, verified, error", [
    ({"status": "done", "verified": True, "at": "x"}, "done", True, ""),
    ({"status": "done", "verified": False, "at": "x", "error": "Slack accepted the change but the save still shows the old state"},
     "done", False, ""),
    ({"status": "done", "at": "x"}, "done", None, ""),
    ({"status": "error", "error": "saved_not_found", "at": "x"}, "failed", None, "other"),
    ({"status": "error", "error": "ratelimited", "at": "x"}, "failed", None, "tool failed"),
    ({"status": "odd"}, "unknown", None, "bad answer"),
    (None, "unknown", None, "not confirmed"),
])
def test_each_thing_slack_can_answer_for_a_save_is_recorded_as_its_own_outcome(wq, reported, outcome, verified, error):
    link(wq)
    fake_tool(wq.bin, "uv", [("slack_browser.py apply", {"ok": True, "applied": {} if reported is None else {SAVE: reported}})])
    server.run_slack([job()])
    [e] = recorded(wq)
    assert (e["outcome"], e["verified"], e["error"]) == (outcome, verified, error)


def test_a_reopened_save_is_recorded_as_done(wq):
    slack_answers(wq, {SAVE: UNDONE})
    server.run_slack([job(undo=True)])
    [e] = recorded(wq)
    assert (e["class"], e["outcome"], e["verified"]) == ("slack.save.uncomplete", "done", True)


@pytest.mark.parametrize("answer, outcome", [
    ({"ok": False, "error": "Slack sign-in needed: run bin/work-queue-setup.py link slack and sign in"}, "failed"),
    ({"ok": False, "error": "saved.list failed: ratelimited"}, "unknown"),
    ({"ok": False}, "unknown"),
])
def test_a_browser_run_that_failed_as_a_whole_is_failed_only_when_nothing_could_have_changed(wq, answer, outcome):
    link(wq)
    fake_tool(wq.bin, "uv", [("slack_browser.py apply", answer)])
    out = server.run_slack([job(), job(key2(TS2), TS2)])
    assert [e["outcome"] for e in recorded(wq)] == [outcome, outcome]
    assert {r["status"] for r in out.values()} == {"error"} and recorded(wq)[0]["error"] == "tool failed"


def test_a_browser_run_that_crashed_or_timed_out_leaves_each_save_unknown(wq, monkeypatch):
    link(wq)

    def crashed(cmd, timeout):
        raise server.tools.SourceError("uv timed out")
    monkeypatch.setattr(server.tools, "run_json", crashed)
    out = server.run_slack([job()])
    assert out[SAVE]["error"] == "uv timed out"
    assert [(e["outcome"], e["error"]) for e in recorded(wq)] == [("unknown", "tool failed")]


def test_a_run_that_never_started_is_failed_and_a_save_may_be_tried_again(wq):
    out = server.run_slack([job()])  # Slack isn't linked
    assert out[SAVE]["error"].startswith("Slack isn't linked")
    (wq.home / ".assistant/work-queue").mkdir(parents=True)
    (wq.home / ".assistant/work-queue/sources.json").write_text("{torn")
    server.run_slack([job()])
    assert [e["outcome"] for e in recorded(wq)] == ["failed", "failed"]
    assert browser_runs(wq) == 0


def test_the_ledgers_words_for_a_save_slack_no_longer_has_leave_applied_json_as_it_was(wq):
    slack_answers(wq, {SAVE: {"status": "error", "error": "saved_not_found", "at": "then"}})
    assert server.run_slack([job()]) == {SAVE: {"status": "gone", "error": "Slack no longer has this save", "at": "then"}}


# --- the intent comes first ---------------------------------------------------------------------------------

def test_the_intent_is_on_disk_before_the_browser_starts(wq, monkeypatch):
    link(wq)
    seen = []

    def run_json(cmd, timeout):
        seen.append([(e["outcome"], e["target"]["ts"]) for e in recorded(wq)])
        return {"ok": True, "applied": {SAVE: DONE, key2(TS2): DONE}}
    monkeypatch.setattr(server.tools, "run_json", run_json)
    server.run_slack([job(), job(key2(TS2), TS2)])
    assert seen == [[(None, TS1), (None, TS2)]]
    assert [e["outcome"] for e in recorded(wq)] == ["done", "done"]


@pytest.mark.parametrize("how", HOWS)
def test_a_save_that_cant_be_recorded_never_starts_the_browser_and_says_so_in_the_ledgers_words(wq, monkeypatch, how):
    slack_answers(wq, {SAVE: DONE})
    break_the_ledger(wq, monkeypatch, how)
    out = server.apply_slack([job()])
    assert out == {SAVE: {"status": "error", "error": server.UNRECORDED, "at": out[SAVE]["at"]}}
    assert browser_runs(wq) == 0 and not (wq.data / "apply-job.json").exists()


@pytest.mark.parametrize("how", HOWS)
def test_a_send_whose_save_cant_be_recorded_leaves_the_todo_and_never_starts_the_browser(wq, monkeypatch, how):
    slack_answers(wq, {SAVE: DONE})
    break_the_ledger(wq, monkeypatch, how)
    assert server.queue_todo({"key": SAVE_LINK, "title": "A"})[0] == 200
    assert wait_for(lambda: applied(wq).get(SAVE))["error"] == server.UNRECORDED
    wait_for(worker_done)
    assert applied(wq)[SAVE]["sent"] is True and browser_runs(wq) == 0


def test_a_save_with_no_decision_time_isnt_marked(wq):
    slack_answers(wq, {SAVE: DONE})
    out = server.run_slack([job(at=None)])
    assert out[SAVE]["error"] == server.UNAPPROVED and browser_runs(wq) == 0 and recorded(wq) == []


@pytest.mark.parametrize("cid, ts", [
    ("D0123456789", TS1), ("G0123456789", TS1), ("C01234567890123", TS1), ("W012345678", TS1), ("c0123456789", TS1),
    ("not a channel", TS1), (CID, "1700000000"), (CID, "1700000000.0001"), (CID, "later"), (None, None)])
def test_a_save_whose_id_the_ledgers_patterns_dont_take_is_still_marked_and_recorded_by_a_digest(wq, cid, ts):
    key = f"{cid}:{ts}"
    slack_answers(wq, {key: DONE})
    out = server.run_slack([(key, {"cid": cid, "ts": ts, "decided_at": AT}, False)])
    assert out == {key: DONE} and browser_runs(wq) == 1
    [e] = recorded(wq)
    assert e["outcome"] == "done" and e["target"]["row"] == pointers.row_pointer(key)
    assert effects.TARGET_KEYS["channel"].fullmatch(e["target"]["channel"]) and effects.TARGET_KEYS["ts"].fullmatch(e["target"]["ts"])


def test_a_digest_names_the_same_save_every_time_and_different_saves_differently(wq):
    assert server.slack_target("not a channel", "x", "k") == server.slack_target("not a channel", "x", "k")
    assert server.slack_target("not a channel", "x", "k") != server.slack_target("another one", "x", "k")
    assert server.slack_target("D0123456789", TS1, "k") == {"channel": "D0123456789", "ts": TS1, "row": pointers.row_pointer("k")}


# --- one refused save leaves the rest ----------------------------------------------------------------------

def hold(key, ts, cls="slack.save.complete"):
    """An intent for this save that nothing has finished: another run has it."""
    effects.begin(cls, target(key, ts), {"kind": "decision", "ref": pointers.row_pointer(key), "at": AT})


def test_a_save_that_is_already_in_progress_is_left_out_and_the_others_still_run(wq):
    slack_answers(wq, {key2(TS2): DONE})
    hold(SAVE, TS1)
    out = server.run_slack([job(), job(key2(TS2), TS2)])
    assert out[SAVE] == {"status": "error", "error": server.IN_PROGRESS, "at": out[SAVE]["at"]}
    assert out[key2(TS2)] == DONE
    assert [j["ts"] for j in json.loads((wq.data / "apply-job.json").read_text())] == [TS2]
    assert [(e["target"]["ts"], e["outcome"]) for e in recorded(wq)] == [(TS1, None), (TS2, "done")]


def test_when_every_save_is_refused_the_browser_isnt_started(wq):
    slack_answers(wq, {})
    hold(SAVE, TS1)
    assert server.run_slack([job()])[SAVE]["error"] == server.IN_PROGRESS
    assert browser_runs(wq) == 0 and not (wq.data / "apply-job.json").exists()


def test_a_reopen_the_record_refuses_keeps_the_saves_row_and_the_next_apply_tries_again(wq):
    slack_answers(wq, {SAVE: UNDONE})
    before = {"status": "done", "verified": True, "at": "before"}
    config.write_json(wq.data / "applied.json", {SAVE: before})
    config.write_json(refresh.candidates_path(), {SAVE: {"kind": "slack", "cid": CID, "ts": TS1}})
    config.write_json(wq.data / "decisions.json", {SAVE: {"decision": "undo", "at": AT}})
    hold(SAVE, TS1, "slack.save.uncomplete")
    server.run_apply()
    assert applied(wq)[SAVE] == {**before, "error": server.IN_PROGRESS, "undo_error": server.IN_PROGRESS}
    assert browser_runs(wq) == 0
    status = json.loads((wq.data / "apply-status.json").read_text())
    assert (status["error"], status["ok"]) == (server.IN_PROGRESS, 0)


def test_the_worker_leaves_out_a_save_the_record_refuses_and_records_why_beside_the_ones_it_marked(wq):
    slack_answers(wq, {key2(TS2): DONE})
    hold(SAVE, TS1)
    server.queue_save(CID, TS1)
    server.queue_save(CID, TS2)
    wait_for(lambda: key2(TS2) in applied(wq) and SAVE in applied(wq))
    wait_for(worker_done)
    assert applied(wq)[SAVE] == {"status": "error", "error": server.IN_PROGRESS, "at": applied(wq)[SAVE]["at"], "sent": True}
    assert applied(wq)[key2(TS2)]["status"] == "done"


# --- the worker and Apply on one save ----------------------------------------------------------------------

def test_the_worker_and_apply_holding_one_save_run_one_after_the_other_and_neither_is_lost(wq, monkeypatch):
    link(wq)
    inside, release, runs = threading.Event(), threading.Event(), []

    def run_json(cmd, timeout):
        runs.append(len(recorded(wq)))
        inside.set()
        release.wait(20)
        return {"ok": True, "applied": {SAVE: DONE}}
    monkeypatch.setattr(server.tools, "run_json", run_json)
    server.queue_save(CID, TS1)
    assert inside.wait(20)
    results = []
    thread = threading.Thread(target=lambda: results.append(server.apply_slack([job()])))
    thread.start()
    # Apply is waiting for the browser profile: it hasn't recorded anything yet, so nothing of it is lost or doubled.
    assert [e["outcome"] for e in recorded(wq)] == [None] and thread.is_alive()
    release.set()
    thread.join(30)
    wait_for(worker_done)
    assert results == [{SAVE: DONE}] and runs == [1, 2]
    assert [(e["authorized_by"]["kind"], e["outcome"]) for e in recorded(wq)] == [("tap", "done"), ("decision", "done")]


def test_an_intent_left_open_by_a_run_that_died_holds_off_both_paths_for_ten_minutes_then_lets_them_through(wq, fixed_clock):
    slack_answers(wq, {SAVE: DONE})
    effects.begin("slack.save.complete", target(), {"kind": "tap", "ref": "/api/todo", "at": AT})
    assert server.run_slack([job()])[SAVE]["error"] == server.IN_PROGRESS
    assert server.run_slack([sent_job()])[SAVE]["error"] == server.IN_PROGRESS
    fixed_clock.t += 601
    assert server.run_slack([job()]) == {SAVE: DONE}


# --- undo ---------------------------------------------------------------------------------------------------

def test_a_reopened_save_is_linked_to_the_mark_it_reverses(wq):
    slack_answers(wq, {SAVE: DONE})
    server.run_slack([job()])
    slack_answers(wq, {SAVE: UNDONE})
    server.run_slack([job(undo=True, at=LATER)])
    done, back = recorded(wq)
    assert (back["class"], back["undoes"]) == ("slack.save.uncomplete", done["id"])
    server.run_slack([job(undo=True, at=LATER)])  # a second reopen of the same mark has nothing left to undo
    assert recorded(wq)[-1]["undoes"] == ""


def test_a_reopen_of_a_save_the_record_never_saw_marked_runs_unlinked(wq):
    slack_answers(wq, {SAVE: UNDONE})
    assert server.run_slack([job(undo=True)]) == {SAVE: UNDONE}
    assert recorded(wq)[0]["undoes"] == ""


def test_a_send_cant_reopen_a_save(wq):
    slack_answers(wq, {SAVE: UNDONE})
    out = server.run_slack([(SAVE, {"cid": CID, "ts": TS1, "sent_at": AT}, True)])
    assert out[SAVE]["error"] == server.UNAPPROVED and browser_runs(wq) == 0


# --- an error nobody expected ---------------------------------------------------------------------------------

def test_an_error_before_the_browser_starts_records_failed_and_goes_on_up(wq, monkeypatch):
    slack_answers(wq, {SAVE: DONE})

    def broken(path, data):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(server.config, "write_json", broken)
    with pytest.raises(OSError):
        server.run_slack([job()])
    assert [(e["outcome"], e["error"]) for e in recorded(wq)] == [("failed", "other")] and browser_runs(wq) == 0


def test_an_error_after_the_browser_started_records_unknown_and_goes_on_up(wq, monkeypatch):
    link(wq)
    monkeypatch.setattr(server.tools, "run_json", lambda cmd, timeout: {"ok": True})
    with pytest.raises(KeyError):
        server.run_slack([job(), job(key2(TS2), TS2)])
    assert [(e["outcome"], e["error"]) for e in recorded(wq)] == [("unknown", "other")] * 2


def test_an_answer_that_cant_be_read_for_one_save_leaves_every_save_unknown(wq):
    link(wq)
    fake_tool(wq.bin, "uv", [("slack_browser.py apply", {"ok": True, "applied": {SAVE: "odd"}})])
    with pytest.raises(AttributeError):
        server.run_slack([job()])
    assert [e["outcome"] for e in recorded(wq)] == ["unknown"]


def test_an_unexpected_error_in_the_worker_is_handled_as_before_and_the_saves_are_unknown(wq, monkeypatch, capsys):
    link(wq)
    monkeypatch.setattr(server.tools, "run_json", lambda cmd, timeout: {"ok": True})
    server.queue_save(CID, TS1)
    wait_for(lambda: applied(wq).get(SAVE))
    wait_for(worker_done)
    assert applied(wq)[SAVE]["status"] == "error" and applied(wq)[SAVE]["sent"] is True
    assert "KeyError" in capsys.readouterr().err
    assert [e["outcome"] for e in recorded(wq)] == ["unknown"]


def test_an_error_while_recording_a_later_save_closes_the_earlier_ones_as_failed(wq, monkeypatch):
    slack_answers(wq, {})
    real = server.begin_effect

    def second_breaks(cls, tgt, *rest, **kw):
        if tgt["ts"] == TS2:
            raise RuntimeError("unexpected")
        return real(cls, tgt, *rest, **kw)
    monkeypatch.setattr(server, "begin_effect", second_breaks)
    with pytest.raises(RuntimeError):
        server.run_slack([job(), job(key2(TS2), TS2)])
    assert [e["outcome"] for e in recorded(wq)] == ["failed"] and browser_runs(wq) == 0


# --- no content ---------------------------------------------------------------------------------------------

def test_the_ledger_holds_only_pointers_for_a_save(wq):
    slack_answers(wq, {SAVE: DONE})
    cand = {"kind": "slack", "cid": CID, "ts": TS1, "decided_at": AT, "link": SAVE_LINK, "title": "ZEBRA-QUARTERLY-INVOICE",
            "ctx": "Ana Lee said the numbers are wrong", "who": "Ana Lee"}
    server.run_slack([(SAVE, cand, False)])
    lines = [json.loads(line) for line in ledger_path(wq).read_text().splitlines()]
    assert set(lines[0]["target"]) == {"channel", "ts", "row"}
    text = ledger_path(wq).read_text()
    for marker in ("ZEBRA", "Ana", "numbers", "slack.com", "archives", "x.slack"):
        assert marker not in text, marker


# --- what worked before still works ----------------------------------------------------------------------------

def test_apply_still_marks_a_save_and_the_page_answers_are_as_they_were(wq):
    slack_answers(wq, {SAVE: DONE})
    config.write_json(refresh.candidates_path(), {SAVE: {"kind": "slack", "cid": CID, "ts": TS1}})
    config.write_json(wq.data / "decisions.json", {SAVE: {"decision": "approve", "at": AT}})
    server.run_apply()
    assert applied(wq) == {SAVE: DONE}
    assert json.loads((wq.data / "apply-job.json").read_text()) == [
        {"key": SAVE, "cid": CID, "ts": TS1, "mark": "completed"}]


# --- a run that failed as a whole ---------------------------------------------------------------------------

def test_a_browser_run_that_failed_as_a_whole_still_answers_for_a_save_the_record_refused(wq):
    link(wq)
    fake_tool(wq.bin, "uv", [("slack_browser.py apply", {"ok": False, "error": "saved.list failed: ratelimited"})])
    hold(SAVE, TS1)
    out = server.run_slack([job(), job(key2(TS2), TS2)])
    assert out[SAVE]["error"] == server.IN_PROGRESS, "the refused save keeps its own answer"
    assert out[key2(TS2)]["error"] == "saved.list failed: ratelimited"
    assert [e["outcome"] for e in recorded(wq)] == [None, "unknown"]


@pytest.mark.parametrize("error", [["a", "list"], {"code": 5}, 7])
def test_an_answer_whose_error_isnt_text_is_recorded_as_text_for_every_save(wq, error):
    link(wq)
    fake_tool(wq.bin, "uv", [("slack_browser.py apply", {"ok": False, "error": error})])
    out = server.run_slack([job(), job(key2(TS2), TS2)])
    assert out[SAVE]["error"] == out[key2(TS2)]["error"] == str(error)
    assert [e["outcome"] for e in recorded(wq)] == ["unknown", "unknown"]


# --- nothing could have changed ----------------------------------------------------------------------------

@pytest.mark.parametrize("fault", ["uv is missing", "zsh is missing", "a field is missing"])
def test_a_run_that_could_not_have_changed_anything_is_failed_not_unknown(wq, monkeypatch, fault):
    link(wq)
    if fault == "uv is missing":
        fake_tool(wq.bin, "uv", [("slack_browser.py apply", {"exit": 127, "stderr": "zsh:exec:1: command not found: uv"})])
    elif fault == "zsh is missing":
        def missing(cmd, timeout):
            raise server.tools.ToolMissing("/bin/zsh isn't installed")
        monkeypatch.setattr(server.tools, "run_json", missing)
    else:
        wq.sources({"slack": {"team_id": "T1"}})
        with pytest.raises(KeyError):
            server.run_slack([job()])
        assert [(e["outcome"], e["error"]) for e in recorded(wq)] == [("failed", "other")]
        return
    out = server.run_slack([job()])
    assert out[SAVE]["status"] == "error"
    assert [e["outcome"] for e in recorded(wq)] == ["failed"]


@pytest.mark.parametrize("stderr", [
    "Traceback ... Timeout 30000ms exceeded",
    "bash: line 1: command not found: git",
    "playwright: command not found: chromium",
    "zsh:1: command not found: uv\nTraceback (most recent call last)",
    "Traceback (most recent call last)\nzsh:1: command not found: uv",
])
def test_a_run_that_started_and_failed_is_unknown_even_when_its_own_output_says_command_not_found(wq, stderr):
    link(wq)
    fake_tool(wq.bin, "uv", [("slack_browser.py apply", {"exit": 1, "stderr": stderr})])
    server.run_slack([job()])
    assert [e["outcome"] for e in recorded(wq)] == ["unknown"]


@pytest.mark.parametrize("stderr", ["zsh:1: command not found: uv", "zsh:exec:1: command not found: uv"])
def test_only_the_shells_own_message_for_uv_means_the_browser_never_started(wq, stderr):
    link(wq)
    fake_tool(wq.bin, "uv", [("slack_browser.py apply", {"exit": 127, "stderr": stderr})])
    server.run_slack([job()])
    assert [e["outcome"] for e in recorded(wq)] == ["failed"]


def test_the_command_is_built_before_the_browser_is_counted_as_started(wq, monkeypatch):
    link(wq)

    def broken(*args):
        raise KeyError("workspace_url")
    monkeypatch.setattr(server.slack, "browser", broken)
    with pytest.raises(KeyError):
        server.run_slack([job()])
    assert [e["outcome"] for e in recorded(wq)] == ["failed"] and browser_runs(wq) == 0


# --- a refused reopen says why on its row ---------------------------------------------------------------

def test_a_refused_reopen_keeps_the_row_and_adds_the_reason_under_its_own_name(wq):
    config.write_json(wq.data / "applied.json", {SAVE: {"status": "done", "verified": False, "at": "before",
                                                       "error": "Slack accepted the change but the save still shows the old state"}})
    hold(SAVE, TS1, "slack.save.uncomplete")
    out = server.run_slack([job(undo=True)])
    assert out[SAVE] == {"status": "done", "verified": False, "at": "before", "error": server.IN_PROGRESS,
                         "undo_error": server.IN_PROGRESS}
