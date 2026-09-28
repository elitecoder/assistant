"""pytest config — make the bin/ scripts and the src/ package importable from tests."""
import os
import sys
from pathlib import Path

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
