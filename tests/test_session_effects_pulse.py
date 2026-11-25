"""Pulse dispatch types into a new workspace only after the intent is on disk (src/assistant/session_effects.py)."""
from __future__ import annotations

import json
import sys
from unittest import mock

import pytest

from assistant import effects, session_effects

from test_busy_session_cap import TURN, cards, fleet  # noqa: F401 - the fleet fixture
from test_pulse_topup import _seed_todo, load_pulse

MARKER = "TITLE-MARKER-do-not-record"
TRUST = "1. Yes, I trust this folder"
READY = "Claude Code v1.2.3  ⏵⏵ bypass permissions on"
WS = "workspace:1000010007"   # cmux numbers its refs from 1000010000


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
    m._dispatch_blocked = ""
    m._dispatch_started.clear()
    with mock.patch.object(m.time, "sleep", lambda *_a, **_k: None):
        yield m


def ledger(home) -> list:
    path = home / ".assistant/effects/effects.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


class Cmux:
    """cmux, faked: records every keystroke and what the ledger held at the moment of the first one."""

    def __init__(self, mod, home, screens=(READY,), refuse=(), workspace=WS, rc=None):
        self.mod, self.home, self.refuse, self.workspace, self.rc = mod, home, refuse, workspace, rc
        self.calls, self.at_first_keystroke = [], None
        self.screens = iter(screens)

    def run(self, cmd, **k):
        if cmd[1] == "new-workspace":
            return (0, self.workspace, "")
        if cmd[1] == "list-pane-surfaces":
            return (0, "surface:3", "")
        if cmd[1] == "rpc" and cmd[2].startswith("surface.send"):  # the real transport, with a status to read
            return self.type(cmd[2], json.loads(cmd[3]))
        return (0, "", "")

    def type(self, method, params):
        if self.at_first_keystroke is None:
            self.at_first_keystroke = ledger(self.home)
        self.calls.append((method, params))
        rc = self.rc if self.rc is not None else (1 if method in self.refuse else 0)
        return (rc, "" if rc == 0 else "", "boom" if rc else "")

    def screen(self, ref, lines=200):
        return next(self.screens, READY)

    def dispatch(self, td="td-1"):
        with mock.patch.object(self.mod, "run", side_effect=self.run), \
                mock.patch.object(self.mod, "_surface_read_text", side_effect=self.screen):
            return self.mod.dispatch_todo(td)


def seed(mod, **extra):
    item = {"id": "td-1", "title": MARKER, "detail": MARKER, "status": "open", "autoDispatch": True,
            "trace": "tr-0123456789abcdef", "source": "work-queue:2026-11-24", **extra}
    _seed_todo(mod, [item])


def stamped(mod) -> dict:
    return json.loads(mod.TODO_PATH.read_text())["items"][0]


def test_the_intent_is_on_disk_before_the_first_keystroke_and_names_the_task(mod, home):
    seed(mod)
    cmux = Cmux(mod, home)
    assert cmux.dispatch() is True
    first = cmux.at_first_keystroke
    assert len(first) == 1 and first[0]["kind"] == "intent"
    assert first[0]["target"] == {"source": "task", "session": WS, "task": "td-1"}
    assert first[0]["auth"]["kind"] == "owner_flag" and first[0]["auth"]["ref"] == "td-1"
    assert first[0]["trace"] == "tr-0123456789abcdef" and first[0]["component"] == "pulse"
    rows = ledger(home)
    assert [r["kind"] for r in rows] == ["intent", "result"]
    assert rows[1]["outcome"] == "unknown" and rows[1]["verified"] is False  # typed, and the transcript never showed it
    raw = (home / ".assistant/effects/effects.jsonl").read_text()
    assert MARKER not in raw and "spawn-prompts" not in raw and "prompt-dispatch" not in raw


@pytest.mark.parametrize("extra, kind, ref", [
    ({"source": "work-queue:2026-11-24"}, "owner_flag", "td-1"),         # the owner's Work queue send
    ({"source": "slack-react:C1:1.2"}, "owner_flag", "td-1"),            # their own Slack reaction, auto-start on
    ({"source": "manual:2026-11-24"}, "owner_flag", "td-1"),             # /todo
    ({"source": "goal:g1:abc", "goalId": "g1"}, "standing", "planner"),   # the goals planner's setting
    ({"source": "something-else", "goalId": "g1"}, "standing", "planner"),  # a goal task whatever its source says
    ({"source": "triage:x", "createdBy": "policy:r1"}, "standing", "policy:r1"),  # a policy rule
    ({"source": "something-else"}, "standing", "unlisted-source"),       # not listed: the more cautious claim
    ({"source": ""}, "standing", "unlisted-source"),
])
def test_what_allowed_the_start_is_recorded_from_where_the_task_came_from(mod, home, extra, kind, ref):
    seed(mod, **extra)
    cmux = Cmux(mod, home)
    assert cmux.dispatch() is True
    auth = ledger(home)[0]["auth"]
    assert (auth["kind"], auth["ref"]) == (kind, ref)


def test_a_slack_capture_not_confirmed_as_the_owners_is_never_started(mod, home):
    seed(mod, source="slack-react:C1:1.2", autoDispatch=None, reactedBy="U0OTHER")
    cmux = Cmux(mod, home)
    assert cmux.dispatch() is False
    assert cmux.calls == [] and ledger(home) == []
    assert stamped(mod)["status"] == "open"


def test_the_trust_answer_is_its_own_effect_before_the_instruction(mod, home):
    seed(mod)
    cmux = Cmux(mod, home, screens=(TRUST, READY))
    assert cmux.dispatch() is True
    intents = [r for r in ledger(home) if r["kind"] == "intent"]
    assert [r["target"]["source"] for r in intents] == ["trust", "task"]
    assert [r["outcome"] for r in ledger(home) if r["kind"] == "result"] == ["done", "unknown"]
    assert [m for m, _ in cmux.calls] == ["surface.send_text", "surface.send_key"] * 2
    assert cmux.at_first_keystroke[0]["target"]["source"] == "trust"


def test_a_typing_that_stops_halfway_is_recorded_unknown_and_still_stamps_the_task(mod, home):
    seed(mod)
    assert Cmux(mod, home, refuse=("surface.send_key",)).dispatch() is True  # the text is there, Enter wasn't pressed
    assert ledger(home)[-1]["outcome"] == "unknown"
    assert stamped(mod)["status"] == "in-progress"


def test_when_cmux_refuses_the_instruction_nothing_is_typed_and_the_dispatch_says_so_and_parks_the_task(mod, home):
    seed(mod)
    cmux = Cmux(mod, home, refuse=("surface.send_text",))
    assert cmux.dispatch() is False
    assert [m for m, _ in cmux.calls] == ["surface.send_text"]  # Enter is held back
    assert ledger(home)[-1]["outcome"] == "failed"
    assert stamped(mod)["status"] == "blocked" and stamped(mod)["parkedBy"] == "session_refused_text"
    assert mod.DISPATCH_REASON == "session_refused_text" and "td-1" in mod._dispatch_started


def test_an_error_making_the_trust_answers_keys_parks_the_task_and_the_next_task_still_goes_out(mod, home):
    _seed_todo(mod, [{"id": "td-1", "title": "a", "status": "open", "autoDispatch": True, "source": "work-queue:x"},
                     {"id": "td-2", "title": "b", "status": "open", "autoDispatch": True, "source": "work-queue:x"}])
    cmux = Cmux(mod, home, screens=(TRUST,))
    with mock.patch.object(session_effects, "Keys", side_effect=session_effects.Refused("unrecorded", "x")):
        assert cmux.dispatch("td-1") is False          # no exception escapes: the pulse loop would go on
    assert cmux.calls == [] and ledger(home)[-1]["outcome"] == "failed"
    items = {i["id"]: i for i in json.loads(mod.TODO_PATH.read_text())["items"]}
    assert items["td-1"]["status"] == "blocked" and items["td-2"]["status"] == "open"
    cmux = Cmux(mod, home, screens=(READY,), workspace="workspace:1000010008")
    assert cmux.dispatch("td-2") is True               # and the next task goes out


def test_a_tool_that_exits_zero_with_no_answer_still_gets_its_enter(mod, home):
    seed(mod)
    cmux = Cmux(mod, home)
    assert cmux.dispatch() is True
    assert [m for m, _ in cmux.calls] == ["surface.send_text", "surface.send_key"]


def test_a_timeout_may_have_landed_so_it_is_unknown_not_failed(mod, home):
    seed(mod)
    assert Cmux(mod, home, rc=124).dispatch() is True
    assert ledger(home)[-1]["outcome"] == "unknown" and ledger(home)[-1]["error"] == "timeout"


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
    assert cmux.calls == [] and mod._dispatch_blocked.startswith("unrecorded")
    assert stamped(mod)["status"] == "open" and "dispatchedAt" not in stamped(mod)


@pytest.mark.parametrize("screens, source", [((READY,), "task"), ((TRUST, READY), "trust")])
def test_when_the_intent_cant_be_written_after_the_workspace_exists_nothing_is_typed_and_the_task_is_parked(
        mod, home, screens, source):
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
    assert mod._dispatch_blocked.startswith("unrecorded") and "td-1" in mod._dispatch_started
    # parked, with the workspace named: the next pulse doesn't make another one, and "Queue to start" tries it again
    assert stamped(mod)["status"] == "blocked" and stamped(mod)["dispatchedWs"] == WS
    assert stamped(mod)["parkedBy"] == "typing_refused" and WS in stamped(mod)["statusReason"]


def test_a_session_ref_the_record_cant_describe_is_parked_and_blocks_with_its_own_reason(mod, home):
    seed(mod)
    cmux = Cmux(mod, home, workspace="workspace:" + "9" * 19)
    assert cmux.dispatch() is False
    assert cmux.calls == [] and mod._dispatch_blocked.startswith("invalid")
    assert stamped(mod)["status"] == "blocked"
    card = mod._dispatch_card([{"id": "td-2", "title": "later"}], mod._parked_tasks())
    assert card["title"] == "Dispatch paused — can't describe the new session (1 TODO(s) waiting)"
    assert card["detail"] == (
        "Assistant didn't recognise the name cmux gave the new session (it expects workspace:N), so it typed nothing "
        "into it and parked td-1. Close that workspace, press \"Queue to start\" on td-1, and update Assistant. The tasks "
        "below will park the same way until then. Waiting: td-2 later.")


def test_a_refusal_that_passes_by_itself_parks_the_task_but_raises_no_card(mod, home):
    seed(mod)
    cmux = Cmux(mod, home)
    with mock.patch.object(effects, "begin", side_effect=effects.Duplicate("again")):
        assert cmux.dispatch() is False
    assert cmux.calls == [] and mod._dispatch_blocked == ""
    assert stamped(mod)["status"] == "blocked"


def test_a_failed_park_stamp_is_logged_and_the_task_stays_open(mod, home, caplog):
    seed(mod)
    with mock.patch.object(effects, "begin", side_effect=effects.Duplicate("again")), \
            mock.patch.object(mod, "_mark_todo_dispatched", return_value=False):
        assert Cmux(mod, home).dispatch() is False
    assert "park stamp FAILED" in caplog.text


def test_keys_that_cant_be_made_for_the_instruction_close_the_intent_as_failed(mod, home):
    seed(mod)
    with mock.patch.object(session_effects, "Keys", side_effect=session_effects.Refused("invalid", "x")):
        assert Cmux(mod, home).dispatch() is False
    assert ledger(home)[-1]["outcome"] == "failed" and stamped(mod)["status"] == "blocked"


# --- the card ---------------------------------------------------------------------------------------------------

class Dispatching:
    """main() with real dispatch_todo replaced by one that behaves as the ledger decides."""

    def __init__(self, fleet, blocked):
        from test_busy_session_cap import Pulse
        self.pulse, self.blocked = Pulse(fleet), blocked
        for index, todo in enumerate(self.pulse.todos):
            todo["title"] = f"Task number {index}"

    def run(self):
        mod = self.pulse.mod

        def dispatch(todo_id):
            if self.blocked and todo_id == "td-0":
                mod._dispatch_blocked = "unrecorded: x"
                mod._dispatch_started.add(todo_id)  # the first was parked: no longer waiting
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
    calls, payload = Dispatching(fleet, blocked=True).run()
    assert calls.call_count == 1  # the rest of this pulse's dispatches are not started
    card = cards(payload)["dispatch-held-no-record"]
    assert card["title"] == "Dispatch paused — can't record what it types (2 TODO(s) waiting)"
    assert card["detail"] == (
        "Assistant can't write its record of what it types into your sessions, so it won't start tasks. Check that "
        "your disk isn't full (`df -h ~`) and that `~/.assistant/effects` can be written to "
        "(`ls -ld ~/.assistant/effects`). Waiting: td-1 Task number 1, td-2 Task number 2. Nothing is lost; the "
        "next round starts them once the record can be written.")
    calls, payload = Dispatching(fleet, blocked=False).run()
    assert calls.call_count == 2 and "dispatch-held-no-record" not in cards(payload)


def test_more_than_five_waiting_tasks_are_counted_not_all_listed(mod):
    mod._dispatch_blocked = "unrecorded: x"
    waiting = [{"id": f"td-{n}", "title": ""} for n in range(8)]
    card = mod._dispatch_card(waiting, [])
    assert "(8 TODO(s) waiting)" in card["title"] and "td-4, " not in card["detail"] and "(+3 more)" in card["detail"]


def test_the_card_never_lists_a_task_started_this_pulse(mod):
    mod._dispatch_blocked = "unrecorded: x"
    mod._dispatch_started.add("td-0")
    card = mod._dispatch_card([{"id": "td-0", "title": "a"}, {"id": "td-1", "title": "b"}], [])
    assert "td-0" not in card["detail"] and "(1 TODO(s) waiting)" in card["title"]


# --- parked tasks: named on the card, kept there, and restarted by "Queue to start" -------------------------------

PARKED = {"id": "td-1", "title": "Fix the ruler after the sign off review now please", "dispatchedWs": WS,
          "status": "blocked", "parkedBy": "typing_refused"}


def test_a_title_is_cut_at_a_word_boundary_with_an_ellipsis(mod):
    assert mod._cut("Fix the ruler after the sign off review", 20) == "Fix the ruler after…"
    assert mod._cut("short title", 60) == "short title" and mod._cut(None) == ""
    assert mod._cut("Unbroken" * 9, 10) == "UnbrokenUn…"


def test_a_card_with_nothing_blocked_still_names_each_parked_task_and_its_workspace(mod):
    mod._dispatch_blocked = ""
    card = mod._dispatch_card([], [PARKED, {**PARKED, "id": "td-2", "title": "b", "dispatchedWs": "workspace:1000010009"}])
    assert card["title"] == "Tasks parked — Assistant couldn't type into their sessions (2)"
    assert card["detail"] == (
        "Assistant started a session for each task below but couldn't type into it, so it parked the task. Close each "
        "workspace, then press \"Queue to start\" on the task. Parked: td-1 Fix the ruler after the sign off review now please "
        "(workspace:1000010007), td-2 b (workspace:1000010009).")


def test_a_card_for_a_record_that_cant_be_written_names_the_parked_task_too(mod):
    mod._dispatch_blocked = "unrecorded: x"
    card = mod._dispatch_card([{"id": "td-3", "title": "later"}], [PARKED])
    assert "Parked: td-1 Fix the ruler after the sign off review now please (workspace:1000010007). Close each workspace, " \
           "then press \"Queue to start\" on the task. Waiting: td-3 later. Nothing is lost; the next round starts them " \
           "once the record can be written." in card["detail"]


def test_parked_tasks_come_from_the_task_file_and_only_those_with_the_marker(mod):
    _seed_todo(mod, [PARKED, {"id": "td-2", "status": "blocked"}, {"id": "td-3", "status": "open", "parkedBy": "x"}])
    assert [t["id"] for t in mod._parked_tasks()] == ["td-1"]
    mod.TODO_PATH.write_text("{ not json")
    assert mod._parked_tasks() == []


def test_the_card_stays_on_every_pulse_until_the_parked_tasks_are_dealt_with(fleet):
    for _ in range(2):  # nothing blocked this pulse, and nothing waiting: the parked task alone keeps the card up
        runner = Dispatching(fleet, blocked=False)
        with mock.patch.object(runner.pulse.mod, "_parked_tasks", return_value=[PARKED]):
            _calls, payload = runner.run()
        assert cards(payload)["dispatch-held-no-record"]["title"].startswith("Tasks parked")
    runner = Dispatching(fleet, blocked=False)
    with mock.patch.object(runner.pulse.mod, "_parked_tasks", return_value=[]):
        _calls, payload = runner.run()
    assert "dispatch-held-no-record" not in cards(payload)


def test_queue_to_start_on_a_parked_task_makes_it_a_task_the_next_pulse_dispatches(mod, home, monkeypatch):
    """The dashboard's "Queue to start" (POST /dispatch-now/<id>) on a parked task: it is open again, with its old
    workspace forgotten, and the picker puts it in the bucket the pulse starts from."""
    import importlib.util
    seed(mod)
    with mock.patch.object(effects, "begin", side_effect=effects.Unrecorded("disk full")):
        assert Cmux(mod, home).dispatch() is False
    assert stamped(mod)["status"] == "blocked"
    spec = importlib.util.spec_from_file_location("todo_server_queue", mod.REPO / "bin/todo-server.py")
    server = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(server)
    monkeypatch.setattr(server, "JSON_PATH", mod.TODO_PATH)
    monkeypatch.setattr(server, "rerender", lambda: None)
    ok, _ = server.dispatch_now("td-1")
    assert ok and stamped(mod)["status"] == "open" and stamped(mod)["dispatchedWs"] is None
    spec = importlib.util.spec_from_file_location("picker_queue", mod.REPO / "bin/pick-open-todos.py")
    picker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(picker)
    monkeypatch.setattr(picker, "TODO_PATH", str(mod.TODO_PATH))
    monkeypatch.setattr(picker, "live_workspaces", lambda: set())
    import contextlib
    import io
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        picker.main()
    assert [t["id"] for t in json.loads(out.getvalue())["bucket_b"]] == ["td-1"]
    # and a task that was only ever "in progress" is not one "Queue to start" can restart: that is why a parked task
    # is blocked
    _seed_todo(mod, [{"id": "td-1", "status": "in-progress", "dispatchedAt": "x", "dispatchedWs": WS}])
    server.dispatch_now("td-1")
    assert stamped(mod)["status"] == "in-progress"


def test_a_good_dispatch_clears_an_earlier_park_marker_and_the_reason_says_what_happened(mod, home):
    seed(mod)
    with mock.patch.object(effects, "begin", side_effect=effects.Unrecorded("disk full")):
        Cmux(mod, home).dispatch()
    parked = stamped(mod)
    assert parked["parkedBy"] == "typing_refused" and parked["statusReason"] == (
        'Parked: Assistant started workspace:1000010007 but nothing could be typed into it (it couldn\'t record the '
        'typing, or cmux refused the text). Close that workspace, then press "Queue to start" on the task on the '
        "Assistant dashboard's Tasks tab.")
    assert mod._mark_todo_dispatched("td-1", "workspace:1000010009") is True  # a later, good dispatch
    assert "parkedBy" not in stamped(mod) and stamped(mod)["status"] == "in-progress"
    assert mod._parked_tasks() == []


def test_queue_to_start_clears_the_park_marker_so_a_later_block_is_not_listed_as_parked(mod, home, monkeypatch):
    import importlib.util
    seed(mod)
    with mock.patch.object(effects, "begin", side_effect=effects.Unrecorded("disk full")):
        Cmux(mod, home).dispatch()
    spec = importlib.util.spec_from_file_location("todo_server_marker", mod.REPO / "bin/todo-server.py")
    server = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(server)
    monkeypatch.setattr(server, "JSON_PATH", mod.TODO_PATH)
    monkeypatch.setattr(server, "rerender", lambda: None)
    server.dispatch_now("td-1")
    assert "parkedBy" not in stamped(mod)
    data = json.loads(mod.TODO_PATH.read_text())
    data["items"][0]["status"] = "blocked"  # the agent later blocks it for its own reason
    mod.TODO_PATH.write_text(json.dumps(data))
    assert mod._parked_tasks() == []


@pytest.fixture(autouse=True)
def pro_mode(monkeypatch):
    """Everything this file tests is Pro mode's; the test says so, instead of leaning on the suite's default."""
    from assistant import mode
    monkeypatch.setattr(mode, "current", lambda: "pro")
