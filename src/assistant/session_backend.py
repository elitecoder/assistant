"""The program Assistant keeps its agent sessions in: cmux, or tmux in Pro mode.

Every script that runs cmux works out cmux's path as it always did and passes it through `cli()`. In the
default mode `cli()` hands that path straight back, so nothing about cmux changes there. In Pro mode it
hands back bin/tmux_backend.py, which answers the same commands from tmux, when sessions are kept in tmux:

  - {"sessions": {"backend": "tmux"}} in ~/.assistant/config.json says so ("cmux" says the opposite);
  - with no setting, tmux is used only where there can be no cmux: on a system other than macOS, and then
    only when CMUX_BIN doesn't name a cmux to run.

A script asks once, when it starts, inside a `try`: if this module can't be loaded, or anything goes wrong
asking, the script runs cmux, as it did before this module existed. A job that keeps running follows a
changed setting the next time it starts. Standard library only.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

CMUX = "cmux"
TMUX = "tmux"
TMUX_CLI = str(Path(__file__).resolve().parents[2] / "bin" / "tmux_backend.py")


def backend() -> str:
    """"tmux" when Pro mode keeps sessions in tmux, else "cmux"."""
    try:
        from assistant import mode  # noqa: PLC0415
        if not mode.is_pro():
            return CMUX
        chosen = mode.session_backend()
    except Exception:  # noqa: BLE001 - see the module's docstring
        return CMUX
    if chosen:
        return chosen
    return CMUX if sys.platform == "darwin" or os.environ.get("CMUX_BIN") else TMUX


def cli(cmux: str) -> str:
    """The command to run for a session-tool call: `cmux`, the path the caller would have run, unless
    sessions are kept in tmux."""
    return TMUX_CLI if backend() == TMUX else cmux
