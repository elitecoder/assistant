"""Refresh the Work queue: read each linked source, sort its rows, and rebuild the page.

    python3 -m work_queue.refresh

sources.json decides what runs: an unlinked source is skipped and shows as
"not linked" on the page. A source that fails shows its error, and the other
sources still render. Then it checks each Ready for you item and clears the
finished ones (work_queue/ready.py). The page goes to $WQ_DATA/site/index.html and the
approval candidates to $WQ_DATA/candidates.json.
"""
from __future__ import annotations

import os
import sys
import time
import traceback
from datetime import datetime, timezone

from assistant import mode
from assistant.slack_focus.client import Client, SlackError, http_transport

from work_queue import config, held_saves, page, ready, todo_result, tools, triage
from work_queue.inputs import gcal, github, gmail, outlook, slack

LABELS = {"slack": "Slack", "github": "GitHub", "gmail": "Gmail", "calendar": "Calendar", "outlook": "Outlook"}
FAILURES = (tools.SourceError, SlackError, OSError, ValueError)


def site_path():
    return config.data_dir() / "site/index.html"


def candidates_path():
    return config.data_dir() / "candidates.json"


def status_path():
    return config.data_dir() / "refresh-status.json"


def emails_path():
    return config.data_dir() / "emails.json"


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
        found = read_one(snap["status"], LABELS[name], read, doc[name])
        if found is not None:
            snap[name] = found
    if "outlook" in doc and doc["outlook"].get("calendar", True):
        found = read_one(snap["status"], "Outlook calendar", outlook.events, doc["outlook"])
        if found:
            snap["calendar"] = snap.get("calendar", []) + found
    return snap


def read_one(status: list[dict], label: str, read, entry: dict):
    """One source's rows, with its status line added to status; None when it fails."""
    try:
        found = read(entry)
    except FAILURES as exc:
        status.append({"name": label, "state": "error", "detail": str(exc) or type(exc).__name__})
        return None
    count = sum(map(len, found.values())) if isinstance(found, dict) else len(found)
    status.append({"name": label, "state": "ok", "detail": f"read {count} item{'' if count == 1 else 's'}"})
    return found


def write_page(snap: dict) -> None:
    doc, cands = page.render(snap)
    site_path().parent.mkdir(parents=True, exist_ok=True)
    site_path().write_text(doc)
    config.write_json(candidates_path(), cands)
    config.write_json(emails_path(), page.emails(snap))


def blocked_rows() -> set[str]:
    """The links of rows sent to Assistant whose TODO is blocked."""
    queued = config.read_json(config.data_dir() / "queued.json", {})
    status = {i.get("id"): i.get("status") for i in todo_result.read_store().get("items", [])}
    return {key for key, q in queued.items() if status.get(q.get("id")) == "blocked"}


def paid(snap: dict, note: str = "") -> dict:
    """The verdicts already paid for; no verdicts, with the note, when the saved cache can't be read."""
    try:
        return triage.cached(snap, note)
    except Exception as exc:  # noqa: BLE001 — a broken cache never holds back the page; the traceback goes to the log
        traceback.print_exc()
        return triage.empty(note or f"triage stopped on an unexpected error ({type(exc).__name__})")


def run(transport=http_transport, now: float | None = None, model=None) -> list[dict]:
    """One refresh. Returns each source's status line. `model(prompt, timeout)` replaces triage.call_model."""
    config.write_json(status_path(), {"running": True, "started": now_iso(), "pid": os.getpid()})
    try:
        doc = config.load()
        snap = read_sources(doc, transport, time.time() if now is None else now)
    except ValueError as exc:
        config.write_json(status_path(), {"running": False, "finished": now_iso(), "error": str(exc)})
        raise
    if "slack" in snap:  # a save Slack no longer lists was marked, so it needn't be tried again
        held_saves.drop({r["key"] for r in snap["slack"]}.__contains__)
    ready.run(doc, transport, os.environ.get("SLACK_USER_TOKEN", ""))
    snap["built"] = datetime.now().strftime("%b %-d, %-I:%M %p")
    snap["built_iso"] = now_iso()
    pro = mode.is_pro()
    if pro:
        snap["triage"] = paid(snap)
    write_page(snap)
    if pro:
        marks = config.read_json(config.data_dir() / "marks.json", {})
        try:
            snap["triage"] = triage.run(snap, model or triage.call_model, marks, blocked_rows())
        except Exception as exc:  # noqa: BLE001 — a triage bug never holds back the page; the traceback goes to the log
            traceback.print_exc()
            snap["triage"] = paid(snap, f"triage stopped on an unexpected error ({type(exc).__name__})")
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
