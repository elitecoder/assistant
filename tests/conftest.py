"""pytest config — make the bin/ scripts and the src/ package importable from tests."""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "bin"))
sys.path.insert(0, str(_ROOT / "src"))
