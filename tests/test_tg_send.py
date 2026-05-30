"""Tests for tg-send.py CLI."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

import comms_lib as cl


def _load(name: str, filename: str):
    """Load a hyphenated CLI by spec, register under `name`."""
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        name, str(Path(__file__).resolve().parent.parent / "bin" / filename))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


tg_send = _load("tg_send", "tg-send.py")


@pytest.fixture
def paths(tmp_path: Path) -> cl.Paths:
    home = tmp_path / "home"
    home.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    cmux_bin = tmp_path / "cmux"
    cmux_bin.write_text("#!/bin/sh\nexit 0\n")
    cmux_bin.chmod(0o755)
    p = cl.Paths.from_env({
        "HOME": str(home),
        "COMMS_HOME": str(home),
        "COMMS_ASSISTANT_DIR": str(tmp_path / "assistant"),
        "COMMS_BIN_DIR": str(bin_dir),
        "CMUX_BIN": str(cmux_bin),
    })
    p.comms_dir.mkdir(parents=True, exist_ok=True)
    p.config.write_text(json.dumps({
        "telegram": {"bot_token": "TOKEN", "chat_ids": [42, 99]},
        "stale_heartbeat_sec": 600,
        "mute_until_epoch": 0,
    }))
    return p


class FakeHttp:
    """Stand-in for tg-send's `http` function. Records calls; returns canned message_id."""
    def __init__(self, next_msg_id: int = 1000, *, raise_on=None):
        self.next = next_msg_id
        self.raise_on = raise_on or ()  # iterable of (chat_id, RuntimeError-msg)
        self.calls: list[dict] = []

    def __call__(self, token: str, method: str, payload: dict) -> dict:
        self.calls.append({"token": token, "method": method, "payload": payload})
        for chat_id, msg in self.raise_on:
            if payload.get("chat_id") == chat_id:
                raise RuntimeError(msg)
        msg_id = self.next
        self.next += 1
        return {"message_id": msg_id, "chat": {"id": payload["chat_id"]}}


def run(args: list[str], paths: cl.Paths, http=None, clock=None) -> tuple[int, list[dict]]:
    """Invoke tg-send.main; capture each printed JSON line."""
    out_lines: list[dict] = []
    captured: list[str] = []
    import io
    stdout = io.StringIO()
    real = sys.stdout
    sys.stdout = stdout
    try:
        rc = tg_send.main(args, http=http, clock=clock, paths=paths)
    finally:
        sys.stdout = real
    for line in stdout.getvalue().splitlines():
        out_lines.append(json.loads(line))
    return rc, out_lines


# --------------------------------------------------------------------------- tests

def test_broadcast_to_all_chats(paths):
    http = FakeHttp(next_msg_id=500)
    rc, out = run(["--text", "hello", "--kind", "action"], paths, http=http)
    assert rc == 0
    assert {r["chat_id"] for r in out} == {42, 99}
    assert {r["message_id"] for r in out} == {500, 501}
    # threads.jsonl is empty since no --ledger-key was supplied.
    assert not paths.threads.exists()


def test_specific_chat(paths):
    http = FakeHttp()
    rc, out = run(["--text", "hi", "--chat", "42"], paths, http=http)
    assert rc == 0 and len(out) == 1 and out[0]["chat_id"] == 42


def test_with_ledger_key_writes_threads(paths):
    http = FakeHttp(next_msg_id=2000)
    rc, out = run(["--text", "alert", "--kind", "urgent",
                   "--ledger-key", "led-1"], paths, http=http)
    assert rc == 0
    rows = [json.loads(l) for l in paths.threads.read_text().splitlines() if l.strip()]
    assert len(rows) == 2
    assert {r["ledger_key"] for r in rows} == {"led-1"}
    assert {r["tg_msg_id"] for r in rows} == {2000, 2001}


def test_reply_to_passes_through(paths):
    http = FakeHttp()
    run(["--text", "x", "--chat", "42", "--reply-to", "999"], paths, http=http)
    payload = http.calls[0]["payload"]
    assert payload["reply_to_message_id"] == 999


def test_silent_passes_disable_notification(paths):
    http = FakeHttp()
    run(["--text", "x", "--chat", "42", "--silent"], paths, http=http)
    assert http.calls[0]["payload"]["disable_notification"] is True


def test_parse_mode_none(paths):
    http = FakeHttp()
    run(["--text", "x", "--chat", "42", "--parse-mode", "None"], paths, http=http)
    assert "parse_mode" not in http.calls[0]["payload"]


def test_parse_mode_html_default(paths):
    http = FakeHttp()
    run(["--text", "x", "--chat", "42"], paths, http=http)
    assert http.calls[0]["payload"]["parse_mode"] == "HTML"


def test_dry_run(paths):
    http = FakeHttp()
    rc, out = run(["--text", "x", "--dry-run"], paths, http=http)
    assert rc == 0
    assert all(r["dry_run"] is True for r in out)
    assert http.calls == []  # API never hit


def test_mute_suppresses_action(paths):
    cfg = cl.Config.load(paths.config)
    cfg.mute_until_epoch = 2_000_000
    cfg.save()
    http = FakeHttp()
    rc, out = run(["--text", "x", "--kind", "action"], paths, http=http,
                  clock=lambda: 1_000_000)
    assert rc == 0
    assert all(r["muted"] is True for r in out)
    assert http.calls == []


def test_mute_does_not_suppress_urgent(paths):
    cfg = cl.Config.load(paths.config)
    cfg.mute_until_epoch = 2_000_000
    cfg.save()
    http = FakeHttp()
    rc, out = run(["--text", "boom", "--kind", "urgent"], paths, http=http,
                  clock=lambda: 1_000_000)
    assert rc == 0
    assert all(r.get("muted") is False for r in out)
    assert len(http.calls) == 2


def test_mute_does_not_suppress_reply(paths):
    cfg = cl.Config.load(paths.config)
    cfg.mute_until_epoch = 2_000_000
    cfg.save()
    http = FakeHttp()
    rc, out = run(["--text", "ack", "--kind", "reply"], paths, http=http,
                  clock=lambda: 1_000_000)
    assert rc == 0 and len(http.calls) == 2


def test_no_chat_ids_configured_returns_1(paths):
    paths.config.write_text(json.dumps({"telegram": {"bot_token": "T", "chat_ids": []}}))
    rc, out = run(["--text", "x"], paths, http=FakeHttp())
    assert rc == 1


def test_partial_failure_returns_0_when_at_least_one_sent(paths):
    http = FakeHttp(raise_on=[(42, "rate limited")])
    rc, out = run(["--text", "x"], paths, http=http)
    assert rc == 0  # 99 succeeded
    errors = [r for r in out if "error" in r]
    successes = [r for r in out if "message_id" in r]
    assert len(errors) == 1 and errors[0]["chat_id"] == 42
    assert len(successes) == 1 and successes[0]["chat_id"] == 99


def test_full_failure_returns_2(paths):
    http = FakeHttp(raise_on=[(42, "x"), (99, "y")])
    rc, out = run(["--text", "x"], paths, http=http)
    assert rc == 2


def test_real_post_http_error(monkeypatch):
    """Trip the real HTTP path with a fake urlopen that raises HTTPError."""
    import urllib.error
    import urllib.request
    class FakeResp:
        code = 500
        def read(self):
            return b"server boom"
    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError("u", 500, "boom", {}, None)
    monkeypatch.setattr(tg_send.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(RuntimeError, match="HTTP 500"):
        tg_send._real_post("T", "sendMessage", {"chat_id": 1, "text": "x"})


def test_real_post_url_error(monkeypatch):
    import urllib.error
    def fake_urlopen(req, timeout=None):
        raise urllib.error.URLError("dns down")
    monkeypatch.setattr(tg_send.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(RuntimeError, match="URL error"):
        tg_send._real_post("T", "sendMessage", {"chat_id": 1, "text": "x"})


def test_real_post_ok_false(monkeypatch):
    class FakeResp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return json.dumps({"ok": False, "description": "bad token"}).encode()
    monkeypatch.setattr(tg_send.urllib.request, "urlopen", lambda req, timeout=None: FakeResp())
    with pytest.raises(RuntimeError, match="telegram error"):
        tg_send._real_post("T", "sendMessage", {"chat_id": 1, "text": "x"})


def test_real_post_success(monkeypatch):
    class FakeResp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self):
            return json.dumps({"ok": True, "result": {"message_id": 7}}).encode()
    monkeypatch.setattr(tg_send.urllib.request, "urlopen", lambda req, timeout=None: FakeResp())
    out = tg_send._real_post("T", "sendMessage", {"chat_id": 1, "text": "x"})
    assert out == {"message_id": 7}
