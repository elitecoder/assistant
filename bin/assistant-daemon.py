#!/usr/bin/env python3
"""assistant-daemon — thin launcher for the single-process daemon.

Equivalent to `python -m assistant`, but runnable directly (it's what the
com.mukul.assistant-daemon.plist LaunchAgent invokes) without needing the
package pip-installed: it puts the repo's src/ on sys.path and delegates to
assistant.__main__:main.

  bin/assistant-daemon.py                 # run the daemon
  bin/assistant-daemon.py --dry-run       # pulse only
  bin/assistant-daemon.py --config <path> # use a specific config.json
  bin/assistant-daemon.py status          # print subsystem status and exit
"""
from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from assistant.__main__ import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
