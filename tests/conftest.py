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

# Point CMUX_BIN — which the comms daemon, the pulse, and the watchers use for
# every cmux call — at a binary that doesn't exist, so a test that misses a
# stub fails fast instead of driving the real cmux. On 2026-09-28 an unstubbed
# watchdog test spawned a live warm workspace and typed into it. Set before any
# test module imports, so import-time CMUX_BIN constants pick it up too; a test
# that needs a fake cmux still overrides it with monkeypatch.setenv.
os.environ["CMUX_BIN"] = str(_ROOT / "tests" / "no-real-cmux-in-tests")


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
@pytest.fixture(autouse=True)
def _watcher_on(request, monkeypatch):
    if "real_mode" not in request.keywords:
        from assistant import mode
        monkeypatch.setattr(mode, "current", lambda: "pro")
    monkeypatch.setenv("ASSISTANT_WATCHER", "1")


@pytest.fixture(autouse=True)
def _no_real_copilot_home(monkeypatch):
    monkeypatch.delenv("COPILOT_HOME", raising=False)


@pytest.fixture(autouse=True)
def _no_real_self_update(monkeypatch):
    guard = types.ModuleType("self_update")

    def maybe_update(repo, **_):
        pytest.fail(f"test reached the real self-update for {repo}")

    guard.maybe_update = maybe_update
    monkeypatch.setitem(sys.modules, "self_update", guard)
