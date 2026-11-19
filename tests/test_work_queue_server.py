"""The Work queue server: Send to Assistant, marks, approvals, apply, refresh, and its Pro mode gate."""
from __future__ import annotations

import errno
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from work_queue import config, refresh, server, todo_result
from work_queue_fakes import calls, fake_tool, wq  # noqa: F401

REPO = Path(__file__).resolve().parents[1]
ROW = "https://x.slack.com/archives/C1/p1"


# ─── Send to Assistant ───────────────────────────────────────────────────────

def test_send_creates_an_auto_dispatch_todo_with_the_next_id_and_finish_instructions(wq):
    code, out = server.queue_todo({"key": ROW, "title": "Check  the merge\nhistory", "detail": "Why it matters",
                                   "url": "https://x.slack.com/a"})
    assert (code, out) == (200, {"id": "td-051", "existing": False})
    item = wq.items()[-1]
    assert (item["priority"], item["status"], item["autoDispatch"], item["url"], item["title"]) == \
        ("P2", "open", True, "https://x.slack.com/a", "Check the merge history")
    detail = item["detail"]
    assert detail.startswith("Why it matters\n\n")
    assert "do not wait for review" in detail
    assert 'start each with "Automated Message"' in detail
    assert "Never send email, merge a PR, force-push, or delete data." in detail
    assert f"python3 {REPO / 'work_queue/todo_result.py'} td-051 \"<what you did, with links>\"" in detail
    assert f"{REPO / 'work_queue/todo_result.py'} td-051 --blocked" in detail
    assert "<td-id>" not in detail
    assert json.loads((wq.data / "queued.json").read_text())[ROW]["id"] == "td-051"


def test_sending_the_same_row_twice_returns_the_first_todo(wq):
    server.queue_todo({"key": ROW, "title": "A"})
    assert server.queue_todo({"key": ROW, "title": "A"}) == (200, {"id": "td-051", "existing": True})
    assert len(wq.items()) == 2


def test_a_row_whose_todo_was_removed_can_be_sent_again(wq):
    server.queue_todo({"key": ROW, "title": "A"})
    doc = json.loads(wq.todo.read_text())
    doc["items"][-1]["status"] = "removed"
    wq.todo.write_text(json.dumps(doc))
    assert server.queue_todo({"key": ROW, "title": "A"}) == (200, {"id": "td-052", "existing": False})


def test_send_rejects_a_missing_title_or_key_and_a_non_https_url(wq, monkeypatch):
    assert server.queue_todo({"key": "k", "title": ""})[0] == 400
    assert server.queue_todo({"key": "", "title": "t"})[0] == 400
    assert server.queue_todo({"key": "k", "title": "t", "url": "http://x"})[0] == 400
    assert len(wq.items()) == 1


def test_queued_path_override_and_an_empty_store_start_at_td_001(wq, monkeypatch):
    monkeypatch.setenv("QUEUED_PATH", str(wq.root / "elsewhere.json"))
    wq.todo.write_text(json.dumps({}))
    assert server.queue_todo({"key": ROW, "title": "A"}) == (200, {"id": "td-001", "existing": False})
    assert (wq.root / "elsewhere.json").exists() and not (wq.data / "queued.json").exists()


def test_state_shows_progress_then_the_agents_result(wq):
    assert server.queued_view() == {}
    server.queue_todo({"key": ROW, "title": "A"})
    view = server.queued_view()[ROW]
    assert (view["status"], view["result"], view["dispatched"]) == ("open", "", False)
    todo_result.main("td-051", "Found it: squash merge was tried in PR 9. https://github.com/x/pull/9")
    view = server.queued_view()[ROW]
    assert (view["status"], view["result"]) == ("done", "Found it: squash merge was tried in PR 9. https://github.com/x/pull/9")
    assert view["reason"] == "agent finished" and view["finished"]


def test_a_blocked_result_marks_blocked_with_the_reason_and_no_result(wq):
    server.queue_todo({"key": ROW, "title": "A"})
    todo_result.main("td-051", "Need Jira access", blocked=True)
    view = server.queued_view()[ROW]
    assert (view["status"], view["reason"], view["result"]) == ("blocked", "Need Jira access", "")


def test_a_removed_todo_is_reported_removed(wq):
    server.queue_todo({"key": ROW, "title": "A"})
    doc = json.loads(wq.todo.read_text())
    doc["items"] = [i for i in doc["items"] if i["id"] != "td-051"]
    wq.todo.write_text(json.dumps(doc))
    assert server.queued_view()[ROW] == {"id": "td-051", "status": "removed", "result": ""}


def test_todo_result_cli_closes_the_todo_and_rejects_bad_input(wq):
    server.queue_todo({"key": ROW, "title": "A"})
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    script = str(REPO / "work_queue/todo_result.py")
    ok = subprocess.run([sys.executable, script, "td-051", "Filed FFP-1"], capture_output=True, text=True, env=env)
    assert ok.returncode == 0 and "td-051: result recorded, marked done" in ok.stdout
    assert server.queued_view()[ROW]["result"] == "Filed FFP-1"
    usage = subprocess.run([sys.executable, script, "td-051"], capture_output=True, text=True, env=env)
    assert usage.returncode == 1 and "usage: todo_result.py" in usage.stderr
    with pytest.raises(SystemExit, match="result text is empty"):
        todo_result.cli(["td-051", "  "])
    with pytest.raises(SystemExit, match="td-999 not found"):
        todo_result.cli(["td-999", "--blocked", "why"])


# ─── marks ───────────────────────────────────────────────────────────────────

def test_marks_are_saved_merged_and_cleared(wq):
    assert server.set_marks({"key": ROW, "state": "done"})[1]["marks"][ROW]["state"] == "done"
    assert set(server.set_marks({"marks": {"https://y/z": {"state": "snoozed", "until": 5}}})[1]["marks"]) == {ROW, "https://y/z"}
    assert json.loads((wq.data / "marks.json").read_text())["https://y/z"]["until"] == 5
    assert ROW not in server.set_marks({"key": ROW, "state": "clear"})[1]["marks"]
    assert server.set_marks({"key": ROW, "state": "bogus"})[0] == 400
    assert server.set_marks({"key": "javascript:1", "state": "done"})[0] == 400
    assert server.set_marks({"marks": {ROW: "done"}})[0] == 400


def test_marks_path_override(wq, monkeypatch):
    monkeypatch.setenv("MARKS_PATH", str(wq.root / "m.json"))
    server.set_marks({"key": ROW, "state": "dropped"})
    assert json.loads((wq.root / "m.json").read_text())[ROW]["state"] == "dropped"
    assert server.state()["marks"][ROW]["state"] == "dropped"


# ─── HTTP ────────────────────────────────────────────────────────────────────

@pytest.fixture
def http(wq):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"

    def call(path, body=None, origin=base, raw=None):
        data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
        req = urllib.request.Request(base + path, data=data, method="GET" if data is None else "POST",
                                     headers={"Origin": origin, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as ex:
            return ex.code, ex.read().decode()

    call.port = srv.server_port
    yield call
    srv.shutdown()


def test_a_cross_origin_post_is_refused_and_creates_nothing(http, wq):
    code, _ = http("/api/todo", {"key": ROW, "title": "t"}, origin="https://evil.example")
    assert code == 403
    assert len(wq.items()) == 1


def test_routes_answer_from_the_same_origin(http, wq):
    code, page = http("/")
    assert code == 200 and "Nothing to show yet" in page and "work-queue-setup.py link" in page
    refresh.site_path().parent.mkdir(parents=True)
    refresh.site_path().write_text("<p>built page</p>")
    assert http("/index.html?x=1") == (200, "<p>built page</p>")
    assert http("/candidates.json")[0] == 404
    assert http("/api/nope", {})[0] == 404
    assert http("/api/mark", raw=b"{not json")[0] == 400
    assert http("/api/decide", raw=b"[1]")[0] == 400
    code, body = http("/api/todo", {"key": ROW, "title": "From the page"})
    assert code == 200 and json.loads(body)["id"] == "td-051"
    code, body = http("/api/mark", {"key": ROW, "state": "done"})
    assert code == 200
    state = json.loads(http("/api/state")[1])
    assert state["marks"][ROW]["state"] == "done" and state["queued"][ROW]["id"] == "td-051"
    assert http("/api/mark", {"key": ROW, "state": "clear"}, origin=f"http://localhost:{http.port}")[0] == 200
    assert http("/api/mark", {"key": ROW, "state": "clear"}, origin=f"http://localhost:{http.port + 1}")[0] == 403


def test_decide_records_and_clears_decisions_for_known_rows_only(wq):
    config.write_json(refresh.candidates_path(), {"k1": {"kind": "gmail"}, "k2": {"kind": "slack"}})
    assert server.decide({"keys": ["k1", "k2"], "decision": "approve"})[1]["decisions"]["k2"]["decision"] == "approve"
    assert set(server.decide({"keys": ["k1"], "decision": "clear"})[1]["decisions"]) == {"k2"}
    assert server.decide({"keys": ["nope"], "decision": "approve"})[0] == 400
    assert server.decide({"keys": "k1", "decision": "approve"})[0] == 400
    assert server.decide({"keys": ["k1"], "decision": "delete"})[0] == 400


# ─── apply ───────────────────────────────────────────────────────────────────

@pytest.fixture
def unsub_site():
    hits = []

    class Site(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            hits.append((self.path, self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200 if self.path == "/ok" else 500)
            self.end_headers()

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Site)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}", hits
    srv.shutdown()


def closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_apply_trashes_gmail_unsubscribes_and_marks_slack_then_verifies(wq, unsub_site):
    base, hits = unsub_site
    fake_tool(wq.bin, "gog", [
        ("gmail trash m1", {"ok": True}),
        ("gmail trash bad", {"exit": 1, "stderr": "quota exceeded"}),
        ("gmail messages modify m9", {"ok": True}),
        ("gmail get m1", {"message": {"labelIds": ["TRASH"]}}),
        ("gmail get m9", {"message": {"labelIds": ["INBOX"]}}),
    ])
    fake_tool(wq.bin, "uv", [("slack_browser.py apply", {"ok": True, "applied": {
        "s1": {"status": "done", "verified": True, "at": "now"}}})])
    wq.sources({"slack": {"team_id": "T1", "workspace_url": "https://x.slack.com"}})
    config.write_json(refresh.candidates_path(), {
        "g1": {"kind": "gmail", "account": "a@x.com", "msg_ids": ["m1"]},
        "g2": {"kind": "gmail", "account": "a@x.com", "msg_ids": ["bad"]},
        "g9": {"kind": "gmail", "account": "a@x.com", "msg_ids": ["m9"]},
        "u1": {"kind": "unsub", "url": base + "/ok"}, "u2": {"kind": "unsub", "url": base + "/no"},
        "u3": {"kind": "unsub", "url": f"http://127.0.0.1:{closed_port()}/x"}, "u4": {"kind": "unsub", "url": base + "/ok"},
        "s1": {"kind": "slack", "cid": "C1", "ts": "1.2"}, "m1": {"kind": "mail"}, "kept": {"kind": "gmail"}})
    config.write_json(wq.data / "decisions.json", {k: {"decision": "approve"} for k in ("g1", "g2", "u1", "u2", "u3", "s1", "m1", "gone")}
                      | {"g9": {"decision": "undo"}, "u4": {"decision": "undo"}, "kept": {"decision": "keep"}})
    config.write_json(wq.data / "applied.json", {"g9": {"status": "done"}, "u4": {"status": "done"}})
    server.run_apply()
    applied = json.loads((wq.data / "applied.json").read_text())
    assert applied["g1"]["status"] == "done" and applied["g1"]["verified"] is True
    assert applied["g2"] == {"status": "error", "error": applied["g2"]["error"], "at": applied["g2"]["at"]}
    assert "quota exceeded" in applied["g2"]["error"]
    assert applied["g9"]["status"] == "undone" and applied["g9"]["verified"] is True
    assert applied["u1"]["verified"] is True and applied["u2"]["error"] == "The sender answered 500"
    assert applied["u3"]["status"] == "error" and applied["u4"]["error"] == "Unsubscribing can't be undone from here"
    assert applied["s1"]["verified"] is True and "m1" not in applied and "kept" not in applied
    assert [p for p, _ in hits] == ["/ok", "/no"] and hits[0][1] == b"List-Unsubscribe=One-Click"
    job = json.loads((wq.data / "apply-job.json").read_text())
    assert job == [{"key": "s1", "cid": "C1", "ts": "1.2", "mark": "completed"}]
    assert "--headless --job" in calls(wq.bin, "uv")[0]
    status = json.loads((wq.data / "apply-status.json").read_text())
    assert (status["running"], status["gmail"], status["unsub"], status["slack"], status["ok"], status["verified"]) == \
        (False, 3, 4, 1, 5, 4)


def test_a_slack_apply_failure_marks_every_save_failed(wq):
    config.write_json(refresh.candidates_path(), {"s1": {"kind": "slack", "cid": "C1", "ts": "1"}})
    config.write_json(wq.data / "decisions.json", {"s1": {"decision": "approve"}})
    server.run_apply()
    assert json.loads((wq.data / "applied.json").read_text())["s1"]["error"].startswith("Slack isn't linked.")
    (wq.home / ".assistant/work-queue").mkdir(parents=True)
    (wq.home / ".assistant/work-queue/sources.json").write_text("{torn")
    server.run_apply()
    assert "isn't valid JSON" in json.loads((wq.data / "applied.json").read_text())["s1"]["error"]
    wq.sources({"slack": {"team_id": "T1", "workspace_url": "https://x.slack.com"}})
    fake_tool(wq.bin, "uv", [("apply", {"ok": False, "error": "Slack sign-in needed"})])
    server.run_apply()
    assert json.loads((wq.data / "applied.json").read_text())["s1"]["error"] == "Slack sign-in needed"
    fake_tool(wq.bin, "uv", [("apply", {"ok": False})])
    server.run_apply()
    assert json.loads((wq.data / "applied.json").read_text())["s1"]["error"] == "Slack apply failed"


def test_apply_runs_once_at_a_time(wq, monkeypatch):
    started = []
    monkeypatch.setattr(server, "run_apply", lambda: started.append(1))
    assert server.start_apply() == (202, {"started": True})
    assert server.start_apply() == (409, {"error": "already running"})
    time.sleep(0.1)
    assert started == [1]


# ─── refresh ─────────────────────────────────────────────────────────────────

class Proc:
    def __init__(self, code):
        self.code = code

    def poll(self):
        return self.code


def test_refresh_state_reports_a_refresh_that_died(wq, monkeypatch):
    monkeypatch.setattr(server, "refresh_proc", None)
    assert server.refresh_state() == {}
    config.write_json(refresh.status_path(), {"running": True})
    monkeypatch.setattr(server, "refresh_proc", Proc(None))
    assert server.refresh_state() == {"running": True}
    assert server.start_refresh() == (409, {"error": "already refreshing"})
    monkeypatch.setattr(server, "refresh_proc", Proc(1))
    state = server.refresh_state()
    assert state["error"] == "The refresh stopped before it finished" and server.refresh_proc is None
    config.write_json(refresh.status_path(), {"running": False, "finished": "t"})
    monkeypatch.setattr(server, "refresh_proc", Proc(0))
    assert server.refresh_state() == {"running": False, "finished": "t"} and server.refresh_proc is None


@pytest.mark.real_mode
def test_the_refresh_button_rebuilds_the_page_in_the_background(http, wq, monkeypatch):
    monkeypatch.setattr(server, "refresh_proc", None)
    (wq.home / ".assistant").mkdir(exist_ok=True)
    (wq.home / ".assistant/config.json").write_text('{"mode": "pro"}')
    code, body = http("/api/refresh", {})
    assert (code, json.loads(body)) == (202, {"started": True})
    for _ in range(200):
        state = json.loads(http("/api/state")[1])["refresh"]
        if not state.get("running"):
            break
        time.sleep(0.1)
    assert state["finished"] and state["error"] == ""
    assert "No sources are linked yet." in http("/")[1]
    assert "Slack: off — not linked" in (wq.data / "refresh.log").read_text()


# ─── the Pro mode gate ───────────────────────────────────────────────────────

@pytest.mark.real_mode
def test_default_mode_never_starts_the_server(wq, monkeypatch, capsys):
    monkeypatch.setattr(server, "ThreadingHTTPServer", lambda *a: pytest.fail("bound a port in default mode"))
    assert server.main() == 0
    assert "Pro mode is off, so the Work queue server doesn't start" in capsys.readouterr().out


def test_a_taken_port_or_unreadable_settings_exit_cleanly_instead_of_restarting(wq, monkeypatch, capsys):
    config.write_json(wq.data / "apply-status.json", {"running": True})
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        port = taken.getsockname()[1]
        monkeypatch.setenv("REVIEW_PORT", str(port))
        assert server.main() == 0
    assert f"Port {port} is in use" in capsys.readouterr().err
    assert json.loads((wq.data / "apply-status.json").read_text()) == {"running": False, "error": "The server restarted during apply"}
    wq.sources({"port": 1})
    (wq.home / ".assistant/work-queue/sources.json").write_text("{torn")
    assert server.main() == 0
    assert "can't read its settings" in capsys.readouterr().err

    bound = []

    def denied(address, handler):
        bound.append(address)
        raise OSError(errno.EACCES, "denied")

    monkeypatch.setattr(server, "ThreadingHTTPServer", denied)
    monkeypatch.delenv("REVIEW_PORT")
    wq.sources({"port": 9555})
    with pytest.raises(OSError):
        server.main()
    assert bound == [("127.0.0.1", 9555)]


@pytest.mark.real_mode
def test_pro_mode_serves_on_localhost_and_stops_on_sigterm(wq):
    (wq.home / ".assistant").mkdir(exist_ok=True)
    (wq.home / ".assistant/config.json").write_text('{"mode": "pro"}')
    port = closed_port()
    env = {**os.environ, "REVIEW_PORT": str(port), "PYTHONPATH": f"{REPO}{os.pathsep}{REPO / 'src'}"}
    proc = subprocess.Popen([sys.executable, "-m", "work_queue.server"], cwd=REPO, env=env,
                            stdout=subprocess.PIPE, text=True)
    assert proc.stdout.readline().strip() == f"Serving the Work queue on http://127.0.0.1:{port}/"
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state") as r:
        assert json.loads(r.read())["queued"] == {}
    proc.send_signal(signal.SIGTERM)
    assert proc.wait(10) == 0
