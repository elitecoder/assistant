"""Trace ids: every Work queue row and every task is given an id when it is first seen, stored with it, and never
replaced. Nothing reads the id yet, so nothing a user sees may change."""
from __future__ import annotations

import contextlib
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
from test_work_queue_inputs import NOW

REPO = Path(__file__).resolve().parent.parent
ROW = "https://x.slack.com/archives/C1/p1"
PR = {"number": 7, "title": "Fix it", "url": "https://github.com/o/r/pull/7", "repository": {"nameWithOwner": "o/r"},
      "author": {"login": "ana"}, "createdAt": "2026-11-02T10:00:00Z", "isDraft": False}
PR_LINK = PR["url"]
DAY = "2026-11-18"
OWN = "tr-" + "ab" * 8
LEGACY = "tl-" + "cd" * 8


def registry() -> dict:
    return json.loads(traces.path().read_text())


def day(offset: int) -> str:
    from datetime import date, timedelta
    return (date.fromisoformat(DAY) + timedelta(days=offset)).isoformat()


@pytest.fixture
def today(monkeypatch):
    """The refresh's clock, fixed, so no test depends on the date."""
    monkeypatch.setattr(traces, "today", lambda: DAY)


def load_pulse():
    spec = importlib.util.spec_from_file_location("pulse_trace_ids", REPO / "bin/pulse.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_todos(wq, items, **extra):
    wq.todo.write_text(json.dumps({"_schema": 1, "_lastUpdated": "2026-01-01T00:00:00Z", **extra, "items": items},
                                  indent=2, ensure_ascii=False) + "\n")


# ─── Work queue rows: the registry ───────────────────────────────────────────

def test_a_refresh_mints_an_id_for_each_new_row_and_keeps_it_across_refreshes_and_a_new_title(wq, today):
    fake_tool(wq.bin, "gh", [("search prs", [PR])])
    wq.sources({"github": {"skip_authors": []}})
    refresh.run(now=NOW)
    first = registry()[PR_LINK]
    assert pointers.is_trace_id(first["trace"]) and first["seen"] == DAY
    fake_tool(wq.bin, "gh", [("search prs", [{**PR, "title": "A new title"}])])
    refresh.run(now=NOW)
    assert registry()[PR_LINK] == first
    assert "A new title" in refresh.site_path().read_text()


def test_a_refresh_notes_the_day_a_row_was_seen_and_writes_nothing_when_that_hasnt_changed(wq, today, monkeypatch):
    traces.stamp([ROW], day(-3))
    first = traces.path().read_text()
    os.utime(traces.path(), (1, 1))
    traces.stamp([ROW], day(-3))
    assert traces.path().read_text() == first and traces.path().stat().st_mtime == 1
    traces.stamp([ROW], DAY)
    assert registry()[ROW] == {"trace": json.loads(first)[ROW]["trace"], "seen": DAY}


def test_rows_are_the_links_of_every_source_and_a_row_without_a_link_is_skipped():
    snap = {"slack": [{"link": "https://a.slack.com/1"}, {"link": None}], "github": [{"link": "https://g/1"}],
            "outlook": [{"link": "https://o/1"}, {"link": "http://not-secure"}],
            "gmail": {"me@x.com": [{"link": "https://m/1"}]}}
    assert refresh.row_links(snap) == ["https://a.slack.com/1", "https://g/1", "https://o/1", "https://m/1"]
    assert refresh.row_links({}) == []


def test_an_id_once_stored_is_never_replaced(wq):
    traces.stamp([ROW], day(-1))
    kept = registry()[ROW]["trace"]
    for _ in range(3):
        traces.stamp([ROW, ROW + "2"], DAY)
        assert traces.trace_for(ROW) == kept
        assert registry()[ROW]["trace"] == kept


def test_a_row_not_seen_for_60_days_is_dropped_unless_a_sent_row_refers_to_it(wq):
    traces.config.write_json(traces.path(), {
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
    assert traces.MAX_ENTRIES == 5 and traces.KEEP_DAYS == 60


def test_the_limits_are_60_days_and_5000_rows():
    assert (traces.KEEP_DAYS, traces.MAX_ENTRIES) == (60, 5000)


def test_a_sent_row_keeps_the_id_its_queued_entry_holds_when_the_registry_is_lost(wq):
    config.write_json(wq.data / "queued.json", {ROW: {"id": "td-1", "trace": OWN}, "https://b": {"id": "td-2", "trace": "bad"}})
    traces.stamp([ROW, "https://b", "https://c"], DAY)
    got = registry()
    assert got[ROW]["trace"] == OWN
    assert all(pointers.is_trace_id(got[k]["trace"]) for k in ("https://b", "https://c"))
    assert got["https://b"]["trace"] != "bad"


def test_a_damaged_registry_is_read_as_empty_and_a_damaged_entry_is_replaced_but_a_good_one_kept(wq):
    wq.data.mkdir(exist_ok=True)
    traces.path().write_text("{not json")
    assert traces.load() == {}
    traces.path().write_text("[1, 2]")
    assert traces.load() == {}
    config.write_json(traces.path(), {"https://a": "text", "https://b": {"trace": "nope", "seen": DAY},
                                      "https://c": {"trace": OWN, "seen": "yesterday"}, "https://d": {"trace": OWN}})
    config.write_json(wq.data / "queued.json", ["not", "an", "object"])
    traces.stamp([ROW, 7, None], DAY)
    assert registry() == {"https://c": {"trace": OWN, "seen": DAY}, "https://d": {"trace": OWN, "seen": DAY},
                          ROW: {"trace": registry()[ROW]["trace"], "seen": DAY}}


def test_a_broken_registry_never_stops_a_refresh(wq, today, monkeypatch, capsys):
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
    monkeypatch.setattr(traces, "stamp", lambda keys: 1 / 0)
    refresh.run(now=NOW)
    assert json.loads(refresh.status_path().read_text())["running"] is False


def test_the_registry_is_read_and_written_inside_its_own_lock(wq, monkeypatch):
    events, real_lock, real_write = [], traces.lock, config.write_json

    @contextlib.contextmanager
    def tracked():
        with real_lock():
            events.append("lock")
            yield
        events.append("unlock")

    def watching(path, data):
        events.append("write " + path.name)
        real_write(path, data)
    monkeypatch.setattr(traces, "lock", tracked)
    monkeypatch.setattr(config, "write_json", watching)
    traces.stamp([ROW], DAY)
    traces.trace_for("https://other")
    assert events == ["lock", "write traces.json", "unlock"] * 2
    assert (wq.data / "traces.lock").exists() and not (wq.data / "marks.lock").exists()


# ─── Sending a row ───────────────────────────────────────────────────────────

def test_a_send_stores_the_rows_id_on_the_task_and_the_queued_entry(wq, today):
    traces.stamp([ROW], day(-1))
    row_id = registry()[ROW]["trace"]
    assert server.queue_todo({"key": ROW, "title": "A"}) == (200, {"id": "td-051", "existing": False})
    assert wq.items()[-1]["trace"] == row_id
    assert json.loads((wq.data / "queued.json").read_text())[ROW]["trace"] == row_id


def test_a_send_mints_and_saves_an_id_for_a_row_that_has_none(wq, today):
    server.queue_todo({"key": ROW, "title": "A"})
    task_id = wq.items()[-1]["trace"]
    assert pointers.is_trace_id(task_id) and task_id.startswith("tr-")
    assert registry()[ROW] == {"trace": task_id, "seen": DAY}


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
    monkeypatch.setattr(traces, "lock", lambda: (_ for _ in ()).throw(OSError("no lock")))
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
             {"id": "td-3", "trace": LEGACY}, {"id": "td-4", "trace": "tr-short"}, {"id": "td-5", "trace": 7}, "stray"]
    write_todos(wq, items, completed=[{"id": "td-9"}], removed=[{"id": "td-8"}])
    before = json.loads(wq.todo.read_text())
    assert todostore.stamp_missing_traces() == 3
    after = json.loads(wq.todo.read_text())
    assert [i.get("trace") for i in after["items"][1:3]] == [OWN, LEGACY]
    for n in (0, 3, 4):
        assert pointers.is_trace_id(after["items"][n]["trace"]) and after["items"][n]["trace"].startswith("tr-")
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


def test_the_backstop_waits_for_the_todo_lock(wq):
    write_todos(wq, [{"id": "td-1"}])
    release, holding = threading.Event(), threading.Event()

    def hold():
        with todostore.todo_lock():
            holding.set()
            release.wait(5)
    holder = threading.Thread(target=hold)
    holder.start()
    holding.wait(5)
    result = []
    worker = threading.Thread(target=lambda: result.append(todostore.stamp_missing_traces()))
    worker.start()
    worker.join(0.3)
    assert worker.is_alive() and "trace" not in json.loads(wq.todo.read_text())["items"][0]
    release.set()
    holder.join(5)
    worker.join(5)
    assert result == [1] and "trace" in json.loads(wq.todo.read_text())["items"][0]


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
    monkeypatch.setattr(todostore, "stamp_missing_traces", lambda: 1 / 0)
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
    built = re.compile(r"[A-Z][a-z]{2} \d{1,2}, \d{1,2}:\d{2} [AP]M")
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
