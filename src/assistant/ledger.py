"""ledger — read/write the append-only actions-ledger.jsonl.

Extracted from the cursor logic in bin/comms_lib.py (read_new_ledger_lines /
ledger cursor) plus bin/pulse.py's append_ledger. The CommsSubsystem uses
`read_new()` to broadcast only ledger lines written since its last pass; tests
and the `status` CLI use `tail(n)`.

The on-disk ledger format and path are unchanged (~/.assistant/actions-ledger.jsonl)
— this is just a clean reader/writer over the existing file.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class LedgerReader:
    """Cursor-tracked reader over actions-ledger.jsonl.

    The cursor is a byte offset persisted to `cursor_path`. `read_new()`
    returns every entry appended since the last call and advances the cursor;
    `tail(n)` is a stateless peek at the last n entries (does not touch the
    cursor).
    """

    def __init__(self, ledger_path: Path, cursor_path: Path):
        self.ledger_path = Path(ledger_path)
        self.cursor_path = Path(cursor_path)

    # ── cursor ────────────────────────────────────────────────────────────

    def read_cursor(self) -> int:
        if not self.cursor_path.exists():
            return 0
        try:
            return int(self.cursor_path.read_text().strip() or "0")
        except ValueError:
            return 0

    def write_cursor(self, offset: int) -> None:
        self.cursor_path.parent.mkdir(parents=True, exist_ok=True)
        self.cursor_path.write_text(str(offset))

    def initialize_cursor_if_missing(self) -> None:
        """First run: skip the existing backlog (start at EOF). Subsequent
        runs: resume from the saved offset."""
        if self.cursor_path.exists():
            return
        if self.ledger_path.exists():
            self.write_cursor(self.ledger_path.stat().st_size)
        else:
            self.write_cursor(0)

    # ── reads ─────────────────────────────────────────────────────────────

    def read_new(self) -> list[dict[str, Any]]:
        """Every ledger entry written since the last cursor; advances the
        cursor. Malformed/blank lines are dropped. Handles rotation
        (file shrank below the cursor → reset to 0)."""
        if not self.ledger_path.exists():
            return []
        cur = self.read_cursor()
        size = self.ledger_path.stat().st_size
        if size < cur:
            cur = 0
            self.write_cursor(0)
        if size == cur:
            return []
        with open(self.ledger_path, "rb") as f:
            f.seek(cur)
            chunk = f.read(size - cur)
        self.write_cursor(size)
        return _parse_lines(chunk.decode("utf-8", errors="replace"))

    def tail(self, n: int = 20) -> list[dict[str, Any]]:
        """Last n parsed entries. Stateless — does not move the cursor."""
        if n <= 0 or not self.ledger_path.exists():
            return []
        # Read the tail bytes only; 256 KB comfortably covers n entries.
        with open(self.ledger_path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 256_000))
            raw = f.read().decode("utf-8", errors="replace")
        entries = _parse_lines(raw)
        return entries[-n:]


def append(ledger_path: Path, entry: dict[str, Any]) -> None:
    """Append one entry as a JSONL line. Creates the parent dir if needed."""
    p = Path(ledger_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _parse_lines(text: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out
