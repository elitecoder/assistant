"""The Slack Later helper drives a real Chromium against a local stand-in for Slack.

The stand-in serves a Later page that calls saved.list with a session token,
the way Slack's web client does, and answers saved.list and saved.update.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api")
from work_queue import slack_browser  # noqa: E402
from work_queue_fakes import real_browsers  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
TOKEN = "xoxc-test-session"
PAGE = """<!doctype html><title>Later</title><script>
const fd = new FormData(); fd.append('token', '%s'); fetch('/api/saved.list', {method: 'POST', body: fd});
</script>""" % TOKEN


class FakeSlack(BaseHTTPRequestHandler):
    saves: dict = {}
    updates: list = []
    fail_list = False

    def log_message(self, *a):
        pass

    def answer(self, code: int, body: str, kind: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.end_headers()
        self.wfile.write(body.encode())

    def do_GET(self):
        self.answer(200, PAGE if self.path == "/client/T1/later" else "<title>Sign in</title>", "text/html")

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()
        if self.path == "/api/saved.list" and "multipart" in self.headers.get("Content-Type", ""):
            return self.answer(200, '{"ok": true, "saved_items": []}', "application/json")
        form = dict(urllib.parse.parse_qsl(raw))
        if form.get("token") != TOKEN:
            return self.answer(200, '{"ok": false, "error": "invalid_auth"}', "application/json")
        if self.path == "/api/saved.list":
            if FakeSlack.fail_list:
                return self.answer(200, '{"ok": false, "error": "ratelimited"}', "application/json")
            items = [{"item_type": "message", "item_id": c, "ts": t, "date_created": 1, "state": "in_progress"}
                     for (c, t), open_ in sorted(FakeSlack.saves.items()) if open_]
            page = items[:2] if not form.get("cursor") else items[2:]
            meta = {"next_cursor": "p2"} if not form.get("cursor") and len(items) > 2 else {}
            return self.answer(200, json.dumps({"ok": True, "saved_items": page, "response_metadata": meta}), "application/json")
        FakeSlack.updates.append(form)
        key = (form["item_id"], form["ts"])
        if key not in FakeSlack.saves:
            return self.answer(200, '{"ok": false, "error": "not_found"}', "application/json")
        if form["item_id"] != "STUCK":
            FakeSlack.saves[key] = form["mark"] == "uncompleted"
        return self.answer(200, '{"ok": true}', "application/json")


@pytest.fixture
def slack_site(monkeypatch):
    real_browsers(monkeypatch)
    FakeSlack.saves = {("C1", "1.1"): True, ("C2", "2.2"): True, ("C3", "3.3"): True, ("STUCK", "4.4"): True,
                       ("C5", "5.5"): False}
    FakeSlack.updates = []
    FakeSlack.fail_list = False
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeSlack)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def args(base: str, tmp_path: Path, action: str, *extra: str) -> list[str]:
    return [action, "--client-url", f"{base}/client/T1/later", "--api-url", f"{base}/api/",
            "--profile", str(tmp_path / "profile"), "--headless", "--wait", "5", *extra]


def output(capsys) -> dict:
    return json.loads(capsys.readouterr().out)


def test_list_reads_every_page_of_open_saves(slack_site, tmp_path, capsys):
    slack_browser.main(args(slack_site, tmp_path, "list"))
    out = output(capsys)
    assert out["ok"] is True
    assert [(i["item_id"], i["ts"]) for i in out["items"]] == [("C1", "1.1"), ("C2", "2.2"), ("C3", "3.3"), ("STUCK", "4.4")]
    assert TOKEN not in json.dumps(out)


def test_apply_marks_saves_and_verifies_each_against_the_open_list(slack_site, tmp_path, capsys):
    job = tmp_path / "job.json"
    job.write_text(json.dumps([
        {"key": "a", "cid": "C1", "ts": "1.1", "mark": "completed"},
        {"key": "b", "cid": "C5", "ts": "5.5", "mark": "uncompleted"},
        {"key": "c", "cid": "STUCK", "ts": "4.4", "mark": "completed"},
        {"key": "d", "cid": "C9", "ts": "9.9", "mark": "completed"}]))
    slack_browser.main(args(slack_site, tmp_path, "apply", "--job", str(job)))
    applied = output(capsys)["applied"]
    assert (applied["a"]["status"], applied["a"]["verified"]) == ("done", True)
    assert (applied["b"]["status"], applied["b"]["verified"]) == ("undone", True)
    assert applied["c"]["verified"] is False and "still shows the old state" in applied["c"]["error"]
    assert applied["d"]["status"] == "error" and applied["d"]["error"] == "not_found"
    assert FakeSlack.updates[0] == {"token": TOKEN, "item_type": "message", "item_id": "C1", "ts": "1.1",
                                    "date_due": "0", "mark": "completed"}


def test_no_session_or_a_refused_call_reports_without_the_token(slack_site, tmp_path, capsys):
    slack_browser.main(["signin", "--client-url", f"{slack_site}/client/OTHER/later", "--api-url", f"{slack_site}/api/",
                        "--profile", str(tmp_path / "profile"), "--headless", "--wait", "2"])
    assert output(capsys) == {"ok": False, "error": slack_browser.SIGN_IN}
    FakeSlack.fail_list = True
    slack_browser.main(args(slack_site, tmp_path, "list"))
    assert output(capsys) == {"ok": False, "error": "saved.list failed: ratelimited"}
    slack_browser.main(args(slack_site, tmp_path, "signin"))
    assert output(capsys) == {"ok": True, "items": []}


def test_the_script_runs_on_its_own(slack_site, tmp_path):
    proc = subprocess.run([sys.executable, str(REPO / "work_queue/slack_browser.py"), *args(slack_site, tmp_path, "signin")],
                          capture_output=True, text=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}, timeout=120)
    assert json.loads(proc.stdout) == {"ok": True, "items": []}
