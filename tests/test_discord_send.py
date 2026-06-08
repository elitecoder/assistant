"""Tests for discord-send.py CLI."""
from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest

import comms_lib as cl

_BIN = Path(__file__).resolve().parent.parent / "bin"


def _load(name: str, filename: str):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, str(_BIN / filename))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


discord_send = _load("discord_send", "discord-send.py")


@pytest.fixture
def paths(tmp_path: Path) -> cl.Paths:
    home = tmp_path / "home"
    home.mkdir()
    cmux_bin = tmp_path / "cmux"
    cmux_bin.write_text("#!/bin/sh\nexit 0\n")
    cmux_bin.chmod(0o755)
    p = cl.Paths.from_env({
        "HOME": str(home),
        "COMMS_HOME": str(home),
        "COMMS_ASSISTANT_DIR": str(tmp_path / "assistant"),
        "COMMS_BIN_DIR": str(tmp_path / "bin"),
        "CMUX_BIN": str(cmux_bin),
    })
    p.comms_dir.mkdir(parents=True, exist_ok=True)
    (tmp_path / "bin").mkdir(exist_ok=True)
    p.config.write_text(json.dumps({
        "discord": {"bot_token": "DISCORD_TOKEN", "channel_id": 111222333},
        "mute_until_epoch": 0,
    }))
    return p


class FakeHttp:
    """Stand-in for discord-send's `http` function.  Records calls; returns
    a canned Discord message object (the `id` field is used as message_id)."""
    def __init__(self, next_msg_id: int = 9001, *, raise_on: bool = False):
        self.next = next_msg_id
        self.raise_on = raise_on
        self.calls: list[dict] = []

    def __call__(self, token: str, channel_id: int, payload: dict) -> dict:
        if self.raise_on:
            raise RuntimeError("network error")
        self.calls.append({"token": token, "channel_id": channel_id, "payload": payload})
        msg_id = self.next
        self.next += 1
        return {"id": str(msg_id), "channel_id": str(channel_id), "content": payload["content"]}


def run(args: list[str], paths: cl.Paths, http=None, clock=None) -> tuple[int, list[dict]]:
    out_lines: list[dict] = []
    stdout = io.StringIO()
    real = sys.stdout
    sys.stdout = stdout
    try:
        rc = discord_send.main(args, http=http, clock=clock, paths=paths)
    finally:
        sys.stdout = real
    for line in stdout.getvalue().splitlines():
        out_lines.append(json.loads(line))
    return rc, out_lines


# --------------------------------------------------------------------------- tests

def test_send_returns_message_id(paths):
    http = FakeHttp(next_msg_id=555)
    rc, out = run(["--text", "hello", "--channel", "111222333"], paths, http=http)
    assert rc == 0
    assert len(out) == 1
    assert out[0]["channel_id"] == 111222333
    assert out[0]["message_id"] == 555
    assert out[0]["kind"] == "reply"
    assert out[0]["muted"] is False


def test_kind_passed_through(paths):
    http = FakeHttp()
    rc, out = run(["--text", "x", "--channel", "111222333", "--kind", "urgent"], paths, http=http)
    assert rc == 0 and out[0]["kind"] == "urgent"


def test_reply_to_sets_message_reference(paths):
    http = FakeHttp()
    run(["--text", "x", "--channel", "111222333", "--reply-to", "777"], paths, http=http)
    payload = http.calls[0]["payload"]
    assert payload["message_reference"]["message_id"] == "777"


def test_no_reply_to_omits_message_reference(paths):
    http = FakeHttp()
    run(["--text", "x", "--channel", "111222333"], paths, http=http)
    assert "message_reference" not in http.calls[0]["payload"]


def test_dry_run_skips_http(paths):
    http = FakeHttp()
    rc, out = run(["--text", "x", "--channel", "111222333", "--dry-run"], paths, http=http)
    assert rc == 0
    assert out[0]["dry_run"] is True
    assert http.calls == []


def test_mute_suppresses_action(paths):
    paths.config.write_text(json.dumps({
        "discord": {"bot_token": "T", "channel_id": 111},
        "mute_until_epoch": 2_000_000,
    }))
    http = FakeHttp()
    rc, out = run(["--text", "x", "--channel", "111", "--kind", "action"],
                  paths, http=http, clock=lambda: 1_000_000)
    assert rc == 0 and out[0]["muted"] is True
    assert http.calls == []


def test_mute_does_not_suppress_urgent(paths):
    paths.config.write_text(json.dumps({
        "discord": {"bot_token": "T", "channel_id": 111},
        "mute_until_epoch": 2_000_000,
    }))
    http = FakeHttp()
    rc, out = run(["--text", "x", "--channel", "111", "--kind", "urgent"],
                  paths, http=http, clock=lambda: 1_000_000)
    assert rc == 0 and out[0].get("muted") is False


def test_mute_does_not_suppress_reply(paths):
    paths.config.write_text(json.dumps({
        "discord": {"bot_token": "T", "channel_id": 111},
        "mute_until_epoch": 2_000_000,
    }))
    http = FakeHttp()
    rc, out = run(["--text", "x", "--channel", "111", "--kind", "reply"],
                  paths, http=http, clock=lambda: 1_000_000)
    assert rc == 0 and out[0].get("muted") is False


def test_api_failure_returns_2(paths):
    http = FakeHttp(raise_on=True)
    rc, out = run(["--text", "x", "--channel", "111222333"], paths, http=http)
    assert rc == 2
    assert "error" in out[0]


def test_token_not_in_output(paths):
    http = FakeHttp()
    rc, out = run(["--text", "x", "--channel", "111222333"], paths, http=http)
    raw = json.dumps(out)
    assert "DISCORD_TOKEN" not in raw


def test_missing_discord_key_in_config_exits(paths):
    paths.config.write_text(json.dumps({"telegram": {"bot_token": "T", "chat_ids": [1]}}))
    with pytest.raises(SystemExit):
        discord_send.DiscordConfig.load(paths.config)


def test_ts_field_present(paths):
    http = FakeHttp()
    _, out = run(["--text", "x", "--channel", "111"], paths, http=http)
    assert "ts" in out[0]


# --------------------------------------------------------------------------- real HTTP path (monkeypatched)

def test_real_post_http_error(monkeypatch):
    import urllib.error
    import urllib.request
    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError("u", 403, "Forbidden", {}, None)
    monkeypatch.setattr(discord_send.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(RuntimeError, match="discord HTTP 403"):
        discord_send._real_post("T", 123, {"content": "x"})


def test_real_post_url_error(monkeypatch):
    import urllib.error
    def fake_urlopen(req, timeout=None):
        raise urllib.error.URLError("dns down")
    monkeypatch.setattr(discord_send.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(RuntimeError, match="discord URL error"):
        discord_send._real_post("T", 123, {"content": "x"})


def test_real_post_success(monkeypatch):
    class FakeResp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self):
            return json.dumps({"id": "42", "content": "x"}).encode()
    monkeypatch.setattr(discord_send.urllib.request, "urlopen",
                        lambda req, timeout=None: FakeResp())
    result = discord_send._real_post("T", 123, {"content": "x"})
    assert result["id"] == "42"
