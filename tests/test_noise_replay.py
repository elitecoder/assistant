"""Discovery shim: run the permanent June-incident regression fixture
(evals/noise/replay-865.py) as part of `python3 -m unittest discover tests`.

The fixture lives under evals/ with the other replay corpora (it IS an eval
— a recorded incident replayed against the interrupt gate), but it must
also gate every test run forever, so this module loads it by path and
re-exports its TestCase for the discoverer.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
REPLAY_PATH = REPO / "evals" / "noise" / "replay-865.py"

_name = "evals_noise_replay_865"
if _name in sys.modules:
    _mod = sys.modules[_name]
else:
    _spec = importlib.util.spec_from_file_location(_name, str(REPLAY_PATH))
    _mod = importlib.util.module_from_spec(_spec)
    sys.modules[_name] = _mod
    _spec.loader.exec_module(_mod)

Replay865 = _mod.Replay865

if __name__ == "__main__":
    import unittest
    unittest.main()
