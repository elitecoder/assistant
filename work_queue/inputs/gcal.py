"""Today's Google Calendar events, read through the `gog` CLI with each account's own sign-in."""
from __future__ import annotations

from datetime import datetime

from work_queue import tools


def when(start: dict) -> str:
    if "dateTime" not in start:
        return "All day"
    return datetime.fromisoformat(start["dateTime"].replace("Z", "+00:00")).astimezone().strftime("%-I:%M %p")


def rows(account: str) -> list[dict]:
    found = tools.run_json(["gog", "calendar", "events", "primary", "--today", "--max", "50", "-a", account, "-j"])
    return [{"account": account, "title": e.get("summary") or "(no title)", "when": when(e.get("start") or {}),
             "link": e.get("htmlLink") or "", "declined": any(a.get("self") and a.get("responseStatus") == "declined"
                                                               for a in e.get("attendees") or [])}
            for e in found.get("events") or [] if e.get("status") != "cancelled"]
