"""The dashboard's "run now" on a proposal types only a short line into a real session, and records it first."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

from assistant import effects

from test_todo_server import _seed_proposal, env, mod  # noqa: F401 - fixtures

REPO = Path(__file__).resolve().parent.parent
MARKER = "RUN-NOW-TEXT-do-not-record"


def seed(env, action=MARKER, ref="workspace:1000010007"):
    return _seed_proposal(env, {"id": "prop-1", "execute_via": "send-text-to-session", "action": action,
                                "touches": [{"type": "session", "ref": ref}]})


@pytest.mark.parametrize("action, ref", [
    (MARKER, "not-a-session"), (MARKER, "workspace:7; rm -rf"), (MARKER, "surface:3"),
    ("one\ntwo", "workspace:1000010007"), ("one\rtwo", "workspace:1000010007"), ("x" * 501, "workspace:1000010007"), ("", "workspace:1000010007"),
    (["a"], "workspace:1000010007"), (MARKER, "workspace:" + "1" * 19),
    # control characters: Ctrl-C, back-tab, Ctrl-U, the end of a bracketed paste, a line separator
    ("a\x03b", "workspace:1000010007"), ("a\x1b[Zb", "workspace:1000010007"), ("a\x15b", "workspace:1000010007"),
    ("a\x1b[201~b", "workspace:1000010007"), ("a\u2028b", "workspace:1000010007"), ("a\u2029b", "workspace:1000010007"),
    ("a\u202eb", "workspace:1000010007"),
])
def test_a_bad_session_or_text_is_refused_with_nothing_typed_and_the_proposal_untouched(env, monkeypatch, action, ref):
    path = seed(env, action, ref)
    before = path.read_text()
    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: pytest.fail("must not type"))
    ok, why = mod.proposal_action("prop-1", "run-now", {})
    assert ok is False and "nothing was typed" in why and path.read_text() == before


@pytest.mark.parametrize("digits", [1, 10, 18])
def test_a_session_ref_of_one_to_eighteen_digits_is_typed_into(env, monkeypatch, digits):
    seed(env, ref="workspace:" + "1" * digits)
    seen = []
    monkeypatch.setattr(mod.subprocess, "run", lambda argv, **k: seen.append(argv) or mock.Mock(returncode=0))
    assert mod.proposal_action("prop-1", "run-now", {})[0] is True and len(seen) == 1


def test_five_hundred_characters_on_one_line_is_allowed(env, monkeypatch):
    seed(env, "x" * 500)
    seen = []
    monkeypatch.setattr(mod.subprocess, "run", lambda argv, **k: seen.append(argv) or mock.Mock(returncode=0))
    assert mod.proposal_action("prop-1", "run-now", {})[0] is True and len(seen) == 1


def test_run_now_is_recorded_as_a_tap_on_the_route_and_types_through_the_one_script(env, tmp_path, monkeypatch):
    """cmux-send.py is run for real here, with the cmux calls it makes replaced: the ledger row is what's checked."""
    path = seed(env)
    send = REPO / "bin/cmux-send.py"
    wrapper = tmp_path / "wrap.py"
    wrapper.write_text(
        "import sys\n"
        "log = sys.argv[-1]\n"
        "sys.argv = ['cmux-send.py'] + sys.argv[1:-1]\n"
        "import importlib.util as u\n"
        f"spec = u.spec_from_file_location('s', {str(send)!r}); s = u.module_from_spec(spec); spec.loader.exec_module(s)\n"
        "s.resolve_terminal_surface = lambda ws: ('S-1', 'surface:1', 'tty', 't')\n"
        "s.rpc = lambda method, payload: (0, {})\n"
        "s.time.sleep = lambda x: None\n"
        "s.SENDS_LOG = log\n"
        "s.main()\n")
    real = subprocess.run
    home = tmp_path / "ledger-home"
    home.mkdir()

    def run(argv, **kw):
        return real([sys.executable, str(wrapper), *argv[2:], str(tmp_path / "sends.jsonl")],
                    **{**kw, "env": {"HOME": str(home), "PATH": "/usr/bin"}})

    monkeypatch.setattr(mod.subprocess, "run", run)
    ok, why = mod.proposal_action("prop-1", "run-now", {})
    assert ok is True, why
    assert json.loads(path.read_text())["status"] == "working"
    rows = [json.loads(line) for line in (home / ".assistant/effects/effects.jsonl").read_text().splitlines()]
    assert [r["kind"] for r in rows] == ["intent", "result"] and rows[1]["outcome"] == "done"
    assert rows[0]["target"] == {"source": "run-now", "session": "workspace:1000010007"}
    assert rows[0]["auth"]["kind"] == "tap" and rows[0]["auth"]["ref"] == "/proposal/run-now"
    assert rows[0]["component"] == "todo-server"
    assert MARKER not in (home / ".assistant/effects/effects.jsonl").read_text()


def test_when_the_script_refuses_the_proposal_is_not_marked_working_and_the_reason_is_shown(env, monkeypatch):
    path = seed(env)
    before = path.read_text()
    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: mock.Mock(
        returncode=4, stderr="cmux-send: Assistant couldn't record what it types, so it typed nothing\n"))
    ok, why = mod.proposal_action("prop-1", "run-now", {})
    assert ok is False and "couldn't record what it types" in why and path.read_text() == before


@pytest.mark.parametrize("record, said", [
    ({"outcome": "rpc_send_key_failed"}, "run-now typed the text into the session but couldn't press Enter, so it is waiting there unsent"),
    ({"outcome": "rpc_send_text_failed"}, "run-now couldn't type into the session"),
    ({"outcome": "no_terminal_surface"}, "run-now couldn't find a terminal in that session, so nothing was typed"),
])
def test_the_reply_says_what_actually_happened(env, monkeypatch, record, said):
    seed(env)
    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: mock.Mock(returncode=2, stdout=json.dumps(record), stderr=""))
    ok, why = mod.proposal_action("prop-1", "run-now", {})
    assert ok is False and why == said


def test_a_reply_from_a_script_that_printed_nothing_useful_still_says_something(env, monkeypatch):
    seed(env)
    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: mock.Mock(returncode=2, stdout="not json", stderr=""))
    assert mod.proposal_action("prop-1", "run-now", {})[1].startswith("run-now didn't type")


def test_a_refusal_with_no_message_still_says_something(env, monkeypatch):
    seed(env)
    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: mock.Mock(returncode=2, stderr=""))
    ok, why = mod.proposal_action("prop-1", "run-now", {})
    assert ok is False and why.startswith("run-now didn't type")
