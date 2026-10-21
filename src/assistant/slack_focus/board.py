"""Board: what the dashboard's Slack tab shows, read from the stored files."""
from __future__ import annotations

from pathlib import Path

from . import settings as settings_mod
from . import store

LANES = (
    ("Do first", "do", "Someone is waiting on your reply or decision."),
    ("Promise", "promise", "You said you would. Deliver it or confirm it's done."),
    ("This week", "week", "Needs you, but not first thing."),
    ("Optional", "optional", "Unread in a channel you watch. Skim it if you have time."),
)


def load(home: Path | None = None) -> dict:
    root = store.base_dir(home)
    cfg = settings_mod.load(home)
    focus = store.read_json(root / "focus.json")
    focus = focus if cfg.configured and isinstance(focus, dict) and isinstance(focus.get("active"), list) else None
    view = {
        "configured": cfg.configured,
        "problem": cfg.problem,
        "title": cfg.title,
        "state": store.read_state(home),
        "focus": focus,
    }
    if focus is None:
        return view
    marks = store.read_json(root / "done.json", {})
    marks = marks if isinstance(marks, dict) else {}
    open_items, done_items = [], []
    for item in focus["active"]:
        item = dict(item, fp=store.fingerprint(item))
        if item["fp"] in marks:
            done_items.append(dict(item, done_at=marks[item["fp"]]))
        else:
            open_items.append(item)
    view.update(
        lanes=[(name, cls, hint, [i for i in open_items if i.get("lane") == name]) for name, cls, hint in LANES],
        done_by_you=done_items,
        read=focus.get("read") or [],
        set_aside=focus.get("judge_dropped") or [],
        handled=focus.get("handled") or [],
        judged=bool((focus.get("judge") or {}).get("ran")),
    )
    view["do_first"] = len(view["lanes"][0][3])
    return view
