"""What starts a task again after its start stopped because the agent session wasn't ready (bin/pulse.py parks it).

The block says so, so the sentence has to be true: "Queue to start" on the dashboard doesn't start a task that is marked as
started, and removing the task and sending the row again does."""
from __future__ import annotations

import importlib.util
import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from work_queue import server, traces
from work_queue_fakes import wq  # noqa: F401
from test_todo_server import env, mod  # noqa: F401

REPO = Path(__file__).resolve().parent.parent
ROW = "https://github.com/o/r/pull/1"


def picks(todo_path: Path) -> dict:
    """What bin/pick-open-todos.py would hand the pulse for this task file."""
    spec = importlib.util.spec_from_file_location("pick_open_todos_restart", REPO / "bin/pick-open-todos.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.TODO_PATH = str(todo_path)
    module.live_workspaces = lambda: set()
    out = io.StringIO()
    with redirect_stdout(out):
        module.main()
    return json.loads(out.getvalue())


def test_queue_to_start_does_not_start_a_task_that_a_parked_start_marked_as_started(env):
    parked = {"id": "td-9", "status": "in-progress", "autoDispatch": True, "dispatchedAt": "2026-11-20T10:00:00Z",
              "dispatchedWs": "workspace:1000010013", "title": "T"}
    env.json_path.write_text(json.dumps({"items": [parked], "completed": [], "removed": []}))
    ok, _ = mod.dispatch_now("td-9")
    assert ok
    task = json.loads(env.json_path.read_text())["items"][0]
    assert (task["status"], task["dispatchedAt"], task["dispatchedWs"]) == ("in-progress", None, None)
    found = picks(env.json_path)
    assert found["bucket_b"] == [] and found["bucket_a"] == [] and found["totals"]["open"] == 0, "nothing would start it"


def test_removing_the_task_and_sending_the_row_again_makes_a_task_the_pulse_will_start(wq):
    sent = server.queue_todo({"key": ROW, "title": "Change", "url": ROW})
    assert sent == (200, {"id": "td-051", "existing": False})
    store = json.loads(wq.todo.read_text())
    parked = next(i for i in store["items"] if i["id"] == "td-051")
    parked.update(status="in-progress", dispatchedAt="2026-11-20T10:00:00Z", dispatchedWs="workspace:1000010013")
    wq.todo.write_text(json.dumps(store))
    assert server.queue_todo({"key": ROW, "title": "Change", "url": ROW}) == (200, {"id": "td-051", "existing": True}), \
        "sending it again while the parked task exists changes nothing"
    store["items"].remove(parked)
    store["removed"] = [{**parked, "status": "removed"}]
    wq.todo.write_text(json.dumps(store))
    again = server.queue_todo({"key": ROW, "title": "Change", "url": ROW})
    assert again == (200, {"id": "td-052", "existing": False})
    task = next(i for i in json.loads(wq.todo.read_text())["items"] if i["id"] == "td-052")
    assert (task["status"], task["autoDispatch"], task.get("dispatchedAt")) == ("open", True, None)
    assert picks(wq.todo)["bucket_b"][0]["id"] == "td-052"
    assert task["trace"] == parked.get("trace"), "it is the same piece of work: the trail carries on"
