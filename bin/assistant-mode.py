#!/usr/bin/env python3
"""Show or change Assistant's mode.

    assistant-mode.py            print the current mode
    assistant-mode.py pro        turn on Pro mode and link the Pro skills
    assistant-mode.py default    switch to default mode and unlink them
    assistant-mode.py --ensure   save the current mode if none is saved yet
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from assistant import mode  # noqa: E402


def main(argv: list[str]) -> int:
    if not argv:
        print(mode.current())
        return 0
    try:
        if argv == ["--ensure"]:
            if mode.saved_mode() is None:
                mode.save(mode.current())
            print(mode.current())
            return 0
        if len(argv) == 1 and argv[0] in mode.MODES:
            for change in mode.switch(argv[0]):
                print(change)
            print(mode.current())
            return 0
    except (OSError, ValueError) as exc:
        print(f"Couldn't change the mode: {exc}", file=sys.stderr)
        return 1
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
