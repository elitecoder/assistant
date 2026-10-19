"""pytest config — make the bin/ scripts and the src/ package importable from tests."""
import os
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


# The pulse imports `self_update` lazily and swallows its exceptions, so a test
# that runs the pulse's main() without a stub would quietly pull this checkout
# and run install.sh --apply. On 2026-10-18 that reloaded live daemons from a
# scratch clone. pytest.fail is a BaseException, so the pulse can't swallow it.
# Tests that need a fake self_update still replace this one in sys.modules.
@pytest.fixture(autouse=True)
def _no_real_self_update(monkeypatch):
    guard = types.ModuleType("self_update")

    def maybe_update(repo, **_):
        pytest.fail(f"test reached the real self-update for {repo}")

    guard.maybe_update = maybe_update
    monkeypatch.setitem(sys.modules, "self_update", guard)
