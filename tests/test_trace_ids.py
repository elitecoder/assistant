"""Trace ids: every Work queue row and every task is given an id when it is first seen, stored with it, and never
replaced. Nothing reads the id yet, so nothing a user sees may change."""
from __future__ import annotations

import contextlib
import fcntl
import importlib.util
import json
import os
import re
import sys
import threading
import time
import types
from pathlib import Path

import pytest

from assistant import goals, pointers, todostore, triage as policy_triage
from work_queue import config, refresh, server, todo_result, traces
from work_queue_fakes import fake_tool, wq  # noqa: F401
from test_work_queue_inputs import ACCT, NOW, gmail_fakes

REPO = Path(__file__).resolve().parent.parent
ROW = "https://x.slack.com/archives/C1/p1"
PR = {"number": 7, "title": "Fix it", "url": "https://github.com/o/r/pull/7", "repository": {"nameWithOwner": "o/r"},
      "author": {"login": "ana"}, "createdAt": "2026-11-02T10:00:00Z", "isDraft": False}
PR_LINK = PR["url"]
DAY = "2026-11-18"
MADE = 1795000000.0  # the clock when an id is made
OWN = "tr-" + "ab" * 8
LEGACY = "tl-" + "cd" * 8


def registry() -> dict:
    return json.loads(traces.path().read_text())


def day(offset: int) -> str:
    from datetime import date, timedelta
    return (date.fromisoformat(DAY) + timedelta(days=offset)).isoformat()


@pytest.fixture(autouse=True)
def today(monkeypatch):
    """The refresh's clock, fixed, so no test depends on the date."""
    monkeypatch.setattr(traces, "today", lambda: DAY)
    monkeypatch.setattr(traces, "now", lambda: MADE)


def load_pulse():
    spec = importlib.util.spec_from_file_location("pulse_trace_ids", REPO / "bin/pulse.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_todos(wq, items, **extra):
    wq.todo.write_text(json.dumps({"_schema": 1, "_lastUpdated": "2026-01-01T00:00:00Z", **extra, "items": items},
                                  indent=2, ensure_ascii=False) + "\n")


# ─── Work queue rows: the registry ───────────────────────────────────────────

def test_a_refresh_mints_an_id_for_each_new_row_and_keeps_it_across_refreshes_and_a_new_title(wq):
    fake_tool(wq.bin, "gh", [("search prs", [PR])])
    wq.sources({"github": {"skip_authors": []}})
    refresh.run(now=NOW)
    first = registry()[PR_LINK]
    assert pointers.is_trace_id(first["trace"]) and first["seen"] == DAY
    fake_tool(wq.bin, "gh", [("search prs", [{**PR, "title": "A new title"}])])
    refresh.run(now=NOW)
    assert registry()[PR_LINK] == first
    assert "A new title" in refresh.site_path().read_text()


def test_a_refresh_notes_the_day_a_row_was_seen_and_writes_nothing_when_that_hasnt_changed(wq):
    traces.stamp([[ROW]], day(-3))
    first = traces.path().read_text()
    os.utime(traces.path(), (1, 1))
    traces.stamp([[ROW]], day(-3))
    assert traces.path().read_text() == first and traces.path().stat().st_mtime == 1
    traces.stamp([[ROW]], DAY)
    assert registry()[ROW] == {"trace": json.loads(first)[ROW]["trace"], "seen": DAY, "first": MADE}


def test_an_id_once_stored_is_never_replaced(wq):
    traces.stamp([[ROW]], day(-1))
    kept = registry()[ROW]["trace"]
    for _ in range(3):
        traces.stamp([[ROW], [ROW, ROW + "2"], [ROW + "2", ROW]], DAY)
        assert traces.trace_for(ROW) == kept and traces.lookup(ROW) == kept
        assert registry()[ROW]["trace"] == kept
    assert registry()[ROW + "2"]["trace"] == kept  # a key that had none takes the id its group already holds


def test_a_key_that_has_an_id_keeps_it_when_the_other_keys_of_its_group_hold_another(wq):
    config.write_json(traces.path(), {"https://a": {"trace": OWN, "seen": DAY}, "https://b": {"trace": LEGACY, "seen": DAY}})
    traces.stamp([["https://a", "https://b", "https://c"]], DAY)
    assert {k: v["trace"] for k, v in registry().items()} == {"https://a": OWN, "https://b": LEGACY, "https://c": OWN}


def test_a_row_not_seen_for_60_days_is_dropped_unless_a_sent_row_refers_to_it(wq):
    config.write_json(traces.path(), {
        "https://old": {"trace": OWN, "seen": day(-61)}, "https://edge": {"trace": OWN, "seen": day(-60)},
        ROW: {"trace": LEGACY, "seen": day(-200)}})
    config.write_json(wq.data / "queued.json", {ROW: {"id": "td-1"}})
    traces.stamp([], DAY)
    assert sorted(registry()) == sorted(["https://edge", ROW])
    assert registry()[ROW]["trace"] == LEGACY


def test_the_file_holds_at_most_5000_rows_and_drops_the_oldest_first(wq, monkeypatch):
    monkeypatch.setattr(traces, "MAX_ENTRIES", 5)
    config.write_json(traces.path(), {f"https://r/{n}": {"trace": OWN, "seen": day(-n)} for n in range(8)})
    config.write_json(wq.data / "queued.json", {"https://r/7": {"id": "td-1"}})
    traces.stamp([], DAY)
    assert sorted(registry()) == [f"https://r/{n}" for n in (0, 1, 2, 3, 7)]


def test_the_limits_are_60_days_and_5000_rows():
    assert (traces.KEEP_DAYS, traces.MAX_ENTRIES) == (60, 5000)


def test_rows_seen_in_this_refresh_are_kept_even_when_there_are_more_than_the_cap(wq, monkeypatch, capsys):
    monkeypatch.setattr(traces, "MAX_ENTRIES", 3)
    config.write_json(traces.path(), {"https://gone": {"trace": OWN, "seen": day(-1)}})
    live = [[f"https://live/{n}"] for n in range(5)]
    traces.stamp(live, DAY)
    first = registry()
    assert sorted(first) == sorted(g[0] for g in live) and "5 rows are live" in capsys.readouterr().err
    traces.stamp(live, DAY)  # the same rows get the same ids at the next refresh
    assert registry() == first


def test_a_sent_row_keeps_the_id_its_queued_entry_holds_when_the_registry_is_lost(wq):
    config.write_json(wq.data / "queued.json", {ROW: {"id": "td-1", "trace": OWN}, "https://b": {"id": "td-2", "trace": "bad"}})
    traces.stamp([[ROW], ["https://b"], ["https://c"]], DAY)
    got = registry()
    assert got[ROW]["trace"] == OWN
    assert all(pointers.is_trace_id(got[k]["trace"]) for k in ("https://b", "https://c"))
    assert got["https://b"]["trace"] != "bad"


def test_a_bare_id_is_adopted_and_an_odd_entry_or_a_future_day_is_mended(wq):
    config.write_json(traces.path(), {"https://a": OWN, "https://b": "text", "https://c": {"trace": "nope", "seen": DAY},
                                      "https://d": {"trace": LEGACY, "seen": "yesterday"},
                                      "https://e": {"trace": OWN, "seen": day(+30)}})
    config.write_json(wq.data / "queued.json", ["not", "an", "object"])
    traces.stamp([[ROW], [7, None, ""]], DAY)
    assert registry() == {"https://a": {"trace": OWN, "seen": DAY}, "https://d": {"trace": LEGACY, "seen": DAY},
                          "https://e": {"trace": OWN, "seen": DAY}, ROW: {"trace": registry()[ROW]["trace"], "seen": DAY, "first": MADE}}
    assert traces.lookup("https://a") == OWN and traces.lookup("https://nowhere") == ""


def test_a_damaged_registry_is_moved_aside_never_written_over_and_the_ids_queued_json_holds_survive(wq, capsys):
    wq.data.mkdir(exist_ok=True)
    config.write_json(wq.data / "queued.json", {ROW: {"id": "td-1", "trace": OWN}})
    for damage in ("{not json", "[1, 2]"):
        traces.path().write_text(damage)
        aside = traces.path().with_name("traces.json.damaged")
        assert traces.trace_for(ROW + "x") and traces.path().read_text() == damage  # a send leaves it for the refresh
        traces.stamp([[ROW]], DAY)
        assert aside.read_text() == damage and registry()[ROW]["trace"] == OWN
        assert capsys.readouterr().err.count("traces.json was damaged") == 1
    assert traces.lookup(ROW) == OWN


def test_a_registry_that_cant_be_read_is_never_written_over(wq):
    traces.path().mkdir(parents=True)
    assert traces.lookup(ROW) == ""
    with pytest.raises(OSError):
        traces.stamp([[ROW]], DAY)
    assert traces.path().is_dir()


def test_a_broken_registry_never_stops_a_refresh(wq, monkeypatch, capsys):
    fake_tool(wq.bin, "gh", [("search prs", [PR])])
    wq.sources({"github": {"skip_authors": []}})
    wq.data.mkdir(exist_ok=True)
    traces.path().write_text("{not json")
    refresh.run(now=NOW)
    assert pointers.is_trace_id(registry()[PR_LINK]["trace"])
    traces.path().unlink()
    traces.path().mkdir(parents=True)  # a folder where the file belongs: it can be neither read nor written
    status = refresh.run(now=NOW)
    assert [s["state"] for s in status if s["name"] == "GitHub"] == ["ok"]
    assert 'data-key="https://github.com/o/r/pull/7"' in refresh.site_path().read_text()
    assert json.loads(refresh.status_path().read_text())["running"] is False
    assert "Traceback" in capsys.readouterr().err
    monkeypatch.setattr(traces, "stamp", lambda groups: (_ for _ in ()).throw(OSError("disk")))
    refresh.run(now=NOW)
    assert json.loads(refresh.status_path().read_text())["running"] is False


def test_the_registry_is_read_and_written_inside_its_own_lock(wq, monkeypatch):
    events, real_lock, real_write = [], traces.lock, config.write_json

    @contextlib.contextmanager
    def tracked(timeout):
        with real_lock(timeout):
            events.append("lock")
            yield
        events.append("unlock")

    def watching(path, data):
        events.append("write " + path.name)
        real_write(path, data)
    monkeypatch.setattr(traces, "lock", tracked)
    monkeypatch.setattr(config, "write_json", watching)
    traces.stamp([[ROW]], DAY)
    traces.trace_for("https://other")
    assert events == ["lock", "write traces.json", "unlock"] * 2
    assert (wq.data / "traces.lock").exists() and not (wq.data / "marks.lock").exists()


# ─── One piece of work, one id ───────────────────────────────────────────────

GMAIL = {"key": "gmail:abc123", "link": "https://mail.google.com/mail/?authuser=me@x.com#inbox/t1", "group": "act",
         "title": "Lunch?", "from": "Ana", "day": "Nov 17", "subject": "Lunch?", "body": "", "reason": "", "unread": False}
MARK = "https://work-queue.local/email/gmail:abc123"


def test_an_email_has_one_id_whether_it_is_sent_from_the_email_tab_or_the_open_list(wq):
    refresh.stamp_traces({"gmail": {"me@x.com": [GMAIL]}}, {})
    assert registry()[MARK]["trace"] == registry()[GMAIL["link"]]["trace"]
    server.queue_todo({"key": MARK, "url": GMAIL["link"], "title": "Email: Lunch?"})
    server.queue_todo({"key": GMAIL["link"], "title": "Email: Lunch?"})
    assert [i["trace"] for i in wq.items()[-2:]] == [registry()[MARK]["trace"]] * 2


def test_an_email_that_moves_to_another_folder_keeps_its_id(wq):
    refresh.stamp_traces({"gmail": {"me@x.com": [GMAIL]}}, {})
    first = registry()[MARK]["trace"]
    moved = {**GMAIL, "link": GMAIL["link"].replace("#inbox/", "#label/Work/")}
    refresh.stamp_traces({"gmail": {"me@x.com": [moved]}}, {})
    assert registry()[moved["link"]]["trace"] == first == registry()[MARK]["trace"]
    server.queue_todo({"key": moved["link"], "title": "Email: Lunch?"})
    assert wq.items()[-1]["trace"] == first


def test_a_send_from_the_email_tab_adopts_the_id_of_its_link_when_the_mark_key_is_new(wq):
    traces.stamp([[GMAIL["link"]]], DAY)
    server.queue_todo({"key": MARK, "url": GMAIL["link"], "title": "Email: Lunch?"})
    assert wq.items()[-1]["trace"] == registry()[GMAIL["link"]]["trace"] == registry()[MARK]["trace"]


def test_every_kind_of_row_the_page_acts_on_gets_an_id_after_a_refresh(wq, monkeypatch):
    gmail_fakes(wq)
    wq.sources({"gmail": {"accounts": [ACCT]}})
    refresh.run(now=NOW)
    cands = json.loads(refresh.candidates_path().read_text())
    assert {c["kind"] for c in cands.values()} == {"gmail", "unsub"}
    got = registry()
    for key, cand in cands.items():
        assert pointers.is_trace_id(got[key]["trace"])
        if cand.get("mk"):
            assert got[cand["mk"]] == {**got[key], "seen": DAY}  # the candidate and the email are one piece of work
    html = refresh.site_path().read_text()
    marks = set(re.findall(r'data-mk="([^"]+)"', html))
    assert marks and marks <= set(got)
    assert all(pointers.is_trace_id(got[k]["trace"]) for k in re.findall(r'class="sf-row" data-key="([^"]+)"', html))


def test_the_keys_each_kind_of_row_is_known_by():
    slack = {"key": "C1:1.1", "link": "https://a.slack.com/archives/C1/p1", "group": "open"}
    closing = {"key": "C2:2.2", "link": "https://a.slack.com/archives/C2/p2", "group": "stale"}
    unread = {"key": "C3:3.3", "link": "https://a.slack.com/archives/C3/p3", "group": "unread"}
    outlook = [{"key": "mail:1", "link": "https://o/1", "group": "act"}, {"key": "mail:2", "link": "https://o/2", "group": "read"},
               {"key": "mail:3", "link": "", "group": "bulk"}, {"key": "mail:4", "link": "https://o/4", "group": "act"}]
    gmail_unsorted = {**GMAIL, "key": "gmail:u", "group": "unsorted"}
    snap = {"slack": [slack, closing, unread], "github": [{"link": "https://g/1"}, {"link": ""}], "outlook": outlook,
            "gmail": {"me@x.com": [GMAIL, gmail_unsorted]}, "calendar": [{"link": "https://c/1"}]}
    cands = {"C2:2.2": {"kind": "slack", "link": closing["link"]}, "mail:3": {"kind": "mail", "link": "", "mk": E + "mail:3"},
             "gmail:x": {"kind": "gmail", "link": "https://m/x", "mk": E + "gmail:x"}, "unsub:9": {"kind": "unsub", "link": "https://m/y"}}
    assert refresh.row_groups(snap, cands) == [
        ["C1:1.1", slack["link"]], ["https://g/1"], [E + "mail:1", "https://o/1"], [E + "mail:2"], [E + "mail:4", "https://o/4"],
        [MARK, GMAIL["link"]], [E + "gmail:u"],
        ["C2:2.2", closing["link"]], ["mail:3", E + "mail:3"], ["gmail:x", E + "gmail:x"], ["unsub:9"]]


E = "https://work-queue.local/email/"


# ─── Rows sent before the update ─────────────────────────────────────────────

def old_send(wq, task=None):
    """A row sent by the code before ids: a task and a queued entry, no id on either."""
    write_todos(wq, [{"id": "td-1", "status": "open", **(task or {})}])
    config.write_json(wq.data / "queued.json", {ROW: {"id": "td-1", "at": "2026-11-01T00:00:00+00:00", "mode": "assist"}})


def agreed(wq) -> str:
    ids = {wq.items()[0]["trace"], json.loads((wq.data / "queued.json").read_text())[ROW]["trace"], registry()[ROW]["trace"]}
    assert len(ids) == 1 and pointers.is_trace_id(ids.copy().pop())
    return ids.pop()


def test_a_row_sent_before_the_update_takes_the_id_the_pulse_gave_its_task(wq):
    old_send(wq)
    todostore.stamp_missing_traces()
    given = wq.items()[0]["trace"]
    refresh.stamp_traces({}, {})
    assert agreed(wq) == given


def test_a_row_sent_before_the_update_gives_its_task_the_id_the_refresh_gave_it(wq):
    old_send(wq)
    traces.stamp([[ROW]], day(-1))
    given = registry()[ROW]["trace"]
    refresh.stamp_traces({}, {})
    assert agreed(wq) == given
    assert todostore.stamp_missing_traces() == 0  # the pulse finds nothing left to stamp


def test_a_row_sent_before_the_update_with_no_id_anywhere_gets_one_id_for_all_three(wq):
    old_send(wq)
    before = json.loads(wq.todo.read_text())
    refresh.stamp_traces({}, {})
    agreed(wq)
    after = json.loads(wq.todo.read_text())
    assert {k: v for k, v in after["items"][0].items() if k != "trace"} == before["items"][0]
    assert after["_lastUpdated"] == before["_lastUpdated"]


def test_a_row_and_a_task_that_both_have_different_ids_keep_them_and_the_entry_records_the_rows(wq):
    old_send(wq, {"trace": LEGACY})
    config.write_json(traces.path(), {ROW: {"trace": OWN, "seen": DAY}})
    refresh.stamp_traces({}, {})
    assert wq.items()[0]["trace"] == LEGACY and registry()[ROW]["trace"] == OWN
    assert json.loads((wq.data / "queued.json").read_text())[ROW]["trace"] == OWN


def test_a_task_that_is_done_is_matched_too_and_a_task_that_is_gone_is_skipped(wq):
    wq.todo.write_text(json.dumps({"items": [], "completed": [{"id": "td-1", "status": "done"}]}))
    config.write_json(wq.data / "queued.json", {ROW: {"id": "td-1"}, ROW + "2": {"id": "td-9"}, ROW + "3": "text"})
    traces.reconcile(DAY)
    assert json.loads(wq.todo.read_text())["completed"][0]["trace"] == registry()[ROW]["trace"]
    assert set(registry()) == {ROW}


def test_sending_a_row_again_matches_it_with_its_task_and_changes_nothing_once_they_agree(wq):
    old_send(wq, {"url": ROW})
    assert server.queue_todo({"key": ROW, "title": "A"}) == (200, {"id": "td-1", "existing": True})
    agreed(wq)
    files = {p: p.read_bytes() for p in (wq.todo, wq.data / "queued.json", traces.path())}
    server.queue_todo({"key": ROW, "title": "A"})
    assert {p: p.read_bytes() for p in files} == files


def test_matching_a_row_with_its_task_never_stops_a_refresh_or_a_send(wq, capsys):
    old_send(wq)
    wq.todo.write_text("{not json")
    refresh.stamp_traces({}, {})
    wq.todo.write_text(json.dumps({"items": [{"id": "td-1"}]}))
    traces.path().mkdir(parents=True)
    refresh.stamp_traces({}, {})
    assert "Traceback" in capsys.readouterr().err
    assert "trace" not in wq.items()[0]


def test_a_task_file_that_changed_while_matching_is_left_as_the_other_writer_wrote_it(wq, monkeypatch):
    old_send(wq)
    real = todostore.replace_if_unchanged

    def raced(before, text):
        wq.todo.write_text(json.dumps({"items": [{"id": "td-1"}, {"id": "td-2", "title": "captured meanwhile"}]}))
        return real(before, text)
    monkeypatch.setattr(todostore, "replace_if_unchanged", raced)
    refresh.stamp_traces({}, {})
    assert [i["id"] for i in wq.items()] == ["td-1", "td-2"] and "trace" not in wq.items()[0]
    monkeypatch.setattr(todostore, "replace_if_unchanged", real)
    refresh.stamp_traces({}, {})  # the next refresh matches them
    assert wq.items()[0]["trace"] == registry()[ROW]["trace"]


# ─── Sending a row ───────────────────────────────────────────────────────────

def test_a_send_stores_the_rows_id_on_the_task_and_the_queued_entry(wq, today):
    traces.stamp([[ROW]], day(-1))
    row_id = registry()[ROW]["trace"]
    assert server.queue_todo({"key": ROW, "title": "A"}) == (200, {"id": "td-051", "existing": False})
    assert wq.items()[-1]["trace"] == row_id
    assert json.loads((wq.data / "queued.json").read_text())[ROW]["trace"] == row_id


def test_a_send_mints_and_saves_an_id_for_a_row_that_has_none(wq, today):
    server.queue_todo({"key": ROW, "title": "A"})
    task_id = wq.items()[-1]["trace"]
    assert pointers.is_trace_id(task_id) and task_id.startswith("tr-")
    assert registry()[ROW] == {"trace": task_id, "seen": DAY, "first": MADE}


def test_an_id_in_the_request_is_ignored(wq):
    forged = "tr-" + "0" * 16
    server.queue_todo({"key": ROW, "title": "A", "trace": forged, "id": forged})
    server.queue_todo({"key": ROW + "2", "title": "B", "trace": forged})
    assert forged not in {i.get("trace") for i in wq.items()}
    assert forged not in traces.path().read_text() and forged not in (wq.data / "queued.json").read_text()


def test_sending_twice_changes_nothing_and_the_second_send_keeps_the_first_task(wq):
    server.queue_todo({"key": ROW, "title": "A"})
    files = {p: p.read_bytes() for p in (wq.todo, wq.data / "queued.json", traces.path(), wq.data / "marks.json")}
    assert server.queue_todo({"key": ROW, "title": "A", "trace": OWN})[1]["existing"] is True
    assert {p: p.read_bytes() for p in files} == files


def test_a_row_sent_again_after_its_task_was_removed_keeps_its_id(wq):
    server.queue_todo({"key": ROW, "title": "A"})
    first = registry()[ROW]["trace"]
    doc = json.loads(wq.todo.read_text())
    doc["items"][-1]["status"] = "removed"
    wq.todo.write_text(json.dumps(doc))
    assert server.queue_todo({"key": ROW, "title": "A"})[1]["existing"] is False
    assert wq.items()[-1]["trace"] == first


def test_a_lost_registry_doesnt_give_a_sent_row_a_second_id(wq):
    server.queue_todo({"key": ROW, "title": "A"})
    first = registry()[ROW]["trace"]
    traces.path().unlink()
    doc = json.loads(wq.todo.read_text())
    doc["items"][-1]["status"] = "removed"
    wq.todo.write_text(json.dumps(doc))
    server.queue_todo({"key": ROW, "title": "A"})
    assert wq.items()[-1]["trace"] == first and registry()[ROW]["trace"] == first


def test_a_registry_that_cant_be_used_never_stops_a_send(wq, capsys):
    traces.path().mkdir(parents=True)
    assert server.queue_todo({"key": ROW, "title": "A"}) == (200, {"id": "td-051", "existing": False})
    carried = wq.items()[-1]["trace"]
    assert pointers.is_trace_id(carried)
    assert json.loads((wq.data / "queued.json").read_text())[ROW]["trace"] == carried
    assert "Traceback" in capsys.readouterr().err


def test_a_lock_that_cant_be_taken_never_stops_a_send(wq, monkeypatch):
    monkeypatch.setattr(traces, "lock", lambda timeout: (_ for _ in ()).throw(OSError("no lock")))
    assert server.queue_todo({"key": ROW, "title": "A"})[0] == 200
    assert pointers.is_trace_id(wq.items()[-1]["trace"])


def test_a_row_looked_up_with_a_bad_known_id_gets_a_new_one(wq):
    assert pointers.is_trace_id(traces.trace_for(ROW, "not an id"))
    assert traces.trace_for(ROW + "2", LEGACY) == LEGACY


# ─── Every place a task is created ───────────────────────────────────────────

def test_a_goal_step_staged_as_a_task_carries_an_id(wq):
    item, created = goals._stage_todo({"id": "g-1"}, "research", "Title", "Detail", True, 1783598400.0)
    assert created and pointers.is_trace_id(item["trace"]) and item["trace"].startswith("tr-")
    assert json.loads(wq.todo.read_text())["items"][-1]["trace"] == item["trace"]
    again, created = goals._stage_todo({"id": "g-1"}, "research", "Title", "Detail", True, 1783598400.0)
    assert not created and again["trace"] == item["trace"]


def test_a_task_created_by_a_policy_carries_an_id_and_a_repeat_keeps_it(wq):
    event = {"id": "eid-12345678", "source": "slack", "kind": "k", "title": "T", "snippet": "s"}
    todo_id, created = policy_triage.create_todo(event, "r1", {"title": "T"}, now=1783598400.0)
    assert created
    made = [i for i in wq.items() if i["id"] == todo_id][0]
    assert pointers.is_trace_id(made["trace"]) and made["trace"].startswith("tr-")
    assert policy_triage.create_todo(event, "r1", {"title": "T"}, now=1783598400.0) == (todo_id, False)
    assert [i for i in wq.items() if i["id"] == todo_id][0]["trace"] == made["trace"]


# ─── The backstop in the pulse ───────────────────────────────────────────────

def test_the_backstop_stamps_only_tasks_without_a_valid_id_and_changes_nothing_else(wq):
    items = [{"id": "td-1", "status": "open", "title": "naïve — ünïcode"}, {"id": "td-2", "trace": OWN, "status": "open"},
             {"id": "td-3", "trace": LEGACY}, {"id": "td-4", "trace": "tr-short"}, {"id": "td-5", "trace": 7}, "stray",
             {"title": "no id of its own"}]
    write_todos(wq, items, completed=[{"id": "td-9"}], removed=[{"id": "td-8"}])
    before = json.loads(wq.todo.read_text())
    standins = [todostore.trace_of(before["items"][n]) for n in (0, 3, 4)]
    assert todostore.stamp_missing_traces() == 4
    after = json.loads(wq.todo.read_text())
    assert [i.get("trace") for i in after["items"][1:3]] == [OWN, LEGACY]
    # the id a caller got from `trace_of` before the pass is the id stored by it
    assert [after["items"][n]["trace"] for n in (0, 3, 4)] == standins and all(t.startswith("tl-") for t in standins)
    assert after["items"][6]["trace"].startswith("tr-") and pointers.is_trace_id(after["items"][6]["trace"])
    for n in (0, 3, 4, 6):
        del after["items"][n]["trace"]
    for n in (3, 4):
        before["items"][n].pop("trace")
    assert after == before  # the other tasks, the other lists, `_lastUpdated`: all as they were
    assert wq.todo.read_text().endswith("}\n") and "naïve — ünïcode" in wq.todo.read_text()
    assert list(wq.todo.parent.glob("*.tmp")) == []


def test_the_backstop_writes_nothing_when_no_task_is_missing_an_id(wq):
    write_todos(wq, [{"id": "td-1", "trace": OWN}, {"id": "td-2", "trace": LEGACY}])
    os.utime(wq.todo, (1, 1))
    before = wq.todo.read_bytes()
    assert todostore.stamp_missing_traces() == 0
    assert wq.todo.read_bytes() == before and wq.todo.stat().st_mtime == 1


def test_the_backstop_keeps_a_file_that_has_no_final_newline_the_way_it_was(wq):
    wq.todo.write_text(json.dumps({"items": [{"id": "td-1"}]}, indent=2))
    todostore.stamp_missing_traces()
    assert not wq.todo.read_text().endswith("\n") and "trace" in json.loads(wq.todo.read_text())["items"][0]


@pytest.mark.parametrize("text", [None, "{not json", "[1]", '{"items": "x"}', '{"completed": []}'])
def test_the_backstop_leaves_a_missing_or_odd_file_alone(wq, text):
    wq.todo.unlink()
    if text is not None:
        wq.todo.write_text(text)
    assert todostore.stamp_missing_traces() == 0
    assert (wq.todo.read_text() if text is not None else not wq.todo.exists()) in (text, True)


def test_the_backstop_waits_for_the_todo_lock(wq, monkeypatch):
    write_todos(wq, [{"id": "td-1"}])
    asked, got = threading.Event(), threading.Event()
    real = todostore.todo_lock

    @contextlib.contextmanager
    def watched():
        asked.set()
        with real():
            got.set()
            yield
    monkeypatch.setattr(todostore, "todo_lock", watched)
    result = []
    worker = threading.Thread(target=lambda: result.append(todostore.stamp_missing_traces()))
    with real():  # another writer holds the lock
        worker.start()
        assert asked.wait(5)  # the pass asked for the lock ...
        assert not got.is_set() and "trace" not in json.loads(wq.todo.read_text())["items"][0]  # ... and has not got it
    worker.join(5)
    assert got.is_set() and result == [1] and "trace" in json.loads(wq.todo.read_text())["items"][0]


def test_two_pulses_at_once_stamp_a_task_once(wq, monkeypatch):
    write_todos(wq, [{"id": "td-1"}, {"id": "td-2"}])

    def slow_loads(text):  # a pause between the read and the write, where two unlocked pulses would both stamp
        found = json.loads(text)
        time.sleep(0.05)
        return found
    monkeypatch.setattr(todostore, "json", types.SimpleNamespace(loads=slow_loads, dumps=json.dumps))
    barrier, counts = threading.Barrier(6), []

    def pulse():
        barrier.wait()
        counts.append(todostore.stamp_missing_traces())
    threads = [threading.Thread(target=pulse) for _ in range(6)]
    [t.start() for t in threads]
    [t.join(10) for t in threads]
    assert sorted(counts) == [0, 0, 0, 0, 0, 2]


def test_the_pulse_step_stamps_and_never_fails_the_pulse(wq, monkeypatch, caplog):
    pulse = load_pulse()
    write_todos(wq, [{"id": "td-1", "status": "open"}])
    monkeypatch.setattr(sys, "path", [p for p in sys.path if p != str(REPO / "src")])  # the step adds it back
    with caplog.at_level("INFO"):
        pulse.run_trace_step()
        pulse.run_trace_step()
    assert pointers.is_trace_id(wq.items()[0]["trace"])
    assert [r.message for r in caplog.records if "trace ids" in r.message] == ["trace ids: stamped 1 TODO(s)"]
    monkeypatch.setattr(todostore, "stamp_missing_traces", lambda: (_ for _ in ()).throw(OSError("disk full")))
    pulse.run_trace_step()
    assert "could not stamp" in caplog.text


def test_the_pulse_runs_the_step_after_the_planner_and_before_dispatch_and_skips_it_on_a_dry_run():
    text = (REPO / "bin/pulse.py").read_text()
    main = text[text.index("def main()"):]
    assert main.index("run_planner_step(pulse_idx)") < main.index("run_trace_step()") < main.index("pick_ws_batch()")
    assert "    if not dry_run:\n        run_trace_step()" in main


def test_the_first_pulse_after_the_upgrade_stamps_the_open_tasks_and_dispatch_works_as_before(wq):
    pulse = load_pulse()
    write_todos(wq, [{"id": "td-1", "status": "open", "autoDispatch": True},
                     {"id": "td-2", "status": "done", "autoDispatch": True}])
    config.write_json(wq.data / "queued.json", {ROW: {"id": "td-1", "at": "2026-11-01T00:00:00+00:00", "mode": "assist"}})
    assert server.queued_view()[ROW]["id"] == "td-1"
    assert pulse._mark_todo_dispatched("td-1", "workspace:7") is True
    stamped = json.loads(wq.todo.read_text())["items"][0]
    assert (stamped["dispatchedWs"], stamped["status"]) == ("workspace:7", "in-progress") and "trace" not in stamped
    pulse.run_trace_step()
    after = json.loads(wq.todo.read_text())
    assert all(pointers.is_trace_id(i["trace"]) for i in after["items"])
    assert after["_lastUpdated"] == "2026-01-01T00:00:00Z"
    assert pulse._mark_todo_dispatched("td-2", "workspace:8") is True
    assert pointers.is_trace_id(json.loads(wq.todo.read_text())["items"][0]["trace"])
    assert server.queued_view()[ROW]["id"] == "td-1"


# ─── Invisible: readers that don't know the field ────────────────────────────

def load_script(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, REPO / "bin" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def without_ids(items):
    return [{k: v for k, v in i.items() if k != "trace"} for i in items]


ITEMS = [{"id": "td-1", "priority": "P1", "title": "Fix the login", "status": "open", "autoDispatch": True,
          "createdAt": "2026-11-01", "detail": "Why", "url": "https://x.example/1"},
         {"id": "td-2", "priority": "P3", "title": "Done thing", "status": "done", "statusUpdatedAt": "2026-11-02T00:00:00Z"},
         {"id": "td-3", "priority": "P2", "title": "Waiting", "status": "blocked", "statusReason": "needs a key"}]
WORLD = {"todo": {}, "live_sessions": [], "now": "2026-11-18T00:00:00Z"}


def test_the_dashboard_renders_the_same_with_ids_and_without(wq):
    (wq.home / ".assistant").mkdir(exist_ok=True)
    renderer = load_script("renderer_trace_ids", "render-assistant-page.py")
    with_ids = [{**i, "trace": f"tr-{n:016x}"} for n, i in enumerate(ITEMS)]
    pages = {}
    for label, items in (("with", with_ids), ("without", ITEMS)):
        write_todos(wq, items, completed=[], removed=[])
        pages[label] = renderer.render_todos_tab(dict(WORLD))
    assert pages["with"] == pages["without"] and "Fix the login" in pages["with"][0]
    assert "tr-" not in pages["with"][0]


def test_the_work_queue_shows_the_same_with_ids_and_without(wq, today):
    import re
    fake_tool(wq.bin, "gh", [("search prs", [PR])])
    wq.sources({"github": {"skip_authors": []}})
    server.queue_todo({"key": ROW, "title": "A"})
    # The page carries the time it was built twice, from the real clock: once for people ("Nov 24, 5:03 PM") and
    # once to the second for the page's own script. Both are left out of the comparison; two refreshes a second
    # apart are still the same page.
    built = re.compile(r"[A-Z][a-z]{2} \d{1,2}, \d{1,2}:\d{2} [AP]M|\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}")
    refresh.run(now=NOW)
    refresh.run(now=NOW)  # the second refresh is the first with the model's saved answers, the same as the third
    page_with, view_with = built.sub("", refresh.site_path().read_text()), server.queued_view()
    # The same files as today's code wrote them: no registry, no id on the task or its queued entry.
    traces.path().unlink()
    doc = json.loads(wq.todo.read_text())
    doc["items"] = without_ids(doc["items"])
    wq.todo.write_text(json.dumps(doc))
    queued = json.loads((wq.data / "queued.json").read_text())
    config.write_json(wq.data / "queued.json", {k: without_ids([v])[0] for k, v in queued.items()})
    refresh.run(now=NOW)
    assert built.sub("", refresh.site_path().read_text()) == page_with
    assert server.queued_view() == view_with and ROW in view_with
    assert "tr-" not in page_with and "tr-" not in json.dumps(view_with)
    assert server.state()["queued"] == view_with


def test_every_reader_of_the_task_file_tolerates_the_new_field(wq, monkeypatch, tmp_path):
    todo_server = load_script("todo_server_trace_ids", "todo-server.py")
    monkeypatch.setattr(todo_server, "JSON_PATH", wq.todo)
    results = {}
    for label, items in (("with", [{**i, "trace": OWN} for i in ITEMS]), ("without", ITEMS)):
        write_todos(wq, items)
        todo_server.set_flag("td-1", "autoDispatch", False)
        todo_server.append_detail("td-1", "more")
        todo_server.dispatch_now("td-2")
        stamped = without_ids(json.loads(wq.todo.read_text())["items"])
        results[label] = [{k: re.sub(r"\d{4}-\d\d-\d\dT[\d:]+Z", "NOW", v) if isinstance(v, str) else v
                           for k, v in i.items()} for i in stamped]  # the edits stamp the clock
    assert results["with"] == results["without"]
    write_todos(wq, [{**i, "trace": OWN} for i in ITEMS])
    todo_server.set_flag("td-1", "autoDispatch", True)
    assert [i["trace"] for i in json.loads(wq.todo.read_text())["items"]] == [OWN] * 3
    # todo_result: the agent's finish command reads and rewrites the task and keeps its id
    server.queue_todo({"key": ROW, "title": "A"})
    task = wq.items()[-1]["id"]
    todo_result.main(task, "did it https://x.example/pr/1")
    done = [i for i in wq.items() if i["id"] == task][0]
    assert done["status"] == "done" and pointers.is_trace_id(done["trace"])
    assert server.queued_view()[ROW]["status"] == "done"


def test_the_slack_reactors_tests_for_its_task_writer_pass():
    """The reactor's own Node test (slack-reactor/test/todo-store.test.js) for the id it puts on a captured thread."""
    import shutil
    import subprocess
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    done = subprocess.run([node, "--test", "slack-reactor/test/todo-store.test.js"], cwd=REPO, text=True,
                          capture_output=True, timeout=120)
    assert done.returncode == 0, done.stdout + done.stderr


# ─── A busy or unusable registry, and the task file's own writes ─────────────

def test_a_send_gives_up_on_a_busy_registry_after_a_short_wait_and_uses_the_id_it_holds(wq, monkeypatch):
    traces.stamp([[ROW]], day(-1))
    held = registry()[ROW]["trace"]
    monkeypatch.setattr(traces, "LOCK_WAIT", 0.05)
    with (wq.data / "traces.lock").open("a") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        assert traces.trace_for(ROW) == held
        fresh = traces.trace_for(ROW + "2")
        assert pointers.is_trace_id(fresh) and fresh != held
        assert traces.trace_for(ROW + "3", known=OWN) == OWN
        with pytest.raises(TimeoutError):
            with traces.lock(0.05):
                pass
    assert ROW + "2" not in registry()


def test_a_send_whose_registry_cant_be_written_uses_the_id_the_row_already_has(wq, monkeypatch, capsys):
    traces.stamp([[ROW]], day(-1))
    held = registry()[ROW]["trace"]
    monkeypatch.setattr(config, "write_json", lambda path, data: (_ for _ in ()).throw(OSError("read-only")))
    assert traces.trace_for(ROW) == held  # found before any write is needed
    fresh = traces.trace_for(ROW + "2")
    assert pointers.is_trace_id(fresh) and "Traceback" in capsys.readouterr().err
    assert traces.trace_for(ROW + "3", known=OWN) == OWN


def test_lookup_only_reads(wq):
    assert traces.lookup(ROW) == ""
    assert not traces.path().exists() and not (wq.data / "traces.lock").exists()
    traces.stamp([[ROW]], DAY)
    assert traces.lookup(ROW) == registry()[ROW]["trace"] and traces.lookup(ROW + "x") == ""


def test_a_task_has_an_id_to_use_even_before_the_pulse_stamps_it():
    assert todostore.trace_of({"id": "td-1", "trace": OWN}) == OWN
    standin = todostore.trace_of({"id": "td-1", "createdAt": "2026-11-01"})
    assert standin == pointers.legacy_trace_id("task", "td-1\x002026-11-01") and standin.startswith("tl-")
    assert standin == todostore.trace_of({"id": "td-1", "createdAt": "2026-11-01", "trace": "bad"})
    assert standin != todostore.trace_of({"id": "td-2", "createdAt": "2026-11-01"})
    assert pointers.is_trace_id(todostore.trace_of(None)) and pointers.is_trace_id(todostore.trace_of({"id": 7}))


def test_the_task_file_is_replaced_through_a_temporary_file_of_its_own_that_is_never_left_behind(wq, monkeypatch):
    write_todos(wq, [{"id": "td-1"}])
    other_writers = wq.todo.with_name(wq.todo.name + ".tmp")
    other_writers.write_text("another writer's half-written file")
    replaced = []
    real = os.replace
    monkeypatch.setattr(todostore.os, "replace", lambda a, b: (replaced.append((Path(a).name, Path(b).name)), real(a, b))[1])
    assert todostore.stamp_missing_traces() == 1
    assert replaced == [(f"assistant-todo.json.{os.getpid()}.tmp", "assistant-todo.json")]
    assert other_writers.read_text() == "another writer's half-written file"
    assert sorted(p.name for p in wq.todo.parent.glob("*.tmp")) == ["assistant-todo.json.tmp"]


def test_a_task_file_changed_between_the_read_and_the_write_is_left_as_the_other_writer_wrote_it(wq, monkeypatch):
    write_todos(wq, [{"id": "td-1"}])
    theirs = json.dumps({"items": [{"id": "td-1"}, {"id": "td-2", "title": "captured from Slack"}]})
    real = todostore.json.dumps

    def racing(*args, **kwargs):
        text = real(*args, **kwargs)
        wq.todo.write_text(theirs)  # the reactor writes after the pass has read and before it replaces
        return text
    monkeypatch.setattr(todostore, "json", __import__("types").SimpleNamespace(loads=json.loads, dumps=racing))
    assert todostore.stamp_missing_traces() == 0
    assert wq.todo.read_text() == theirs
    assert list(wq.todo.parent.glob("*.tmp")) == []
    monkeypatch.setattr(todostore, "json", json)
    assert todostore.stamp_missing_traces() == 2  # the next pulse stamps both


def test_a_task_file_that_vanishes_before_the_replace_is_not_recreated_and_leaves_nothing_behind(wq):
    write_todos(wq, [{"id": "td-1"}])
    before = wq.todo.read_text()
    wq.todo.unlink()
    assert todostore.replace_if_unchanged(before, "{}") is False
    assert not wq.todo.exists() and list(wq.todo.parent.glob("*.tmp")) == []


# ─── Round three: ordering, bounded waits, stable stand-ins ──────────────────

def test_a_discarded_task_write_leaves_the_row_the_entry_and_the_task_for_the_next_refresh(wq, monkeypatch):
    old_send(wq)
    real, calls = todostore.replace_if_unchanged, []

    def discarded_once(before, text):
        calls.append(1)
        return False if len(calls) == 1 else real(before, text)
    monkeypatch.setattr(todostore, "replace_if_unchanged", discarded_once)
    traces.reconcile(DAY)
    assert not traces.path().exists() and "trace" not in json.loads((wq.data / "queued.json").read_text())[ROW]
    assert "trace" not in wq.items()[0]
    todostore.stamp_missing_traces()  # the pulse stamps the task meanwhile
    stored = wq.items()[0]["trace"]
    traces.reconcile(DAY)
    assert agreed(wq) == stored == todostore.trace_of({"id": "td-1"})


def test_matching_never_overwrites_an_id_the_queued_entry_holds_and_an_idle_refresh_leaves_it_alone(wq):
    old_send(wq)
    config.write_json(wq.data / "queued.json", {ROW: {"id": "td-1", "mode": "assist", "trace": LEGACY}})
    config.write_json(traces.path(), {ROW: {"trace": OWN, "seen": DAY}})
    traces.reconcile(DAY)
    entry = json.loads((wq.data / "queued.json").read_text())[ROW]
    assert entry["trace"] == LEGACY and registry()[ROW]["trace"] == OWN and wq.items()[0]["trace"] == OWN
    queued = wq.data / "queued.json"
    before = (queued.stat().st_ino, queued.stat().st_mtime_ns, queued.read_bytes())
    refresh.stamp_traces({}, {})
    refresh.stamp_traces({}, {})
    assert (queued.stat().st_ino, queued.stat().st_mtime_ns, queued.read_bytes()) == before


def test_every_wait_while_the_task_lock_is_held_is_at_most_half_a_second(wq, monkeypatch):
    waits, real = [], traces.lock

    @contextlib.contextmanager
    def recording(timeout):
        waits.append(timeout)
        with real(timeout):
            yield
    monkeypatch.setattr(traces, "lock", recording)
    old_send(wq, {"url": ROW})
    server.queue_todo({"key": ROW, "title": "A"})  # the already-sent path
    refresh.stamp_traces({}, {})
    assert waits and max(waits[:2]) <= 0.5 == traces.LOCK_WAIT and traces.TASK_LOCK_WAIT <= 2
    assert max(waits) <= traces.REFRESH_WAIT


def test_a_refresh_gives_up_on_a_busy_task_lock_and_matches_the_rows_next_time(wq, monkeypatch, capsys):
    old_send(wq)
    monkeypatch.setattr(traces, "TASK_LOCK_WAIT", 0.05)
    done = threading.Event()
    with todostore.todo_lock():  # another writer holds the task lock
        worker = threading.Thread(target=lambda: (refresh.stamp_traces({}, {}), done.set()), daemon=True)
        worker.start()
        finished = done.wait(5)
    worker.join(5)
    assert finished and "TODO store was busy" in capsys.readouterr().err
    assert "trace" not in wq.items()[0]
    refresh.stamp_traces({}, {})
    agreed(wq)


def test_the_task_lock_can_be_taken_with_a_time_limit(wq):
    release, holding = threading.Event(), threading.Event()

    def hold():
        with todostore.todo_lock():
            holding.set()
            release.wait(5)
    holder = threading.Thread(target=hold)
    holder.start()
    holding.wait(5)
    with pytest.raises(TimeoutError):
        with todostore.todo_lock(0.05):
            pass
    release.set()
    holder.join(5)
    with todostore.todo_lock(0.05):
        pass


def test_a_busy_registry_lock_is_given_up_on_after_the_time_limit_and_not_before_it_runs_out(wq, monkeypatch):
    sleeps = []

    def counting(seconds):
        sleeps.append(seconds)
        assert len(sleeps) < 200, "the wait for the lock has no end"
        time.sleep(seconds)
    monkeypatch.setattr(traces, "time", types.SimpleNamespace(monotonic=time.monotonic, sleep=counting))
    wq.data.mkdir(exist_ok=True)
    with (wq.data / "traces.lock").open("a") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        with pytest.raises(TimeoutError):
            with traces.lock(0.05):
                pass
    assert 1 <= len(sleeps) < 200


def test_a_page_of_rows_is_looked_up_with_one_read(wq, monkeypatch):
    traces.stamp([[ROW], ["https://b"]], DAY)
    reads, real = [], traces.read_registry
    monkeypatch.setattr(traces, "read_registry", lambda: (reads.append(1), real())[1])
    got = traces.lookup_many([ROW, "https://b", "https://none"])
    assert reads == [1] and got == {ROW: registry()[ROW]["trace"], "https://b": registry()["https://b"]["trace"],
                                    "https://none": ""}
    assert traces.lookup_many([]) == {}


def test_a_file_that_cant_be_read_is_unreadable_not_empty(wq):
    config.write_json(traces.path(), {ROW: {"trace": OWN, "seen": DAY}})
    before = traces.path().read_bytes()
    traces.path().chmod(0)
    try:
        assert traces.lookup(ROW) == "" and traces.lookup_many([ROW]) == {ROW: ""}
        with pytest.raises(OSError):
            traces.stamp([[ROW + "2"]], DAY)
        assert traces.trace_for(ROW + "3", known=OWN) == OWN  # a send carries on
    finally:
        traces.path().chmod(0o644)
    assert traces.path().read_bytes() == before


def test_a_temporary_file_a_killed_pulse_left_behind_is_removed_after_an_hour_and_no_other_writers_is(wq):
    write_todos(wq, [{"id": "td-1"}])
    folder = wq.todo.parent
    names = {"stale": "assistant-todo.json.4242.tmp", "fresh": "assistant-todo.json.4343.tmp",
             "old shared": "assistant-todo.json.tmp", "reactor": "assistant-todo.json.4444.ab12cd34.tmp"}
    for name in names.values():
        (folder / name).write_text("x")
    for kind in ("stale", "old shared", "reactor"):
        os.utime(folder / names[kind], (1, 1))
    todostore.stamp_missing_traces()
    assert sorted(p.name for p in folder.glob("*.tmp")) == sorted(
        [names["fresh"], names["old shared"], names["reactor"]])


def test_matching_gives_an_id_only_to_the_tasks_that_lack_one_and_cleaning_up_never_stops_a_write(wq, monkeypatch):
    write_todos(wq, [{"id": "td-1", "trace": OWN}, {"id": "td-2"}])
    config.write_json(wq.data / "queued.json", {ROW: {"id": "td-1"}, ROW + "2": {"id": "td-2"}})
    traces.reconcile(DAY)
    assert [i["trace"] for i in wq.items()][0] == OWN and registry()[ROW]["trace"] == OWN
    assert wq.items()[1]["trace"] == registry()[ROW + "2"]["trace"] != OWN
    monkeypatch.setattr(todostore, "re", types.SimpleNamespace(fullmatch=lambda *a: (_ for _ in ()).throw(OSError("gone"))))
    (wq.todo.parent / "assistant-todo.json.1.tmp").write_text("x")
    before = wq.todo.read_text()
    assert todostore.replace_if_unchanged(before, before + " ") is True
