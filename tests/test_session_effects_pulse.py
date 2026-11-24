"""Pulse dispatch types into a new workspace only after the intent is on disk (src/assistant/session_effects.py)."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest import mock

import pytest

from assistant import effects, session_effects

from test_busy_session_cap import TURN, cards, fleet  # noqa: F401 - the fleet fixture
from test_pulse_topup import _seed_todo, load_pulse

MARKER = "TITLE-MARKER-do-not-record"
TRUST = "1. Yes, I trust this folder"
READY = "Claude Code v1.2.3  ⏵⏵ bypass permissions on"


@pytest.fixture()
def home(tmp_path):
    (tmp_path / ".assistant/inbox").mkdir(parents=True)
    (tmp_path / ".assistant/observer-summaries").mkdir(parents=True)
    (tmp_path / ".claude/cache").mkdir(parents=True)
    effects._forget()
    return tmp_path


@pytest.fixture()
def mod(home):
    m = load_pulse(home)
    m.SPAWN_PROMPT_DIR = home / "spawn-prompts"
    m.DISPATCH_CLASSIFICATION_PROMPT = home / "missing-rules.md"
    m.DISPATCH_CWD = home / "dev"
    m.DISPATCH_CWD.mkdir(parents=True, exist_ok=True)
    with mock.patch.object(m.time, "sleep", lambda *_a, **_k: None):
        yield m


def ledger(home) -> list:
    path = home / ".assistant/effects/effects.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


class Cmux:
    """cmux, faked: records every keystroke and what the ledger held at the moment of the first one."""

    def __init__(self, mod, home, screens=(READY,), refuse=()):
        self.mod, self.home, self.refuse = mod, home, refuse
        self.calls, self.at_first_keystroke = [], None
        self.screens = iter(screens)

    def run(self, cmd, **k):
        if cmd[1] == "new-workspace":
            return (0, "workspace:7", "")
        if cmd[1] == "list-pane-surfaces":
            return (0, "surface:3", "")
        return (0, "", "")

    def rpc(self, method, params, timeout=15):
        if self.at_first_keystroke is None:
            self.at_first_keystroke = ledger(self.home)
        self.calls.append((method, params))
        return None if method in self.refuse else {}

    def screen(self, ref, lines=200):
        return next(self.screens, READY)

    def dispatch(self, td="td-1"):
        with mock.patch.object(self.mod, "run", side_effect=self.run), \
                mock.patch.object(self.mod, "_cmux_rpc", side_effect=self.rpc), \
                mock.patch.object(self.mod, "_surface_read_text", side_effect=self.screen):
            return self.mod.dispatch_todo(td)


def seed(mod, **extra):
    _seed_todo(mod, [{"id": "td-1", "title": MARKER, "detail": MARKER, "status": "open", "autoDispatch": True,
                      "trace": "tr-0123456789abcdef", **extra}])


def test_the_intent_is_on_disk_before_the_first_keystroke_and_names_the_task(mod, home):
    seed(mod)
    cmux = Cmux(mod, home)
    assert cmux.dispatch() is True
    first = cmux.at_first_keystroke
    assert len(first) == 1 and first[0]["kind"] == "intent"
    assert first[0]["target"] == {"source": "task", "session": "workspace:7", "task": "td-1"}
    assert first[0]["auth"]["kind"] == "owner_flag" and first[0]["auth"]["ref"] == "td-1"
    assert first[0]["trace"] == "tr-0123456789abcdef" and first[0]["component"] == "pulse"
    rows = ledger(home)
    assert [r["kind"] for r in rows] == ["intent", "result"]
    assert rows[1]["outcome"] == "done" and rows[1]["verified"] is False  # no transcript appeared in the fake
    raw = (home / ".assistant/effects/effects.jsonl").read_text()
    assert MARKER not in raw and "spawn-prompts" not in raw and "prompt-dispatch" not in raw


def test_the_trust_answer_is_its_own_effect_before_the_instruction(mod, home):
    seed(mod)
    cmux = Cmux(mod, home, screens=(TRUST, READY))
    assert cmux.dispatch() is True
    intents = [r for r in ledger(home) if r["kind"] == "intent"]
    assert [r["target"]["source"] for r in intents] == ["trust", "task"]
    assert [r["outcome"] for r in ledger(home) if r["kind"] == "result"] == ["done", "done"]
    assert [m for m, _ in cmux.calls] == ["surface.send_text", "surface.send_key"] * 2
    assert cmux.at_first_keystroke[0]["target"]["source"] == "trust"


@pytest.mark.parametrize("refuse, outcome", [
    (("surface.send_text",), "failed"),   # nothing was typed
    (("surface.send_key",), "unknown"),   # the text is there, Enter wasn't pressed
])
def test_a_typing_that_fails_or_stops_halfway_is_recorded_as_such_and_still_stamps_the_task(mod, home, refuse, outcome):
    seed(mod)
    assert Cmux(mod, home, refuse=refuse).dispatch() is True
    assert ledger(home)[-1]["outcome"] == outcome
    assert json.loads(mod.TODO_PATH.read_text())["items"][0]["status"] == "in-progress"


def test_a_failed_stamp_still_closes_the_typing(mod, home):
    seed(mod)
    with mock.patch.object(mod, "_mark_todo_dispatched", return_value=False):
        assert Cmux(mod, home).dispatch() is False
    assert ledger(home)[-1]["outcome"] == "done"


def test_with_the_ledger_unwritable_nothing_is_started_or_typed_and_the_task_stays_open(mod, home):
    seed(mod)
    cmux = Cmux(mod, home)
    with mock.patch.object(session_effects, "writable", return_value=False):
        assert cmux.dispatch() is False
    assert cmux.calls == [] and mod._dispatch_unrecorded is True
    assert json.loads(mod.TODO_PATH.read_text())["items"][0]["status"] == "open"
    assert "dispatchedAt" not in json.loads(mod.TODO_PATH.read_text())["items"][0]


@pytest.mark.parametrize("screens, source", [((READY,), "task"), ((TRUST, READY), "trust")])
def test_when_the_intent_cant_be_written_after_the_workspace_exists_nothing_is_typed(mod, home, screens, source):
    seed(mod)
    cmux = Cmux(mod, home, screens=screens)
    real = effects.begin

    def begin(cls, target, *a, **k):
        if target.get("source") == source:
            raise effects.Unrecorded("disk full")
        return real(cls, target, *a, **k)

    with mock.patch.object(effects, "begin", side_effect=begin):
        assert cmux.dispatch() is False
    assert not any(m == "surface.send_text" and p["text"].startswith("Read ") for m, p in cmux.calls)
    assert source != "trust" or cmux.calls == []
    assert mod._dispatch_unrecorded is True
    assert json.loads(mod.TODO_PATH.read_text())["items"][0]["status"] == "open"


def test_a_refusal_other_than_not_recording_does_not_raise_the_card(mod, home):
    seed(mod)
    cmux = Cmux(mod, home)
    mod._dispatch_unrecorded = False
    with mock.patch.object(effects, "begin", side_effect=effects.Duplicate("again")):
        assert cmux.dispatch() is False
    assert cmux.calls == [] and mod._dispatch_unrecorded is False


# --- the card ---------------------------------------------------------------------------------------------------

class Dispatching:
    """main() with real dispatch_todo replaced by one that behaves as the ledger decides."""

    def __init__(self, fleet, recordable):
        from test_busy_session_cap import Pulse
        self.pulse, self.recordable = Pulse(fleet), recordable

    def run(self):
        mod = self.pulse.mod

        def dispatch(todo_id):
            if not self.recordable:
                mod._dispatch_unrecorded = True
                return False
            return True

        batch = {"to_reclassify": [], "reuse_cached": [], "backed_off": [], "total_ws": 0}
        with mock.patch.object(mod, "pick_ws_batch", return_value=batch), \
                mock.patch.object(mod, "purge_stale_awaiting"), \
                mock.patch.object(mod, "pick_open_todos", return_value={"bucket_b": list(self.pulse.todos)}), \
                mock.patch.object(mod, "dispatch_todo", side_effect=dispatch) as calls, \
                mock.patch.object(mod, "run", return_value=(0, "", "")) as run, \
                mock.patch.object(sys, "argv", ["pulse.py"]):
            assert mod.main() == 0
        saved = [c for c in run.call_args_list if c.args and "state-write.py" in str(c.args[0])]
        return calls, json.loads(saved[-1].kwargs["input_text"])


def test_a_card_says_dispatch_is_paused_when_it_cant_be_recorded_and_it_goes_when_it_can(fleet):
    calls, payload = Dispatching(fleet, recordable=False).run()
    assert calls.call_count == 1  # the rest of this pulse's dispatches are not started
    card = cards(payload)["dispatch-held-no-record"]
    assert card["title"] == "Dispatch paused — 3 TODO(s) waiting"
    assert card["detail"] == (
        "Assistant can't record what it types into your sessions, so it started nothing. Check that your disk "
        "isn't full and that the folder ~/.assistant/effects can be written to. Holding: td-0, td-1, td-2. "
        "Nothing is lost. Assistant looks again every few minutes and starts them once it can record.")
    calls, payload = Dispatching(fleet, recordable=True).run()
    assert calls.call_count == 2 and "dispatch-held-no-record" not in cards(payload)
