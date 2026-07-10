#!/usr/bin/env python3
"""interrupt-gate.py — the fleet's ONLY push chokepoint (Keel M3).

An interrupt is any write to a push surface (Notification Center today;
Slack DM / phone would count too). Dashboard tabs, briefs, cards, digests
are pull and never come through here. This file is the single module in the
repo permitted to touch a push API — tests/test_no_rogue_notifications.py
greps bin/ + src/ and fails CI on any other call site, which is what makes
the June 865-notification incident structurally impossible to repeat
(evals/noise/replay-865.py replays it against this gate forever).

API (import via importlib — the filename keeps its dash on purpose so the
grep test's allowlist stays one exact path):

    request(level, key, title, detail="", *, lane=None, urgency=None,
            pageable=False, now=None, deliver=None) -> dict

Every request consults ~/.assistant/noise-budget.json (design section 3
schema; created on first use with the owner-mandated default budget of
page:0, notify:0 — v1 ships fully silent, raising it is Mukul's deliberate
config edit) and applies, in order:

  1. ladder requirements — a "page" needs ALL of lane=escalate,
     urgency=now, policy pageable:true (the ladder is config but the page
     entry is restored if missing — config can tighten, not erase);
  2. 24h same-key dedup — one delivery per (level, key) per day, tracked
     across the midnight rollover (a louder page is never suppressed by an
     earlier notify on the same key);
  3. budget headroom — used[level] < budget[level] for the current date
     (used counts reset when the stored date rolls over).

Deliver or downgrade: a passing request bumps the budget, records the key,
and fires osascript; ANY failed check ledgers an ``interrupt:denied`` row on
the actions ledger (kind interrupt-denied) and appends to the budget file's
suppressed_today tail — silence is auditable in the brief's health section,
not assumed. A delivery-subprocess failure is also recorded as denied (the
budget slot is consumed; better one lost ping than a retry storm).

The budget file is mutated under an fcntl flock (sibling .lock file, same
idiom as decisions.py) so the workspace-watcher daemon and the pulse can't
lose each other's counts. Paths are computed per-call so tests that point
$HOME at a tmp dir see fresh paths. Pure stdlib, no LLM, never closes
workspaces. Exit codes (CLI): 0 delivered, 3 denied — callers must treat a
denial as normal operation, never an error.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

LEVELS = ("page", "notify")
# Owner's default: fully silent (design section 8 — Spine's notify=1 was
# explicitly rejected). Raising it is a deliberate edit to noise-budget.json.
DEFAULT_BUDGET = {"page": 0, "notify": 0}
DEFAULT_LADDER = [
    {"level": "page",
     "requires": {"lane": "escalate", "urgency": "now", "pageable": True}},
]
DEDUP_WINDOW_SEC = 24 * 3600
# suppressed_today keeps only a tail in the budget file (the ledger is the
# full audit trail); denied_today carries the true count.
MAX_SUPPRESSED_KEPT = 200


def _home() -> Path:
    return Path(os.environ.get("HOME", str(Path.home())))


def budget_path() -> Path:
    return _home() / ".assistant" / "noise-budget.json"


def lock_path() -> Path:
    return _home() / ".assistant" / "noise-budget.lock"


def ledger_path() -> Path:
    return _home() / ".assistant" / "actions-ledger.jsonl"


def utc_iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def local_date(epoch: float) -> str:
    """Budget days are LOCAL calendar days — the budget protects Mukul's
    attention, which lives in local time."""
    return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d")


def _is_date(s: str) -> bool:
    """True iff s is a parseable YYYY-MM-DD calendar date."""
    try:
        datetime.strptime(s, "%Y-%m-%d")
        return True
    except (ValueError, TypeError):
        return False


@contextlib.contextmanager
def _budget_lock():
    d = budget_path().parent
    d.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path()), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


def _default_budget(now: float) -> dict:
    return {
        "date": local_date(now),
        "budget": dict(DEFAULT_BUDGET),
        "used": {},
        "ladder": [dict(e) for e in DEFAULT_LADDER],
        "last_delivered": {},
        "denied_today": 0,
        "suppressed_today": [],
    }


def load_budget(now: float) -> dict:
    """The budget doc for `now`, rolled over if the stored date is old:
    used counts, denied counter and suppressed tail reset; budget + ladder
    (Mukul's config) survive; last_delivered survives too — the 24h same-key
    dedup must span midnight — but entries older than the dedup window are
    pruned. An unreadable file falls back to the silent default (never to a
    louder state)."""
    doc = None
    try:
        doc = json.loads(budget_path().read_text())
    except (OSError, FileNotFoundError, json.JSONDecodeError, ValueError):
        doc = None
    if not isinstance(doc, dict):
        return _default_budget(now)
    base = _default_budget(now)
    if isinstance(doc.get("budget"), dict):
        base["budget"].update({k: int(v) for k, v in doc["budget"].items()
                               if isinstance(v, (int, float))})
    if isinstance(doc.get("ladder"), list):
        base["ladder"] = doc["ladder"]
    last = doc.get("last_delivered")
    if isinstance(last, dict):
        base["last_delivered"] = {
            k: float(v) for k, v in last.items()
            if isinstance(v, (int, float)) and now - float(v) < DEDUP_WINDOW_SEC
        }
    # Reset the daily counters ONLY on a genuine rollover: a stored date that
    # is a VALID calendar date and older than today. A missing / typo'd /
    # unparseable date must NOT hand back a fresh quota while keeping a raised
    # budget — that would let a hand edit (or corruption) silently re-arm the
    # push surface. Fail toward quieter: keep the used counts (F2).
    stored = doc.get("date")
    date_valid = isinstance(stored, str) and _is_date(stored)
    if stored == base["date"] or not date_valid:
        if isinstance(doc.get("used"), dict):
            base["used"] = {k: int(v) for k, v in doc["used"].items()
                            if isinstance(v, (int, float))}
        if isinstance(doc.get("denied_today"), int):
            base["denied_today"] = doc["denied_today"]
        if isinstance(doc.get("suppressed_today"), list):
            base["suppressed_today"] = doc["suppressed_today"][-MAX_SUPPRESSED_KEPT:]
    return base


def save_budget(doc: dict) -> None:
    p = budget_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
    os.replace(tmp, p)


def _append_ledger(entry: dict) -> None:
    """Best-effort actions-ledger row (same shape pulse.py appends). A ledger
    failure never blocks the gate's verdict."""
    try:
        p = ledger_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass


def _ladder_check(doc: dict, level: str, lane, urgency, pageable) -> str | None:
    """Return a denial reason when the ladder blocks this level, else None.
    The page rung's requirements are enforced even if a hand-edited ladder
    drops them — config can tighten the ladder, never erase the page gate."""
    ladder = doc.get("ladder") or []
    entries = [e for e in ladder
               if isinstance(e, dict) and e.get("level") == level]
    default_requires = {}
    for e in DEFAULT_LADDER:
        if e.get("level") == level:
            default_requires = e.get("requires") or {}
            break
    if level == "page" and not entries:
        entries = [dict(e) for e in DEFAULT_LADDER]
    got = {"lane": lane, "urgency": urgency, "pageable": bool(pageable)}
    for entry in entries:
        requires = entry.get("requires") or {}
        # A ladder entry that omits or EMPTIES its requires falls back to the
        # default gate — config can TIGHTEN the ladder, never ERASE it. A bare
        # {"requires":{}} for the page rung must not silently open the page
        # surface (F1); an absent entries list is already handled above.
        if not requires and default_requires:
            requires = default_requires
        for field, want in requires.items():
            if got.get(field) != want:
                return (f"ladder: {level} requires {field}={want!r}, "
                        f"got {got.get(field)!r}")
    return None


def _deliver_osascript(title: str, detail: str) -> None:
    """The one real push call in the repo. Escape backslashes first, then
    quotes, to prevent AppleScript injection (same escaping the old
    workspace-watcher notify() used)."""
    safe_title = title.replace("\\", "\\\\").replace('"', '\\"')
    safe_msg = detail.replace("\\", "\\\\").replace('"', '\\"')
    script = (f'display notification "{safe_msg}" '
              f'with title "{safe_title}"')
    subprocess.run(["osascript", "-e", script], timeout=5,
                   capture_output=True, check=False)


def request(level: str, key: str, title: str, detail: str = "", *,
            lane=None, urgency=None, pageable: bool = False,
            now: float | None = None, deliver=None) -> dict:
    """Ask the gate to interrupt. Returns
    {delivered: bool, reason: str, level, key}. NEVER raises on the deny
    path; a caller that treats a denial as an error is holding it wrong —
    denial IS the product working."""
    now = now if now is not None else time.time()
    deliver = deliver if deliver is not None else _deliver_osascript
    key = str(key or "")[:200]
    title = str(title or "")
    detail = str(detail or "")
    reason = None
    if level not in LEVELS:
        reason = f"unknown level {level!r} (want one of {list(LEVELS)})"
    with _budget_lock():
        doc = load_budget(now)
        if reason is None:
            reason = _ladder_check(doc, level, lane, urgency, pageable)
        # Dedup is per (level, key), NOT the bare key (F12): a delivered notify
        # must not suppress a later PAGE on the same key for 24h — a page is a
        # strictly louder tier and gets its own dedup slot. (Latent until M4
        # adds a page caller, but wrong-by-construction to leave.)
        dedup_key = f"{level}:{key}"
        if reason is None:
            last = doc["last_delivered"].get(dedup_key)
            if isinstance(last, (int, float)) and now - last < DEDUP_WINDOW_SEC:
                reason = (f"same-key delivery within 24h "
                          f"(last at {utc_iso(last)})")
        if reason is None:
            used = int(doc["used"].get(level, 0))
            allowed = int(doc["budget"].get(level, 0))
            if used >= allowed:
                reason = f"budget exhausted ({used}/{allowed} {level} today)"
        if reason is not None:
            doc["denied_today"] = int(doc.get("denied_today") or 0) + 1
            doc["suppressed_today"] = (doc.get("suppressed_today") or [])[
                -(MAX_SUPPRESSED_KEPT - 1):]
            doc["suppressed_today"].append({
                "ts": utc_iso(now), "level": level, "key": key,
                "title": title[:120], "reason": reason,
            })
            save_budget(doc)
            _append_ledger({
                "ts": utc_iso(now), "epoch": int(now),
                "key": f"interrupt:denied:{level}:{key}"[:240],
                "kind": "interrupt-denied",
                "ws_ref": "(interrupt-gate)",
                "outcome": "skipped",
                "evidence": f"{level} {title[:120]!r} denied: {reason}",
            })
            return {"delivered": False, "reason": reason,
                    "level": level, "key": key}
        # All checks passed: book the slot BEFORE delivering, so a crash
        # mid-delivery can only under-notify, never over-notify.
        doc["used"][level] = int(doc["used"].get(level, 0)) + 1
        doc["last_delivered"][dedup_key] = float(now)
        save_budget(doc)
    try:
        deliver(title, detail)
    except Exception as exc:  # noqa: BLE001 — a broken push surface stays silent
        reason = f"delivery failed: {exc}"
        _append_ledger({
            "ts": utc_iso(now), "epoch": int(now),
            "key": f"interrupt:denied:{level}:{key}"[:240],
            "kind": "interrupt-denied",
            "ws_ref": "(interrupt-gate)",
            "outcome": "failed",
            "evidence": f"{level} {title[:120]!r} booked but not delivered: "
                        f"{reason}",
        })
        return {"delivered": False, "reason": reason,
                "level": level, "key": key}
    _append_ledger({
        "ts": utc_iso(now), "epoch": int(now),
        "key": f"interrupt:delivered:{level}:{key}"[:240],
        "kind": "interrupt-delivered",
        "ws_ref": "(interrupt-gate)",
        "outcome": "verified",
        "evidence": f"{level} delivered: {title[:120]!r}",
    })
    return {"delivered": True, "reason": "delivered",
            "level": level, "key": key}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--level", required=True, choices=list(LEVELS))
    ap.add_argument("--key", required=True,
                    help="dedup key — one delivery per key per 24h")
    ap.add_argument("--title", required=True)
    ap.add_argument("--detail", default="")
    ap.add_argument("--lane", default=None)
    ap.add_argument("--urgency", default=None)
    ap.add_argument("--pageable", action="store_true")
    args = ap.parse_args(argv)
    result = request(args.level, args.key, args.title, args.detail,
                     lane=args.lane, urgency=args.urgency,
                     pageable=args.pageable)
    print(json.dumps(result))
    return 0 if result["delivered"] else 3


if __name__ == "__main__":
    sys.exit(main())
