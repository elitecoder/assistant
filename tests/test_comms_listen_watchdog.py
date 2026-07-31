"""Tests for the warm-session liveness watchdog in comms-listen.py.

Regression: ensure_warm_session (the spawn/respawn logic) was called ONLY on
daemon startup and per inbound Slack message. A warm workspace that died during
a quiet period (cmux restart, crash, machine sleep) stayed dead — session.json
pointing at a ref cmux no longer knew — until the next inbound message arrived,
sometimes hours. The watchdog loop closes that gap by calling
ensure_warm_session on a slow cadence.

These tests pin the watchdog's contract WITHOUT a real cmux/Slack: every
network + cmux touchpoint is monkeypatched. The mutation probes below are
load-bearing — each names the production mutation its assertion catches.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import threading
from pathlib import Path

import comms_lib as cl
import pytest


def _load():
    if "comms_listen" in sys.modules:
        return sys.modules["comms_listen"]
    spec = importlib.util.spec_from_file_location(
        "comms_listen", str(Path(__file__).resolve().parent.parent / "bin" / "comms-listen.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["comms_listen"] = mod
    spec.loader.exec_module(mod)
    return mod


listen = _load()


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    """Root comms state under tmp_path so log()/Paths.from_env never touch the
    real ~/.assistant. No real cmux or Slack: ensure_warm_session is stubbed per
    test."""
    home = tmp_path / "home"
    (home / ".assistant" / "comms").mkdir(parents=True)
    (home / ".assistant" / "config.json").write_text(json.dumps(
        {"slack": {"target": "C0", "allowed_targets": ["C0"]}}))
    monkeypatch.setenv("COMMS_HOME", str(home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("SLACK_PING_TARGET", raising=False)
    return cl.Paths.from_env()


# ─── watchdog_tick: the per-pass contract ───────────────────────────────────


def test_watchdog_tick_calls_ensure_warm_session(env, monkeypatch):
    """watchdog_tick delegates to ensure_warm_session exactly once with the
    paths it was given. Mutation probe: if the watchdog stopped calling
    ensure_warm_session (e.g. the body was stubbed out), calls would be empty."""
    calls = []

    def fake_ensure(paths):
        calls.append(paths)
        return {"ws_ref": "workspace:1"}

    monkeypatch.setattr(listen, "ensure_warm_session", fake_ensure)
    status = listen.watchdog_tick(env)
    assert calls == [env]
    assert status == "alive"


def test_watchdog_tick_no_session(env, monkeypatch):
    """ensure_warm_session returning None (cmux down, spawn failed) must surface
    as 'no-session', NOT 'alive'. Mutation probe: if watchdog_tick always
    returned 'alive' (e.g. `return 'alive'` unconditionally), this fails."""
    monkeypatch.setattr(listen, "ensure_warm_session", lambda paths: None)
    assert listen.watchdog_tick(env) == "no-session"


def test_watchdog_tick_survives_exception(env, monkeypatch):
    """A transient cmux error inside ensure_warm_session must NOT propagate — the
    watchdog thread would die and never retry. Mutation probe: removing the
    try/except (letting the exception raise) makes this test raise instead of
    returning an 'error:...' status."""
    def boom(paths):
        raise RuntimeError("cmux RPC timed out")
    monkeypatch.setattr(listen, "ensure_warm_session", boom)
    status = listen.watchdog_tick(env)
    assert status.startswith("error:")
    assert "RuntimeError" in status


def test_watchdog_tick_error_status_names_exception_type(env, monkeypatch):
    """The error status carries the exception class so the log line is
    actionable (a ValueError vs a TimeoutError mean different things). Mutation
    probe: if the handler returned a generic 'error' without the type name,
    'OSError' would be absent."""
    monkeypatch.setattr(listen, "ensure_warm_session",
                        lambda paths: (_ for _ in ()).throw(OSError("nope")))
    assert "OSError" in listen.watchdog_tick(env)


# ─── watchdog_loop: cadence + self-heal on the next tick ────────────────────


def test_watchdog_interval_default():
    """Default cadence is 60s — slow enough to be a safety net, fast enough that
    a dead warm session self-heals within a minute. Mutation probe: if the
    default were changed (e.g. dropped to 0 or raised to a huge value), this
    pins the intended value."""
    # Read the raw env-default, independent of any test's monkeypatched env.
    assert listen.WATCHDOG_INTERVAL_SEC == 60


def test_watchdog_loop_self_heals_across_ticks(env, monkeypatch):
    """The loop must keep retrying: tick 1 fails (no session), tick 2 succeeds
    (respawned). A real ensure_warm_session behaves exactly this way after a
    cmux blip. Mutation probe: if the loop ran only once (no `while not
    stop.is_set()`), the second call would never happen and calls would be 1."""
    monkeypatch.setattr(listen.time, "sleep", lambda *a, **k: None)

    seq = iter([None, {"ws_ref": "workspace:9"}])
    calls = []
    monkeypatch.setattr(listen, "ensure_warm_session",
                        lambda paths: (calls.append(1), next(seq))[1])
    # Stop the loop after two `stop.wait` returns by pre-setting the event the
    # second time. We do that by making stop.wait set the event after the 2nd
    # call — simplest: a stop Event we trip from a fake wait.
    stop = threading.Event()
    waits = {"n": 0}

    def fake_wait(timeout=None):
        waits["n"] += 1
        if waits["n"] >= 2:
            stop.set()
            return True
        return False  # keep looping
    monkeypatch.setattr(stop, "wait", fake_wait)

    listen.watchdog_loop(stop, {})
    assert len(calls) == 2, "watchdog must retry after a failed first tick"


# ─── _loop_threads: the watchdog is wired into the daemon ───────────────────


def test_loop_threads_includes_watchdog():
    """The daemon's worker set must include a 'watchdog' thread targeting
    watchdog_loop. Mutation probe: if the watchdog thread were removed from
    _loop_threads, no thread would be named 'watchdog'."""
    stop = threading.Event()
    threads = listen._loop_threads(stop, {})
    names = [t.name for t in threads]
    assert "watchdog" in names
    watchdog = next(t for t in threads if t.name == "watchdog")
    assert watchdog._target is listen.watchdog_loop
    assert watchdog.daemon is True


def test_loop_threads_count_is_six():
    """Six loops now (inbound, watchdog, ledger, inbox, proposals, heartbeat).
    Mutation probe: dropping any thread (e.g. the watchdog) makes this 5."""
    stop = threading.Event()
    assert len(listen._loop_threads(stop, {})) == 6


def test_loop_threads_all_unique_names():
    """No two loops share a name — a duplicate would mean one is shadowed and
    never runs. Mutation probe: a copy-paste name collision fails this."""
    stop = threading.Event()
    names = [t.name for t in listen._loop_threads(stop, {})]
    assert len(names) == len(set(names))
