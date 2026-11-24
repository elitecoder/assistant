"""pytest config — make the bin/ scripts and the src/ package importable from tests."""
import os
import pwd
import sys
import types
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "bin"))
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT))

import agent_session  # noqa: E402

# Point CMUX_BIN — which the comms daemon, the pulse, and the watchers use for
# every cmux call — at a binary that doesn't exist, so a test that misses a
# stub fails fast instead of driving the real cmux. On 2026-09-28 an unstubbed
# watchdog test spawned a live warm workspace and typed into it. Set before any
# test module imports, so import-time CMUX_BIN constants pick it up too; a test
# that needs a fake cmux still overrides it with monkeypatch.setenv.
os.environ["CMUX_BIN"] = str(_ROOT / "tests" / "no-real-cmux-in-tests")

# Drop the pulse knobs an operator sets for the whole login session (for
# example with `launchctl setenv`), so tests see the shipped defaults. With
# ASSISTANT_OBSERVER_HOLD_ACTIONS=1 every Pro-mode verdict became a skip, and
# OBSERVER_MODEL pointed the Observer at the triage model. Cleared before any
# test module imports, so import-time constants read the defaults too; a test
# that needs a knob still sets it with monkeypatch.setenv.
for _knob in ("ASSISTANT_OBSERVER_HOLD_ACTIONS", "OBSERVER_MODEL", "OBSERVER_AUDIT",
              "OBSERVER_EVERY_MIN"):
    os.environ.pop(_knob, None)


# Files in the real user's home that tests must never write: Copilot's hook
# folder and the rules folder Assistant links for Copilot. On 2026-11-12 a test
# wrote its fake hook list over the real ~/.copilot/hooks/cmux.json.
_REAL_HOME = Path(pwd.getpwuid(os.getuid()).pw_dir)
_GUARDED = (_REAL_HOME / ".copilot" / "hooks", _REAL_HOME / ".assistant" / "copilot-instructions")


def _snapshot(roots) -> dict:
    out = {}
    for root in roots:
        for path in [root, *sorted(root.rglob("*"))] if root.exists() else []:
            stat = path.lstat()
            out[str(path)] = (stat.st_mtime_ns, stat.st_size)
    return out


@pytest.fixture(autouse=True, scope="session")
def _real_home_untouched():
    before = _snapshot(_GUARDED)
    yield
    changed = sorted(set(before.items()) ^ set(_snapshot(_GUARDED).items()))
    assert not changed, f"a test wrote the real home: {changed[:5]}"


# The pulse imports `self_update` lazily and swallows its exceptions, so a test
# that runs the pulse's main() without a stub would quietly pull this checkout
# and run install.sh --apply. On 2026-10-18 that reloaded live daemons from a
# scratch clone. pytest.fail is a BaseException, so the pulse can't swallow it.
# Tests that need a fake self_update still replace this one in sys.modules.
# Fresh installs start in default mode with the watcher off. Tests written for
# Pro mode behavior run with both on; tests marked `real_mode` read the real
# setting, and tests of the defaults clear ASSISTANT_WATCHER themselves.
# The same goes for the jobs that are off until someone turns them on (decisions,
# the brief, the planner, the lesson extractor's ledger pass): tests of Pro
# behavior run with them on (the three jobs by patching `mode.job_settings`, the
# ledger pass by variable), and `real_mode` tests read the real settings. Tests
# of the defaults restore `job_settings` and delete the variable themselves.
@pytest.fixture(autouse=True)
def _watcher_on(request, monkeypatch):
    if "real_mode" not in request.keywords:
        from assistant import mode
        monkeypatch.setattr(mode, "current", lambda: "pro")
        monkeypatch.setattr(mode, "job_settings", lambda: dict.fromkeys(mode.JOBS, True))
        monkeypatch.setenv("ASSISTANT_LESSON_LEDGER_PASS", "1")
    monkeypatch.setenv("ASSISTANT_WATCHER", "1")


@pytest.fixture(autouse=True)
def _no_real_copilot_home(monkeypatch):
    monkeypatch.delenv("COPILOT_HOME", raising=False)
    for var in ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN", "COPILOT_PROVIDER_BASE_URL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(agent_session, "gh_signed_in", lambda home=None, env=None: False)


@pytest.fixture(autouse=True)
def _no_real_self_update(monkeypatch):
    guard = types.ModuleType("self_update")

    def maybe_update(repo, **_):
        pytest.fail(f"test reached the real self-update for {repo}")

    guard.maybe_update = maybe_update
    monkeypatch.setitem(sys.modules, "self_update", guard)


# Failure counts go to ~/.assistant/failures.json. Point them at a scratch file
# for every test, so a test that fails a model call or a render can't write the
# real home; the failwatch tests set their own path.
@pytest.fixture(autouse=True)
def _no_real_failures_file(monkeypatch, tmp_path_factory):
    monkeypatch.setenv("ASSISTANT_FAILURES_FILE",
                       str(tmp_path_factory.mktemp("failwatch") / "failures.json"))


@pytest.fixture(autouse=True)
def _restore_crash_hooks(monkeypatch):
    """A service's main() installs the redacting crash hooks; don't let them outlive the test."""
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)
    import threading
    monkeypatch.setattr(threading, "excepthook", threading.excepthook)
