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

    parse → dedup → archive raw copy → append events.jsonl → unlink

  - A file is NEVER unlinked before its content is safely elsewhere. Killed
    between archive and append? The inbox file is still there; the next drain
    reprocesses it (archive overwrite is idempotent, dedup is checked before
    append). Killed between append and unlink? The next drain sees the id in
    the dedup set (the index PLUS a tail scan of events.jsonl, which catches
    an append whose index write never landed) and just unlinks. A duplicate's
    content is already in events.jsonl by definition, so duplicates are
    unlinked without archiving — raw/ never accumulates dedup copies, and
    raw/<day>/ dirs older than RAW_RETENTION_DAYS are pruned each drain.
  - Malformed files are MOVED to a quarantine dir and ledgered — they never
    crash the pulse and never silently vanish.
  - Dedup key: sha256(source + ":" + external_id), retained 30 days.

Fleet-as-connector (connector #0, no producer changes):
  - cmux-watcher signal drops (`{event: "workspace_signal", ...}`) →
    external_id `cmux:<ws_ref>:<signal>:<ts-bucket>:<content-hash>`. The
    10-min bucket collapses identical re-pings for one blocked state; the
    content hash (pattern + snippet) keeps two genuinely different signals
    in the same bucket distinct — the watcher is edge-triggered, so a
    swallowed signal is never re-sent. A null ws_ref keeps the drop filename
    stem in the id so distinct unresolved workspaces never collapse.
  - Orphaned ~/.claude/cmux-crash-events/*.json drops from workspace-watcher
    → external_id `cmux:<ws_ref>:closed:<epoch>`. We do NOT own that dir
    (workspace-watcher writes it, the dashboard reads it), so those files are
    never unlinked — exactly-once is the dedup index's job, and we only scan
    files younger than the index retention so an expired entry can't re-admit
    an old file.
  - Already-normalized `world-event/1` drops (future connectors) pass through.

Exactly one consumer: `drain_typed_inbox` holds fcntl.flock(LOCK_EX|LOCK_NB)
on a persistent lock file. The kernel releases the lock the instant the
holder dies (including SIGKILL), so there is no pid heuristic, no staleness
window, and no reclaim path to race. The lock file's {pid, ts} content is
observability only — the kernel lock is the sole authority. If another live
consumer holds it the drain is skipped, never blocked.

Paths are computed per-call (not module constants) so tests that point $HOME
at a tmp dir see fresh paths even when this module stays cached in
sys.modules. Pure stdlib, no LLM, never closes workspaces.
"""
from __future__ import annotations

import fcntl
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
# collapse into one WorldEvent instead of one per ping. Distinct signals in
# the same bucket stay distinct via the content hash in the external_id.
SIGNAL_TS_BUCKET_SEC = 600

# raw/<YYYY-MM-DD>/ archive dirs older than this are pruned each drain.
RAW_RETENTION_DAYS = 30

# A crash-event drop that fails to parse is skipped SILENTLY while younger
# than this — workspace-watcher rewrites those files non-atomically, so a
# fresh JSONDecodeError is usually a healthy file caught mid-write. Only a
# persistently-unparseable (old) file earns its once-ledger row.
CRASH_SKIP_GRACE_SEC = 120

# Snippet cap from the world-event/1 store schema (≤2KB). The cap is on the
# ENCODED UTF-8 byte length, not code points (N3): a code-point cap lets a
# snippet of multi-byte characters (emoji, CJK) blow past 2KB — up to 4× — in
# the store. SNIPPET_MAX_CHARS is retained as the byte budget (same 2048 value,
# so an ASCII snippet is unchanged) and named for back-compat with callers.
SNIPPET_MAX_CHARS = 2048
SNIPPET_MAX_BYTES = SNIPPET_MAX_CHARS

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
    s = str(text or "")
    b = s.encode("utf-8")
    if len(b) <= SNIPPET_MAX_BYTES:
        return s
    # Truncate on the encoded bytes, then drop any partial trailing multi-byte
    # sequence the cut left behind (errors="ignore") so the result is valid
    # UTF-8 and never exceeds the byte budget.
    return b[:SNIPPET_MAX_BYTES].decode("utf-8", "ignore")


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
    inject a failure between archive and append to prove replay safety.

    Repairs a torn tail first: a crash mid-append can leave a partial line
    with no trailing newline; appending straight onto it would glue two rows
    together, making BOTH unreadable (and invisible to the tail-scan dedup).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+b") as f:  # a+: writes always append, reads may seek
        f.seek(0, os.SEEK_END)
        if f.tell() > 0:
            f.seek(-1, os.SEEK_END)
            if f.read(1) != b"\n":
                f.write(b"\n")
        f.write((json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8"))


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
            # Preserve the PRODUCER's raw_path (N2): a connector already
            # archived the real UPSTREAM payload under its own raw/ dir and
            # pointed raw_path at it. Re-minting must carry that through so the
            # canonical events.jsonl row references the upstream payload, not a
            # normalized copy the spine would otherwise mint below.
            raw_path=data.get("raw_path"),
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
        snippet = data.get("screen_snippet") or ""
        # Content hash: identical re-pings (same pattern + screen) in one
        # bucket still collapse, but two genuinely different signals in the
        # same bucket (e.g. two different questions 8 min apart) stay
        # distinct. The producer is edge-triggered — it never re-sends a
        # signal the bucket swallowed, so over-dedup here loses events.
        content_h = hashlib.sha256(
            f"{pattern}\n{snippet}".encode("utf-8", "replace")).hexdigest()[:12]
        # An unresolved ws_ref must not collapse different workspaces into
        # one "unknown" identity — key those on the drop filename instead.
        id_ws = ws_ref if data.get("ws_ref") else f"unknown/{Path(name).stem}"
        refs = {"ws_ref": ws_ref} if ws_ref != "unknown" else {}
        return _base_event(
            ts_epoch=ts_epoch, source="cmux", kind=signal,
            external_id=f"cmux:{id_ws}:{signal}:{bucket}:{content_h}",
            title=f"{ws_ref} {signal}" + (f" ({pattern})" if pattern else ""),
            snippet=snippet, refs=refs,
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
    ts_epoch = float(m.group(1)) if m else parse_iso(data.get("died_at"))
    if ts_epoch is None:
        # No filename epoch AND no parseable died_at: there is no stable
        # identity to mint. A time.time() fallback would create a fresh id
        # (= one duplicate row) EVERY drain, forever. Raise so the caller
        # routes the file to the ledger-once skip path instead.
        raise ValueError("crash event has no filename epoch or died_at "
                         "(no stable identity)")
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
#
# fcntl.flock on a persistent lock file. The kernel owns liveness: the lock
# vanishes with the holder's fd on ANY death (clean exit, crash, SIGKILL), so
# there is no pid heuristic to fool (pid reuse) and no reclaim path to race
# (the old O_EXCL + pid-check + rename scheme let a contender steal a LIVE
# lock between its pid check and its rename). The file itself is never
# unlinked — unlink would let two contenders lock two different inodes of the
# same path. Its {pid, ts} content is observability only.

_lock_fd: int | None = None  # held open (and flocked) while we own the lock


def acquire_consumer_lock() -> bool:
    """Take the single-consumer lock. Returns False when another live
    consumer holds it — never blocks, never steals."""
    global _lock_fd
    path = lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o644)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return False
    try:  # who-holds-it breadcrumb; the kernel lock is the authority
        os.ftruncate(fd, 0)
        os.write(fd, json.dumps(
            {"pid": os.getpid(), "ts": utc_iso(time.time())}).encode())
    except OSError:
        pass
    _lock_fd = fd
    return True


def release_consumer_lock() -> None:
    """Drop the flock we hold (no-op when we don't). The lock file stays —
    only the kernel lock is released."""
    global _lock_fd
    if _lock_fd is None:
        return
    try:
        fcntl.flock(_lock_fd, fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        os.close(_lock_fd)
    except OSError:
        pass
    _lock_fd = None


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
        _prune_raw_archive(now, log)
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


_RAW_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _prune_raw_archive(now: float, log) -> None:
    """Delete raw/<YYYY-MM-DD>/ day dirs older than RAW_RETENTION_DAYS.
    Cheap lexicographic dirname compare (the names are ISO dates); raw/ is a
    crash-forensics buffer, not a permanent archive."""
    root = raw_archive_dir()
    if not root.is_dir():
        return
    cutoff = utc_iso(now - RAW_RETENTION_DAYS * 86400)[:10]
    for d in root.iterdir():
        if d.is_dir() and _RAW_DAY_RE.match(d.name) and d.name < cutoff:
            try:
                shutil.rmtree(str(d))
            except OSError as e:
                log.warning("eventspine: raw-archive prune of %s failed: %s",
                            d.name, e)


def _quarantine(src: Path, err: str, pulse_idx: int, now: float, log) -> bool:
    """Move a malformed drop to quarantine + ledger it. Returns False when
    even the copy-fallback failed (file left in the inbox for the next
    drain) — a quarantine failure must never abort the whole drain."""
    qdir = quarantine_dir()
    qdir.mkdir(parents=True, exist_ok=True)
    dst = qdir / f"{int(now)}-{src.name}"
    try:
        os.replace(str(src), str(dst))
    except OSError:
        try:
            shutil.copy2(str(src), str(dst))
            src.unlink()
        except OSError as e2:
            _append_ledger({
                "ts": utc_iso(now), "epoch": int(now), "pulse_idx": pulse_idx,
                "key": f"eventspine-quarantine-{src.name}",
                "kind": "eventspine-quarantine", "ws_ref": "(inbox)",
                "outcome": "failed",
                "evidence": f"quarantine of {src.name} itself failed "
                            f"(left in inbox): {str(e2)[:200]}",
            })
            log.warning("eventspine: quarantine of %s failed (left in "
                        "inbox): %s", src.name, e2)
            return False
    _append_ledger({
        "ts": utc_iso(now), "epoch": int(now), "pulse_idx": pulse_idx,
        "key": f"eventspine-quarantine-{src.name}",
        "kind": "eventspine-quarantine", "ws_ref": "(inbox)",
        "outcome": "failed",
        "evidence": f"quarantined {src.name} → {dst}: {err[:200]}",
    })
    log.warning("eventspine: quarantined %s: %s", src.name, err)
    return True


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
            if _quarantine(p, str(e), pulse_idx, now, log):
                summary["inbox_quarantined"] += 1
            else:
                summary["inbox_deferred"] += 1
            continue
        except OSError as e:
            # Read failure (vanished mid-drain, permissions): leave in place.
            log.warning("eventspine: cannot read %s (deferred): %s", p.name, e)
            summary["inbox_deferred"] += 1
            continue
        try:
            if event["id"] in seen:
                # Refresh the index even on a duplicate sighting: a mid-drain
                # crash may have left this id protected only by the 512KB
                # tail scan, and never-unlinked producers re-drop after tail
                # rollover — the refresh restores the 30-day protection.
                index[event["id"]] = now
                # Content already lives in events.jsonl (that is what a
                # duplicate id means) — dispose without archiving so raw/
                # never accumulates dedup copies.
                p.unlink()
                summary["inbox_duplicates"] += 1
            else:
                # A producer that already archived the real upstream payload
                # (connector drops) set raw_path in the drop — keep it (N2).
                # Only self-archive the inbox file when no producer raw_path
                # exists (cmux signals, legacy pulse pings), where the inbox
                # file IS the closest thing to a raw payload.
                if not event.get("raw_path"):
                    event["raw_path"] = str(_archive_raw(p, now))
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
            # workspace-watcher rewrites these files non-atomically, so a
            # parse failure on a FRESH file is usually a healthy file caught
            # mid-write: skip silently and retry next pulse. Only a
            # persistently-broken file earns its once-ledger row.
            try:
                if now - p.stat().st_mtime < CRASH_SKIP_GRACE_SEC:
                    continue
            except OSError:
                continue
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
            # Refresh the index on every duplicate sighting: crash files are
            # never unlinked, so once the row rolls out of the events.jsonl
            # tail window only the index stands between this file and a
            # duplicate append — and workspace-watcher keeps bumping the
            # file's mtime past any first-seen boundary.
            index[event["id"]] = now
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
