#!/usr/bin/env python3
"""Mechanically purge awaiting cards whose predicate is now FALSE.

Awaiting cards live in ~/.claude/cache/assistant-state.json under
`awaiting_input[]`. The Assistant's prompt has a Step 2.5 rule to re-validate
these every pulse — but under context-rot that step gets skipped, and cards
linger after the user has done the work and closed the workspace.

This script does the mechanical purge so it doesn't depend on Assistant
attention. Triggers a card to be DROPPED if any of:

  1. Workspace closed: card key contains `workspace:N` and that workspace is
     not in `cmux tree` anymore.
  2. PR resolved: card key contains `pr-NNN` (or detail mentions PR #NNN)
     and `gh pr view NNN --json state` returns MERGED or CLOSED.
  3. TODO done: card key contains `td-NNN` and that TODO has status=done
     in ~/.claude/assistant-todo.json.
  4. autoDispatch all set: `autodispatch-unset:*` cards where every
     referenced td-NNN now has `autoDispatch != null`.
  0. Decision cards (Keel M2): a card whose key is a `dec-*` decision id
     exists IFF that decision is still `open` in the decision queue
     (~/.assistant/decisions/). Open → keep (regardless of every other
     predicate); anything else (accepted/rejected/snoozed/expired/auto_done/
     unknown) → drop. Card existence derives from queue state — the
     865-repeat class is structurally impossible. If the queue stores are
     unreadable we KEEP the card (fail-safe, same spirit as the cmux-down
     guard).

Atomically rewrites assistant-state.json (tmpfile + rename) and appends a
log of every purge to ~/.assistant/awaiting-purge.log.

Idempotent — safe to run on every pulse. Run BEFORE the Assistant turn so
the Assistant sees the cleaned state.

Usage:
  purge-stale-awaiting.py [--dry-run]
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

STATE_PATH = Path.home() / ".claude/cache/assistant-state.json"
TODO_PATH = Path.home() / ".claude/assistant-todo.json"
LOG_PATH = Path.home() / ".assistant/awaiting-purge.log"
DECISIONS_QUEUE_PATH = Path.home() / ".assistant/decisions/queue.json"
DECISIONS_LOG_PATH = Path.home() / ".assistant/decisions/decisions.jsonl"
CMUX_BIN = os.environ.get("CMUX_BIN", "/Applications/cmux.app/Contents/Resources/bin/cmux")

WS_RE = re.compile(r"workspace:(\d+)")
PR_RE = re.compile(r"\bpr-(\d+)\b|PR #(\d+)|pr/(\d+)|pull/(\d+)")
TD_RE = re.compile(r"\btd-(\d{3,4})\b")
DEC_KEY_RE = re.compile(r"^dec-[a-f0-9]{8,64}$")


def log(msg: str) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with open(LOG_PATH, "a") as f:
        f.write(f"[{ts}] {msg}\n")


def get_open_workspaces() -> set[str]:
    """Returns set of currently-open `workspace:N` refs from cmux tree."""
    try:
        r = subprocess.run(
            [CMUX_BIN, "tree", "--json"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode != 0:
            return set()
        data = json.loads(r.stdout)
    except Exception:
        return set()

    open_ws = set()

    def walk(node):
        if isinstance(node, dict):
            ref = node.get("ref") or node.get("workspace_ref")
            if ref and isinstance(ref, str) and ref.startswith("workspace:"):
                open_ws.add(ref)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    walk(data)
    return open_ws


def get_open_workspaces_via_text() -> set[str]:
    """Fallback: parse `cmux tree` plain output for `workspace:N`."""
    try:
        r = subprocess.run(
            [CMUX_BIN, "tree"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode != 0:
            return set()
        return set(f"workspace:{m}" for m in re.findall(r"\bworkspace:(\d+)\b", r.stdout))
    except Exception:
        return set()


def load_todos() -> dict[str, dict]:
    try:
        data = json.load(open(TODO_PATH))
    except Exception:
        return {}
    items = data.get("items", []) if isinstance(data, dict) else data
    return {it.get("id"): it for it in items if it.get("id")}


def gh_pr_state(pr: str) -> str | None:
    """Returns 'OPEN' / 'MERGED' / 'CLOSED' / None on failure."""
    try:
        r = subprocess.run(
            ["gh", "pr", "view", pr, "--json", "state", "-q", ".state"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0:
            return r.stdout.strip() or None
    except Exception:
        pass
    return None


def load_decision_statuses():
    """{decision_id: latest status} from the decision queue (Keel M2).

    Prefers the materialized queue.json; a MISSING **or unreadable/corrupt**
    queue.json falls back to folding the append-only decisions.jsonl — the
    healthy truth next door (queue.json is delete-safe/derived, so a torn
    view must never freeze every dec-* card in place). The fallback is
    logged. Returns None only when the log ALSO exists-but-unreadable, or
    when a corrupt queue.json has no log beside it — the caller then keeps
    every dec-* card (fail-safe). Both stores missing returns {} (no
    decisions ⇒ no dec-* card is legitimate).
    """
    queue_unreadable = False
    if DECISIONS_QUEUE_PATH.exists():
        try:
            view = json.loads(DECISIONS_QUEUE_PATH.read_text())
            out = {}
            for d in view.get("decisions", []):
                if isinstance(d, dict) and d.get("id"):
                    out[d["id"]] = d.get("status")
            return out
        except Exception as e:
            queue_unreadable = True
            log(f"queue.json unreadable ({e}) — falling back to folding "
                f"decisions.jsonl")
    if DECISIONS_LOG_PATH.exists():
        try:
            out = {}
            for line in DECISIONS_LOG_PATH.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if isinstance(rec, dict) and rec.get("id"):
                    out[rec["id"]] = rec.get("status")  # last record wins
            return out
        except Exception:
            return None
    # Corrupt queue with no log beside it: nothing trustworthy — fail safe.
    return None if queue_unreadable else {}


def card_should_drop(card: dict, open_ws: set[str], todos: dict[str, dict],
                     dec_statuses=None) -> str | None:
    """Returns a reason string if the card should be dropped, else None."""
    key = card.get("key", "") or ""
    detail = card.get("detail", "") or ""
    title = card.get("title", "") or ""
    haystack = f"{key} {detail} {title}"

    # 0. Decision cards: existence derives SOLELY from queue state. Open →
    #    keep (no other predicate may drop it — the decision is the truth);
    #    resolved/expired/unknown → drop; unreadable stores → keep.
    if DEC_KEY_RE.match(key):
        if dec_statuses is None:
            return None  # queue unreadable — fail safe, keep the card
        status = dec_statuses.get(key)
        if status == "open":
            return None
        return "decision {} is {} (card derives from queue state)".format(
            key, status if status is not None else "unknown")

    # 1. Workspace closed
    ws_matches = WS_RE.findall(key) or WS_RE.findall(detail)
    if ws_matches:
        refs = {f"workspace:{n}" for n in ws_matches}
        gone = refs - open_ws
        if gone == refs and open_ws:
            # All referenced workspaces are gone (and we have a non-empty
            # open set, i.e. cmux is reachable — don't purge if cmux is down).
            return f"workspace(s) closed: {', '.join(sorted(gone))}"

    # 2. PR merged or closed
    pr_match = PR_RE.search(haystack)
    if pr_match:
        pr = next(g for g in pr_match.groups() if g)
        state = gh_pr_state(pr)
        if state in ("MERGED", "CLOSED"):
            return f"PR #{pr} is {state}"

    # 3. TODO done
    td_matches = TD_RE.findall(haystack)
    if td_matches:
        all_done = True
        any_known = False
        for tid in td_matches:
            full = f"td-{tid}"
            it = todos.get(full)
            if it is None:
                continue
            any_known = True
            if it.get("status") not in ("done", "deferred"):
                all_done = False
                break
        if any_known and all_done:
            ids = ", ".join(f"td-{t}" for t in td_matches)
            return f"all referenced TODOs done/deferred: {ids}"

    # 4. autodispatch-unset where every TODO now has autoDispatch set
    if "autodispatch-unset" in key and td_matches:
        any_unset = False
        any_known = False
        for tid in td_matches:
            full = f"td-{tid}"
            it = todos.get(full)
            if it is None:
                continue
            any_known = True
            if it.get("autoDispatch") is None:
                any_unset = True
                break
        if any_known and not any_unset:
            return "all referenced TODOs now have autoDispatch set"

    return None


def main():
    parser = argparse.ArgumentParser(description="Purge stale awaiting cards")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not STATE_PATH.exists():
        return

    try:
        state = json.load(open(STATE_PATH))
    except Exception as e:
        log(f"state.json unreadable: {e}")
        return

    cards = state.get("awaiting_input", []) or []
    if not cards:
        return

    open_ws = get_open_workspaces() or get_open_workspaces_via_text()
    if not open_ws:
        log("cmux tree returned no workspaces — skipping (cmux may be down)")
        return
    todos = load_todos()
    dec_statuses = load_decision_statuses()

    keep = []
    dropped = []
    for card in cards:
        reason = card_should_drop(card, open_ws, todos, dec_statuses)
        if reason:
            dropped.append((card.get("key", "?"), reason))
        else:
            keep.append(card)

    if not dropped:
        return

    for key, reason in dropped:
        log(f"DROP {key}: {reason}")

    if args.dry_run:
        for key, reason in dropped:
            print(f"would drop: {key} ({reason})")
        return

    state["awaiting_input"] = keep

    tmp = STATE_PATH.with_suffix(STATE_PATH.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2) + "\n")
    os.replace(tmp, STATE_PATH)
    log(f"purged {len(dropped)} card(s); {len(keep)} remain")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"crash: {e}")
        sys.exit(1)
