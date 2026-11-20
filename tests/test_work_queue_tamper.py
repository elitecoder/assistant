"""A tampered saved file, a crashed lock holder, and two refreshes in separate processes never stop the Work queue."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time

import pytest

from work_queue import config, refresh, triage
from work_queue_fakes import Model, fake_tool, gh_pr, wq  # noqa: F401
from test_work_queue_triage import pr, snap

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def base(wq):
    fake_tool(wq.bin, "gh", [("search prs", [gh_pr(2), gh_pr(3)])])
    wq.sources({"github": {}})
    triage.run(snap(pr(2)), Model(), {})


def write_raw(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def refreshed(wq) -> tuple[dict, str]:
    refresh.run(model=Model())
    return json.loads(refresh.status_path().read_text()), refresh.site_path().read_text()


CACHE_TAMPERS = {
    "list": "[1,2]",
    "rows_list": lambda v: json.dumps({"version": v, "rows": [1], "batches": [], "start": [], "changes": 3}),
    "nested_nulls": lambda v: json.dumps({"version": v, "rows": {"a": None, "b": {"verdict": None, "seen": None, "batch": None,
                                                                                 "skipped": None}},
                                          "batches": {"s": None, "t": [[None, None]]}, "start": {"key": None, "items": None},
                                          "changes": {"key": "k", "text": None}}),
    "wrong_types": lambda v: json.dumps({"version": v, "rows": {"a": {"verdict": {"why": 1, "disposition": ["you"],
                                                                                  "effort": {"x": 1}, "draft": 5},
                                                                      "seen": "9999-99-99", "batch": "s"}},
                                         "batches": {"s": [["a", "GitHub"]]},
                                         "start": {"key": "k", "items": [{"link": 1, "reason": "x"}]},
                                         "changes": {"key": "k", "text": {"Nope": "x", "GitHub": 1}}}),
    "huge_nan": lambda v: f'{{"version": "{v}", "rows": {{"a": {{"verdict": null, "seen": 1e999, "batch": NaN, "skipped": Infinity}}}}, '
                          f'"batches": {{"s": [[1e400, -1e400]]}}, "start": {{"key": "k", "items": [{{"link": "x", "reason": NaN}}]}}, '
                          f'"changes": {{"key": "k", "text": {{"GitHub": "{"é" * 400}"}}}}}}',
    "unicode": lambda v: json.dumps({"version": v, "rows": {"‮\ud800": {"verdict": {"why": "\u0000" * 3, "disposition": "you",
                                                                                            "effort": "Quick", "draft": ""},
                                                                               "seen": "￿", "batch": "‮"}},
                                     "batches": {"‮": [["‮\ud800", "GitHub"]]}}),
    "start_huge_items": lambda v: json.dumps({"version": v, "rows": {}, "batches": {},
                                              "start": {"key": "k", "items": [{"link": "l", "reason": "r"}] * 100000}}),
}


@pytest.mark.parametrize("name", list(CACHE_TAMPERS))
def test_a_tampered_cache_never_stops_the_refresh(wq, name):
    base(wq)
    t = CACHE_TAMPERS[name]
    write_raw(triage.cache_path(), t if isinstance(t, str) else t(triage.version()))
    status, html = refreshed(wq)
    assert status["running"] is False and "o/r#3: Change 3" in html


SNAP_TAMPERS = {
    "list": "[1]",
    "rows_nested_null": json.dumps({"day": "2000-01-01", "built": "b", "rows": {"GitHub": None}}),
    "rows_side_list": json.dumps({"day": "2000-01-01", "built": "b", "rows": {"GitHub": [1]}}),
    "unreadable_and_hash": json.dumps({"day": "2000-01-01", "built": "b",
                                       "rows": {"GitHub": {"k": {"unreadable": True}}, "Slack": {"z": {"unreadable": 1}}}}),
    "nan_day": '{"day": NaN, "built": "b", "rows": {}}',
    "huge": '{"day": "2000-01-01", "built": "b", "rows": {"GitHub": {"k": {"title": "t", "hash": "h", "x": 1e999}}}}',
    "title_huge": json.dumps({"day": "2000-01-01", "built": "b",
                              "rows": {"GitHub": {f"k{i}": {"title": "t" * 1000, "hash": "h"} for i in range(5000)}}}),
    "unknown_src": json.dumps({"day": "2000-01-01", "built": "b", "rows": {"Nope": {"k": {"title": "t", "hash": "h"}}}}),
}


@pytest.mark.parametrize("name", list(SNAP_TAMPERS))
@pytest.mark.parametrize("which", ["work/rows.json", "work/rows-baseline.json"])
def test_a_tampered_snapshot_never_stops_the_refresh(wq, name, which):
    base(wq)
    write_raw(wq.data / which, SNAP_TAMPERS[name])
    status, html = refreshed(wq)
    assert status["running"] is False and "Triage unavailable" not in html


RES_TAMPERS = {
    "future_at": json.dumps({"a": {"usd": 1000.0, "at": 1e18}}),
    "huge_usd": '{"a": {"usd": 1e308, "at": ' + str(time.time()) + '}}',
    "nan": '{"a": {"usd": NaN, "at": NaN}, "b": {"usd": Infinity, "at": 1}}',
    "nested_null": json.dumps({"a": None, "b": {"usd": None, "at": None}}),
    "string": '"x"',
    "unicode_keys": json.dumps({"\ud800": {"usd": 0.0, "at": 0}}),
}


@pytest.mark.parametrize("name", list(RES_TAMPERS))
def test_a_tampered_reservation_file_never_stops_the_refresh(wq, name):
    base(wq)
    write_raw(triage.reservations_path(), RES_TAMPERS[name])
    status, html = refreshed(wq)
    assert status["running"] is False
    assert "Triage unavailable" not in html or name == "huge_usd", "only a real, current reservation counts"


@pytest.mark.parametrize("marks", ['[1]', '{"https://github.com/o/r/pull/3": [1]}',
                                   '{"https://github.com/o/r/pull/3": {"state": "snoozed", "until": "x"}}',
                                   '{"https://github.com/o/r/pull/3": {"nostate": 1}}'])
def test_tampered_marks_never_stop_the_refresh(wq, marks):
    base(wq)
    write_raw(wq.data / "marks.json", marks)
    status, html = refreshed(wq)
    assert status["running"] is False and "o/r#3: Change 3" in html


HOLDER = r'''
import sys, time, os
sys.path[:0] = [sys.argv[1] + "/src", sys.argv[1] + "/bin", sys.argv[1]]
os.environ["WQ_DATA"] = sys.argv[2]
from work_queue import triage
with triage.cache_lock():
    print("held", flush=True)
    time.sleep(60)
'''


def test_a_lock_holder_that_crashed_never_blocks_the_next_save(wq):
    proc = subprocess.Popen([sys.executable, "-c", HOLDER, REPO, str(wq.data)], stdout=subprocess.PIPE, text=True,
                            env={**os.environ, "HOME": str(wq.home)})
    assert proc.stdout.readline().strip() == "held"
    proc.send_signal(signal.SIGKILL)
    proc.wait()
    started = time.monotonic()
    triage.run(snap(pr(1)), Model(), {})
    assert time.monotonic() - started < 5


CHILD = r'''
import sys, os, time
sys.path[:0] = [sys.argv[1] + "/src", sys.argv[1] + "/bin", sys.argv[1], sys.argv[1] + "/tests"]
os.environ["WQ_DATA"] = sys.argv[2]
from work_queue import triage
from work_queue_fakes import Model
from test_work_queue_triage import pr, snap
n = int(sys.argv[3]); delay = float(sys.argv[4])
m = Model()
def slow(p, t):
    time.sleep(delay)
    return m(p, t)
triage.run(snap(*(pr(k) for k in range(n, n + 3))), slow, {})
print("done", flush=True)
'''


def test_two_refreshes_in_separate_processes_keep_each_others_verdicts(wq):
    env = {**os.environ, "HOME": str(wq.home)}
    first = subprocess.Popen([sys.executable, "-c", CHILD, REPO, str(wq.data), "1", "1.5"], env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    time.sleep(0.3)
    second = subprocess.Popen([sys.executable, "-c", CHILD, REPO, str(wq.data), "10", "0"], env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    assert [p.communicate(timeout=60)[0].strip() for p in (first, second)] == ["done", "done"]
    every = snap(*(pr(k) for k in (1, 2, 3, 10, 11, 12)))
    assert {i["hash"] for i in triage.open_items(every)} <= set(config.read_json(triage.cache_path(), {})["rows"])
    again = Model()
    triage.run(every, again, {})
    assert again.of("rows") == []
