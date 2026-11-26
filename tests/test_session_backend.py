"""assistant.session_backend picks the program sessions are kept in: cmux, or tmux in Pro mode only.

Three kinds of test:
  * the choice itself, read from the real config file (`real_mode`);
  * every script that names a cmux path: in the default mode, in Pro mode without tmux, and when the module
    that knows can't be loaded, the path is the one origin/main works out, written here as literals; with tmux
    chosen it is bin/tmux_backend.py;
  * a source gate that fails when a file starts running cmux without asking `session_backend`.
"""
from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

import cmux_sessions
import comms_lib
from assistant import mode, session_backend

REPO = Path(__file__).resolve().parent.parent
APP = "/Applications/cmux.app/Contents/Resources/bin/cmux"
FAKE = str(REPO / "tests" / "no-real-cmux-in-tests")  # what conftest points CMUX_BIN at
ON_PATH = "/found/on/path/cmux"
TMUX_CLI = str(REPO / "bin" / "tmux_backend.py")

# (script under bin/, the constant holding cmux's path, origin/main's value with CMUX_BIN set, and with it unset)
SCRIPTS = (
    ("pulse.py", "CMUX_BIN", FAKE, APP),
    ("cmux-watcher.py", "CMUX_BIN", FAKE, APP),
    ("purge-stale-awaiting.py", "CMUX_BIN", FAKE, APP),
    ("merge-pr-dispatch.py", "CMUX", FAKE, APP),
    ("cmux-send.py", "CMUX", FAKE, APP),
    ("build-ws-context.py", "CMUX_BIN", FAKE, APP),
    ("tools/workspace-peek.py", "CMUX", FAKE, APP),
    ("todo-server.py", "CMUX_BIN", ON_PATH, ON_PATH),        # these three never read CMUX_BIN
    ("workspace-watcher.py", "CMUX_BIN", ON_PATH, ON_PATH),
    ("world-scanner.py", "CMUX_BIN", ON_PATH, ON_PATH),
    ("pick-ws-batch.py", "CMUX", APP, APP),                  # these two name the app outright
    ("pick-open-todos.py", "CMUX", APP, APP),
    ("render-assistant-page.py", "CMUX_CLI", "cmux", "cmux"),  # and these three run whatever `cmux` PATH finds
    ("tools/write-receipt.py", "CMUX_CLI", "cmux", "cmux"),
    ("tools/memory_seeds.py", "CMUX_CLI", "cmux", "cmux"),
)


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".assistant").mkdir()
    return tmp_path


def configure(home: Path, **config) -> None:
    (home / ".assistant" / "config.json").write_text(json.dumps(config))


def load(filename: str):
    name = "session_backend_test_" + re.sub(r"\W", "_", filename)
    spec = importlib.util.spec_from_file_location(name, REPO / "bin" / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses look their module up here while loading
    try:
        spec.loader.exec_module(module)
    finally:
        del sys.modules[name]
    return module


# ── the choice ────────────────────────────────────────────────────────────────

@pytest.mark.real_mode
@pytest.mark.parametrize("config", [
    {},
    {"mode": "default"},
    {"mode": "default", "sessions": {"backend": "tmux"}},
    {"sessions": {"backend": "tmux"}},  # no mode saved and no Pro data: the default mode
])
def test_the_default_mode_keeps_cmux_whatever_else_the_config_says(home, monkeypatch, config):
    configure(home, **config)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("CMUX_BIN")
    assert session_backend.backend() == "cmux"
    assert session_backend.cli("/any/cmux") == "/any/cmux"


@pytest.mark.real_mode
def test_pro_mode_keeps_sessions_in_tmux_when_the_config_says_so(home):
    configure(home, mode="pro", sessions={"backend": "tmux"})
    assert session_backend.backend() == "tmux"
    assert session_backend.cli("/any/cmux") == TMUX_CLI
    configure(home, mode="pro", sessions={"backend": "cmux"})
    assert session_backend.cli("/any/cmux") == "/any/cmux"


@pytest.mark.real_mode
@pytest.mark.parametrize("platform, cmux_bin, chosen", [
    ("darwin", None, "cmux"),            # a Mac can have cmux: only the setting moves it to tmux
    ("darwin", "/a/cmux", "cmux"),
    ("linux", None, "tmux"),             # nowhere else can, so Pro mode uses tmux there
    ("linux", "/a/cmux", "cmux"),        # unless someone named a cmux to run
    ("linux", "", "tmux"),
])
def test_with_no_setting_pro_mode_uses_tmux_only_where_there_can_be_no_cmux(home, monkeypatch, platform,
                                                                         cmux_bin, chosen):
    configure(home, mode="pro")
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.delenv("CMUX_BIN")
    if cmux_bin is not None:
        monkeypatch.setenv("CMUX_BIN", cmux_bin)
    assert session_backend.backend() == chosen
    # "cmux" in the config outranks the system, as "tmux" does.
    configure(home, mode="pro", sessions={"backend": "cmux"})
    assert session_backend.backend() == "cmux"
    configure(home, mode="pro", sessions={"backend": "tmux"})
    assert session_backend.backend() == "tmux"


@pytest.mark.real_mode
@pytest.mark.parametrize("sessions", ["tmux", ["tmux"], {"backend": "screen"}, {"backend": True}, {"backend": ["tmux"]},
                                      {}, None])
def test_a_setting_that_is_not_exactly_cmux_or_tmux_is_no_setting(home, sessions):
    configure(home, mode="pro", sessions=sessions)
    assert mode.session_backend() is None


@pytest.mark.real_mode
def test_an_unreadable_config_is_cmux(home, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("CMUX_BIN")
    (home / ".assistant" / "config.json").write_text('{"mode": "pro", "sessions": {"backend": "tmux"}')
    assert mode.session_backend() is None and session_backend.backend() == "cmux"


def test_a_failure_while_asking_is_cmux_and_never_stops_the_script(home, monkeypatch):
    configure(home, sessions={"backend": "tmux"})
    assert session_backend.cli("/any/cmux") == TMUX_CLI  # conftest's Pro mode, with the setting

    def broken():
        raise RuntimeError("mode can't be read")
    monkeypatch.setattr(mode, "is_pro", broken)
    assert session_backend.cli("/any/cmux") == "/any/cmux"


# ── every script that names a cmux path ───────────────────────────────────────

@pytest.fixture
def cmux_on_path(monkeypatch):
    import shutil
    real = shutil.which
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: ON_PATH if name == "cmux" else real(name, *a, **k))


@pytest.mark.real_mode
@pytest.mark.parametrize("config", [{"mode": "default", "sessions": {"backend": "tmux"}}, {"mode": "pro"},
                                    {"mode": "pro", "sessions": {"backend": "cmux"}}],
                         ids=["default mode", "pro mode", "pro mode, cmux chosen"])
@pytest.mark.parametrize("filename, constant, with_cmux_bin, without", SCRIPTS, ids=[row[0] for row in SCRIPTS])
def test_without_tmux_each_script_runs_the_cmux_it_always_ran(home, monkeypatch, cmux_on_path, config, filename,
                                                           constant, with_cmux_bin, without):
    configure(home, **config)
    monkeypatch.setattr(sys, "platform", "darwin")
    assert getattr(load(filename), constant) == with_cmux_bin
    monkeypatch.delenv("CMUX_BIN")
    assert getattr(load(filename), constant) == without


@pytest.mark.real_mode
@pytest.mark.parametrize("filename, constant", [row[:2] for row in SCRIPTS], ids=[row[0] for row in SCRIPTS])
def test_with_tmux_chosen_each_script_runs_the_tmux_backend(home, cmux_on_path, filename, constant):
    configure(home, mode="pro", sessions={"backend": "tmux"})
    assert getattr(load(filename), constant) == TMUX_CLI


@pytest.mark.real_mode
@pytest.mark.parametrize("filename, constant, with_cmux_bin, without", SCRIPTS, ids=[row[0] for row in SCRIPTS])
def test_a_script_runs_cmux_as_ever_when_the_module_that_knows_cant_be_loaded(home, monkeypatch, cmux_on_path,
                                                                             filename, constant, with_cmux_bin,
                                                                             without):
    """Even with tmux chosen: a checkout whose session_backend.py is missing or broken must still start every
    script, on cmux."""
    configure(home, mode="pro", sessions={"backend": "tmux"})
    monkeypatch.setitem(sys.modules, "assistant.session_backend", None)  # importing it now raises ImportError
    monkeypatch.delattr("assistant.session_backend", raising=False)
    assert getattr(load(filename), constant) == with_cmux_bin
    monkeypatch.delenv("CMUX_BIN")
    assert getattr(load(filename), constant) == without


def test_the_session_reader_runs_cmux_when_the_module_that_knows_cant_be_loaded(home, monkeypatch):
    configure(home, sessions={"backend": "tmux"})
    assert cmux_sessions.cli_path() == TMUX_CLI  # conftest's Pro mode, with the setting
    monkeypatch.setitem(sys.modules, "assistant.session_backend", None)
    monkeypatch.delattr("assistant.session_backend", raising=False)
    reloaded = load("cmux_sessions.py")
    assert reloaded._session_backend is None and reloaded.in_tmux() is False
    assert reloaded.cli_path() == FAKE and reloaded.session_cli("/any/cmux") == "/any/cmux"


@pytest.mark.real_mode
def test_the_comms_daemon_and_the_session_reader_follow_the_same_choice(home, monkeypatch, cmux_on_path, tmp_path):
    configure(home, mode="default", sessions={"backend": "tmux"})
    assert str(comms_lib.Paths.from_env().cmux_bin) == FAKE
    assert str(comms_lib.Paths.from_env({"HOME": str(home)}).cmux_bin) == APP
    assert cmux_sessions.cli_path() == FAKE
    monkeypatch.delenv("CMUX_BIN")
    bundled = tmp_path / "bundled-cmux"
    bundled.write_text("#!/bin/sh\n")
    monkeypatch.setenv("CMUX_BUNDLED_CLI_PATH", str(bundled))
    assert cmux_sessions.cli_path() == ON_PATH  # not executable, so not used
    bundled.chmod(0o755)
    assert cmux_sessions.cli_path() == str(bundled)

    assert cmux_sessions.in_tmux() is False

    configure(home, mode="pro", sessions={"backend": "tmux"})
    assert str(comms_lib.Paths.from_env().cmux_bin) == TMUX_CLI
    assert cmux_sessions.cli_path() == TMUX_CLI and cmux_sessions.in_tmux() is True


def test_no_file_runs_cmux_without_asking_which_program_holds_the_sessions():
    """A new script that works out cmux's path must pass it through session_backend.cli()."""
    # cmux-only tools: the sidebar numberer and the restore command act on the cmux app itself, the hook
    # forwards to the cmux an agent runs inside, and agent_session's constant installs cmux's Copilot hooks.
    cmux_only = {"bin/cmux-ws-numberer.py", "bin/cmux-restore-sessions.py", "hooks/cmux-auto-resume.py",
                 "bin/agent_session.py", "src/assistant/session_backend.py", "bin/tmux_backend.py"}
    names_cmux = re.compile(r"cmux\.app/Contents|which\(\"cmux\"\)|\"CMUX_BIN\"|or \"cmux\"|= \"cmux\"$", re.M)
    asks = re.compile(r"session_backend\.(cli|backend)\(|session_cli\(")
    # A command line that starts with the bare word, as in ["cmux", "list-workspaces"], asks nobody.
    bare = re.compile(r"\[\s*[\"']cmux[\"']\s*,")
    unasked = []
    for folder in ("bin", "src", "hooks", "work_queue"):
        for path in sorted((REPO / folder).rglob("*.py")):
            relative = path.relative_to(REPO).as_posix()
            text = path.read_text()
            if relative in cmux_only:
                continue
            if bare.search(text) or (names_cmux.search(text) and not asks.search(text)):
                unasked.append(relative)
    assert unasked == []
