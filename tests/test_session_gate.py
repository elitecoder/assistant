"""The default mode doesn't depend on the ledger's modules loading, and Pro mode says so when they don't.

`assistant.session_gate` is what the services that type load (inside a `try`): it asks the mode, types as it always did
in the default mode, and loads `session_effects` (then `effects` and `pointers`) only in Pro mode. This file:
  - runs every test of `tests/test_default_mode_is_main.py` again with `session_effects` and `effects` unimportable, and
    again with `assistant.mode` unimportable (a stand-in that raises on import; no real file is edited), so each of the
    five services (the pulse, comms-listen, todo-server, cmux-send.py and restore) still starts and does what origin/main
    does. The pulse (like the doctor and the dashboard's renderer, which aren't services that type) loads `assistant.mode` at
    its start on origin/main too, so they are left out of the second run;
  - says in Pro mode what each service does when `session_effects` can't load: it types nothing, loudly.
"""
from __future__ import annotations

import json
import sys

import pytest
from test_default_mode_is_main import *  # noqa: F401,F403 - the tests themselves, run again below
import test_default_mode_is_main as dm

from assistant import mode, session_gate


@pytest.fixture(autouse=True, params=["no-ledger", "no-mode"])
def broken(request, monkeypatch):
    """Make the named modules unimportable: gone from the package, and None in sys.modules, which raises on import."""
    import assistant
    names = ("session_effects", "effects") if request.param == "no-ledger" else ("mode",)
    if request.param == "no-mode" and any(word in request.node.name for word in ("doctor", "tasks_tab", "parked_task_row", "blocked_or_parked")):
        pytest.skip("the doctor and the dashboard's renderer load assistant.mode on origin/main too")
    monkeypatch.setattr(session_gate.effects, "_module", None)
    monkeypatch.setattr(session_gate.effects, "_said", False)
    for name in names:
        monkeypatch.setitem(sys.modules, f"assistant.{name}", None)
        monkeypatch.delattr(assistant, name, raising=False)
    if request.param == "no-mode":
        load = dm.load

        def without_pulse(name, filename):
            if filename in ("pulse.py", "assistant-doctor.py", "render-assistant-page.py"):
                pytest.skip(f"{filename} loads assistant.mode at start on origin/main too")
            return load(name, filename)

        monkeypatch.setattr(dm, "load", without_pulse)
    return request.param


# --- the gate itself ------------------------------------------------------------------------------------------------

@pytest.fixture
def pro(monkeypatch):
    monkeypatch.setattr(mode, "current", lambda: "pro")


def test_the_keys_of_the_default_mode_type_the_text_then_enter_whatever_cmux_says(default_mode):
    calls = []

    def rpc(method, params):
        calls.append((method, params))
        return None  # refused

    keys = session_gate.Keys(rpc, "s")
    assert keys.line("hi") is False
    assert calls == [("surface.send_text", {"surface_id": "s", "text": "hi"}),
                     ("surface.send_key", {"surface_id": "s", "key": "enter"})]
    assert keys.outcome() == "done" and keys.finish() is None and keys.confirmed is None


def test_a_time_out_from_the_transport_is_a_refusal_in_the_default_mode(default_mode):
    def rpc(method, params):
        raise TimeoutError(method)

    keys = session_gate.Keys(rpc, "s")
    assert keys.text("x") is False and keys.line("x", enter="cr") is False


def test_a_line_can_end_with_a_carriage_return_or_with_nothing_and_can_wait_first(default_mode):
    calls, waits = [], []
    keys = session_gate.Keys(lambda method, params: calls.append(params) or {}, "s")
    keys.line("a", enter="", )
    keys.line("b", enter="cr", pause=1, sleep=waits.append)
    assert calls == [{"surface_id": "s", "text": "a"}, {"surface_id": "s", "text": "b"},
                     {"surface_id": "s", "text": "\r"}] and waits == [1]


def test_run_now_legacy_sends_the_text_then_enter_to_the_workspace(default_mode):
    from unittest import mock
    with mock.patch.object(session_gate.subprocess, "run") as ran:
        session_gate.run_now_legacy("/cmux", "workspace:1", "go")
    kw = {"check": False, "timeout": 4, "capture_output": True}
    assert [c.args[0] for c in ran.call_args_list] == [["/cmux", "send", "--workspace", "workspace:1", "go"],
                                                        ["/cmux", "send-key", "--workspace", "workspace:1", "Enter"]]
    assert all(c.kwargs == kw for c in ran.call_args_list)


def test_a_mode_that_cant_be_read_is_the_default_mode_for_the_gate(monkeypatch):
    monkeypatch.setattr(mode, "is_pro", lambda: (_ for _ in ()).throw(OSError("unreadable")))
    assert session_gate.active() is False


def test_in_the_default_mode_the_stand_in_loads_nothing_and_names_a_refusal_nothing_raises(default_mode):
    assert session_gate.effects.active() is False
    assert issubclass(session_gate.effects.Refused, Exception)
    assert session_gate.effects._module is None
    assert session_gate.effects.Refused is session_gate._NothingRaised


def test_a_stand_in_attribute_that_starts_with_underscores_isnt_loaded(default_mode):
    with pytest.raises(AttributeError):
        session_gate.effects.__wrapped__


# --- Pro mode with session_effects that can't load ------------------------------------------------------------------

def ledger_is_unloadable(request):
    if request.node.callspec.params["broken"] != "no-ledger":
        pytest.skip("this one needs session_effects unimportable")


def test_pro_mode_without_the_ledger_refuses_everything_that_would_type_and_says_so(request, pro, caplog):
    ledger_is_unloadable(request)
    fx = session_gate.effects
    assert fx.active() is True and fx.writable() is False and fx.trace_of({}) == ""
    assert fx.finish("x", "done") is None and "can't be loaded" in caplog.text
    assert fx.Refused is session_gate.Unavailable
    for call in (lambda: fx.begin("w", "s", kind="tap", ref="r", component="c"), lambda: fx.Keys("x", None, "s"),
                 lambda: fx.type_line("x", None, "s", "t")):
        with pytest.raises(fx.Refused) as refusal:
            call()
        assert refusal.value.code == "unrecorded" and "typed nothing" in str(refusal.value)
    assert fx.UNRECORDED.startswith("Assistant couldn't record")


def test_the_pulse_in_pro_without_the_ledger_starts_no_task_and_shows_the_card(request, pro, default_mode):
    ledger_is_unloadable(request)
    run = dm.Dispatch(default_mode)
    assert run.go() is False
    assert run.calls == [] and run.mod._dispatch_blocked.startswith("unrecorded")
    assert run.task()["status"] == "open"


def test_cmux_send_in_pro_without_the_ledger_exits_4_and_types_nothing(request, pro, default_mode, capsys):
    ledger_is_unloadable(request)
    code, out, err = dm.run_script("cmux-send.py", ["--ws", dm.WS, "--text", "x", "--no-log"])
    record = json.loads(out)
    assert code == 4 and record["outcome"] == "refused" and record["refused"]["code"] == "unrecorded"
    assert "so nothing was typed" in err


def test_restore_in_pro_without_the_ledger_types_nothing(request, pro, default_mode):
    ledger_is_unloadable(request)
    restore = dm.restore_module()
    calls = []
    restore.run_cmux = lambda *a, **k: calls.append(a) or (0, "{}", "")
    out = dm.io.StringIO()
    with dm.contextlib.redirect_stdout(out):
        assert restore.respawn_panel({"title": "t"}, dm.PANEL, "cd /x", False, False) is False
    assert calls == [] and "NOT TYPED: the record of what is typed can't be loaded" in out.getvalue()


def test_the_slack_service_in_pro_without_the_ledger_types_nothing_and_keeps_the_message(
        request, pro, listen, default_mode, monkeypatch):
    ledger_is_unloadable(request)
    session = dm.Session(listen, monkeypatch)
    from assistant import session_gate as gate
    with pytest.raises(gate.Unavailable):
        with listen.comms_session.typing_scope(listen.comms_lib.Paths.from_env(), dm.WS, "surface:3", "clear",
                                               kind="standing", ref="slack-comms"):
            pytest.fail("a scope that can't be recorded must not open")
    assert session.calls == []


def test_the_dashboard_server_in_pro_without_the_ledger_hands_run_now_to_the_send_script(
        request, pro, env, default_mode):
    ledger_is_unloadable(request)
    from unittest import mock
    from test_todo_server import _seed_proposal
    _seed_proposal(env, {"id": "prop-1", "execute_via": "send-text-to-session", "action": "go",
                         "touches": [{"type": "session", "ref": dm.WS}]})
    server = dm._server()
    refused = mock.Mock(returncode=4, stdout="{}", stderr="cmux-send: nothing was typed\n")
    with mock.patch.object(server.subprocess, "run", return_value=refused) as ran:
        ok, message = server.proposal_action("prop-1", "run-now", {})
    assert ok is False and "--source" in ran.call_args.args[0] and "nothing was typed" in message
