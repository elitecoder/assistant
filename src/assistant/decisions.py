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

import contextlib
import fcntl
import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = "decision/1"
QUEUE_SCHEMA = "decision-queue/1"

# Compact decisions.jsonl once it exceeds this: the log is rewritten as its
# folded snapshot (one record per id, terminal states preserved) and the old
# log rotates to decisions.jsonl.1 — same idiom as metering's rotation.
MAX_LOG_BYTES = 5_000_000

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

# Queue ordering PARTITIONS by lane so the escalate fail-safe lane is always at
# the top regardless of urgency tuning (design section 4 invariant, F6): the
# urgency span (50) exceeds the 40-point inter-lane band gap, so a staged+now
# item can score above an escalate+low one — the partition keeps escalate on
# top anyway, score only ordering WITHIN a lane.
_LANE_RANK = {"escalate": 0, "staged": 1, "digest": 2}


def lane_rank(lane) -> int:
    """Sort-partition rank for a lane: escalate < staged < digest < everything
    else. Lower sorts first (queue top). A non-string lane (unhashable
    list/dict from a hand-edited row) ranks last rather than crashing the sort
    (F3)."""
    return _LANE_RANK.get(lane, 3) if isinstance(lane, str) else 3


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


def parse_iso(ts) -> float | None:
    """ISO-8601 ('Z' or offset) → epoch seconds, or None."""
    if not isinstance(ts, str) or not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


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
    """Latest record per id, ordered by (epoch, file-order): the record with
    the highest epoch wins, file order breaking ties (transitions are appended
    after the create, so for a healthy log this IS last-write-wins; a
    hand-merged or clock-skewed log still folds to the newest state)."""
    out: dict[str, dict] = {}
    best: dict[str, tuple[int, int]] = {}
    for i, rec in enumerate(records):
        epoch = rec.get("epoch")
        key = (int(epoch) if isinstance(epoch, (int, float)) else 0, i)
        if rec["id"] not in best or key >= best[rec["id"]]:
            best[rec["id"]] = key
            out[rec["id"]] = rec
    return out


@contextlib.contextmanager
def _writer_lock():
    """The single-writer flock. EVERYTHING that reads-then-appends must do
    both inside ONE lock hold — a check-then-append with the read outside the
    lock can validate against a state another writer is about to change
    (todo-server accept racing pulse expire_open)."""
    d = decisions_dir()
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


def _maybe_compact() -> None:
    """MUST be called under _writer_lock. When the log exceeds MAX_LOG_BYTES,
    rewrite it as its folded snapshot (one record per id — terminal states
    are each id's latest record, so fold(before) == fold(after)) and rotate
    the full history to decisions.jsonl.1 (previous .1 is dropped, same as
    metering rotation)."""
    p = decisions_path()
    try:
        if not p.exists() or p.stat().st_size <= MAX_LOG_BYTES:
            return
    except OSError:
        return
    records = read_log()
    # This is the ONE moment the full history is in hand — capture each id's
    # true creation epoch (earliest appended record) and stamp it on the folded
    # snapshot, so the creation time survives after the create record is
    # discarded (F15). Without this, _created_epochs and expire_open would read
    # the surviving latest record's transition `epoch` as the creation and
    # corrupt ages/auto_coverage and silently restart TTLs.
    earliest: dict[str, int] = {}
    for rec in records:
        e = rec.get("epoch")
        if isinstance(e, (int, float)):
            earliest.setdefault(rec.get("id"), int(e))
    folded = fold(records)
    tmp = p.with_suffix(".jsonl.compact.tmp")
    with open(tmp, "w") as f:
        for rid, rec in folded.items():
            rec = dict(rec)
            rec["created_epoch"] = (rec.get("created_epoch")
                                    or earliest.get(rid) or rec.get("epoch"))
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    os.replace(p, p.with_name(p.name + ".1"))
    os.replace(tmp, p)


def _append_in_lock(rows: list[dict]) -> None:
    """MUST be called under _writer_lock: compact if oversized, append rows,
    rebuild queue.json — one queue rebuild per call however many rows."""
    if not rows:
        return
    _maybe_compact()
    with open(decisions_path(), "a") as f:
        for rec in rows:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    _write_queue(fold(read_log()))


def _append_locked(rows: list[dict]) -> None:
    """Append rows under the writer flock, then rebuild queue.json while the
    lock is still held (so the view can never interleave two writers)."""
    if not rows:
        return
    with _writer_lock():
        _append_in_lock(rows)


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
            lane_rank(rec.get("lane")),
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
                  resolution=None, goal_refs=None,
                  now: float | None = None) -> tuple[dict, bool]:
    """Create (or find) the decision for one event. Returns (record, created).

    goal_refs (Keel M4) links the decision to the goals that produced/relate to
    it, so the brief's goal_boost ranking term and staged_accept_rate metric can
    see it; default [] preserves every M2/M3 caller's behavior unchanged.

    created=False means the stable id already exists in the log — re-triaging
    the same (source, external_id, action_class) can never enqueue twice; the
    existing latest record is returned untouched. ONE exception: an EXPIRED
    decision re-opens when the event is a genuinely NEW sighting (event ts
    newer than the expiry) — expiry means "nobody acted in time", not "this
    can never matter again". Rejected/accepted/edited stay dead forever."""
    now = now if now is not None else time.time()
    source = event.get("source") or ""
    external_id = event.get("external_id") or event.get("id") or ""
    action_class = (action or {}).get("class") or ""
    dec_id = decision_id(source, external_id, action_class)
    reopened_from = None
    with _writer_lock():
        existing = fold(read_log()).get(dec_id)
        if existing is not None:
            if existing.get("status") == "expired":
                res_ts = parse_iso((existing.get("resolution") or {}).get("ts"))
                if res_ts is None:
                    res_ts = float(existing.get("epoch") or 0)
                ev_ts = parse_iso(event.get("ts"))
                if ev_ts is None:
                    ev_epoch = event.get("epoch")
                    ev_ts = float(ev_epoch) if isinstance(
                        ev_epoch, (int, float)) else None
                if ev_ts is not None and ev_ts > res_ts:
                    reopened_from = "expired"
            if reopened_from is None:
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
            # The creation epoch, stamped once and carried forward on every
            # transition and across log compaction (F15). `epoch` becomes the
            # latest TRANSITION time; created_epoch is the immutable birth time
            # the age/TTL math anchors on, so a compacted log (which keeps only
            # the latest record per id) can't misread a transition as the
            # creation and corrupt ages/auto_coverage or silently extend TTLs.
            "created_epoch": int(now),
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
            "goal_refs": list(goal_refs or []),
            "score": score_decision(lane, urgency),
            "urgency": urgency,
            "ttl_h": ttl_h,
            "status": status,
            "resolution": resolution,
        }
        _append_in_lock([record])
    if reopened_from is not None:
        _append_ledger({
            "ts": utc_iso(now), "epoch": int(now),
            "key": f"decision:{dec_id}:{reopened_from}->{record['status']}",
            "kind": "decision-transition",
            "ws_ref": (event.get("refs") or {}).get("ws_ref") or "(decisions)",
            "outcome": "verified",
            "evidence": (f"{reopened_from}->{record['status']} via re-sighting "
                         f"of {source}/{event.get('kind')}: "
                         f"{(event.get('title') or '')[:120]}"),
        })
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
    # Read-fold-validate-append is ONE critical section: the from_status
    # guard must see the log as it is at append time, or two concurrent
    # transitions (dashboard accept vs pulse expire) could both pass the
    # guard and silently flip each other.
    with _writer_lock():
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
        # Carry the creation epoch forward — `epoch` is now the transition time
        # (F15). Legacy records lacking the field fall back to their own epoch.
        record["created_epoch"] = latest.get("created_epoch", latest.get("epoch"))
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
        _append_in_lock([record])
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
    record carrying triage {suggested_lane, rationale} and NOTHING else. The
    suggestion is a PURE annotation (design section 5: "suggestions land on
    the decision record, never act"): the effective lane, score, TTL, card
    mirror, and digest all derive ONLY from policy-confirmed lanes, so an
    unmatched decision stays escalate — with its fail-safe card — until a
    human acts or a confirmed policy exists. Status is NOT touched either
    (the caller has already validated the lane against
    policy.TRIAGE_LANE_MAP)."""
    now = now if now is not None else time.time()
    with _writer_lock():
        latest = fold(read_log()).get(dec_id)
        if latest is None or latest.get("status") != OPEN:
            return None
        record = dict(latest)
        record["ts"] = utc_iso(now)
        record["epoch"] = latest.get("epoch")  # keep creation epoch for TTL/miner
        record["created_epoch"] = latest.get("created_epoch", latest.get("epoch"))
        record["triage"] = {"suggested_lane": suggested_lane,
                            "rationale": (rationale or "")[:300]}
        _append_in_lock([record])
    return record


def expire_open(now: float | None = None) -> list[dict]:
    """TTL sweep: open decisions whose ttl_h has elapsed since creation move
    to expired (a new appended record, ledgered like any transition). Lanes
    with ttl_h null (escalate by default) never expire here. Also wakes
    snoozed decisions whose wake_ts has passed (back to open, ledgered as
    ``decision:<id>:snoozed->open``).

    Batched: ONE lock hold, one log read, every expiry/wake appended in one
    pass, one queue.json rebuild — a big sweep costs one rebuild, not one
    per decision."""
    now = now if now is not None else time.time()
    expired: list[dict] = []
    rows: list[dict] = []
    ledger_rows: list[dict] = []
    with _writer_lock():
        records = read_log()
        created_epoch: dict[str, int] = {}
        for rec in records:
            # Prefer the preserved creation epoch (survives compaction, F15);
            # fall back to the earliest raw epoch for legacy rows. Using a
            # transition epoch here would silently RESTART the TTL clock after
            # a compaction and extend the decision's life past its ttl_h.
            ce = rec.get("created_epoch")
            if isinstance(ce, (int, float)):
                created_epoch[rec["id"]] = int(ce)
            else:
                created_epoch.setdefault(rec["id"], int(rec.get("epoch") or 0))
        for dec_id, latest in fold(records).items():
            status = latest.get("status")
            ws_ref = (latest.get("refs") or {}).get("ws_ref") or "(decisions)"
            if status == "snoozed":
                wake = latest.get("wake_ts")
                if isinstance(wake, (int, float)) and now >= wake:
                    record = dict(latest)
                    record["ts"] = utc_iso(now)
                    record["epoch"] = int(now)
                    record["status"] = OPEN
                    record["resolution"] = None
                    rows.append(record)
                    ledger_rows.append({
                        "ts": utc_iso(now), "epoch": int(now),
                        "key": f"decision:{dec_id}:snoozed->open",
                        "kind": "decision-transition",
                        "ws_ref": ws_ref,
                        "outcome": "verified",
                        "evidence": "snoozed->open via wake_ts",
                    })
                continue
            if status != OPEN:
                continue
            ttl_h = latest.get("ttl_h")
            if not isinstance(ttl_h, (int, float)):
                continue
            if now - created_epoch.get(dec_id, int(now)) > ttl_h * 3600:
                record = dict(latest)
                record["ts"] = utc_iso(now)
                record["epoch"] = int(now)
                record["status"] = "expired"
                record["resolution"] = {
                    "ts": utc_iso(now),
                    "via": "ttl",
                    "ledger_key": f"decision:{dec_id}:open->expired",
                }
                rows.append(record)
                expired.append(record)
                ledger_rows.append({
                    "ts": utc_iso(now), "epoch": int(now),
                    "key": f"decision:{dec_id}:open->expired",
                    "kind": "decision-transition",
                    "ws_ref": ws_ref,
                    "outcome": "verified",
                    "evidence": "open->expired via ttl",
                })
        _append_in_lock(rows)
    for entry in ledger_rows:
        _append_ledger(entry)
    return expired
