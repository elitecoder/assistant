"""Tests for discord-poll.py CLI."""
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


discord_poll = _load("discord_poll", "discord-poll.py")


@pytest.fixture
def paths(tmp_path: Path) -> cl.Paths:
    home = tmp_path / "home"
    home.mkdir()
    p = cl.Paths.from_env({
        "HOME": str(home),
        "COMMS_HOME": str(home),
        "COMMS_ASSISTANT_DIR": str(tmp_path / "assistant"),
        "COMMS_BIN_DIR": str(tmp_path / "bin"),
        "CMUX_BIN": "/bin/true",
    })
    p.comms_dir.mkdir(parents=True, exist_ok=True)
    (tmp_path / "bin").mkdir(exist_ok=True)
    p.config.write_text(json.dumps({
        "discord": {"bot_token": "BOT_TOKEN", "channel_id": 999888777},
    }))
    return p


class FakeGetMessages:
    def __init__(self, messages: list[dict]):
        self.messages = messages
        self.calls: list[dict] = []

    def __call__(self, token, channel_id, after, limit):
        self.calls.append({"token": token, "channel_id": channel_id,
                           "after": after, "limit": limit})
        return self.messages


def make_message(msg_id: int, content: str, username: str = "mukul",
                 bot: bool = False, reply_to: int | None = None,
                 msg_type: int = 0) -> dict:
    msg: dict = {
        "id": str(msg_id),
        "type": msg_type,
        "content": content,
        "author": {"id": "1", "username": username, "bot": bot},
    }
    if reply_to is not None:
        msg["message_reference"] = {"message_id": str(reply_to)}
    return msg


def run(args: list[str], paths: cl.Paths, http=None, clock=None) -> tuple[int, list[dict]]:
    real = sys.stdout
    sys.stdout = io.StringIO()
    try:
        rc = discord_poll.main(args, http=http, clock=clock, paths=paths)
    finally:
        captured = sys.stdout.getvalue()
        sys.stdout = real
    return rc, json.loads(captured.strip() or "[]")


# --------------------------------------------------------------------------- cursor helpers

def read_cursor(paths: cl.Paths) -> int:
    return discord_poll.read_discord_cursor(paths)


def write_cursor(paths: cl.Paths, val: int) -> None:
    discord_poll.write_discord_cursor(paths, val)


# --------------------------------------------------------------------------- tests

def test_no_messages(paths):
    http = FakeGetMessages([])
    rc, out = run([], paths, http=http)
    assert rc == 0 and out == []
    assert read_cursor(paths) == 0  # cursor unchanged when no messages


def test_single_message_advances_cursor(paths):
    http = FakeGetMessages([make_message(1000, "hello")])
    rc, out = run([], paths, http=http)
    assert rc == 0
    assert len(out) == 1
    assert out[0]["channel_id"] == 999888777
    assert out[0]["msg_id"] == 1000
    assert out[0]["author"] == "mukul"
    assert out[0]["text"] == "hello"
    assert out[0]["reply_to"] is None
    assert "ts" in out[0]
    assert read_cursor(paths) == 1000


def test_multiple_messages(paths):
    http = FakeGetMessages([
        make_message(1000, "a"),
        make_message(1001, "b"),
        make_message(1002, "c"),
    ])
    rc, out = run([], paths, http=http)
    assert rc == 0 and len(out) == 3
    assert read_cursor(paths) == 1002


def test_uses_existing_cursor(paths):
    write_cursor(paths, 5000)
    http = FakeGetMessages([])
    run([], paths, http=http)
    assert http.calls[0]["after"] == 5000


def test_bot_messages_filtered(paths):
    http = FakeGetMessages([
        make_message(1000, "bot says hi", bot=True),
        make_message(1001, "human says hi", bot=False),
    ])
    rc, out = run([], paths, http=http)
    assert rc == 0 and len(out) == 1
    assert out[0]["msg_id"] == 1001
    # Cursor still advances past the bot message.
    assert read_cursor(paths) == 1001


def test_reply_to_extracted(paths):
    http = FakeGetMessages([make_message(2000, "re: that", reply_to=1999, msg_type=19)])
    rc, out = run([], paths, http=http)
    assert rc == 0 and out[0]["reply_to"] == 1999


def test_no_reply_to_is_none(paths):
    http = FakeGetMessages([make_message(2000, "just a msg")])
    rc, out = run([], paths, http=http)
    assert out[0]["reply_to"] is None


def test_system_message_type_filtered(paths):
    # Type 7 = channel_pinned_message — should be skipped.
    http = FakeGetMessages([make_message(2000, "pinned!", msg_type=7)])
    rc, out = run([], paths, http=http)
    assert rc == 0 and out == []
    # Cursor still advances past the system message.
    assert read_cursor(paths) == 2000


def test_cursor_advances_even_when_all_filtered(paths):
    """If every message was filtered (bot / system), cursor still advances
    so we don't refetch the same noise next call."""
    http = FakeGetMessages([
        make_message(5000, "bleep", bot=True),
        make_message(5001, "bloop", msg_type=6),
    ])
    rc, out = run([], paths, http=http)
    assert rc == 0 and out == []
    assert read_cursor(paths) == 5001


def test_reset_cursor_advances_to_latest(paths):
    http = FakeGetMessages([make_message(9999, "latest")])
    rc, out = run(["--reset-cursor"], paths, http=http)
    assert rc == 0 and out == []
    assert read_cursor(paths) == 9999


def test_reset_cursor_no_messages(paths):
    http = FakeGetMessages([])
    rc, out = run(["--reset-cursor"], paths, http=http)
    assert rc == 0 and out == []
    assert read_cursor(paths) == 0


def test_reset_cursor_api_error(paths, capsys):
    def boom(token, channel_id, after, limit):
        raise RuntimeError("network gone")
    rc, _ = run(["--reset-cursor"], paths, http=boom)
    assert rc == 1
    assert "network gone" in capsys.readouterr().err


def test_api_error_returns_1(paths, capsys):
    def boom(token, channel_id, after, limit):
        raise RuntimeError("flake")
    rc, _ = run([], paths, http=boom)
    assert rc == 1
    assert "flake" in capsys.readouterr().err


def test_missing_discord_key_in_config_exits(paths):
    paths.config.write_text(json.dumps({"telegram": {"bot_token": "T", "chat_ids": [1]}}))
    with pytest.raises(SystemExit):
        discord_poll.DiscordPollConfig.load(paths.config)


def test_missing_channel_id_exits(paths):
    paths.config.write_text(json.dumps({"discord": {"bot_token": "T"}}))
    with pytest.raises(SystemExit):
        discord_poll.DiscordPollConfig.load(paths.config)


def test_token_not_in_output(paths):
    http = FakeGetMessages([make_message(1, "hi")])
    _, out = run([], paths, http=http)
    assert "BOT_TOKEN" not in json.dumps(out)


# --------------------------------------------------------------------------- real HTTP path (monkeypatched)

def test_real_get_messages_http_error(monkeypatch):
    import urllib.error
    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError("u", 429, "Too Many Requests", {}, None)
    monkeypatch.setattr(discord_poll.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(RuntimeError, match="discord HTTP 429"):
        discord_poll._real_get_messages("T", 123, 0, 20)


def test_real_get_messages_url_error(monkeypatch):
    import urllib.error
    def fake_urlopen(req, timeout=None):
        raise urllib.error.URLError("no dns")
    monkeypatch.setattr(discord_poll.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(RuntimeError, match="discord URL error"):
        discord_poll._real_get_messages("T", 123, 0, 20)


def test_real_get_messages_success(monkeypatch):
    msgs = [{"id": "1", "type": 0, "content": "hi", "author": {"username": "u"}}]

    class FakeResp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return json.dumps(msgs).encode()

    monkeypatch.setattr(discord_poll.urllib.request, "urlopen",
                        lambda req, timeout=None: FakeResp())
    out = discord_poll._real_get_messages("T", 123, 0, 20)
    assert len(out) == 1 and out[0]["id"] == "1"


# --------------------------------------------------------------------------- project_message unit tests

def test_project_message_basic():
    msg = make_message(100, "hey there")
    result = discord_poll.project_message(msg, channel_id=42)
    assert result is not None
    assert result["msg_id"] == 100
    assert result["text"] == "hey there"
    assert result["channel_id"] == 42
    assert result["author"] == "mukul"
    assert result["reply_to"] is None


def test_project_message_bot_returns_none():
    msg = make_message(100, "bot", bot=True)
    assert discord_poll.project_message(msg, channel_id=42) is None


def test_project_message_system_type_returns_none():
    msg = make_message(100, "pinned", msg_type=7)
    assert discord_poll.project_message(msg, channel_id=42) is None


def test_project_message_reply_type_allowed():
    msg = make_message(100, "reply", msg_type=19, reply_to=99)
    result = discord_poll.project_message(msg, channel_id=42)
    assert result is not None
    assert result["reply_to"] == 99
