"""Tests for tg-poll.py CLI."""
from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest

import comms_lib as cl


def _load(name, fname):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        name, str(Path(__file__).resolve().parent.parent / "bin" / fname))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


tg_poll = _load("tg_poll", "tg-poll.py")


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
        "telegram": {"bot_token": "T", "chat_ids": [42]}
    }))
    return p


class FakeGetUpdates:
    def __init__(self, updates: list[dict]):
        self.updates = updates
        self.calls: list[dict] = []
    def __call__(self, token, offset, timeout, limit):
        self.calls.append({"offset": offset, "timeout": timeout, "limit": limit})
        return self.updates


def make_update(update_id: int, chat_id: int, msg_id: int, text: str,
                reply_to: int | None = None, username: str = "mukul") -> dict:
    msg = {"message_id": msg_id, "chat": {"id": chat_id},
           "from": {"id": chat_id, "username": username}, "text": text}
    if reply_to is not None:
        msg["reply_to_message"] = {"message_id": reply_to}
    return {"update_id": update_id, "message": msg}


def make_photo_update(update_id: int, chat_id: int, msg_id: int,
                      caption: str | None = None, username: str = "mukul") -> dict:
    """A Telegram photo update: a `photo` array of escalating resolutions and
    no `text` field. The largest size is last in the array."""
    msg = {
        "message_id": msg_id,
        "chat": {"id": chat_id},
        "from": {"id": chat_id, "username": username},
        "photo": [
            {"file_id": "small", "width": 320, "height": 240},
            {"file_id": "medium", "width": 800, "height": 600},
            {"file_id": "large", "width": 1200, "height": 900},
        ],
    }
    if caption is not None:
        msg["caption"] = caption
    return {"update_id": update_id, "message": msg}


def run(args: list[str], paths: cl.Paths, http=None, clock=None) -> tuple[int, list[dict]]:
    real = sys.stdout
    sys.stdout = io.StringIO()
    try:
        rc = tg_poll.main(args, http=http, clock=clock, paths=paths)
    finally:
        captured = sys.stdout.getvalue()
        sys.stdout = real
    return rc, json.loads(captured.strip() or "[]")


def test_no_updates(paths):
    http = FakeGetUpdates([])
    rc, out = run([], paths, http=http)
    assert rc == 0 and out == []
    assert cl.read_tg_cursor(paths) == 0


def test_single_update_advances_cursor(paths):
    http = FakeGetUpdates([make_update(100, 42, 1, "ping")])
    rc, out = run([], paths, http=http)
    assert rc == 0
    assert len(out) == 1
    assert out[0] == {"update_id": 100, "chat_id": 42, "msg_id": 1,
                      "from_user": "mukul", "text": "ping",
                      "reply_to_msg_id": None, "ts": out[0]["ts"]}
    assert cl.read_tg_cursor(paths) == 101


def test_multiple_updates(paths):
    http = FakeGetUpdates([
        make_update(100, 42, 1, "a"),
        make_update(101, 42, 2, "b"),
        make_update(102, 42, 3, "c"),
    ])
    rc, out = run([], paths, http=http)
    assert rc == 0 and len(out) == 3
    assert cl.read_tg_cursor(paths) == 103


def test_unauthorized_chat_filtered(paths):
    http = FakeGetUpdates([
        make_update(100, 42, 1, "ok"),
        make_update(101, 999, 2, "spam"),  # not in chat_ids
    ])
    rc, out = run([], paths, http=http)
    assert rc == 0 and len(out) == 1 and out[0]["chat_id"] == 42
    # cursor still advanced past the spam so we don't keep re-fetching it.
    assert cl.read_tg_cursor(paths) == 102


def test_uses_existing_cursor(paths):
    cl.write_tg_cursor(paths, 500)
    http = FakeGetUpdates([])
    run([], paths, http=http)
    assert http.calls[0]["offset"] == 500


def test_reply_to_extracted(paths):
    http = FakeGetUpdates([make_update(100, 42, 5, "y", reply_to=999)])
    rc, out = run([], paths, http=http)
    assert out[0]["reply_to_msg_id"] == 999


def test_message_without_text(paths):
    # An attached image with no text — text defaults to empty.
    upd = make_update(100, 42, 1, "")
    http = FakeGetUpdates([upd])
    rc, out = run([], paths, http=http)
    assert rc == 0 and out[0]["text"] == ""


def test_photo_message_parsed(paths):
    # A photo with a caption: text is the caption, photo flags are set, and the
    # largest (last) file_id is carried for future use.
    http = FakeGetUpdates([make_photo_update(100, 42, 222, caption="look at this")])
    rc, out = run([], paths, http=http)
    assert rc == 0 and len(out) == 1
    assert out[0]["text"] == "look at this"
    assert out[0]["has_photo"] is True
    assert out[0]["photo_file_id"] == "large"
    assert out[0]["msg_id"] == 222
    assert cl.read_tg_cursor(paths) == 101


def test_photo_message_no_caption(paths):
    # A photo with no caption falls back to the "[photo]" placeholder so the
    # warm session never sees an empty turn.
    http = FakeGetUpdates([make_photo_update(100, 42, 223)])
    rc, out = run([], paths, http=http)
    assert rc == 0 and len(out) == 1
    assert out[0]["text"] == "[photo]"
    assert out[0]["has_photo"] is True
    assert out[0]["photo_file_id"] == "large"


def test_text_message_unchanged(paths):
    # Regular text messages still carry no photo fields at all.
    http = FakeGetUpdates([make_update(100, 42, 1, "ping")])
    rc, out = run([], paths, http=http)
    assert rc == 0 and len(out) == 1
    assert out[0]["text"] == "ping"
    assert "has_photo" not in out[0]
    assert "photo_file_id" not in out[0]


def test_username_fallback_to_first_name(paths):
    upd = {"update_id": 1, "message": {
        "message_id": 1, "chat": {"id": 42},
        "from": {"id": 42, "first_name": "Mukul"},  # no username
        "text": "hi",
    }}
    http = FakeGetUpdates([upd])
    rc, out = run([], paths, http=http)
    assert out[0]["from_user"] == "Mukul"


def test_username_fallback_to_id(paths):
    upd = {"update_id": 1, "message": {
        "message_id": 1, "chat": {"id": 42},
        "from": {"id": 42},  # no username, no first_name
        "text": "hi",
    }}
    http = FakeGetUpdates([upd])
    rc, out = run([], paths, http=http)
    assert out[0]["from_user"] == "42"


def test_skips_non_message_updates(paths):
    upd = {"update_id": 1, "edited_message": {"chat": {"id": 42}}}  # no `message` key
    http = FakeGetUpdates([upd])
    rc, out = run([], paths, http=http)
    assert rc == 0 and out == []
    # Cursor still advanced.
    assert cl.read_tg_cursor(paths) == 2


def test_skips_message_missing_chat_or_id(paths):
    # An update with a `message` but missing chat — skipped.
    upd = {"update_id": 1, "message": {"message_id": 5, "from": {"id": 1}}}
    http = FakeGetUpdates([upd])
    rc, out = run([], paths, http=http)
    assert out == [] and cl.read_tg_cursor(paths) == 2


def test_reset_cursor(paths):
    cl.write_tg_cursor(paths, 0)
    http = FakeGetUpdates([make_update(100, 42, 1, "old")])
    rc, out = run(["--reset-cursor"], paths, http=http)
    assert rc == 0 and out == []
    assert cl.read_tg_cursor(paths) == 101  # advanced past last update


def test_reset_cursor_no_updates(paths):
    http = FakeGetUpdates([])
    rc, out = run(["--reset-cursor"], paths, http=http)
    assert rc == 0 and out == []
    assert cl.read_tg_cursor(paths) == 0


def test_reset_cursor_api_error(paths, capsys):
    def boom(token, offset, timeout, limit):
        raise RuntimeError("network gone")
    rc, _ = run(["--reset-cursor"], paths, http=boom)
    assert rc == 1
    err = capsys.readouterr().err
    assert "network gone" in err


def test_main_api_error(paths, capsys):
    def boom(token, offset, timeout, limit):
        raise RuntimeError("flake")
    rc, _ = run([], paths, http=boom)
    assert rc == 1
    assert "flake" in capsys.readouterr().err


def test_real_get_updates_http_error(monkeypatch):
    import urllib.error
    def fake(url, timeout=None):
        raise urllib.error.HTTPError("u", 502, "bad gw", {}, None)
    monkeypatch.setattr(tg_poll.urllib.request, "urlopen", fake)
    with pytest.raises(RuntimeError, match="HTTP 502"):
        tg_poll._real_get_updates("T", 0, 5, 20)


def test_real_get_updates_url_error(monkeypatch):
    import urllib.error
    monkeypatch.setattr(tg_poll.urllib.request, "urlopen",
                        lambda url, timeout=None: (_ for _ in ()).throw(urllib.error.URLError("no dns")))
    with pytest.raises(RuntimeError, match="URL error"):
        tg_poll._real_get_updates("T", 0, 5, 20)


def test_real_get_updates_ok_false(monkeypatch):
    class FakeResp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return json.dumps({"ok": False, "description": "no"}).encode()
    monkeypatch.setattr(tg_poll.urllib.request, "urlopen", lambda url, timeout=None: FakeResp())
    with pytest.raises(RuntimeError, match="telegram error"):
        tg_poll._real_get_updates("T", 0, 5, 20)


def test_real_get_updates_success(monkeypatch):
    class FakeResp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self):
            return json.dumps({"ok": True, "result": [{"update_id": 1}]}).encode()
    monkeypatch.setattr(tg_poll.urllib.request, "urlopen", lambda url, timeout=None: FakeResp())
    out = tg_poll._real_get_updates("T", 0, 5, 20)
    assert out == [{"update_id": 1}]
