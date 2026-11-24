"""The records of one piece of work: each step it goes through (it arrives, the model sorts it, the person sends it, an
agent is started on it, the agent reports back, the result is checked) leaves a telemetry record with the row's trace
id, holding only ids, counts, timings and states. Recording never changes what Assistant does, and never breaks it."""
from __future__ import annotations

import builtins
import contextlib
import importlib.util
import json
import re
import sys
import time
from pathlib import Path

import pytest

from assistant import effects, goals, pointers, records, telemetry, todostore, triage as policy_triage
from work_queue import config, ready, refresh, server, todo_result, traces, triage
from work_queue_fakes import Env, MODEL_ENV, Model, fake_claude, fake_tool, gh_pr, use_fakes, wq  # noqa: F401
from test_work_queue_inputs import NOW

REPO = Path(__file__).resolve().parent.parent
REAL_CALL = triage.call_model
PR = gh_pr(7)
LINK = PR["url"]
OWN = "tr-" + "ab" * 8
RUN = "tr-" + "cd" * 8
GH = [("search prs", [PR]), ("pr view", {"state": "MERGED", "comments": [], "reviews": []}),
      ("api user", {"login": "me"})]
DONE_TEXT = f"Opened {LINK} for review"


def trail(trace: str) -> list[dict]:
    return telemetry.events(trace=trace, limit=1000)


def of_kind(kind: str, **fields) -> list[dict]:
    return [e for e in telemetry.events(kind=kind, limit=1000)
            if all(e["fields"].get(k) == v for k, v in fields.items())]


def load_script(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, REPO / "bin" / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def write_todos(env: Env, items, **extra) -> None:
    env.todo.write_text(json.dumps({"_schema": 1, **extra, "items": items}, indent=2) + "\n")


def make_env(root: Path, monkeypatch, enabled: bool = True) -> Env:
    """The `wq` fixture, for a test that needs two of them: its own HOME, data folder and fake tools."""
    env = Env(root)
    env.todo.parent.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(env.home))
    monkeypatch.setenv("WQ_DATA", str(env.data))
    for name in ("SLACK_USER_TOKEN", "GH_HOST", "CMUX_BIN", *MODEL_ENV):
        monkeypatch.delenv(name, raising=False)
    env.todo.write_text(json.dumps({"items": [{"id": "td-041", "status": "open"}], "completed": [{"id": "td-050"}],
                                    "removed": []}))
    use_fakes(monkeypatch, env.bin)
    if not enabled:
        (env.home / ".assistant").mkdir(exist_ok=True)
        (env.home / ".assistant/config.json").write_text(json.dumps({"telemetry": {"enabled": False}}))
    return env


def dispatch(monkeypatch, todo_id: str, workspace: str = "workspace:9", ping_ok: bool = True):
    """`pulse.dispatch_todo(todo_id)` against a fake cmux: (the pulse, its result, the commands it ran)."""
    pulse = load_script("pulse_work_records", "pulse.py")
    pulse.self_update_pulse = lambda pulse_idx: None
    commands: list = []

    def run(cmd, *a, **k):
        commands.append(cmd[1:])
        if "ping" in cmd:
            return (0 if ping_ok else 1), "", ""
        if "new-workspace" in cmd:
            return 0, f"OK {workspace}\n", ""
        if "list-pane-surfaces" in cmd:
            return 0, "surface:9\n", ""
        return 0, "", ""

    monkeypatch.setattr(pulse, "run", run)
    monkeypatch.setattr(pulse, "_cmux_rpc", lambda method, params, timeout=15: {})
    monkeypatch.setattr(pulse, "_surface_read_text", lambda ref, lines=200: "Claude Code v2.1")
    monkeypatch.setattr(pulse.time, "sleep", lambda seconds: None)
    return pulse, pulse.dispatch_todo(todo_id), commands


def source_rows(env: Env, monkeypatch, gh=GH, model=None) -> None:
    fake_tool(env.bin, "gh", gh)
    env.sources({"github": {"skip_authors": []}})


# ─── one piece of work, end to end ───────────────────────────────────────────

def test_one_piece_of_work_leaves_one_trail_from_arrival_to_the_check(wq, monkeypatch):
    fake_claude(wq.home)
    monkeypatch.setattr(triage, "call_model", REAL_CALL)
    source_rows(wq, monkeypatch)
    refresh.run(now=NOW)
    row_trace = traces.lookup(LINK)
    assert pointers.is_trace_id(row_trace)
    status, sent = server.queue_todo({"key": LINK, "title": "Review the change", "mode": "handsoff"})
    assert status == 200
    task = sent["id"]
    pulse, dispatched, _ = dispatch(monkeypatch, task)
    assert dispatched is True
    todo_result.main(task, DONE_TEXT)
    refresh.run(now=NOW)
    assert [(e["kind"], e["subject"], e["fields"].get("state") or e["fields"].get("name") or e["fields"].get("verdict"))
            for e in trail(row_trace)] == [
        ("triage_verdict", pointers.row_pointer(LINK), "agent"),
        ("task", task, "open"),
        ("dispatch", task, None),
        ("task", task, "in-progress"),
        ("task", task, "done"),
        ("check", task, "pr-merged"),
    ]
    verdict, created, dispatch_record, started, finished, check = trail(row_trace)
    assert created["fields"] == {"state": "open", "origin": "work-queue", "mode": "handsoff"}
    assert (dispatch_record["fields"]["outcome"], dispatch_record["fields"]["workspace"],
            dispatch_record["fields"]["mode"]) == ("done", "workspace:9", "handsoff")
    assert started["fields"]["prev_state"] == "open" and finished["fields"]["prev_state"] == "in-progress"
    assert check["fields"]["outcome"] == "done"
    # The row's arrival is the refresh's run: the verdict names it, and that run's records carry the run's own trace.
    run_trace = verdict["fields"]["run_trace"]
    assert pointers.is_trace_id(run_trace) and run_trace != row_trace
    assert {e["kind"] for e in trail(run_trace)} >= {"source_fetch", "model_call", "job_run"}
    fetch = [e for e in trail(run_trace) if e["kind"] == "source_fetch"][0]
    assert (fetch["fields"]["source"], fetch["fields"]["items"], fetch["fields"]["outcome"]) == ("github", 1, "done")


# ─── a refresh ───────────────────────────────────────────────────────────────

def test_a_refresh_records_its_run_and_each_source_it_read(wq):
    source_rows(wq, None)
    refresh.run(now=NOW, model=Model())
    run = of_kind("job_run", job="work-queue-refresh")[0]
    assert pointers.is_trace_id(run["trace"]) and run["component"] == "work-queue"
    assert run["fields"]["outcome"] == "done" and run["fields"]["ok"] is True and run["fields"]["items"] == 1
    assert isinstance(run["fields"]["ms"], int)
    fetches = trail(run["trace"])
    assert [(e["kind"], e["fields"]["source"], e["fields"]["items"], e["fields"]["ok"])
            for e in fetches if e["kind"] == "source_fetch"] == [("source_fetch", "github", 1, True)]
    assert all(isinstance(e["fields"]["ms"], int) for e in fetches if e["kind"] == "source_fetch")


def test_a_source_that_fails_is_recorded_with_the_class_of_its_error_and_the_run_is_marked_failed(wq):
    fake_tool(wq.bin, "gh", [("search prs", {"exit": 1, "stderr": "secret words in the error"})])
    wq.sources({"github": {"skip_authors": []}})
    statuses = refresh.run(now=NOW, model=Model())
    assert statuses[-1]["state"] == "error" or any(s["state"] == "error" for s in statuses)
    fetch = of_kind("source_fetch", source="github")[0]
    assert fetch["fields"]["outcome"] == "failed" and fetch["fields"]["ok"] is False
    assert fetch["fields"]["error"] == "SourceError" and "items" not in fetch["fields"]
    run = of_kind("job_run", job="work-queue-refresh")[0]
    assert run["trace"] == fetch["trace"] and run["fields"]["outcome"] == "failed" and run["fields"]["ok"] is False
    assert "secret words" not in json.dumps(telemetry.events(limit=1000))


def test_the_outlook_calendar_is_a_source_of_its_own(wq, monkeypatch):
    fetched = []
    monkeypatch.setattr(refresh.outlook, "rows", lambda entry: fetched.append("rows") or [])
    monkeypatch.setattr(refresh.outlook, "events", lambda entry: fetched.append("events") or [])
    wq.sources({"outlook": {"accounts": ["a@example.com"], "calendar": True}})
    refresh.run(now=NOW, model=Model())
    assert fetched == ["rows", "events"]
    assert [e["fields"]["source"] for e in of_kind("source_fetch")] == ["outlook", "outlook"]


def test_a_refresh_that_stops_on_an_error_records_a_failed_run_and_goes_on_raising(wq, monkeypatch):
    source_rows(wq, None)
    monkeypatch.setattr(refresh, "write_page", lambda snap: (_ for _ in ()).throw(RuntimeError("no disk")))
    with pytest.raises(RuntimeError):
        refresh.run(now=NOW, model=Model())
    run = of_kind("job_run", job="work-queue-refresh")[0]
    assert run["fields"]["outcome"] == "failed" and run["fields"]["error"] == "RuntimeError"
    assert run["fields"]["items"] == 1
    (wq.home / ".assistant/work-queue/sources.json").write_text("not json")
    with pytest.raises(ValueError):
        refresh.run(now=NOW, model=Model())
    assert of_kind("job_run", job="work-queue-refresh")[1]["fields"]["error"] == "ValueError"


def test_a_refresh_of_200_rows_makes_a_bounded_number_of_records(wq, monkeypatch):
    fake_tool(wq.bin, "gh", [("search prs", [gh_pr(n) for n in range(1, 201)])])
    wq.sources({"github": {"skip_authors": []}})
    made = []
    real = telemetry.emit
    monkeypatch.setattr(telemetry, "emit", lambda *a, **k: made.append(a[0]) or real(*a, **k))
    refresh.run(now=NOW, model=Model())
    # A verdict for each row the model sorted (at most triage.MAX_ROWS), and a handful for the run itself.
    assert made.count("triage_verdict") == triage.MAX_ROWS
    assert len(made) <= triage.MAX_ROWS + 5
    made.clear()
    refresh.run(now=NOW, model=Model())  # the next refresh sorts the rest: no more than the rows it sorts
    assert made.count("triage_verdict") == 200 - triage.MAX_ROWS and len(made) <= 200 - triage.MAX_ROWS + 5
    made.clear()
    refresh.run(now=NOW, model=Model())  # everything is sorted: only the run itself is recorded
    assert len(made) <= 5 and "triage_verdict" not in made


# ─── the model sorts a row ───────────────────────────────────────────────────

def test_a_new_verdict_is_recorded_with_the_rows_trace_and_a_saved_one_is_not(wq):
    source_rows(wq, None)
    model = Model(rows=lambda items: {"rows": [{"id": i["id"], "why": "Ana waits.", "disposition": "you", "effort": "Small",
                                                 "lane": "do_first", "draft": ""} for i in items]})
    refresh.run(now=NOW, model=model)
    verdict = of_kind("triage_verdict")[0]
    assert verdict["trace"] == traces.lookup(LINK) and verdict["subject"] == pointers.row_pointer(LINK)
    assert verdict["component"] == "work-queue"
    assert verdict["fields"] == {
        "verdict": "you", "lane": "do_first", "model": triage.model_name(), "source": "github",
        "prompt_version": "rubric-" + triage.version()[:8],
        "run_trace": of_kind("job_run", job="work-queue-refresh")[0]["trace"]}
    refresh.run(now=NOW, model=Model())
    assert len(of_kind("triage_verdict")) == 1


def test_a_verdict_without_a_lane_leaves_the_lane_out(wq):
    source_rows(wq, None)
    refresh.run(now=NOW, model=Model())  # the fake's verdict has no lane
    assert "lane" not in of_kind("triage_verdict")[0]["fields"]


def test_a_row_the_model_didnt_answer_has_no_verdict_record(wq):
    fake_tool(wq.bin, "gh", [("search prs", [gh_pr(1), gh_pr(2)])])
    wq.sources({"github": {"skip_authors": []}})
    refresh.run(now=NOW, model=Model(rows=lambda items: {"rows": [{"id": items[0]["id"], "why": "Ana waits.", "disposition": "you",
                                                                    "effort": "Small", "draft": ""}]}))
    [only] = of_kind("triage_verdict")
    assert only["subject"] in (pointers.row_pointer(gh_pr(1)["url"]), pointers.row_pointer(gh_pr(2)["url"]))


def test_a_call_that_fails_leaves_no_verdicts(wq):
    from work_queue_fakes import raises
    source_rows(wq, None)
    refresh.run(now=NOW, model=Model(rows=raises("the model is down")))
    assert of_kind("triage_verdict") == []
    assert of_kind("job_run", job="work-queue-refresh")[0]["fields"]["outcome"] == "done"


def test_the_run_trace_is_cleared_when_triage_ends(wq):
    source_rows(wq, None)
    refresh.run(now=NOW, model=Model())
    assert triage.RUN_TRACE == ""
    triage.run({"built": "x", "github": []}, Model(), {}, trace=OWN)
    assert triage.RUN_TRACE == ""


# ─── model calls and the spend row ───────────────────────────────────────────

def spend(home: Path) -> list[dict]:
    return [json.loads(line) for line in (home / ".assistant/cost-ledger.jsonl").read_text().splitlines()]


def meter(**given):
    import metering
    base = {"caller": "work_queue", "model": "claude-sonnet-5-5", "usage": {"tokens_in": 1200, "tokens_out": 80,
                                                                           "cost_usd": 0.0123, "source": "cli"},
            "wall_ms": 1500, "status": "ok", "provider": "claude"}
    metering.append_cost_row(**{**base, **given})
    return metering


def test_a_model_call_is_recorded_and_its_trace_is_written_into_the_spend_row(wq):
    meter(trace=RUN)
    call = of_kind("model_call")[0]
    assert call["trace"] == RUN and call["component"] == "work-queue" and call["subject"] == ""
    assert call["fields"] == {"model": "claude-sonnet-5-5", "tier": "balanced", "purpose": "triage", "outcome": "done",
                              "ok": True, "tokens_in": 1200, "tokens_out": 80, "cost_usd": 0.0123, "ms": 1500}
    assert spend(wq.home)[0]["trace"] == RUN


@pytest.mark.parametrize("error, outcome, word", [("timed out", "timeout", "timeout"), ("not installed", "failed", "not_found"),
                                                   ("exit code 1", "failed", "other"),
                                                   ("a long message of the tool's own", "failed", "other")])
def test_a_failed_model_call_is_recorded_with_a_word_for_its_error_never_its_text(wq, error, outcome, word):
    meter(status="failed", error=error, trace=OWN)
    call = of_kind("model_call")[0]
    assert (call["fields"]["outcome"], call["fields"]["ok"], call["fields"]["error"]) == (outcome, False, word)
    assert error not in json.dumps(call)


@pytest.mark.parametrize("caller, purpose", [("work_queue", "triage"), ("triage", "triage"), ("observer-audit", "observer"),
                                              ("strategist", "strategist"), ("narrator", "narrate"), ("lessons", "lesson"),
                                              ("slack_focus", "other")])
def test_each_caller_of_the_spend_row_has_a_purpose(wq, caller, purpose):
    meter(caller=caller)
    call = of_kind("model_call")[0]
    assert call["fields"]["purpose"] == purpose
    assert call["component"] == ("work-queue" if caller == "work_queue" else "pulse")


def test_a_call_that_serves_no_work_has_no_trace_and_a_forged_trace_is_not_kept(wq):
    meter()
    meter(trace="not a trace")
    assert [e["trace"] for e in of_kind("model_call")] == ["", ""]
    assert [row["trace"] for row in spend(wq.home)] == ["", ""]


def test_a_model_id_the_store_would_refuse_is_left_out_not_the_record(wq):
    meter(model="us.anthropic.claude-opus-4-8[1m]")
    call = of_kind("model_call")[0]
    assert "model" not in call["fields"] and call["fields"]["tier"] == "frontier"
    assert "_dropped" not in call["fields"]


def test_the_work_queues_real_model_call_books_the_runs_trace(wq, monkeypatch):
    fake_claude(wq.home)
    monkeypatch.setattr(triage, "call_model", REAL_CALL)
    source_rows(wq, monkeypatch)
    refresh.run(now=NOW)
    run = of_kind("job_run", job="work-queue-refresh")[0]["trace"]
    rows = spend(wq.home)
    assert rows and {row["trace"] for row in rows} == {run}
    calls = of_kind("model_call")
    assert len(calls) == len(rows) and {c["trace"] for c in calls} == {run}


def test_a_spend_row_with_the_new_field_is_read_by_every_reader_of_the_spend_file(wq):
    import metering
    from assistant import strategist
    import datetime as dt
    meter(trace=OWN)
    meter(trace="")
    today = dt.datetime.now(dt.timezone.utc)
    assert len(metering.read_cost_ledger()) == 2
    assert triage.feature_spend(today.timestamp()) == pytest.approx(0.0246)
    assert strategist.day_spend_usd(today.timestamp()) == pytest.approx(0.0246)
    agg = metering.aggregate([], now=int(today.timestamp()), window_days=7, cost_rows=metering.read_cost_ledger())
    assert "cost_ledger_per_day_usd" in agg
    renderer = load_script("renderer_work_records", "render-assistant-page.py")
    assert renderer.render_metering_stats() == ""


def test_the_pulses_own_model_calls_book_the_pulses_trace(wq, monkeypatch):
    (wq.home / ".assistant/comms").mkdir(parents=True)
    (wq.home / ".assistant/comms/config.json").write_text(json.dumps({"triage": {"provider": "claude"}}))
    pulse = load_script("pulse_work_records_calls", "pulse.py")
    envelope = {"usage": {"input_tokens": 500, "output_tokens": 40}, "total_cost_usd": 0.002, "result": ""}
    monkeypatch.setattr(pulse, "run", lambda cmd, **k: (0, json.dumps(envelope), ""))
    monkeypatch.setattr(pulse, "PULSE_TRACE", OWN)
    pulse.call_triage_batch([{"schema": "world-event/1", "id": "eid-1", "source": "github", "kind": "mystery",
                              "external_id": "x:1", "title": "event", "snippet": "?", "refs": {}}], 7)
    assert [row["trace"] for row in spend(wq.home)] == [OWN]
    assert of_kind("model_call")[0]["trace"] == OWN and of_kind("model_call")[0]["fields"]["purpose"] == "triage"


def test_the_pulses_observer_audit_books_the_pulses_trace(wq, monkeypatch):
    from assistant import strategist
    monkeypatch.setenv("OBSERVER_AUDIT", "1")
    pulse = load_script("pulse_work_records_audit", "pulse.py")
    monkeypatch.setattr(strategist, "over_ceiling", lambda now: False)
    monkeypatch.setattr(pulse, "PULSE_TRACE", OWN)
    monkeypatch.setattr(pulse, "call_observer_batch", lambda ctxs, idx, batch, *, model=None, label="batch": (
        {}, {"tokens_in": 10, "tokens_out": 1, "cost_usd": 0.01, "source": "cli", "provider": "claude",
             "model": "claude-opus-4-8"}))
    interval = pulse.OBSERVER_AUDIT_INTERVAL_HOURS * 3600
    pulse.run_observer_audit([{"ws_ref": "workspace:1", "transcript_path": "/x/1.jsonl"}],
                             {"workspace:1": {"verdict": "active"}}, 0, now=1_800_000_000 // interval * interval)
    assert [row["trace"] for row in spend(wq.home)] == [OWN]
    call = of_kind("model_call")[0]
    assert call["trace"] == OWN and call["fields"]["purpose"] == "observer" and call["fields"]["outcome"] == "failed"


# ─── tasks ───────────────────────────────────────────────────────────────────

def test_a_row_that_becomes_a_task_leaves_a_task_record_with_the_rows_trace(wq):
    source_rows(wq, None)
    refresh.run(now=NOW, model=Model())
    status, sent = server.queue_todo({"key": LINK, "title": "Review it", "mode": "assist"})
    created = of_kind("task")[0]
    assert (created["trace"], created["subject"], created["component"]) == (traces.lookup(LINK), sent["id"], "work-queue")
    assert created["fields"] == {"state": "open", "origin": "work-queue", "mode": "assist"}
    assert wq.items()[-1]["trace"] == created["trace"]
    server.queue_todo({"key": LINK, "title": "Review it"})  # sent again: the task already exists
    assert len(of_kind("task")) == 1


def test_a_task_the_planner_stages_and_one_a_policy_creates_each_leave_a_record(wq, monkeypatch):
    item, created = goals._stage_todo({"id": "goal-1"}, "check-in", "A title", "Some detail", False, 1_800_000_000)
    assert created is True
    record = of_kind("task")[0]
    assert (record["trace"], record["subject"], record["component"]) == (item["trace"], item["id"], "pulse")
    assert record["fields"] == {"state": "open", "origin": "goal"}
    goals._stage_todo({"id": "goal-1"}, "check-in", "A title", "Some detail", False, 1_800_000_000)  # idempotent
    assert len(of_kind("task")) == 1
    todo_id, made = policy_triage.create_todo({"id": "abcdef1234", "title": "T", "source": "github"}, "p1", {}, now=1_800_000_000)
    assert made is True
    second = of_kind("task")[1]
    assert second["subject"] == "" and second["fields"]["origin"] == "agent"  # `td-auto-...` isn't an id a record takes
    assert second["trace"] == [i for i in wq.items() if i["id"] == todo_id][0]["trace"]
    policy_triage.create_todo({"id": "abcdef1234", "title": "T", "source": "github"}, "p1", {}, now=1_800_000_000)
    assert len(of_kind("task")) == 2


@pytest.mark.parametrize("source, origin", [("slack-react:C1:1.2", "slack"), ("manual:2026-11-01", "manual"),
                                             ("work-queue:2026-11-01", "work-queue"), ("goal:goal-1:ab", "goal"),
                                             ("policy:p:e", "agent"), ("github:o/r", "github"), ("", "manual"),
                                             ("something-else", "manual")])
def test_a_task_that_arrives_without_an_id_is_recorded_when_the_pulse_stamps_it(wq, source, origin):
    write_todos(wq, [{"id": "td-7", "title": "From elsewhere", "status": "in-progress", "source": source},
                     {"id": "td-8", "title": "Has one", "status": "open", "trace": OWN}])
    assert todostore.stamp_missing_traces() == 1
    [record] = of_kind("task")
    assert record["subject"] == "td-7" and record["trace"] == wq.items()[0]["trace"]
    assert record["fields"] == {"state": "in-progress", "origin": origin} and record["component"] == "pulse"
    assert todostore.stamp_missing_traces() == 0 and len(of_kind("task")) == 1


def test_a_task_the_agent_finishes_or_blocks_records_its_new_and_its_old_state(wq):
    write_todos(wq, [{"id": "td-1", "status": "in-progress", "trace": OWN}, {"id": "td-2", "status": "open", "trace": RUN}])
    todo_result.main("td-1", "did it")
    todo_result.main("td-2", "needs a key", blocked=True)
    first, second = of_kind("task")
    assert (first["trace"], first["subject"], first["component"]) == (OWN, "td-1", "cli")
    assert first["fields"] == {"state": "done", "prev_state": "in-progress"}
    assert (second["trace"], second["fields"]) == (RUN, {"state": "blocked", "prev_state": "open"})


def test_a_dashboard_removal_or_requeue_records_the_new_state(wq, monkeypatch):
    todo_server = load_script("todo_server_work_records", "todo-server.py")
    monkeypatch.setattr(todo_server, "JSON_PATH", wq.todo)
    monkeypatch.setattr(todo_server, "rerender", lambda: None)
    write_todos(wq, [{"id": "td-1", "status": "blocked", "trace": OWN}, {"id": "td-2", "status": "open", "trace": RUN,
                                                                         "workQueueMode": "handsoff"}])
    assert todo_server.dispatch_now("td-1")[0] is True
    assert todo_server.remove_item("td-2")[0] is True
    assert todo_server.remove_item("td-9")[0] is False and todo_server.dispatch_now("td-9")[0] is False
    requeued, removed = of_kind("task")
    assert (requeued["trace"], requeued["subject"], requeued["component"]) == (OWN, "td-1", "dashboard")
    assert requeued["fields"] == {"state": "open", "prev_state": "blocked"}
    assert (removed["trace"], removed["fields"]) == (RUN, {"state": "removed", "prev_state": "open", "mode": "handsoff"})


def test_a_task_with_no_id_of_its_own_still_leaves_a_record_with_its_stand_in_trace(wq):
    todo_server = load_script("todo_server_work_records2", "todo-server.py")
    todo_server._record("task", {"id": "td-5", "createdAt": "2026-11-01", "status": "open"}, "removed", "dashboard", "open")
    assert of_kind("task")[0]["trace"] == pointers.legacy_trace_id("task", "td-5\x002026-11-01")


# ─── dispatch ────────────────────────────────────────────────────────────────

def sent_task(env: Env, **extra) -> dict:
    write_todos(env, [{"id": "td-1", "title": "Fix it", "status": "open", "trace": OWN, "autoDispatch": True,
                       "workQueueMode": "assist", **extra}])
    return env.items()[0]


def test_a_dispatch_records_its_outcome_the_workspace_and_the_new_state(wq, monkeypatch):
    sent_task(wq)
    pulse, ok, _ = dispatch(monkeypatch, "td-1")
    assert ok is True
    dispatched, started = trail(OWN)
    assert (dispatched["kind"], dispatched["subject"], dispatched["component"]) == ("dispatch", "td-1", "pulse")
    assert dispatched["fields"]["outcome"] == "done" and dispatched["fields"]["ok"] is True
    assert dispatched["fields"]["workspace"] == "workspace:9" and dispatched["fields"]["mode"] == "assist"
    assert isinstance(dispatched["fields"]["ms"], int)
    assert (started["kind"], started["fields"]) == ("task", {"state": "in-progress", "prev_state": "open", "mode": "assist"})
    assert pulse.PULSE_DISPATCHED == 1


def test_a_dispatch_that_fails_is_recorded_as_failed_and_leaves_the_task_alone(wq, monkeypatch):
    sent_task(wq)
    _, ok, _ = dispatch(monkeypatch, "td-1", ping_ok=False)
    assert ok is False
    [dispatched] = trail(OWN)
    assert dispatched["fields"]["outcome"] == "failed" and dispatched["fields"]["ok"] is False
    assert "workspace" not in dispatched["fields"]
    _, ok, _ = dispatch(monkeypatch, "td-404")  # not in the file: nothing to say a record about
    assert ok is False and len(telemetry.events(limit=1000)) == 1


def test_a_dispatch_that_fails_does_not_claim_the_workspace_an_earlier_one_left_on_the_task(wq, monkeypatch):
    sent_task(wq, status="in-progress", dispatchedWs="workspace:2")
    _, ok, _ = dispatch(monkeypatch, "td-1", ping_ok=False)
    [dispatched] = trail(OWN)
    assert ok is False and "workspace" not in dispatched["fields"]


def test_a_dispatch_that_raises_is_recorded_and_still_raises(wq, monkeypatch):
    sent_task(wq)
    pulse = load_script("pulse_work_records_raises", "pulse.py")
    monkeypatch.setattr(pulse, "run", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("cmux fell over")))
    with pytest.raises(RuntimeError):
        pulse.dispatch_todo("td-1")
    assert trail(OWN)[0]["fields"]["outcome"] == "failed" and pulse.PULSE_DISPATCHED == 0


def test_a_pulse_records_one_run_with_its_own_trace_and_how_many_it_dispatched(wq, monkeypatch):
    seen = {}
    monkeypatch.setattr(sys, "argv", ["pulse.py"])  # another test may have left a dry run's arguments behind
    pulse = load_script("pulse_work_records_run", "pulse.py")

    def main():
        seen["trace"] = pulse.PULSE_TRACE
        pulse.PULSE_DISPATCHED += 2
        return 0

    assert pulse._recorded_pulse(main)() == 0
    run = of_kind("job_run", job="pulse")[0]
    assert run["trace"] == seen["trace"] and pointers.is_trace_id(run["trace"]) and run["component"] == "pulse"
    assert run["fields"]["outcome"] == "done" and run["fields"]["ok"] is True and run["fields"]["items"] == 2
    assert pulse.PULSE_TRACE == ""
    assert pulse._recorded_pulse(lambda: 1)() == 1
    assert of_kind("job_run", job="pulse")[1]["fields"]["outcome"] == "failed"
    with pytest.raises(OSError):
        pulse._recorded_pulse(lambda: (_ for _ in ()).throw(OSError("disk")))()
    last = of_kind("job_run", job="pulse")[2]
    assert last["fields"]["error"] == "OSError" and last["fields"]["items"] == 0
    monkeypatch.setattr(sys, "argv", ["pulse.py", "--dry-run"])
    assert pulse._recorded_pulse(lambda: 0)() == 0
    assert len(of_kind("job_run", job="pulse")) == 3  # a dry run records nothing


def test_the_pulses_main_is_the_recorded_one(wq):
    pulse = load_script("pulse_work_records_main", "pulse.py")
    assert hasattr(pulse.main, "__wrapped__") and hasattr(pulse.dispatch_todo, "__wrapped__")


# ─── checks ──────────────────────────────────────────────────────────────────

def test_a_check_that_clears_a_finished_task_is_recorded_with_its_name_and_the_tasks_trace(wq):
    fake_tool(wq.bin, "gh", GH)
    write_todos(wq, [{"id": "td-1", "status": "done", "trace": OWN, "detail": DONE_TEXT, "url": ""}])
    config.write_json(wq.data / "queued.json", {LINK: {"id": "td-1"}})
    ready.check({}, None, "")
    [check] = of_kind("check")
    assert (check["trace"], check["subject"], check["fields"]) == (OWN, "td-1", {"name": "pr-merged", "outcome": "done", "ok": True})
    ready.check({}, None, "")  # cleared once, never again
    assert len(of_kind("check")) == 1


def test_a_check_that_finds_a_blocker_gone_is_recorded(wq):
    fake_tool(wq.bin, "gh", [("api repos/o/r", {"id": 1})])
    write_todos(wq, [{"id": "td-1", "status": "blocked", "trace": OWN, "statusReason": "I can't read https://github.com/o/r access"}])
    config.write_json(wq.data / "queued.json", {LINK: {"id": "td-1"}})
    ready.check({}, None, "")
    assert [e["fields"]["name"] for e in of_kind("check")] == ["unblocked"]


# ─── effects ─────────────────────────────────────────────────────────────────

def test_an_effect_that_finishes_is_recorded_with_its_trace_class_and_target(wq):
    target = {"row": pointers.row_pointer(LINK), "account": "me@example.com", "msgs": ["m1"]}
    ef = effects.begin("gmail.trash", target, {"kind": "decision", "ref": "r", "at": server.now()}, trace=OWN,
                       component="work-queue")
    assert of_kind("effect") == []
    assert effects.finish(ef, "done", verified=True) is True
    [record] = of_kind("effect")
    assert (record["trace"], record["subject"], record["component"]) == (OWN, ef, "work-queue")
    assert record["fields"] == {"action": "gmail.trash", "outcome": "done", "ok": True, "target": pointers.row_pointer(LINK),
                                "reversible": True}


def test_an_effect_that_fails_or_has_no_target_id_is_recorded_too(wq):
    ef = effects.begin("gmail.archive", {"account": "me@example.com", "thread": "t1"},
                       {"kind": "tap", "ref": "r", "at": server.now()})
    effects.finish(ef, "failed", error="tool failed")
    ef = effects.begin("session.type", {"session": "workspace:3"}, {"kind": "tap", "ref": "r", "at": server.now()},
                       trace=OWN)
    effects.finish(ef, "unknown")
    failed, typed = of_kind("effect")
    assert failed["trace"] == "" and failed["fields"] == {"action": "gmail.archive", "outcome": "failed", "ok": False,
                                                          "reversible": True}
    assert typed["fields"]["target"] == "workspace:3" and typed["component"] == "pulse" and typed["fields"]["reversible"] is False
    assert effects.finish(ef, "done") is False and len(of_kind("effect")) == 2  # already finished: nothing new


def test_the_work_queue_gives_an_effect_its_rows_trace(wq):
    source_rows(wq, None)
    refresh.run(now=NOW, model=Model())
    row_trace = traces.lookup(LINK)
    ef, refusal = server.begin_effect("gmail.trash", {"account": "me@example.com", "msgs": ["m1"], "row": pointers.row_pointer(LINK)},
                                      "decision", pointers.row_pointer(LINK), server.now(), trace_key=LINK)
    assert refusal is None and effects.history(trace=row_trace)[0]["id"] == ef
    ef2, _ = server.begin_effect("gmail.archive", {"account": "me@example.com", "thread": "t1"}, "tap", "/api/gmail",
                                 server.now(), trace_key="https://nowhere.example/unknown")
    assert effects.history(trace="")[0]["id"] == ef2  # a row with no id gives the effect none


def test_each_place_that_begins_an_effect_gives_it_the_trace_of_the_row_it_acts_on(wq, monkeypatch):
    from test_work_queue_actions import KEY, gmail, thread
    from test_work_queue_effects import gmail_job, gog_trash, unsub_job
    gmail(wq, thread([]))
    traces.stamp([[server.EMAIL_MARK + KEY], ["g1"], ["u1"]])
    assert server.gmail_action({"key": KEY, "action": "archive"})[0] == 200
    gog_trash(wq)
    server.apply_gmail(gmail_job("g1"))
    monkeypatch.setattr(server, "unsafe_target", lambda url: "")

    class Opener:
        def open(self, req, timeout):
            raise server.urllib.error.HTTPError(req.full_url, 410, "Gone", {}, None)
    monkeypatch.setattr(server.urllib.request, "build_opener", lambda *a: Opener())
    server.apply_unsub(unsub_job("https://news.example.com/u", "u1"))
    by_class = {e["class"]: e["trace"] for e in effects.history(limit=100)}
    assert by_class == {"gmail.archive": traces.lookup(server.EMAIL_MARK + KEY), "gmail.trash": traces.lookup("g1"),
                        "mail.unsubscribe": traces.lookup("u1")}
    assert all(pointers.is_trace_id(t) for t in by_class.values()) and len(set(by_class.values())) == 3


def test_a_registry_that_cant_be_read_costs_the_effect_its_trace_not_its_start(wq, monkeypatch):
    monkeypatch.setattr(traces, "lookup", lambda key: (_ for _ in ()).throw(OSError("unreadable")))
    monkeypatch.setenv("ASSISTANT_TELEMETRY_STRICT", "0")
    ef, refusal = server.begin_effect("gmail.archive", {"account": "me@example.com", "thread": "t1"}, "tap", "/api/gmail",
                                      server.now(), trace_key=LINK)
    assert refusal is None and ef


# ─── nothing changes ─────────────────────────────────────────────────────────

NORMAL = [(re.compile(r"t[rl]-[0-9a-f]{16}"), "TRACE"), (re.compile(r"\b[0-9a-f]{16}\b"), "HEX"),
          (re.compile(r"\d{4}-\d\d-\d\d[T ][\d:]+(?:\+00:00|Z)?"), "NOW"), (re.compile(r"\d{8}-\d{6}"), "STAMP"),
          (re.compile(r"[A-Z][a-z]{2} \d{1,2}, \d{1,2}:\d{2} [AP]M"), "BUILT"), (re.compile(r'"pid": \d+'), '"pid": 0')]


def plain(text: str) -> str:
    for pattern, word in NORMAL:
        text = pattern.sub(word, text)
    return text


def everything(env: Env, monkeypatch) -> dict:
    """Every step of one piece of work, and what each one answered and wrote, with ids and clocks made plain."""
    source_rows(env, monkeypatch)
    out = {}
    refresh.run(now=NOW, model=Model())
    out["page"] = plain(refresh.site_path().read_text())
    out["sent"] = server.queue_todo({"key": LINK, "title": "Review it", "detail": "More", "mode": "handsoff", "note": "Hi"})
    out["again"] = server.queue_todo({"key": LINK, "title": "Review it"})
    _, out["dispatched"], commands = dispatch(monkeypatch, out["sent"][1]["id"])
    out["commands"] = plain(json.dumps(commands)).replace(str(env.root), "ROOT")
    todo_result.main(out["sent"][1]["id"], DONE_TEXT)
    refresh.run(now=NOW, model=Model())
    out["page2"] = plain(refresh.site_path().read_text())
    for name in ("queued.json", "marks.json", "ready-checks.json", "refresh-status.json", "triage-cache.json"):
        out[name] = plain((env.data / name).read_text())
    out["todos"] = plain(env.todo.read_text())
    out["prompt"] = plain(next((env.home / ".claude/spawn-prompts").glob("prompt-dispatch-*.md")).read_text()).replace(
        str(env.root), "ROOT")
    return out


def test_every_response_file_and_prompt_is_the_same_with_recording_on_and_off(tmp_path, monkeypatch):
    on = everything(make_env(tmp_path / "on", monkeypatch), monkeypatch)
    assert telemetry.events(limit=1000)
    off = everything(make_env(tmp_path / "off", monkeypatch, enabled=False), monkeypatch)
    assert telemetry.events(limit=1000) == []
    assert on == off and on["dispatched"] is True


def broken_emit(monkeypatch):
    monkeypatch.setenv("ASSISTANT_TELEMETRY_STRICT", "0")
    monkeypatch.setattr(telemetry, "emit", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("the store is on fire")))


def missing_telemetry(monkeypatch):
    monkeypatch.setenv("ASSISTANT_TELEMETRY_STRICT", "0")
    monkeypatch.delitem(sys.modules, "assistant.telemetry", raising=False)
    monkeypatch.delattr(sys.modules["assistant"], "telemetry", raising=False)
    real = builtins.__import__

    def refuse(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "assistant.telemetry" or (name == "assistant" and "telemetry" in (fromlist or ())):
            raise ImportError("telemetry is gone")
        return real(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", refuse)


def missing_records(monkeypatch):
    """The module that builds the records can't be loaded where a step loads it when it needs it."""
    monkeypatch.setenv("ASSISTANT_TELEMETRY_STRICT", "0")
    monkeypatch.delitem(sys.modules, "assistant.records", raising=False)
    monkeypatch.delattr(sys.modules["assistant"], "records", raising=False)
    real = builtins.__import__

    def refuse(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "assistant.records" or (name == "assistant" and "records" in (fromlist or ())):
            raise ImportError("records is gone")
        return real(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", refuse)


def builds_wrong(monkeypatch):
    """The code that prepares a record's arguments fails, in each of the places it is used."""
    monkeypatch.setenv("ASSISTANT_TELEMETRY_STRICT", "0")
    monkeypatch.setattr(records, "task_trace", lambda item: (_ for _ in ()).throw(KeyError("trace")))
    monkeypatch.setattr(records, "origin_of", lambda source: (_ for _ in ()).throw(KeyError("origin")))
    monkeypatch.setattr(records, "count_of", lambda found: (_ for _ in ()).throw(TypeError("count")))
    monkeypatch.setattr(pointers, "new_trace_id", lambda: (_ for _ in ()).throw(OSError("no entropy")))
    monkeypatch.setattr(records, "name_of", lambda value: (_ for _ in ()).throw(KeyError("name")))
    monkeypatch.setattr(records, "one_of", lambda value, listed: (_ for _ in ()).throw(KeyError("listed")))


@pytest.mark.parametrize("breaks", [broken_emit, missing_telemetry, missing_records, builds_wrong])
def test_every_step_still_completes_and_answers_the_same_when_recording_breaks(tmp_path, monkeypatch, breaks):
    expected = everything(make_env(tmp_path / "off", monkeypatch, enabled=False), monkeypatch)
    breaks(monkeypatch)
    got = everything(make_env(tmp_path / "broken", monkeypatch), monkeypatch)
    assert got == expected


@pytest.mark.parametrize("breaks", [broken_emit, missing_telemetry, missing_records])
def test_the_effects_ledger_works_with_telemetry_broken_or_missing(wq, monkeypatch, breaks):
    breaks(monkeypatch)
    ef = effects.begin("gmail.archive", {"account": "me@example.com", "thread": "t1"}, {"kind": "tap", "ref": "r", "at": server.now()})
    assert effects.finish(ef, "done") is True
    assert [e["outcome"] for e in effects.history()] == ["done"]


@pytest.mark.parametrize("breaks", [broken_emit, missing_telemetry])
def test_a_record_handed_to_a_store_that_fails_costs_only_the_record(wq, monkeypatch, breaks):
    breaks(monkeypatch)
    assert records.emit("task", OWN, "td-1", "cli", state="open") is None


@pytest.mark.parametrize("breaks", [broken_emit, missing_telemetry, missing_records, builds_wrong])
def test_stamping_a_tasks_trace_is_never_held_back_by_its_record(wq, monkeypatch, breaks):
    write_todos(wq, [{"id": "td-7", "title": "From elsewhere", "status": "open", "source": "slack-react:C1:1.2"}])
    breaks(monkeypatch)
    assert todostore.stamp_missing_traces() == 1
    assert pointers.is_trace_id(wq.items()[0]["trace"])


def test_a_model_call_is_booked_when_the_record_module_is_missing(wq, monkeypatch):
    missing_records(monkeypatch)
    meter(trace=OWN)
    assert [row["trace"] for row in spend(wq.home)] == [OWN] and of_kind("model_call") == []


def test_metering_finds_the_record_module_when_the_source_folder_is_not_on_the_path(wq, monkeypatch):
    import metering
    src = str(REPO / "src")
    monkeypatch.setattr(sys, "path", [p for p in sys.path if p != src])
    assert metering._records() is records and src in sys.path


def test_a_pulse_or_a_dashboard_edit_goes_on_when_a_record_cant_be_built_and_a_test_says_so(wq, monkeypatch):
    todo_server = load_script("todo_server_work_records3", "todo-server.py")
    pulse = load_script("pulse_work_records_strict", "pulse.py")
    for script in (pulse, todo_server):
        with pytest.raises(AttributeError):
            script._record("no_such_record")
    monkeypatch.setenv("ASSISTANT_TELEMETRY_STRICT", "0")
    assert pulse._record("no_such_record") is None and todo_server._record("no_such_record") is None
    assert pulse._record("new_trace").startswith("tr-")


def test_a_dispatch_with_no_task_file_is_recorded_as_nothing(wq, monkeypatch):
    wq.todo.unlink()
    _, ok, _ = dispatch(monkeypatch, "td-1")
    assert ok is False and telemetry.events(limit=100) == []


def test_in_the_tests_strict_mode_a_missing_record_module_is_seen_even_by_the_effects_ledger(wq, monkeypatch):
    ef = effects.begin("gmail.archive", {"account": "me@example.com", "thread": "t1"}, {"kind": "tap", "ref": "r", "at": server.now()})
    missing_records(monkeypatch)
    monkeypatch.setenv("ASSISTANT_TELEMETRY_STRICT", "1")
    with pytest.raises(ImportError):
        effects.finish(ef, "done")


def test_a_mistake_in_a_record_is_raised_in_the_tests_strict_mode_and_costs_only_the_record_otherwise(wq, monkeypatch):
    with pytest.raises(ValueError):
        records.task({"id": "td-1", "trace": OWN}, "not a state", "cli")
    monkeypatch.setenv("ASSISTANT_TELEMETRY_STRICT", "0")
    records.task({"id": "td-1", "trace": OWN}, "not a state", "cli")  # recorded with the state left out, no error
    assert of_kind("task")[0]["fields"]["_dropped"] == 1
    monkeypatch.setenv("ASSISTANT_TELEMETRY_STRICT", "1")
    with pytest.raises(KeyError):
        records.guard(lambda: {}["missing"])
    assert records.loud() is True
    monkeypatch.setenv("ASSISTANT_TELEMETRY_STRICT", "0")
    assert records.guard(lambda: {}["missing"]) is None and records.loud() is False


# ─── no content ──────────────────────────────────────────────────────────────

def test_no_record_holds_a_title_a_sender_a_link_a_reason_or_the_tasks_detail(wq, monkeypatch):
    secret = "-".join(["sk", "ant", "api03", "A1b2C3d4E5f6G7h8I9j0"])
    token = "ghp_" + "Zy9Xw8Vu7Ts6Rq5Po4Nm3Lk2Ji1Hg0Fe9Dc"
    marks = {"title": "Zebra-title-marker", "sender": "quokka-sender-marker", "link": "marmot-link-marker",
             "text": "narwhal-text-marker", "reason": "okapi-reason-marker", "detail": "ibis-detail-marker",
             "note": "lemur-note-marker"}
    pr = {**gh_pr(9, f"{marks['title']} {secret}"), "author": {"login": marks["sender"]},
          "url": f"https://github.com/o/r/pull/9?x={marks['link']}&t={token}"}
    fake_tool(wq.bin, "gh", [("search prs", [pr]), ("pr view", {"state": "MERGED", "comments": [], "reviews": []})])
    wq.sources({"github": {"skip_authors": []}})
    model = Model(rows=lambda items: {"rows": [{"id": i["id"], "why": f"{marks['reason']} {token}", "disposition": "you",
                                                 "effort": "Small", "draft": marks["text"]} for i in items]})
    refresh.run(now=NOW, model=model)
    status, sent = server.queue_todo({"key": pr["url"], "title": f"{marks['title']} {secret}", "detail": marks["detail"],
                                      "note": marks["note"], "url": pr["url"], "mode": "handsoff"})
    dispatch(monkeypatch, sent["id"])
    todo_result.main(sent["id"], f"{marks['text']} {pr['url']} {token}")
    refresh.run(now=NOW, model=model)
    ef = effects.begin("gmail.trash", {"account": "me@example.com", "msgs": ["m1"], "row": pointers.row_pointer(pr["url"])},
                       {"kind": "decision", "ref": "r", "at": server.now()}, trace=traces.lookup(pr["url"]))
    effects.finish(ef, "done", error=f"{marks['reason']} {secret}")
    meter(trace=traces.lookup(pr["url"]), error=f"{marks['reason']} {token}", status="failed")
    telemetry.maintain()
    assert len(telemetry.events(limit=1000)) >= 10
    found = b""
    for each in sorted(Path(telemetry.folder()).iterdir()):
        found += each.read_bytes()
    for word in (*marks.values(), secret, token, "github.com", "example.com", "o/r", pr["url"], "ana"):
        assert word.encode() not in found, word
    for event in telemetry.events(limit=1000):
        assert "_dropped" not in event["fields"], event


# ─── the registry ────────────────────────────────────────────────────────────

def test_a_sources_rows_are_counted_whether_it_returns_a_list_or_a_list_for_each_account(wq):
    assert records.count_of([1, 2]) == 2 and records.count_of({"a": [1, 2], "b": [3]}) == 3
    fetched = records.fetching("gmail", lambda entry: {"a": [1, 2], "b": [3]}, OWN)
    assert fetched({}) == {"a": [1, 2], "b": [3]}
    assert of_kind("source_fetch")[0]["fields"]["items"] == 3 and of_kind("source_fetch")[0]["trace"] == OWN


def test_the_registry_lists_the_field_that_ties_a_verdict_to_the_run_that_made_it():
    assert telemetry.KINDS["triage_verdict"]["run_trace"] == telemetry.ID
