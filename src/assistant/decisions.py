"""decisions — the append-only, event-sourced decision queue (Keel M2).

Truth is ~/.assistant/decisions/decisions.jsonl (schema "decision/1"): a
decision is CREATED as one appended record and every status change is a NEW
appended record with the same id — nothing is ever rewritten in place, so the
full lifecycle of every decision is replayable. queue.json next to it is a
materialized latest-state-per-id view; it is delete-safe and rebuilt from the
log on every write (and on demand by load_queue when someone deleted it).

Single writer: every append happens under fcntl.flock(LOCK_EX) on a persistent
lock file, so the pulse's triage step and todo-server's /decision/act routes
can both write without a lost-update race (the kernel releases the lock on any
death, same rationale as eventspine's consumer lock).

Dedup is structural: a decision id is derived from
(source, external_id, action_class) — sha256, not a timestamp — so re-triaging
the same event can never enqueue twice. open_decision() checks the folded log
before appending and returns the existing record instead.

Every status transition is ledgered to the actions ledger as
``decision:<from>-><to>`` (design section 3), so the audit trail the rest of
the fleet uses covers decisions too.

Paths are computed per-call (not module constants) so tests that point $HOME
at a tmp dir see fresh paths even when this module stays cached in
sys.modules. Pure stdlib, no LLM, never closes workspaces.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = "decision/1"
QUEUE_SCHEMA = "decision-queue/1"

OPEN = "open"
STATUSES = ("open", "accepted", "edited", "rejected", "snoozed", "expired",
            "auto_done")
# Statuses a human/timer can move an OPEN decision to. auto_done is minted at
# creation (auto lane), never a transition target.
TRANSITION_TARGETS = ("accepted", "edited", "rejected", "snoozed", "expired")

# Deterministic ranking weights (design: lane base + urgency; no LLM ordering).
_LANE_SCORE = {"escalate": 100, "staged": 60, "digest": 20, "auto": 0,
               "drop": 0}
_URGENCY_SCORE = {"now": 50, "high": 20, "low": 0}


def _home() -> Path:
    return Path(os.environ.get("HOME", str(Path.home())))


def decisions_dir() -> Path:
    return _home() / ".assistant" / "decisions"


def decisions_path() -> Path:
    return decisions_dir() / "decisions.jsonl"


def queue_path() -> Path:
    return decisions_dir() / "queue.json"


def lock_path() -> Path:
    return decisions_dir() / "writer.lock"


def ledger_path() -> Path:
    return _home() / ".assistant" / "actions-ledger.jsonl"


def utc_iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def decision_id(source: str, external_id: str, action_class: str = "") -> str:
    """Stable id from (source, external_id, action_class). Timestamp-free by
    design: the same event re-triaged tomorrow folds onto the same id."""
    digest = hashlib.sha256(
        f"{source}:{external_id}:{action_class}".encode()).hexdigest()
    return f"dec-{digest[:16]}"


def score_decision(lane: str, urgency) -> int:
    """Deterministic queue rank: lane base + urgency bump. Unit-tested; no
    judgment in the ordering path."""
    return _LANE_SCORE.get(lane, 0) + _URGENCY_SCORE.get(urgency or "", 0)


# ─── log I/O ────────────────────────────────────────────────────────────────

def read_log(path: Path | None = None) -> list[dict]:
    """All parseable decision records, oldest-first. Corrupt lines (torn
    append, hand edit) are skipped — one bad line never blanks the queue."""
    p = path if path is not None else decisions_path()
    try:
        lines = p.read_text().splitlines()
    except (OSError, FileNotFoundError):
        return []
    out: list[dict] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict) and rec.get("schema") == SCHEMA \
                and rec.get("id"):
            out.append(rec)
    return out


def fold(records: list[dict]) -> dict[str, dict]:
    """Latest record per id, in log order (last write wins — transitions are
    appended after the create, so the fold IS current state)."""
    out: dict[str, dict] = {}
    for rec in records:
        out[rec["id"]] = rec
    return out


def _append_locked(rows: list[dict]) -> None:
    """Append rows under the writer flock, then rebuild queue.json while the
    lock is still held (so the view can never interleave two writers)."""
    if not rows:
        return
    d = decisions_dir()
    d.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path()), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        with open(decisions_path(), "a") as f:
            for rec in rows:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        _write_queue(fold(read_log()))
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


def _append_ledger(entry: dict) -> None:
    """Best-effort actions-ledger row (same shape pulse.py appends). A ledger
    write failure must never block a decision write."""
    try:
        path = ledger_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass


# ─── materialized view ──────────────────────────────────────────────────────

def _sort_key(rec: dict):
    return (0 if rec.get("status") == OPEN else 1,
            -int(rec.get("score") or 0),
            -int(rec.get("epoch") or 0),
            rec.get("id") or "")


def _write_queue(folded: dict[str, dict]) -> dict:
    view = {
        "schema": QUEUE_SCHEMA,
        "ts": utc_iso(time.time()),
        "decisions": sorted(folded.values(), key=_sort_key),
    }
    qp = queue_path()
    qp.parent.mkdir(parents=True, exist_ok=True)
    tmp = qp.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(view, ensure_ascii=False, indent=2))
    os.replace(tmp, qp)
    return view


def rebuild_queue() -> dict:
    """Rebuild queue.json from the log (delete-safe: the view is derived)."""
    return _write_queue(fold(read_log()))


def load_queue() -> dict:
    """The materialized view; rebuilt from the log when missing/corrupt."""
    try:
        view = json.loads(queue_path().read_text())
        if isinstance(view, dict) and isinstance(view.get("decisions"), list):
            return view
    except (OSError, FileNotFoundError, json.JSONDecodeError, ValueError):
        pass
    return rebuild_queue()


def open_decisions(view: dict | None = None) -> list[dict]:
    v = view if view is not None else load_queue()
    return [d for d in v.get("decisions", []) if d.get("status") == OPEN]


# ─── lifecycle ──────────────────────────────────────────────────────────────

def open_decision(*, event: dict, lane: str, policy_id, triage=None,
                  action=None, urgency=None, ttl_h=None, status: str = OPEN,
                  resolution=None, now: float | None = None) -> tuple[dict, bool]:
    """Create (or find) the decision for one event. Returns (record, created).

    created=False means the stable id already exists in the log — re-triaging
    the same (source, external_id, action_class) can never enqueue twice; the
    existing latest record is returned untouched."""
    now = now if now is not None else time.time()
    source = event.get("source") or ""
    external_id = event.get("external_id") or event.get("id") or ""
    action_class = (action or {}).get("class") or ""
    dec_id = decision_id(source, external_id, action_class)
    existing = fold(read_log()).get(dec_id)
    if existing is not None:
        return existing, False
    recommended = None
    if isinstance(action, dict) and action.get("class"):
        recommended = {
            "class": action.get("class"),
            "summary": (event.get("title") or "")[:120],
            "payload_path": event.get("raw_path"),
        }
    record = {
        "schema": SCHEMA,
        "id": dec_id,
        "ts": utc_iso(now),
        "epoch": int(now),
        "event_ref": event.get("id"),
        "source": source,
        "kind": event.get("kind"),
        "title": (event.get("title") or "")[:120],
        "snippet": (event.get("snippet") or "")[:500],
        "refs": dict(event.get("refs") or {}),
        "lane": lane,
        "policy_id": policy_id,
        "triage": triage,
        "recommended": recommended,
        "goal_refs": [],
        "score": score_decision(lane, urgency),
        "urgency": urgency,
        "ttl_h": ttl_h,
        "status": status,
        "resolution": resolution,
    }
    _append_locked([record])
    if status == "auto_done":
        _append_ledger({
            "ts": utc_iso(now), "epoch": int(now),
            "key": f"decision:{dec_id}:auto_done",
            "kind": "decision-auto-done",
            "ws_ref": (event.get("refs") or {}).get("ws_ref") or "(events)",
            "outcome": "verified",
            "evidence": (f"policy {policy_id} auto-handled "
                         f"{source}/{event.get('kind')}: "
                         f"{(event.get('title') or '')[:120]}"),
        })
    return record, True


def transition(dec_id: str, to_status: str, *, via: str, note: str | None = None,
               wake_ts: float | None = None,
               now: float | None = None) -> tuple[dict | None, str | None]:
    """Move a decision to a new status by APPENDING a new record (the log is
    never rewritten). Returns (new_record, error). Only OPEN/snoozed decisions
    can transition; every transition lands on the actions ledger as
    ``decision:<from>-><to>``."""
    now = now if now is not None else time.time()
    if to_status not in TRANSITION_TARGETS:
        return None, f"invalid target status {to_status!r}"
    latest = fold(read_log()).get(dec_id)
    if latest is None:
        return None, f"decision {dec_id!r} not found"
    from_status = latest.get("status")
    if from_status not in (OPEN, "snoozed"):
        return None, (f"decision {dec_id!r} is {from_status!r} "
                      "(only open/snoozed can transition)")
    record = dict(latest)
    record["ts"] = utc_iso(now)
    record["epoch"] = int(now)
    record["status"] = to_status
    record["resolution"] = {
        "ts": utc_iso(now),
        "via": via,
        "ledger_key": f"decision:{dec_id}:{from_status}->{to_status}",
    }
    if note:
        record["resolution"]["note"] = note[:500]
    if to_status == "snoozed" and wake_ts is not None:
        record["wake_ts"] = int(wake_ts)
    _append_locked([record])
    _append_ledger({
        "ts": utc_iso(now), "epoch": int(now),
        "key": f"decision:{dec_id}:{from_status}->{to_status}",
        "kind": "decision-transition",
        "ws_ref": (latest.get("refs") or {}).get("ws_ref") or "(decisions)",
        "outcome": "verified",
        "evidence": f"{from_status}->{to_status} via {via}"
                    + (f": {note[:120]}" if note else ""),
    })
    return record, None


def annotate_triage(dec_id: str, suggested_lane: str, rationale: str,
                    now: float | None = None) -> dict | None:
    """Land a triage-LLM suggestion ON the decision record — a new appended
    record carrying triage {suggested_lane, rationale} and the suggested lane
    (which affects TTL/ranking only). The status is NOT touched: suggestions
    never act, never resolve, never open the auto lane (the caller has already
    validated the lane against policy.TRIAGE_LANE_MAP)."""
    now = now if now is not None else time.time()
    latest = fold(read_log()).get(dec_id)
    if latest is None or latest.get("status") != OPEN:
        return None
    record = dict(latest)
    record["ts"] = utc_iso(now)
    record["epoch"] = latest.get("epoch")  # keep creation epoch for TTL/miner
    record["triage"] = {"suggested_lane": suggested_lane,
                        "rationale": (rationale or "")[:300]}
    record["lane"] = suggested_lane
    record["ttl_h"] = latest.get("ttl_h")
    record["score"] = score_decision(suggested_lane, latest.get("urgency"))
    _append_locked([record])
    return record


def expire_open(now: float | None = None) -> list[dict]:
    """TTL sweep: open decisions whose ttl_h has elapsed since creation move
    to expired (a new appended record, ledgered like any transition). Lanes
    with ttl_h null (escalate by default) never expire here. Also wakes
    snoozed decisions whose wake_ts has passed (back to open)."""
    now = now if now is not None else time.time()
    expired: list[dict] = []
    records = read_log()
    created_epoch: dict[str, int] = {}
    for rec in records:
        created_epoch.setdefault(rec["id"], int(rec.get("epoch") or 0))
    for dec_id, latest in fold(records).items():
        status = latest.get("status")
        if status == "snoozed":
            wake = latest.get("wake_ts")
            if isinstance(wake, (int, float)) and now >= wake:
                record = dict(latest)
                record["ts"] = utc_iso(now)
                record["epoch"] = int(now)
                record["status"] = OPEN
                record["resolution"] = None
                _append_locked([record])
            continue
        if status != OPEN:
            continue
        ttl_h = latest.get("ttl_h")
        if not isinstance(ttl_h, (int, float)):
            continue
        if now - created_epoch.get(dec_id, int(now)) > ttl_h * 3600:
            rec, err = transition(dec_id, "expired", via="ttl", now=now)
            if rec is not None and err is None:
                expired.append(rec)
    return expired
