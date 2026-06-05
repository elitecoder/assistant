"""Tests for conversation.py CLI."""
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


conversation = _load("conversation", "conversation.py")


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


def run(args, paths, clock=None, now=None) -> tuple[int, str]:
    real = sys.stdout
    sys.stdout = io.StringIO()
    try:
        rc = conversation.main(args, paths=paths, clock=clock, now=now)
    finally:
        out = sys.stdout.getvalue()
        sys.stdout = real
    return rc, out


def test_append_then_window_roundtrip(paths):
    rc, out = run(["append", "--chat", "42", "--msg-id", "100", "--direction", "in",
                   "--text", "was that the right PR?"], paths, clock=lambda: 1000)
    assert rc == 0 and "appended" in out
    rc, out = run(["append", "--chat", "42", "--msg-id", "101", "--direction", "out",
                   "--text", "yes, #10604", "--kind", "reply"], paths, clock=lambda: 1001)
    assert rc == 0
    rc, out = run(["window", "--chat", "42"], paths, now=lambda: 1002)
    rows = json.loads(out)
    assert [r["text"] for r in rows] == ["was that the right PR?", "yes, #10604"]
    assert rows[0]["direction"] == "in"
    assert rows[1]["kind"] == "reply"


def test_append_with_reply_to(paths):
    run(["append", "--chat", "42", "--msg-id", "5", "--direction", "in",
         "--text", "q", "--reply-to", "4008"], paths, clock=lambda: 1)
    rc, out = run(["window", "--chat", "42"], paths, now=lambda: 2)
    assert json.loads(out)[0]["reply_to"] == 4008


def test_window_empty(paths):
    rc, out = run(["window", "--chat", "42"], paths, now=lambda: 1)
    assert rc == 0 and json.loads(out) == []


def test_window_default_bounds(paths):
    # 25 turns; default max_turns=20 keeps last 20.
    for i in range(25):
        run(["append", "--chat", "7", "--msg-id", str(i), "--direction", "in",
             "--text", f"t{i}"], paths, clock=lambda i=i: 1000 + i)
    rc, out = run(["window", "--chat", "7"], paths, now=lambda: 2000)
    rows = json.loads(out)
    assert len(rows) == 20
    assert rows[0]["text"] == "t5" and rows[-1]["text"] == "t24"


def test_window_custom_bounds(paths):
    for i in range(10):
        run(["append", "--chat", "7", "--msg-id", str(i), "--direction", "in",
             "--text", f"t{i}"], paths, clock=lambda i=i: 1000 + i)
    rc, out = run(["window", "--chat", "7", "--max-turns", "3"], paths, now=lambda: 2000)
    rows = json.loads(out)
    assert [r["text"] for r in rows] == ["t7", "t8", "t9"]


def test_window_age_bound(paths):
    run(["append", "--chat", "7", "--msg-id", "1", "--direction", "in",
         "--text", "old"], paths, clock=lambda: 100)
    run(["append", "--chat", "7", "--msg-id", "2", "--direction", "in",
         "--text", "new"], paths, clock=lambda: 9000)
    rc, out = run(["window", "--chat", "7", "--max-age-sec", "1000"], paths, now=lambda: 9500)
    assert [r["text"] for r in json.loads(out)] == ["new"]


def test_append_default_direction_validation(paths):
    # argparse rejects an invalid choice with SystemExit(2).
    with pytest.raises(SystemExit):
        run(["append", "--chat", "7", "--direction", "bogus", "--text", "x"], paths)


def test_requires_subcommand(paths):
    with pytest.raises(SystemExit):
        run([], paths)
