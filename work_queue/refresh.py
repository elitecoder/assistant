"""Refresh the Work queue: read each linked source, sort its rows, and rebuild the page.

    python3 -m work_queue.refresh

sources.json decides what runs: an unlinked source is skipped and shows as
"not linked" on the page. A source that fails shows its error, and the other
sources still render. The page goes to $WQ_DATA/site/index.html and the
approval candidates to $WQ_DATA/candidates.json.
"""
from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timezone

from assistant import mode
from assistant.slack_focus.client import Client, SlackError, http_transport

from work_queue import config, page, tools, triage
from work_queue.inputs import gcal, github, gmail, outlook, slack

LABELS = {"slack": "Slack", "github": "GitHub", "gmail": "Gmail", "calendar": "Calendar", "outlook": "Outlook"}
FAILURES = (tools.SourceError, SlackError, OSError, ValueError)


def site_path():
    return config.data_dir() / "site/index.html"


def candidates_path():
    return config.data_dir() / "candidates.json"


def status_path():
    return config.data_dir() / "refresh-status.json"


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def read_sources(doc: dict, transport, now: float) -> dict:
    readers = {
        "slack": lambda e: slack.rows(e, Client(os.environ.get("SLACK_USER_TOKEN", ""), transport), now),
        "github": github.rows,
        "gmail": lambda e: {a: gmail.rows(a) for a in e["accounts"]},
        "calendar": lambda e: [r for a in e["accounts"] for r in gcal.rows(a)],
        "outlook": outlook.rows,
    }
    snap: dict = {"status": []}
    for name, read in readers.items():
        if name not in doc:
            snap["status"].append({"name": LABELS[name], "state": "off",
                                   "detail": f"not linked. Run bin/work-queue-setup.py link {name}"})
            continue
        try:
            snap[name] = read(doc[name])
        except FAILURES as exc:
            snap["status"].append({"name": LABELS[name], "state": "error", "detail": str(exc) or type(exc).__name__})
            continue
        found = snap[name]
        count = sum(map(len, found.values())) if isinstance(found, dict) else len(found)
        snap["status"].append({"name": LABELS[name], "state": "ok", "detail": f"read {count} item{'' if count == 1 else 's'}"})
    return snap


def write_page(snap: dict) -> None:
    doc, cands = page.render(snap)
    site_path().parent.mkdir(parents=True, exist_ok=True)
    site_path().write_text(doc)
    config.write_json(candidates_path(), cands)


def run(transport=http_transport, now: float | None = None, model=None) -> list[dict]:
    """One refresh. Returns each source's status line. `model(prompt, timeout)` replaces triage.call_model."""
    config.write_json(status_path(), {"running": True, "started": now_iso(), "pid": os.getpid()})
    try:
        snap = read_sources(config.load(), transport, time.time() if now is None else now)
    except ValueError as exc:
        config.write_json(status_path(), {"running": False, "finished": now_iso(), "error": str(exc)})
        raise
    snap["built"] = datetime.now().strftime("%b %-d, %-I:%M %p")
    write_page(snap)
    if mode.is_pro():
        snap["triage"] = triage.run(snap, model or triage.call_model, config.read_json(config.data_dir() / "marks.json", {}))
        write_page(snap)
    errors = [s for s in snap["status"] if s["state"] == "error"]
    config.write_json(status_path(), {"running": False, "finished": now_iso(),
                                      "error": "; ".join(f"{s['name']}: {s['detail']}" for s in errors)})
    return snap["status"]


def main() -> int:
    if not mode.is_pro():
        print("Pro mode is off, so the Work queue doesn't refresh. Turn it on with ./install.sh --apply --pro.")
        return 1
    try:
        lines = run()
    except ValueError as exc:
        print(f"Couldn't refresh: {exc}", file=sys.stderr)
        return 1
    for s in lines:
        print(f"{s['name']}: {s['state']} — {s['detail']}")
    return 1 if any(s["state"] == "error" for s in lines) else 0


if __name__ == "__main__":
    sys.exit(main())
