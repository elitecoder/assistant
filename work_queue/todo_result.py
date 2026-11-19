#!/usr/bin/env python3
"""Record an agent's outcome on an Assistant TODO and close it.

    todo_result.py td-NNN "what you did, with links"
    todo_result.py td-NNN --blocked "what you need"
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from assistant import todostore  # noqa: E402

RESULT_TAG = "RESULT:"


def todo_path() -> Path:
    return Path(os.environ.get("TODO_PATH") or todostore.todo_path())


def main(td_id: str, text: str, blocked: bool = False) -> None:
    text = " ".join(text.split())
    if not text:
        raise SystemExit("result text is empty")
    path = todo_path()
    with todostore.todo_lock():
        doc = json.loads(path.read_text())
        item = next((i for i in doc.get("items", []) if i.get("id") == td_id), None)
        if item is None:
            raise SystemExit(f"{td_id} not found")
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        if blocked:
            item.update(status="blocked", statusReason=text, statusUpdatedAt=now)
        else:
            item["detail"] = f"{(item.get('detail') or '').rstrip()}\n\n{RESULT_TAG} {text}"
            item.update(status="done", closedReason="agent finished", resultAt=now, statusUpdatedAt=now)
        doc["_lastUpdated"] = now
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
        tmp.replace(path)
    print(f"{td_id}: {'blocked' if blocked else 'result recorded, marked done'}")


def cli(argv: list[str]) -> None:
    blocked = "--blocked" in argv
    args = [a for a in argv if a != "--blocked"]
    if len(args) != 2:
        raise SystemExit('usage: todo_result.py td-NNN [--blocked] "text"')
    main(args[0], args[1], blocked)


if __name__ == "__main__":
    cli(sys.argv[1:])
