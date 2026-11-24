"""A save the worker couldn't mark done after a send is tried again by itself, then reported on the page."""
from __future__ import annotations

import json
import threading

import pytest

from work_queue import config, held_saves, refresh, server
from work_queue.inputs import slack
from work_queue_fakes import calls, fake_tool, wq  # noqa: F401
from test_work_queue_effects import break_the_ledger, fixed_clock  # noqa: F401
from test_work_queue_effects_slack import CID, DONE, TS1, TS2, hold, key2, link
from test_work_queue_server import AT, SAVE, applied, slack_answers, wait_for, worker_done
from test_work_queue_inputs import ENTRY, NOW, fake_slack  # noqa: F401

T0 = 1_800_000_000.0
SENT = "2026-11-02T10:00:00+00:00"


@pytest.fixture
def now(monkeypatch):
    """The held saves' clock: `now.t` is what time it is for them."""
    clock = type("Clock", (), {"t": T0})()
    monkeypatch.setattr(server, "clock", lambda: clock.t)
    return clock


def held(wq) -> dict:
    return json.loads((wq.data / "slack-held.json").read_text())


def settle(wq) -> None:
    wait_for(worker_done)
    with server.saves_lock:
        assert server.pending_saves == {} and server.running_saves == set()


def failing_runs(monkeypatch, error="ratelimited"):
    """Every browser run answers with a Slack error for each save, and says which saves it was asked about."""
    runs = []

    def run(jobs):
        runs.append([k for k, _, _ in jobs])
        return {k: {"status": "error", "error": error, "at": "then"} for k, _, _ in jobs}
    monkeypatch.setattr(server, "run_slack", run)
    return runs


# --- the schedule --------------------------------------------------------------------------------------------------

def test_a_save_that_cant_be_marked_is_tried_again_after_one_five_and_fifteen_minutes_and_then_no_more(wq, now, monkeypatch):
    runs = failing_runs(monkeypatch)
    server.queue_save(CID, TS1, SENT)
    settle(wq)
    assert runs == [[SAVE]] and held(wq)[SAVE] == {"cid": CID, "ts": TS1, "at": SENT, "tries": 1, "reason": "ratelimited",
                                                    "link": "", "next": T0 + 60}
    server.retry_due(T0 + 59)
    settle(wq)
    assert len(runs) == 1, "not before the minute is up"
    now.t = T0 + 60
    server.retry_due(now.t)
    settle(wq)
    assert len(runs) == 2 and held(wq)[SAVE]["next"] == T0 + 60 + 300 and held(wq)[SAVE]["tries"] == 2
    server.retry_due(T0 + 359)
    settle(wq)
    assert len(runs) == 2
    now.t = T0 + 360
    server.retry_due(now.t)
    settle(wq)
    assert len(runs) == 3 and held(wq)[SAVE]["next"] == T0 + 360 + 900
    now.t = T0 + 1260
    server.retry_due(now.t)
    settle(wq)
    assert len(runs) == 4
    assert held(wq)[SAVE] == {"cid": CID, "ts": TS1, "at": SENT, "link": "", "tries": 4, "reason": "ratelimited",
                                "gave_up": True}
    server.retry_due(T0 + 10**7)
    settle(wq)
    assert len(runs) == 4, "after the last try it stops"
    assert applied(wq)[SAVE]["status"] == "error" and applied(wq)[SAVE]["sent"] is True


def test_the_retry_marks_the_save_with_the_time_of_the_original_send(wq, now):
    slack_answers(wq, {SAVE: DONE})
    hold_for_retry(wq, now)
    server.retry_due(T0 + 60)
    settle(wq)
    assert applied(wq)[SAVE]["status"] == "done"
    [e] = [e for e in server.effects.history() if e["class"] == "slack.save.complete"]
    assert e["authorized_by"] == {"kind": "tap", "ref": "/api/todo", "at": "2026-11-02T10:00:00Z"}
    assert not (wq.data / "slack-held.json").exists() or held(wq) == {}, "a marked save stops waiting"


def hold_for_retry(wq, now, key=SAVE, ts=TS1):
    held_saves.hold({key: ({"cid": CID, "ts": ts, "at": SENT}, "ratelimited")}, now.t)
    config.write_json(wq.data / "applied.json", {key: {"status": "error", "error": "ratelimited", "at": "x", "sent": True}})


# --- every reason --------------------------------------------------------------------------------------------------

def reason_of(wq) -> str:
    return held(wq)[SAVE]["reason"]


def test_a_slack_error_for_the_save_is_held_and_its_words_are_kept(wq, now):
    slack_answers(wq, {SAVE: {"status": "error", "error": "ratelimited", "at": "then"}})
    server.queue_save(CID, TS1, SENT)
    settle(wq)
    assert reason_of(wq) == "ratelimited"


def test_slack_not_being_linked_is_held(wq, now):
    server.queue_save(CID, TS1, SENT)
    settle(wq)
    assert reason_of(wq).startswith("Slack isn't linked")


def test_a_sign_in_that_is_needed_is_held(wq, now):
    link(wq)
    fake_tool(wq.bin, "uv", [("slack_browser.py apply", {"ok": False, "error": "Slack sign-in needed: run it and sign in"})])
    server.queue_save(CID, TS1, SENT)
    settle(wq)
    assert reason_of(wq).startswith("Slack sign-in needed")


def test_a_browser_run_that_crashed_is_held(wq, now):
    link(wq)
    fake_tool(wq.bin, "uv", [("slack_browser.py apply", {"exit": 1, "stderr": "boom"})])
    server.queue_save(CID, TS1, SENT)
    settle(wq)
    assert "boom" in reason_of(wq)


def test_a_save_the_ledger_could_not_record_is_held_and_goes_through_once_the_disk_is_fine(wq, now, monkeypatch):
    slack_answers(wq, {SAVE: DONE})
    break_the_ledger(wq, monkeypatch, "folder is a file")
    server.queue_save(CID, TS1, SENT)
    settle(wq)
    assert reason_of(wq) == server.UNRECORDED and calls(wq.bin, "uv") == []
    (wq.home / ".assistant/effects").unlink()
    server.retry_due(T0 + 60)
    settle(wq)
    assert applied(wq)[SAVE]["status"] == "done" and held(wq) == {}


def test_a_save_another_run_has_in_progress_is_held_and_gets_through_after_ten_minutes(wq, now, fixed_clock):
    slack_answers(wq, {SAVE: DONE})
    hold(SAVE, TS1)
    server.queue_save(CID, TS1, SENT)
    settle(wq)
    assert reason_of(wq) == server.IN_PROGRESS
    fixed_clock.t += 601
    now.t = T0 + 60
    server.retry_due(now.t)
    settle(wq)
    assert applied(wq)[SAVE]["status"] == "done" and held(wq) == {}


def test_an_unexpected_error_in_the_worker_is_held(wq, now, monkeypatch):
    def broken(jobs):
        raise RuntimeError("unexpected")
    monkeypatch.setattr(server, "run_slack", broken)
    server.queue_save(CID, TS1, SENT)
    settle(wq)
    assert reason_of(wq) == "Couldn't mark the save done: RuntimeError: unexpected"


def test_a_save_the_run_never_mentioned_is_held_and_so_is_one_whose_run_was_unknown(wq, now, monkeypatch):
    monkeypatch.setattr(server, "run_slack", lambda jobs: {})
    server.queue_save(CID, TS1, SENT)
    settle(wq)
    assert reason_of(wq) == server.UNREPORTED


def test_a_save_slack_no_longer_has_is_not_held(wq, now):
    slack_answers(wq, {SAVE: {"status": "error", "error": "saved_not_found", "at": "then"}})
    server.queue_save(CID, TS1, SENT)
    settle(wq)
    assert applied(wq)[SAVE]["status"] == "gone" and not (wq.data / "slack-held.json").exists()


def test_a_save_that_was_marked_is_not_held_and_a_batch_holds_only_the_ones_that_failed(wq, now):
    slack_answers(wq, {SAVE: DONE, key2(TS2): {"status": "error", "error": "ratelimited", "at": "then"}})
    server.queue_save(CID, TS1, SENT)
    server.queue_save(CID, TS2, SENT)
    settle(wq)
    assert list(held(wq)) == [key2(TS2)] or wait_for(lambda: list(held(wq)) == [key2(TS2)])


def test_a_save_apply_marks_stops_waiting_to_be_tried_again(wq, now):
    hold_for_retry(wq, now)
    hold_for_retry(wq, now, key2(TS2), TS2)
    slack_answers(wq, {SAVE: DONE})
    from work_queue import refresh as refresher
    config.write_json(refresher.candidates_path(), {SAVE: {"kind": "slack", "cid": CID, "ts": TS1}})
    config.write_json(wq.data / "decisions.json", {SAVE: {"decision": "approve", "at": AT}})
    server.run_apply()
    assert list(held(wq)) == [key2(TS2)]


# --- a restart and a double check ------------------------------------------------------------------------------

def test_a_restart_keeps_the_waiting_saves(wq, now, monkeypatch):
    runs = failing_runs(monkeypatch)
    server.queue_save(CID, TS1, SENT)
    settle(wq)
    on_disk = (wq.data / "slack-held.json").read_text()
    # a new server: nothing in memory, the file is all it has
    with server.saves_lock:
        server.pending_saves.clear()
        server.running_saves.clear()
    assert (wq.data / "slack-held.json").read_text() == on_disk
    now.t = T0 + 61
    server.retry_due(now.t)
    settle(wq)
    assert len(runs) == 2 and held(wq)[SAVE]["tries"] == 2


def test_the_server_checks_for_held_saves_the_moment_it_starts(wq, now, monkeypatch):
    seen = []
    monkeypatch.setattr(server, "RETRY_POLL", 60)
    monkeypatch.setattr(server, "retry_due", lambda at: seen.append(at))
    server.retry_stop.clear()
    thread = threading.Thread(target=server.retry_loop, daemon=True)
    thread.start()
    assert wait_for(lambda: seen == [T0], 20)
    server.retry_stop.set()
    thread.join(20)
    server.retry_stop.clear()
    assert not thread.is_alive()


def test_the_server_checks_for_held_saves_on_a_timer_after_that(wq, now, monkeypatch):
    seen = []
    monkeypatch.setattr(server, "RETRY_POLL", 0.01)
    monkeypatch.setattr(server, "retry_due", lambda at: seen.append(at))
    server.retry_stop.clear()
    thread = threading.Thread(target=server.retry_loop, daemon=True)
    thread.start()
    assert wait_for(lambda: len(seen) >= 3)
    server.retry_stop.set()
    thread.join(20)
    server.retry_stop.clear()
    assert not thread.is_alive()


def test_a_check_that_fails_never_stops_the_next_one(wq, now, monkeypatch, capsys):
    tries = []

    def flaky(at):
        tries.append(at)
        if len(tries) == 1:
            raise OSError("no space left")
    monkeypatch.setattr(server, "RETRY_POLL", 0.01)
    monkeypatch.setattr(server, "retry_due", flaky)
    server.retry_stop.clear()
    thread = threading.Thread(target=server.retry_loop, daemon=True)
    thread.start()
    assert wait_for(lambda: len(tries) >= 2)
    server.retry_stop.set()
    thread.join(20)
    server.retry_stop.clear()
    assert "OSError: no space left" in capsys.readouterr().err


def test_a_save_already_waiting_or_running_is_not_queued_twice(wq, now, monkeypatch):
    queued = []
    monkeypatch.setattr(server, "queue_save", lambda cid, ts, at="", link="": queued.append(ts))
    held_saves.hold({SAVE: ({"cid": CID, "ts": TS1, "at": SENT}, "x"), key2(TS2): ({"cid": CID, "ts": TS2, "at": SENT}, "x")}, T0)
    with server.saves_lock:
        server.pending_saves[SAVE] = {}
    server.retry_due(T0 + 60)
    assert queued == [TS2]
    with server.saves_lock:
        server.pending_saves.clear()
        server.running_saves.add(key2(TS2))
    queued.clear()
    server.retry_due(T0 + 60)
    with server.saves_lock:
        server.running_saves.clear()
    assert queued == [TS1]


# --- the line on the page ----------------------------------------------------------------------------------------

def link_of(n) -> str:
    return f"https://acme.slack.com/archives/{CID}/p170000000{n}000100"


def give_up(wq, reasons, now, links=True):
    """Each reason is a save that was tried four times and couldn't be marked."""
    for n, why in enumerate(reasons):
        for _ in range(4):
            held_saves.hold({key2(f"170000000{n}.000100"): ({"cid": CID, "ts": f"170000000{n}.000100", "at": SENT,
                                                             "link": link_of(n) if links else ""}, why)}, now.t)


def test_a_save_still_unmarked_after_the_last_try_is_one_plain_line_in_the_state_with_a_link_to_it(wq, now):
    give_up(wq, ["ratelimited"], now)
    assert server.state()["slack_held"] == {
        "text": "1 save you sent to an agent couldn't be marked done in Slack: Slack asked us to slow down. "
                "Mark it done in Slack yourself.", "links": [link_of(0)], "more": 0}


def test_several_saves_say_how_many_and_each_reason_once_and_link_the_first_three(wq, now):
    give_up(wq, ["ratelimited", "ratelimited", "invalid_auth", "TimeoutError: Page.goto", "x"], now)
    assert held_saves.line() == {
        "text": "5 saves you sent to an agent couldn't be marked done in Slack: Slack asked us to slow down; "
                "Slack needs you to sign in again. Sign in to Slack again, then mark them done in Slack yourself.",
        "links": [link_of(0), link_of(1), link_of(2)], "more": 2}


def test_a_save_with_no_link_says_it_is_still_on_the_later_list(wq, now):
    give_up(wq, ["x", "x"], now, links=False)
    assert held_saves.line() == {
        "text": "2 saves you sent to an agent couldn't be marked done in Slack: Slack refused the change. Mark them "
                "done in Slack yourself. They are still open in your Slack Later list.", "links": [], "more": 0}


def test_a_link_is_offered_only_when_it_is_a_link(wq, now):
    give_up(wq, ["x"], now)
    entries = held(wq)
    entries[key2("1700000000.000100")]["link"] = "javascript:alert(1)"
    config.write_json(wq.data / "slack-held.json", entries)
    assert held_saves.line()["links"] == []


@pytest.mark.parametrize("raw, plain", [
    ("ratelimited", "Slack asked us to slow down"),
    ("invalid_auth", "Slack needs you to sign in again"), ("not_authed", "Slack needs you to sign in again"),
    ("token_expired", "Slack needs you to sign in again"),
    ("Slack sign-in needed: run bin/work-queue-setup.py link slack and sign in", "Slack needs you to sign in again"),
    ("TimeoutError: Page.goto: Timeout 60000ms exceeded.", "Slack didn't answer"), ("Error: net::ERR_FAILED", "Slack didn't answer"),
    ("uv run --no-project --quiet --with playwright==1.63.0 python slack_browser.py failed: Call log: navigating to the page",
     "Slack didn't answer"),
    ("Assistant couldn't record this action, so it didn't run it. Check that your disk isn't full, then try again",
     "Assistant couldn't record the change (is your disk full?)"),
    ("Assistant couldn't record this action, so it didn't run it. This is a fault in Assistant, not something you did",
     "Assistant couldn't record the change (is your disk full?)"),
    ("saved_not_found_x", "Slack refused the change"), ("Couldn't mark the save done: RuntimeError: x", "Slack refused the change"),
    ("", "Slack refused the change"), (None, "Slack refused the change"),
])
def test_each_reason_reads_in_plain_words_and_the_raw_text_is_never_shown(wq, now, raw, plain):
    assert held_saves.plain(raw) == plain
    config.write_json(wq.data / "slack-held.json", {SAVE: {"cid": CID, "ts": TS1, "at": SENT, "link": link_of(0),
                                                           "tries": 4, "gave_up": True, "reason": raw}})
    assert plain in held_saves.line()["text"] and (not raw or raw not in held_saves.line()["text"])


def test_there_is_no_line_while_a_save_is_still_waiting_or_when_nothing_is_held(wq, now):
    assert server.state()["slack_held"] == {}
    held_saves.hold({SAVE: ({"cid": CID, "ts": TS1, "at": SENT}, "x")}, T0)
    assert server.state()["slack_held"] == {}


def test_a_save_that_needs_a_sign_in_shows_the_line_at_once_and_is_still_tried_on_the_same_schedule(wq, now, monkeypatch):
    runs = failing_runs(monkeypatch, "invalid_auth")
    server.queue_save(CID, TS1, SENT, link_of(0))
    settle(wq)
    assert held(wq)[SAVE]["next"] == T0 + 60 and "gave_up" not in held(wq)[SAVE]
    assert held_saves.line() == {
        "text": "1 save you sent to an agent couldn't be marked done in Slack: Slack needs you to sign in again. "
                "Sign in to Slack again, then mark it done in Slack yourself.", "links": [link_of(0)], "more": 0}
    now.t = T0 + 60
    server.retry_due(now.t)
    settle(wq)
    assert len(runs) == 2 and held(wq)[SAVE]["next"] == T0 + 60 + 300, "the schedule is the same"
    assert held_saves.line()["text"].startswith("1 save ")


def test_a_malformed_entry_beside_a_good_one_stops_nothing(wq, now):
    good = {"cid": CID, "ts": TS1, "at": SENT, "link": link_of(0), "tries": 1, "next": T0, "reason": "x"}
    waiting_bad = [{**good, "next": "soon"}, {**good, "next": True}, {k: v for k, v in good.items() if k != "next"},
                   {k: v for k, v in good.items() if k != "cid"}, {**good, "ts": 5}, {**good, "at": None}, "text", 7, None]
    gave_up_bad = [{"gave_up": True, "cid": CID}, {"gave_up": True, "cid": CID, "ts": TS1, "at": 3}]
    entries = {f"bad{n}": bad for n, bad in enumerate(waiting_bad + gave_up_bad)}
    config.write_json(wq.data / "slack-held.json", {**entries, SAVE: good, key2(TS2): {**good, "ts": TS2, "gave_up": True}})
    assert held_saves.due(T0 + 1) == [(SAVE, CID, TS1, SENT, link_of(0))]
    assert held_saves.line()["text"].startswith("1 save ")
    queued = []
    from_queue = server.queue_save
    server.queue_save = lambda cid, ts, at="", link="": queued.append(ts)
    try:
        server.retry_due(T0 + 1)
    finally:
        server.queue_save = from_queue
    assert queued == [TS1]


def test_a_junk_link_field_is_ignored_by_the_retry_and_the_line(wq, now):
    config.write_json(wq.data / "slack-held.json", {SAVE: {"cid": CID, "ts": TS1, "at": SENT, "next": T0, "link": 5, "tries": 1}})
    assert held_saves.due(T0) == [(SAVE, CID, TS1, SENT, "")]


def test_the_row_s_own_link_is_kept_through_the_worker_and_every_retry(wq, now, monkeypatch):
    runs = failing_runs(monkeypatch)
    server.queue_todo({"key": link_of(0), "title": "A save"})
    wait_for(lambda: SAVE in held(wq) if (wq.data / "slack-held.json").exists() else False)
    settle(wq)
    assert held(wq)[SAVE]["link"] == link_of(0)
    server.retry_due(T0 + 60)
    settle(wq)
    assert len(runs) == 2 and held(wq)[SAVE]["link"] == link_of(0) and held(wq)[SAVE]["tries"] == 2


@pytest.mark.parametrize("link", ["", "https://evil.example/archives/C0123456789/p1700000000000100",
                                  "https://acme.slack.com/archives/C0123456789/p1700000009000100",
                                  "http://acme.slack.com/archives/C0123456789/p1700000000000100"])
def test_a_link_that_is_not_to_this_save_is_dropped(wq, now, monkeypatch, link):
    failing_runs(monkeypatch)
    server.queue_save(CID, TS1, SENT, link)
    settle(wq)
    assert held(wq)[SAVE]["link"] == ""


def test_the_line_clears_when_the_save_is_marked_or_slack_no_longer_has_it(wq, now):
    give_up(wq, ["ratelimited", "other"], now)
    two = [key2("1700000000.000100"), key2("1700000001.000100")]
    server.record({two[0]: {"status": "done"}})
    assert held_saves.line()["text"].startswith("1 save ")
    server.record({two[1]: {"status": "gone"}})
    assert held_saves.line() == {} and held(wq) == {}


def test_a_junk_file_or_a_junk_entry_never_breaks_the_held_saves(wq, now):
    config.write_json(wq.data / "slack-held.json", [1])
    assert held_saves.read() == {} and held_saves.due(T0) == []
    config.write_json(wq.data / "slack-held.json", {SAVE: {"tries": "x", "cid": CID, "ts": TS1, "at": SENT}, "j": 1})
    held_saves.hold({SAVE: ({"cid": CID, "ts": TS1, "at": SENT}, "why  \n  with\nbreaks")}, T0)
    assert held(wq)[SAVE]["tries"] == 1 and held(wq)[SAVE]["reason"] == "why with breaks"
    assert held_saves.due(T0 + 60) == [(SAVE, CID, TS1, SENT, "")]
    held_saves.drop(lambda k: True)
    assert held(wq)["j"] == 1, "nothing is rewritten when nothing changes"


# --- a refresh that sees the save gone from the Later list ------------------------------------------------------------

def test_a_refresh_forgets_a_held_save_that_is_no_longer_on_the_later_list_but_not_when_slack_couldnt_be_read(wq, fake_slack, monkeypatch):
    fake_tool(wq.bin, "gh", [("search prs", [])])
    monkeypatch.setenv("SLACK_USER_TOKEN", "xoxp-test")
    wq.sources({"slack": ENTRY})
    refresh.run(transport=fake_slack._transport, now=NOW)
    on_the_list = next(c for c in json.loads(refresh.candidates_path().read_text()).values() if c["kind"] == "slack")
    key = f"{on_the_list['cid']}:{on_the_list['ts']}"
    for k, cid, ts in ((key, on_the_list["cid"], on_the_list["ts"]), (SAVE, CID, TS1)):
        held_saves.hold({k: ({"cid": cid, "ts": ts, "at": SENT}, "x")}, T0)
    refresh.run(transport=fake_slack._transport, now=NOW)
    assert list(held(wq)) == [key], "the one Slack no longer lists is dropped"
    monkeypatch.delenv("SLACK_USER_TOKEN")
    refresh.run(transport=fake_slack._transport, now=NOW)
    assert list(held(wq)) == [key], "a Slack that couldn't be read says nothing about the list"
