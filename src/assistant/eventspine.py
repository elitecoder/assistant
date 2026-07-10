"""eventspine — typed WorldEvent consumer for the ~/.assistant/inbox drop dir.

WHY THIS EXISTS: the old pulse step (`drain_inbox`) unlinked `pulse-*.json`
files WITHOUT READING THEM and ignored everything else — so the signal files
cmux-watcher atomically drops (`cmux-*.json`: needs_input, work_complete)
accumulated in the inbox forever and never influenced anything. The producer
side of the fleet contract worked; the consumer side was severed. This module
is the consumer: every inbox drop becomes a normalized, deduplicated
WorldEvent row in ~/.assistant/events.jsonl (schema "world-event/1") that
downstream Keel components (policy engine, decision queue, pick-ws-batch
promotion) can read and replay.

Pipeline per inbox file — ordering is load-bearing for crash safety:

    parse → archive raw copy → dedup → append events.jsonl → unlink

  - A file is NEVER unlinked before its content is safely elsewhere. Killed
    between archive and append? The inbox file is still there; the next drain
    reprocesses it (archive overwrite is idempotent, dedup is checked before
    append). Killed between append and unlink? The next drain sees the id in
    the dedup set (the index PLUS a tail scan of events.jsonl, which catches
    an append whose index write never landed) and just unlinks.
  - Malformed files are MOVED to a quarantine dir and ledgered — they never
    crash the pulse and never silently vanish.
  - Dedup key: sha256(source + ":" + external_id), retained 30 days.

Fleet-as-connector (connector #0, no producer changes):
  - cmux-watcher signal drops (`{event: "workspace_signal", ...}`) →
    external_id `cmux:<ws_ref>:<signal>:<ts-bucket>` (10-min bucket, wider
    than the watcher's 120s re-ping cooldown, so a still-blocked workspace
    is one event per bucket, not a ping storm).
  - Orphaned ~/.claude/cmux-crash-events/*.json drops from workspace-watcher
    → external_id `cmux:<ws_ref>:closed:<epoch>`. We do NOT own that dir
    (workspace-watcher writes it, the dashboard reads it), so those files are
    never unlinked — exactly-once is the dedup index's job, and we only scan
    files younger than the index retention so an expired entry can't re-admit
    an old file.
  - Already-normalized `world-event/1` drops (future connectors) pass through.

Exactly one consumer: `drain_typed_inbox` takes a pid-checked lockfile
(O_CREAT|O_EXCL). A lock whose pid is dead is reclaimed via rename — only one
contender's rename can succeed, so two processes can't both steal a stale
lock. If the lock is held by a live pid the drain is skipped, never blocked.

Paths are computed per-call (not module constants) so tests that point $HOME
at a tmp dir see fresh paths even when this module stays cached in
sys.modules. Pure stdlib, no LLM, never closes workspaces.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = "world-event/1"

# Dedup index retention. Anything older can't collide with a live producer:
# cmux signal buckets are minutes wide and crash-event ids embed their epoch.
DEDUP_RETENTION_SEC = 30 * 86400

# ts-bucket width for cmux workspace signals. Must exceed cmux-watcher's
# 120s per-(ws, signal) cooldown so repeated pings for the same blocked state
# collapse into one WorldEvent instead of one per ping.
SIGNAL_TS_BUCKET_SEC = 600

# Snippet cap from the world-event/1 store schema (≤2KB).
SNIPPET_MAX_CHARS = 2048

# How far back into events.jsonl the drain looks for ids whose dedup-index
# write may not have landed (crash between append and index write). Recent
# rows are all that window can contain, so a bounded tail read suffices.
EVENTS_TAIL_BYTES = 512_000


def _home() -> Path:
    return Path(os.environ.get("HOME", str(Path.home())))


def assistant_dir() -> Path:
    return _home() / ".assistant"


def inbox_dir() -> Path:
    return assistant_dir() / "inbox"


def events_path() -> Path:
    return assistant_dir() / "events.jsonl"


def spine_dir() -> Path:
    return assistant_dir() / "eventspine"


def raw_archive_dir() -> Path:
    return spine_dir() / "raw"


def quarantine_dir() -> Path:
    return spine_dir() / "quarantine"


def dedup_index_path() -> Path:
    return spine_dir() / "dedup-index.json"


def lock_path() -> Path:
    return spine_dir() / "consumer.lock"


def crash_events_dir() -> Path:
    return _home() / ".claude" / "cmux-crash-events"


def ledger_path() -> Path:
    return assistant_dir() / "actions-ledger.jsonl"


# ─── small helpers ──────────────────────────────────────────────────────────

def utc_iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s) -> float | None:
    """ISO-8601 ('Z' or offset) → epoch seconds, or None."""
    if not s or not isinstance(s, str):
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def event_id(source: str, external_id: str) -> str:
    """The stable dedup key: sha256 over source ':' external_id."""
    return hashlib.sha256(f"{source}:{external_id}".encode()).hexdigest()


def _snippet(text) -> str:
    return str(text or "")[:SNIPPET_MAX_CHARS]


def _append_ledger(entry: dict) -> None:
    """Best-effort actions-ledger row (same shape pulse.py appends). A ledger
    write failure must never block event consumption."""
    try:
        path = ledger_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass


def _append_event(path: Path, event: dict) -> None:
    """Append one WorldEvent row. Module-level (not inlined) so tests can
    inject a failure between archive and append to prove replay safety."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


# ─── normalization ──────────────────────────────────────────────────────────

def _base_event(*, ts_epoch: float, source: str, kind: str, external_id: str,
                actor=None, title: str = "", snippet: str = "", url=None,
                refs=None, raw_path=None) -> dict:
    return {
        "schema": SCHEMA,
        "id": event_id(source, external_id),
        "ts": utc_iso(ts_epoch),
        "epoch": int(ts_epoch),
        "source": source,
        "kind": kind,
        "external_id": external_id,
        "actor": actor,
        "title": (title or "")[:200],
        "snippet": _snippet(snippet),
        "url": url,
        "refs": dict(refs or {}),
        "raw_path": raw_path,
    }


def normalize_inbox_item(name: str, data, received_epoch: float) -> dict:
    """One parsed inbox file → one WorldEvent dict. Raises ValueError on any
    shape we don't recognize — the caller quarantines those (a file that
    parses but means nothing must surface, not vanish)."""
    if not isinstance(data, dict):
        raise ValueError("inbox item is not a JSON object")

    # Already-normalized drop from a future connector (connector contract:
    # `evt-<source>-<stamp>.json` carrying world-event/1). Recompute the id —
    # producers don't get to choose their dedup key.
    if data.get("schema") == SCHEMA:
        source = data.get("source")
        external_id = data.get("external_id")
        kind = data.get("kind")
        if not (isinstance(source, str) and source
                and isinstance(external_id, str) and external_id
                and isinstance(kind, str) and kind):
            raise ValueError("world-event/1 item missing source/kind/external_id")
        ts_epoch = parse_iso(data.get("ts")) or received_epoch
        return _base_event(
            ts_epoch=ts_epoch, source=source, kind=kind,
            external_id=external_id, actor=data.get("actor"),
            title=data.get("title") or "", snippet=data.get("snippet") or "",
            url=data.get("url"), refs=data.get("refs") or {},
        )

    # cmux-watcher signal drop (fleet-as-connector, zero producer changes).
    if data.get("event") == "workspace_signal":
        signal = data.get("signal_type")
        if not isinstance(signal, str) or not signal:
            raise ValueError("workspace_signal item missing signal_type")
        ws_ref = data.get("ws_ref") or "unknown"
        ts_epoch = parse_iso(data.get("ts")) or received_epoch
        bucket = int(ts_epoch) // SIGNAL_TS_BUCKET_SEC
        pattern = data.get("pattern_matched") or ""
        refs = {"ws_ref": ws_ref} if ws_ref != "unknown" else {}
        return _base_event(
            ts_epoch=ts_epoch, source="cmux", kind=signal,
            external_id=f"cmux:{ws_ref}:{signal}:{bucket}",
            title=f"{ws_ref} {signal}" + (f" ({pattern})" if pattern else ""),
            snippet=data.get("screen_snippet") or "", refs=refs,
        )

    # Legacy wake-up ping (`pulse-*.json`). No live producer writes these any
    # more, but a backlog must drain cleanly. Filename is the identity: each
    # ping was a distinct one-shot wake, not a re-observable external fact.
    if name.startswith("pulse-"):
        ts_epoch = parse_iso(data.get("ts")) or received_epoch
        return _base_event(
            ts_epoch=ts_epoch, source="pulse", kind="ping",
            external_id=f"pulse:{name}", title="legacy pulse ping",
            snippet=json.dumps(data)[:200],
        )

    raise ValueError("unrecognized inbox item shape")


_CRASH_NAME_EPOCH_RE = re.compile(r"-(\d{9,})\.json$")


def normalize_crash_event(path: Path, data) -> dict:
    """One workspace-watcher crash-event drop → one WorldEvent. The epoch in
    the filename (`<ref>-<epoch>.json`) is the stable identity: it never
    changes even though workspace-watcher rewrites the file body as the
    resume attempt progresses."""
    if not isinstance(data, dict):
        raise ValueError("crash event is not a JSON object")
    ws_ref = data.get("workspace_ref") or "workspace:?"
    m = _CRASH_NAME_EPOCH_RE.search(path.name)
    ts_epoch = float(m.group(1)) if m else (
        parse_iso(data.get("died_at")) or time.time())
    cause = data.get("cause") or "unknown"
    resume = data.get("resume") or {}
    refs = {"ws_ref": ws_ref} if "?" not in ws_ref else {}
    return _base_event(
        ts_epoch=ts_epoch, source="cmux", kind="workspace_closed",
        external_id=f"cmux:{ws_ref}:closed:{int(ts_epoch)}",
        title=f"{data.get('name') or ws_ref} closed ({cause})",
        snippet=f"cause={cause} resume={json.dumps(resume)[:400]}",
        url=data.get("last_pr"), refs=refs, raw_path=str(path),
    )


# ─── dedup index ────────────────────────────────────────────────────────────

def _load_dedup_index(now: float) -> dict:
    """{event_id: first_seen_epoch}, pruned to the retention window."""
    try:
        raw = json.loads(dedup_index_path().read_text())
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    cutoff = now - DEDUP_RETENTION_SEC
    out = {}
    for k, v in raw.items():
        try:
            if float(v) >= cutoff:
                out[k] = float(v)
        except (TypeError, ValueError):
            continue
    return out


def _write_dedup_index(index: dict) -> None:
    path = dedup_index_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(index))
    os.replace(tmp, path)


def _recent_event_ids(path: Path, max_bytes: int = EVENTS_TAIL_BYTES) -> set:
    """Ids from the tail of events.jsonl. Covers the crash window where a row
    was appended but the dedup-index write never landed — that row is by
    definition one of the most recent, so a bounded tail read catches it."""
    if not path.exists():
        return set()
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            tail = f.read().decode("utf-8", errors="replace")
    except OSError:
        return set()
    ids = set()
    for line in tail.splitlines():
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(d, dict) and d.get("id"):
            ids.add(d["id"])
    return ids


# ─── consumer lock ──────────────────────────────────────────────────────────

def _pid_alive(pid) -> bool:
    """True iff pid plausibly belongs to a live process. Unknown/permission
    cases count as alive — we only reclaim a lock on positive evidence of
    death, never on uncertainty."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OverflowError):
        return True
    return True


def _read_lock_pid(path: Path):
    try:
        return json.loads(path.read_text()).get("pid")
    except (OSError, json.JSONDecodeError, ValueError, AttributeError):
        return None


def acquire_consumer_lock() -> bool:
    """Take the single-consumer lock. Returns False when a live consumer
    holds it. A lock left by a dead pid (crashed drain) is reclaimed: we
    rename it aside first — os.rename on an already-stolen path raises, so
    exactly one of two simultaneous reclaimers wins; the loser loops back to
    O_EXCL and finds the winner's fresh, live lock."""
    path = lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            pid = _read_lock_pid(path)
            if _pid_alive(pid):
                return False
            grave = f"{path}.stale.{os.getpid()}"
            try:
                os.rename(str(path), grave)
            except OSError:
                continue  # another contender reclaimed first; retry O_EXCL
            try:
                os.unlink(grave)
            except OSError:
                pass
            continue
        with os.fdopen(fd, "w") as f:
            json.dump({"pid": os.getpid(), "ts": utc_iso(time.time())}, f)
        return True
    return False


def release_consumer_lock() -> None:
    """Release only a lock we own — never clobber another consumer's."""
    path = lock_path()
    if _read_lock_pid(path) == os.getpid():
        try:
            path.unlink()
        except OSError:
            pass


# ─── the drain ──────────────────────────────────────────────────────────────

def drain_typed_inbox(pulse_idx: int = 0, log=None, now=None) -> dict:
    """Consume the inbox + orphaned crash events into events.jsonl.

    Returns a summary dict; key `locked` is True when another live consumer
    held the lock (nothing was touched). Per-file failures never abort the
    drain: malformed → quarantine + ledger; IO failure on a well-formed file
    → left in the inbox for the next pulse (`deferred`)."""
    log = log or logging.getLogger("eventspine")
    now = now if now is not None else time.time()
    summary = {"locked": False, "events_appended": 0,
               "inbox_consumed": 0, "inbox_duplicates": 0,
               "inbox_quarantined": 0, "inbox_deferred": 0,
               "crash_appended": 0}

    if not acquire_consumer_lock():
        summary["locked"] = True
        log.info("eventspine: lock held by a live consumer — drain skipped")
        return summary
    try:
        index = _load_dedup_index(now)
        seen = set(index) | _recent_event_ids(events_path())
        _drain_inbox_files(summary, index, seen, pulse_idx, now, log)
        _drain_crash_events(summary, index, seen, pulse_idx, now, log)
        _write_dedup_index(index)
    finally:
        release_consumer_lock()
    return summary


def _archive_raw(src: Path, now: float) -> Path:
    """Copy the raw drop into the dated archive BEFORE anything can unlink
    it. Re-archiving after a crash overwrites the same path — idempotent."""
    day_dir = raw_archive_dir() / utc_iso(now)[:10]
    day_dir.mkdir(parents=True, exist_ok=True)
    dst = day_dir / src.name
    shutil.copy2(str(src), str(dst))
    return dst


def _quarantine(src: Path, err: str, pulse_idx: int, now: float, log) -> None:
    qdir = quarantine_dir()
    qdir.mkdir(parents=True, exist_ok=True)
    dst = qdir / f"{int(now)}-{src.name}"
    try:
        os.replace(str(src), str(dst))
    except OSError:
        shutil.copy2(str(src), str(dst))
        src.unlink()
    _append_ledger({
        "ts": utc_iso(now), "epoch": int(now), "pulse_idx": pulse_idx,
        "key": f"eventspine-quarantine-{src.name}",
        "kind": "eventspine-quarantine", "ws_ref": "(inbox)",
        "outcome": "failed",
        "evidence": f"quarantined {src.name} → {dst}: {err[:200]}",
    })
    log.warning("eventspine: quarantined %s: %s", src.name, err)


def _drain_inbox_files(summary: dict, index: dict, seen: set,
                       pulse_idx: int, now: float, log) -> None:
    inbox = inbox_dir()
    if not inbox.is_dir():
        return
    for p in sorted(inbox.glob("*.json")):
        try:
            data = json.loads(p.read_text())
            event = normalize_inbox_item(p.name, data, received_epoch=now)
        except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as e:
            _quarantine(p, str(e), pulse_idx, now, log)
            summary["inbox_quarantined"] += 1
            continue
        except OSError as e:
            # Read failure (vanished mid-drain, permissions): leave in place.
            log.warning("eventspine: cannot read %s (deferred): %s", p.name, e)
            summary["inbox_deferred"] += 1
            continue
        try:
            event["raw_path"] = str(_archive_raw(p, now))
            if event["id"] in seen:
                summary["inbox_duplicates"] += 1
            else:
                _append_event(events_path(), event)
                seen.add(event["id"])
                index[event["id"]] = now
                summary["events_appended"] += 1
                summary["inbox_consumed"] += 1
            p.unlink()
        except OSError as e:
            # Archive/append/unlink failed on a well-formed item. The inbox
            # file stays; dedup makes the next pulse's retry safe.
            log.warning("eventspine: consume of %s failed (deferred): %s",
                        p.name, e)
            summary["inbox_deferred"] += 1


def _drain_crash_events(summary: dict, index: dict, seen: set,
                        pulse_idx: int, now: float, log) -> None:
    cdir = crash_events_dir()
    if not cdir.is_dir():
        return
    mtime_cutoff = now - DEDUP_RETENTION_SEC
    for p in sorted(cdir.glob("*.json")):
        try:
            if p.stat().st_mtime < mtime_cutoff:
                continue  # older than the dedup memory — never re-admit
        except OSError:
            continue
        try:
            event = normalize_crash_event(p, json.loads(p.read_text()))
        except (ValueError, json.JSONDecodeError, OSError) as e:
            # Not ours to move — ledger once (keyed into the dedup index so
            # a broken file doesn't re-ledger every pulse) and skip.
            skip_id = event_id("cmux-crash-skip", p.name)
            if skip_id not in seen:
                seen.add(skip_id)
                index[skip_id] = now
                _append_ledger({
                    "ts": utc_iso(now), "epoch": int(now),
                    "pulse_idx": pulse_idx,
                    "key": f"eventspine-crash-skip-{p.name}",
                    "kind": "eventspine-crash-skip", "ws_ref": "(crash-events)",
                    "outcome": "failed",
                    "evidence": f"unparsable crash event {p} left in place: "
                                f"{str(e)[:200]}",
                })
                log.warning("eventspine: unparsable crash event %s: %s",
                            p.name, e)
            continue
        if event["id"] in seen:
            continue
        try:
            _append_event(events_path(), event)
        except OSError as e:
            log.warning("eventspine: crash-event append failed (retry next "
                        "pulse): %s", e)
            continue
        seen.add(event["id"])
        index[event["id"]] = now
        summary["events_appended"] += 1
        summary["crash_appended"] += 1
