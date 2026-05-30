"""Tests for link-msg.py and lookup-thread.py."""
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


link_msg = _load("link_msg", "link-msg.py")
lookup_thread = _load("lookup_thread", "lookup-thread.py")


@pytest.fixture
def paths(tmp_path: Path) -> cl.Paths:
    home = tmp_path / "home"; home.mkdir()
    bin_dir = tmp_path / "bin"; bin_dir.mkdir()
    p = cl.Paths.from_env({
        "HOME": str(home), "COMMS_HOME": str(home),
        "COMMS_ASSISTANT_DIR": str(tmp_path / "assistant"),
        "COMMS_BIN_DIR": str(bin_dir), "CMUX_BIN": "/bin/true",
    })
    p.comms_dir.mkdir(parents=True, exist_ok=True)
    return p


def run_link(args, paths, clock=None) -> tuple[int, str]:
    real = sys.stdout
    sys.stdout = io.StringIO()
    try:
        rc = link_msg.main(args, clock=clock, paths=paths)
    finally:
        out = sys.stdout.getvalue()
        sys.stdout = real
    return rc, out


def run_lookup(args, paths) -> tuple[int, dict | list]:
    real = sys.stdout
    sys.stdout = io.StringIO()
    try:
        rc = lookup_thread.main(args, paths=paths)
    finally:
        out = sys.stdout.getvalue()
        sys.stdout = real
    return rc, json.loads(out.strip())


# --------------------------------------------------------------------------- link-msg

def test_link_msg_appends(paths):
    rc, out = run_link(["--tg-msg", "100", "--chat", "42", "--kind", "action",
                        "--ledger-key", "led-1"], paths)
    assert rc == 0 and "linked" in out
    rec = cl.lookup_thread_by_msg_id(paths, 100)
    assert rec["ledger_key"] == "led-1" and rec["kind"] == "action"


def test_link_msg_without_ledger_key(paths):
    rc, _ = run_link(["--tg-msg", "5", "--chat", "1", "--kind", "info"], paths)
    assert rc == 0
    rec = cl.lookup_thread_by_msg_id(paths, 5)
    assert rec["ledger_key"] is None


# --------------------------------------------------------------------------- lookup-thread

def test_lookup_by_msg_id_found(paths):
    cl.append_thread(paths, "led-1", 100, 42, "action")
    rc, body = run_lookup(["--tg-msg", "100"], paths)
    assert rc == 0
    assert body["thread"]["ledger_key"] == "led-1"
    assert body["ledger"] is None  # --include-ledger not set


def test_lookup_by_msg_id_with_ledger_resolves(paths):
    cl.append_thread(paths, "led-1", 100, 42, "action")
    paths.ledger.write_text(json.dumps({"key": "led-1", "kind": "cleanup", "ws_ref": "ws:1"}) + "\n")
    rc, body = run_lookup(["--tg-msg", "100", "--include-ledger"], paths)
    assert rc == 0
    assert body["thread"]["ledger_key"] == "led-1"
    assert body["ledger"]["kind"] == "cleanup"


def test_lookup_by_msg_id_with_include_ledger_no_ledger_file(paths):
    cl.append_thread(paths, "led-1", 100, 42, "action")
    rc, body = run_lookup(["--tg-msg", "100", "--include-ledger"], paths)
    assert rc == 0 and body["ledger"] is None


def test_lookup_by_msg_id_with_include_ledger_key_not_in_ledger(paths):
    cl.append_thread(paths, "led-1", 100, 42, "action")
    paths.ledger.write_text(json.dumps({"key": "OTHER"}) + "\n")
    rc, body = run_lookup(["--tg-msg", "100", "--include-ledger"], paths)
    assert rc == 0 and body["ledger"] is None


def test_lookup_by_msg_id_not_found(paths):
    rc, body = run_lookup(["--tg-msg", "999"], paths)
    assert rc == 1
    assert body == {"thread": None, "ledger": None}


def test_lookup_by_msg_id_with_thread_but_no_ledger_key(paths):
    cl.append_thread(paths, None, 100, 42, "info")
    rc, body = run_lookup(["--tg-msg", "100", "--include-ledger"], paths)
    assert rc == 0 and body["ledger"] is None


def test_lookup_by_ledger_key(paths):
    cl.append_thread(paths, "k", 100, 42, "action")
    cl.append_thread(paths, "k", 101, 99, "action")
    cl.append_thread(paths, "other", 200, 42, "action")
    rc, rows = run_lookup(["--ledger-key", "k"], paths)
    assert rc == 0 and len(rows) == 2
    assert {r["chat_id"] for r in rows} == {42, 99}


def test_lookup_by_ledger_key_empty(paths):
    rc, rows = run_lookup(["--ledger-key", "missing"], paths)
    assert rc == 0 and rows == []


def test_find_ledger_entry_by_key_missing_file(paths):
    assert lookup_thread.find_ledger_entry_by_key(paths, "x") is None


def test_find_ledger_entry_by_key_skips_malformed_and_blank(paths):
    paths.ledger.write_text(
        "not-json\n\n" + json.dumps({"key": "k", "v": 1}) + "\n"
    )
    rec = lookup_thread.find_ledger_entry_by_key(paths, "k")
    assert rec == {"key": "k", "v": 1}


def test_find_ledger_entry_by_key_dup_keeps_last(paths):
    paths.ledger.write_text(
        json.dumps({"key": "k", "v": 1}) + "\n" +
        json.dumps({"key": "k", "v": 2}) + "\n"
    )
    assert lookup_thread.find_ledger_entry_by_key(paths, "k")["v"] == 2
