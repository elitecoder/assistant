"""triage — the pulse step that lanes new WorldEvents (Keel M2).

Runs right after the event spine's drain: every event appended to
~/.assistant/events.jsonl since the last pulse goes through the deterministic
policy engine (src/assistant/policy.py). Policy hits act MECHANICALLY:

    auto     → the rule's pre-declared action via existing channels only
               (todo.create into ~/.claude/assistant-todo.json, or an append
               to the daily digest); decision recorded as auto_done + ledgered
    staged   → open decision in the queue
    escalate → open decision; open escalate decisions mirror to awaiting
               cards keyed by their `dec-` id (purge-stale-awaiting derives
               card existence from queue state)
    digest   → daily-FYI row in ~/.assistant/digest/<date>.jsonl + an open
               decision that TTLs out in 24h
    drop     → ledgered tombstone, nothing else (explicit rules only)

Events NO rule matches are batched to at most ONE suggestion-only triage LLM
call per pulse (the caller injects that as `llm_batch` — pulse.py spawns the
Observer-pattern subprocess; this module never talks to an LLM itself). Each
unmatched event gets an OPEN decision laned `escalate` (the fail-safe default)
BEFORE the LLM is consulted; a returned suggestion is validated against
policy.TRIAGE_LANE_MAP (structurally auto-less and drop-less) and landed on
the decision record as {suggested_lane, rationale}. Suggestions never act:
no status change, no action class, ever.

Replayability: every laned event gets a ``triage.disposition`` row appended
back onto events.jsonl ({kind, refs.event_id, lane, policy_id, decision_id}),
so the whole laning history is reconstructable from the event log alone. The
byte-offset cursor (~/.assistant/policies/triage-cursor.json) is a derived
convenience — delete it and the next pulse re-scans from byte 0, skipping
anything a disposition row already covers.

Paths are computed per-call (not module constants) so tests that point $HOME
at a tmp dir see fresh paths even when this module stays cached in
sys.modules. Pure stdlib; the ONLY LLM touchpoint is the injected,
suggestion-only `llm_batch` callable. Never closes workspaces.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from . import decisions, policy, todostore

EVENT_SCHEMA = "world-event/1"
DISPOSITION_KIND = "triage.disposition"

# Bound one pulse's work so a huge backlog can't blow the tick budget. Events
# past the cap simply wait for the next pulse (the cursor doesn't advance past
# unprocessed rows).
MAX_EVENTS_PER_PULSE = 200
# At most this many unmatched events ride the single triage LLM call; the
# rest still get their fail-safe escalate decisions, just no suggestion.
MAX_TRIAGE_BATCH = 40
# Cap the escalate-card mirror so a pathological queue can't flood the
# dashboard (the queue itself is the full record).
MAX_ESCALATE_CARDS = 30


def _home() -> Path:
    return Path(os.environ.get("HOME", str(Path.home())))


def events_path() -> Path:
    return _home() / ".assistant" / "events.jsonl"


def cursor_path() -> Path:
    return _home() / ".assistant" / "policies" / "triage-cursor.json"


def digest_dir() -> Path:
    return _home() / ".assistant" / "digest"


def todo_path() -> Path:
    return _home() / ".claude" / "assistant-todo.json"


def ledger_path() -> Path:
    return _home() / ".assistant" / "actions-ledger.jsonl"


def utc_iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def _append_ledger(entry: dict) -> None:
    """Best-effort actions-ledger row; a ledger failure never blocks laning."""
    try:
        path = ledger_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass


# ─── event scan (cursor + disposition dedup) ─────────────────────────────────

def _load_cursor() -> int:
    try:
        d = json.loads(cursor_path().read_text())
        off = d.get("offset")
        return int(off) if isinstance(off, (int, float)) and off >= 0 else 0
    except (OSError, FileNotFoundError, json.JSONDecodeError, ValueError,
            TypeError):
        return 0


def _save_cursor(offset: int) -> None:
    p = cursor_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"offset": int(offset),
                               "ts": utc_iso(time.time())}))
    os.replace(tmp, p)


def scan_new_events(max_events: int = MAX_EVENTS_PER_PULSE) -> tuple[list[dict], int]:
    """New (never-disposition'd) WorldEvents past the cursor, plus the byte
    offset the cursor should advance to once they are all processed.

    Delete-safe: with no cursor file the whole log is re-scanned and every
    event that already has a triage.disposition row is skipped — deleting the
    cursor can slow one pulse, never double-lane an event. A cursor pointing
    past EOF (truncated/replaced log) resets to 0 the same way."""
    p = events_path()
    try:
        size = p.stat().st_size
    except (OSError, FileNotFoundError):
        return [], 0
    offset = _load_cursor()
    if offset > size:
        offset = 0
    # Disposition'd event ids — scanned from byte 0 whenever we (re)start from
    # 0, else from the cursor (rows before the cursor were processed by the
    # pulse that advanced it).
    seen: set[str] = set()
    events: list[tuple[int, dict]] = []  # (end_offset, event)
    try:
        with open(p, "rb") as f:
            f.seek(offset)
            while True:
                line = f.readline()
                if not line:
                    break
                end = f.tell()
                try:
                    row = json.loads(line.decode("utf-8", errors="replace"))
                except json.JSONDecodeError:
                    events.append((end, None))  # advance past torn/garbage
                    continue
                if not isinstance(row, dict):
                    events.append((end, None))
                    continue
                if row.get("kind") == DISPOSITION_KIND:
                    ev_id = (row.get("refs") or {}).get("event_id")
                    if ev_id:
                        seen.add(ev_id)
                    events.append((end, None))
                    continue
                if row.get("schema") != EVENT_SCHEMA or not row.get("id"):
                    events.append((end, None))
                    continue
                events.append((end, row))
    except OSError:
        return [], offset

    out: list[dict] = []
    new_offset = offset
    for end, row in events:
        if row is not None and row["id"] not in seen:
            if len(out) >= max_events:
                break  # cursor stops BEFORE the first unprocessed event
            out.append(row)
        new_offset = end
    return out, new_offset


def append_disposition(event_id: str, lane: str, policy_id,
                       decision_id=None, now: float | None = None) -> None:
    """Append the replayable disposition row back onto events.jsonl (design
    section 3). Not a WorldEvent — carries no schema/id, so the spine's dedup
    tail-scan and pick-ws-batch's promotion scan both ignore it."""
    now = now if now is not None else time.time()
    row = {
        "kind": DISPOSITION_KIND,
        "ts": utc_iso(now),
        "epoch": int(now),
        "refs": {"event_id": event_id},
        "lane": lane,
        "policy_id": policy_id,
        "decision_id": decision_id,
    }
    p = events_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


# ─── mechanical lane actions (existing channels only) ────────────────────────

def append_digest(event: dict, policy_id, now: float | None = None) -> bool:
    """One daily-FYI row, deduped by event id within the day file so a
    reprocessed event (crash between act and disposition) can't double-post."""
    now = now if now is not None else time.time()
    d = digest_dir()
    d.mkdir(parents=True, exist_ok=True)
    day_file = d / f"{utc_iso(now)[:10]}.jsonl"
    if day_file.exists():
        try:
            for line in day_file.read_text().splitlines():
                try:
                    if json.loads(line).get("event_id") == event.get("id"):
                        return False
                except (json.JSONDecodeError, AttributeError):
                    continue
        except OSError:
            pass
    row = {
        "ts": utc_iso(now),
        "epoch": int(now),
        "event_id": event.get("id"),
        "source": event.get("source"),
        "kind": event.get("kind"),
        "title": (event.get("title") or "")[:200],
        "policy_id": policy_id,
    }
    with open(day_file, "a") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return True


def create_todo(event: dict, policy_id: str, params: dict,
                now: float | None = None) -> tuple[str | None, bool]:
    """todo.create standing action: one TODO in ~/.claude/assistant-todo.json,
    idempotent via an exact source key (same dedup contract the goal planner
    will use). Returns (todo_id, created). Atomic read-modify-write, matching
    todo-server's save_json."""
    now = now if now is not None else time.time()
    source_key = f"policy:{policy_id}:{event.get('id')}"
    p = todo_path()
    # Read AND write under the ONE shared todo-file lock (M3): the pulse dispatch
    # stamp, the goals planner, and the todo-server all write this file — an
    # unlocked read-modify-write here loses whichever update lands second.
    with todostore.todo_lock():
        try:
            data = json.loads(p.read_text())
        except (OSError, FileNotFoundError, json.JSONDecodeError, ValueError):
            data = {"items": []}
        if not isinstance(data, dict):
            data = {"items": []}
        items = data.setdefault("items", [])
        for it in items:
            if isinstance(it, dict) and it.get("source") == source_key:
                return it.get("id"), False
        todo_id = f"td-auto-{(event.get('id') or '')[:8] or int(now)}"
        if any(isinstance(it, dict) and it.get("id") == todo_id for it in items):
            todo_id = f"{todo_id}-{int(now)}"
        items.append({
            "id": todo_id,
            "title": (params.get("title") or event.get("title") or "")[:120],
            "detail": (params.get("detail")
                       or f"Auto-created by policy {policy_id} from "
                          f"{event.get('source')}/{event.get('kind')} event "
                          f"{event.get('id')}.\n\n{(event.get('snippet') or '')[:500]}"),
            "status": "open",
            "priority": params.get("priority") or "P3",
            "tags": list(params.get("tags") or ["auto-policy"]),
            "autoDispatch": bool(params.get("autoDispatch", False)),
            "source": source_key,
            "createdAt": utc_iso(now),
            "createdBy": f"policy:{policy_id}",
        })
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
        os.replace(tmp, p)
    return todo_id, True


def _run_auto_action(event: dict, decision_dict: dict,
                     now: float) -> tuple[bool, str]:
    """Execute an auto rule's pre-declared action. Returns (ok, evidence).
    The class set is closed (policy.AUTO_ACTION_CLASSES) — validated again
    here so even a hand-edited policies.json can't smuggle a new verb."""
    action = decision_dict.get("action") or {}
    cls = action.get("class")
    params = action.get("params") or {}
    policy_id = decision_dict.get("policy_id")
    if cls == "todo.create":
        todo_id, created = create_todo(event, policy_id, params, now=now)
        return True, (f"todo.create → {todo_id}"
                      + ("" if created else " (already existed)"))
    if cls == "digest.append":
        appended = append_digest(event, policy_id, now=now)
        return True, ("digest.append → daily digest"
                      + ("" if appended else " (already present)"))
    return False, f"unknown auto action class {cls!r}"


# ─── escalate-card mirror ────────────────────────────────────────────────────

def escalate_cards(view: dict | None = None,
                   cap: int = MAX_ESCALATE_CARDS) -> list[dict]:
    """Awaiting cards derived from queue state: one card per OPEN escalate
    decision, keyed by its `dec-` id. Purely derived — purge-stale-awaiting
    drops a card the moment its decision leaves `open`, and this mirror
    re-emits cards only for decisions still open, so the 865-repeat class is
    structurally impossible."""
    cards: list[dict] = []
    for dec in decisions.open_decisions(view):
        if dec.get("lane") != "escalate":
            continue
        detail = (dec.get("snippet") or "").strip()
        prov = dec.get("policy_id") or "triage"
        triage_info = dec.get("triage") or {}
        if triage_info.get("suggested_lane"):
            prov += (f"; triage suggests {triage_info['suggested_lane']}"
                     f" — {triage_info.get('rationale') or ''}".rstrip(" —"))
        cards.append({
            "key": dec["id"],
            "tier": "T2",
            "title": (dec.get("title") or dec["id"])[:120],
            "detail": (f"{detail}\n\n[decision {dec['id']} · lane=escalate · "
                       f"via {prov}]")[:1200].strip(),
            "ws_ref": (dec.get("refs") or {}).get("ws_ref"),
        })
        if len(cards) >= cap:
            break
    return cards


# ─── the step ────────────────────────────────────────────────────────────────

def triage_new_events(pulse_idx: int = 0, log=None, now: float | None = None,
                      llm_batch=None) -> dict:
    """Lane every new WorldEvent; act mechanically on policy hits; batch the
    unmatched remainder to ONE suggestion-only LLM call (via the injected
    `llm_batch(events) -> {event_id: {suggested_lane, rationale}}`); mirror
    open escalate decisions to awaiting cards; TTL-expire; mine proposals.

    Returns a summary dict with `cards` for the pulse's awaiting_input. Every
    acting path in here is deterministic Python — the LLM's only output is a
    suggestion string that lands on a decision record."""
    log = log or logging.getLogger("triage")
    now = now if now is not None else time.time()
    summary = {
        "events_processed": 0,
        "lanes": {},
        "auto_done": 0,
        "decisions_opened": 0,
        "dropped": 0,
        "triage_batch": 0,
        "triage_suggested": 0,
        "expired": 0,
        "proposals": 0,
        "policy_installed": False,
        "policy_invalid": [],
        "cards": [],
    }

    summary["policy_installed"] = policy.ensure_policies_installed()
    rules, invalid, error = policy.load_policies()
    if invalid:
        summary["policy_invalid"] = invalid
        log.warning("triage: %d invalid policy rule(s) skipped: %s",
                    len(invalid), "; ".join(invalid[:3]))
    if error:
        log.warning("triage: policy load failed (%s) — every event this "
                    "pulse escalates", error)

    events, new_offset = scan_new_events()
    unmatched: list[dict] = []

    for event in events:
        laned = policy.lane_event(event, rules, error)
        lane = laned["lane"]
        if lane == "unmatched":
            unmatched.append(event)
            continue
        _apply_lane(event, laned, summary, now, log)

    # Fail-safe decisions FIRST, one suggestion-only LLM call second. Every
    # unmatched event is already an open escalate decision before any LLM
    # output is read — a hung/failed call changes nothing.
    batch = unmatched[:MAX_TRIAGE_BATCH]
    dec_ids: dict[str, str] = {}
    for event in unmatched:
        record, created = decisions.open_decision(
            event=event, lane="escalate", policy_id="triage", triage=None,
            urgency=None, ttl_h=policy.DEFAULT_TTL_H["escalate"], now=now)
        dec_ids[event["id"]] = record["id"]
        if created:
            summary["decisions_opened"] += 1
        summary["lanes"]["triage"] = summary["lanes"].get("triage", 0) + 1
        summary["events_processed"] += 1

    suggestions: dict = {}
    if batch and llm_batch is not None:
        summary["triage_batch"] = len(batch)
        try:
            suggestions = llm_batch(batch) or {}
        except Exception as e:  # noqa: BLE001 — a broken LLM never blocks laning
            log.warning("triage: LLM batch failed (suggestions skipped): %s", e)
            suggestions = {}

    for event in batch:
        sug = suggestions.get(event["id"])
        suggested_lane = policy.valid_triage_lane(
            (sug or {}).get("suggested_lane") if isinstance(sug, dict) else None)
        if suggested_lane is not None:
            decisions.annotate_triage(
                dec_ids[event["id"]], suggested_lane,
                str((sug or {}).get("rationale") or ""), now=now)
            summary["triage_suggested"] += 1
        # The disposition records the EFFECTIVE lane — always escalate for
        # unmatched events. A suggestion is a pure annotation on the decision
        # record; it never becomes the lane of record (design section 5).
        append_disposition(event["id"], "escalate", "triage",
                           decision_id=dec_ids[event["id"]], now=now)
    for event in unmatched[MAX_TRIAGE_BATCH:]:
        append_disposition(event["id"], "escalate", "triage",
                           decision_id=dec_ids[event["id"]], now=now)

    if events or new_offset:
        _save_cursor(new_offset)

    try:
        summary["expired"] = len(decisions.expire_open(now=now))
    except Exception as e:  # noqa: BLE001
        log.warning("triage: TTL sweep failed (ignored): %s", e)

    try:
        mined = policy.mine_policy_proposals(
            decisions.read_log(), now=now, rules=rules)
        summary["proposals"] = len(mined)
        for prop in mined:
            log.info("triage: mined policy proposal %s (pending confirm)",
                     prop["proposed_policy"]["id"])
    except Exception as e:  # noqa: BLE001
        log.warning("triage: proposal miner failed (ignored): %s", e)

    try:
        summary["cards"] = escalate_cards()
    except Exception as e:  # noqa: BLE001
        log.warning("triage: card mirror failed (ignored): %s", e)

    return summary


def _apply_lane(event: dict, laned: dict, summary: dict, now: float,
                log) -> None:
    """Act on one policy-matched event. Deterministic; ledgered."""
    lane = laned["lane"]
    policy_id = laned.get("policy_id")
    summary["lanes"][lane] = summary["lanes"].get(lane, 0) + 1
    summary["events_processed"] += 1
    decision_id = None

    if lane == "drop":
        # Explicit rule only (lane_event can't produce drop otherwise).
        # Tombstone on the ledger so a dropped event is auditable, not gone.
        _append_ledger({
            "ts": utc_iso(now), "epoch": int(now),
            "key": f"event-drop:{event.get('id')}",
            "kind": "event-drop",
            "ws_ref": (event.get("refs") or {}).get("ws_ref") or "(events)",
            "outcome": "verified",
            "evidence": (f"policy {policy_id} dropped "
                         f"{event.get('source')}/{event.get('kind')}: "
                         f"{(event.get('title') or '')[:120]}"),
        })
        summary["dropped"] += 1
    elif lane == "auto":
        ok, evidence = _run_auto_action(event, laned, now)
        if not ok:
            # Ambiguity in an acting rule → escalate, never act (in-code
            # invariant; lane_event already blocks unknown classes, this is
            # the second fence).
            log.warning("triage: auto rule %s refused (%s) — escalating",
                        policy_id, evidence)
            record, created = decisions.open_decision(
                event=event, lane="escalate", policy_id=policy_id,
                urgency=laned.get("urgency"), ttl_h=None, now=now)
            decision_id = record["id"]
            if created:
                summary["decisions_opened"] += 1
            summary["lanes"]["auto"] -= 1
            summary["lanes"]["escalate"] = summary["lanes"].get("escalate", 0) + 1
            append_disposition(event["id"], "escalate", policy_id,
                               decision_id=decision_id, now=now)
            return
        record, created = decisions.open_decision(
            event=event, lane="auto", policy_id=policy_id,
            action=laned.get("action"), urgency=laned.get("urgency"),
            ttl_h=laned.get("ttl_h"), status="auto_done",
            resolution={"ts": utc_iso(now), "via": policy_id,
                        "ledger_key": f"decision:auto:{event.get('id')}",
                        "note": evidence},
            now=now)
        decision_id = record["id"]
        summary["auto_done"] += 1
    else:  # staged | escalate | digest → an open decision
        if lane == "digest":
            append_digest(event, policy_id, now=now)
        record, created = decisions.open_decision(
            event=event, lane=lane, policy_id=policy_id,
            action=laned.get("action"), urgency=laned.get("urgency"),
            ttl_h=laned.get("ttl_h"), now=now)
        decision_id = record["id"]
        if created:
            summary["decisions_opened"] += 1

    append_disposition(event["id"], lane, policy_id,
                       decision_id=decision_id, now=now)
