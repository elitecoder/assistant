"""Tests for the small comms CLIs: conversation.py, link-msg.py, lookup-thread.py."""
from __future__ import annotations

import importlib.util
import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path

import comms_lib as cl
import pytest


def _load(name: str, filename: str):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        name, str(Path(__file__).resolve().parent.parent / "bin" / filename))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


conversation = _load("conversation_cli", "conversation.py")
link_msg = _load("link_msg_cli", "link-msg.py")
lookup_thread = _load("lookup_thread_cli", "lookup-thread.py")


@pytest.fixture
def paths(tmp_path: Path) -> cl.Paths:
    home = tmp_path / "home"
    (home / ".assistant").mkdir(parents=True)
    return cl.Paths.from_env({"HOME": str(home), "COMMS_HOME": str(home)})


def _cap(fn, *args, **kwargs):
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = fn(*args, **kwargs)
    return rc, buf.getvalue().strip()


# ─── conversation.py ────────────────────────────────────────────────────────

def test_conversation_append_and_window(paths: cl.Paths):
    rc, _ = _cap(conversation.main,
                 ["append", "--channel", "D42", "--msg-ts", "1.1",
                  "--direction", "in", "--text", "hi"], paths=paths, clock=lambda: 1000)
    assert rc == 0
    _cap(conversation.main,
         ["append", "--channel", "D42", "--msg-ts", "1.2", "--direction", "out",
          "--text", "hey", "--kind", "reply", "--reply-to", "1.1"],
         paths=paths, clock=lambda: 1001)
    rc, out = _cap(conversation.main, ["window", "--channel", "D42"],
                   paths=paths, now=lambda: 1001)
    rows = json.loads(out)
    assert [r["text"] for r in rows] == ["hi", "hey"]


# ─── link-msg.py + lookup-thread.py ─────────────────────────────────────────

def test_link_then_lookup_by_ts(paths: cl.Paths):
    rc, _ = _cap(link_msg.main,
                 ["--msg-ts", "1700.3", "--channel", "D42", "--kind", "action",
                  "--ledger-key", "assistant:close:ws:9"], paths=paths, clock=lambda: 1)
    assert rc == 0
    rc, out = _cap(lookup_thread.main, ["--msg-ts", "1700.3"], paths=paths)
    assert rc == 0
    rec = json.loads(out)
    assert rec["thread"]["ledger_key"] == "assistant:close:ws:9"
    assert rec["ledger"] is None


def test_lookup_by_ts_missing_returns_1(paths: cl.Paths):
    rc, out = _cap(lookup_thread.main, ["--msg-ts", "nope"], paths=paths)
    assert rc == 1 and json.loads(out)["thread"] is None


def test_lookup_by_ledger_key_includes_resolved_ledger(paths: cl.Paths):
    # write a ledger entry and a thread linking to it
    paths.ledger.write_text(json.dumps({"key": "assistant:close:ws:9", "kind": "cleanup"}) + "\n")
    _cap(link_msg.main, ["--msg-ts", "1700.4", "--channel", "D42", "--kind", "action",
                         "--ledger-key", "assistant:close:ws:9"], paths=paths, clock=lambda: 1)
    rc, out = _cap(lookup_thread.main,
                   ["--msg-ts", "1700.4", "--include-ledger"], paths=paths)
    rec = json.loads(out)
    assert rec["ledger"]["kind"] == "cleanup"

    rc2, out2 = _cap(lookup_thread.main, ["--ledger-key", "assistant:close:ws:9"], paths=paths)
    assert rc2 == 0 and len(json.loads(out2)) == 1
