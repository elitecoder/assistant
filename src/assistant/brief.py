"""brief — the morning-brief builder (Keel M3).

The brief is a PURE DERIVATION over stores other components own:
decisions.jsonl/queue.json (M2), the actions ledger, the daily digest files,
the cost ledger + metrics (metering), world.json's event-spine health, the
noise budget's interrupt counts, connector heartbeats (none exist before M5 —
only fleet sources ride the spine today) and the goals store (absent until
M4 — the section degrades to a stub). build_brief(now) reads all of them and
emits one JSON document; given the same inputs and the same `now` it emits
the same bytes, so ~/.assistant/brief/brief-<date>.json is delete-safe and
rebuildable on demand (a delete-and-rebuild test diffs the bytes).

Four sections, per design section 5:
  1. queue    — open decisions ranked by a DETERMINISTIC scorer (lane base +
                urgency + age-decay freshness term; weights live in
                SCORE_CONFIG, unit-tested; no LLM anywhere near the ordering).
                Every row carries title, mechanical provenance (policy_id)
                and the recommended default action.
  2. handled_overnight — receipts from the actions ledger (auto_done rows
                with their rule ids, PR merges) plus closure tombstones when
                ~/.assistant/tombstones.jsonl exists (A1 amendment).
  3. digest   — the daily FYI rows, grouped by source, for the renderer to
                collapse.
  4. health   — event-source staleness from world.json, interrupts
                delivered/denied (the gate's ledger rows — silence is
                auditable, not assumed), $/day from the metering data,
                expired-unseen count, connector heartbeats.

Seen-ness lives in a SIDECAR (brief-<date>.seen.json, written by the
todo-server's /brief/seen route), never inside the brief file — the brief
stays a pure derivation. Neglect degradation (design section 5: "the
pressure valve is rules, not taps"): a brief unseen for >48h has its
non-escalate open decisions TTL'd to digest, and those expiries feed the
policy-proposal miner so unseen items graduate into rules.

The daily north-star metrics row appends to
~/.assistant/brief/brief-metrics.jsonl (a sidecar of metering's
metrics.jsonl, which is per-pulse; same append/read-tolerant conventions),
at most once per date.

Paths are computed per-call (not module constants) so tests that point $HOME
at a tmp dir see fresh paths even when this module stays cached in
sys.modules. Pure stdlib, no LLM, never closes workspaces.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import decisions, policy, triage

BRIEF_SCHEMA = "morning-brief/1"

# Deterministic queue-ranking weights (design section 5: "lane base +
# urgency + goal_boost + age decay, weights in config, unit-tested").
# `goal_boost` stays a flat baseline term (0) added unconditionally; the
# RANK-BASED boost (Keel M4) is a per-decision term computed from the goal a
# decision links to, via goal_boost_by_rank below and passed into brief_score.
# age_decay is a FRESHNESS term: a new decision gets +age_decay_cap, decaying
# age_decay_per_day points per day to 0.
#
# Both the freshness cap AND the goal-boost cap sit BELOW the 40-point lane
# bands, and — decisively — the queue ORDER is partitioned by lane_rank
# (_build_queue), so no urgency/freshness/goal_boost combination can ever lift
# a staged+goal decision above an escalate one. goal_boost tunes order WITHIN a
# lane only; it can never cross a band (design M4 item 8; M3 invariant F6).
SCORE_CONFIG = {
    "lane_base": {"escalate": 100, "staged": 60, "digest": 20},
    "urgency": {"now": 50, "high": 20, "low": 0},
    "goal_boost": 0,
    "goal_boost_by_rank": {"top": 12.0, "per_rank": 3.0, "floor": 3.0,
                           "cap": 20.0},
    "age_decay_per_day": 5.0,
    "age_decay_cap": 20.0,
}


def goal_boost_for_rank(rank, config: dict | None = None) -> float:
    """f(rank) → the deterministic goal-boost term (design M4 item 8). Rank 1
    (top goal) gets the most; each lower rank sheds per_rank points down to a
    floor; the whole thing is capped BELOW the 40-point lane band so a
    goal-linked staged decision can never out-band an escalate one. A non-int
    rank (no goal / malformed) → 0.0."""
    if not isinstance(rank, int):
        return 0.0
    cfg = (config or SCORE_CONFIG).get("goal_boost_by_rank") or {}
    top = cfg.get("top", 12.0)
    per = cfg.get("per_rank", 3.0)
    floor = cfg.get("floor", 3.0)
    cap = cfg.get("cap", 20.0)
    val = max(floor, top - max(0, rank - 1) * per)
    return float(min(val, cap))


def _record_goal_boost(rec: dict, goal_ranks: dict | None,
                       config: dict | None = None) -> float:
    """Goal boost for one decision: the BEST (lowest rank number) goal it links
    to. No goal_refs / no rank known → 0.0."""
    if not goal_ranks:
        return 0.0
    best_rank = None
    for gid in (rec.get("goal_refs") or []):
        r = goal_ranks.get(gid)
        if isinstance(r, int) and (best_rank is None or r < best_rank):
            best_rank = r
    return goal_boost_for_rank(best_rank, config) if best_rank is not None else 0.0

# Briefs unseen for longer than this have their non-escalate open decisions
# TTL'd to digest (and mined into policy proposals).
UNSEEN_TTL_H = 48
# First pulse at/after this local hour builds the day's brief (config.json
# {"brief": {"wake_hour": N}} overrides).
DEFAULT_WAKE_HOUR = 7

# How far back the receipts/digest/metrics windows look.
WINDOW_SEC = 24 * 3600
# Tail bound for the SMALL, latest-wins sidecars (metrics rows, seen files):
# their last N rows are all a read ever needs. The 24h WINDOWED reads of the
# shared actions ledger do NOT use this cap — a fixed line tail clips in-window
# rows on a >LEDGER_TAIL_LINES-event/24h incident day (the 865-denial class)
# and freezes an undercount into brief-metrics.jsonl forever (F11); they scan
# to the window boundary by timestamp via _read_jsonl_window instead.
LEDGER_TAIL_LINES = 4000
# Receipt kinds that count as "handled overnight". "merge-dispatched" is the
# kind pulse.py:1131 writes for an overnight PR merge (NOT "merge-pr") — the
# old name silently dropped every merge from the receipts (F16).
RECEIPT_KINDS = ("decision-auto-done", "merge-dispatched", "event-drop")

_REPO = Path(__file__).resolve().parents[2]


def _home() -> Path:
    return Path(os.environ.get("HOME", str(Path.home())))


def brief_dir() -> Path:
    return _home() / ".assistant" / "brief"


def brief_path(date_str: str) -> Path:
    return brief_dir() / f"brief-{date_str}.json"


def seen_path(date_str: str) -> Path:
    return brief_dir() / f"brief-{date_str}.seen.json"


def metrics_path() -> Path:
    return brief_dir() / "brief-metrics.jsonl"


def world_path() -> Path:
    return _home() / ".claude" / "cache" / "world.json"


def goals_path() -> Path:
    return _home() / ".claude" / "assistant-goals.json"


def tombstones_path() -> Path:
    return _home() / ".assistant" / "tombstones.jsonl"


def noise_budget_path() -> Path:
    return _home() / ".assistant" / "noise-budget.json"


def connectors_dir() -> Path:
    return _home() / ".assistant" / "connectors"


def config_path() -> Path:
    return _home() / ".assistant" / "comms" / "config.json"


def proposals_path() -> Path:
    return _home() / ".assistant" / "comms" / "proposals.jsonl"


def utc_iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def local_date(epoch: float) -> str:
    """The brief is Mukul's morning artifact, so its date key is the LOCAL
    calendar date (digest files stay UTC-keyed — they belong to triage)."""
    return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d")


def wake_hour() -> int:
    """config.json {"brief": {"wake_hour": N}}, default 7. Any read problem
    yields the default — the brief must build even with a mangled config."""
    try:
        raw = json.loads(config_path().read_text())
        v = (raw.get("brief") or {}).get("wake_hour")
        if isinstance(v, (int, float)) and 0 <= int(v) <= 23:
            return int(v)
    except (OSError, FileNotFoundError, json.JSONDecodeError, ValueError,
            AttributeError):
        pass
    return DEFAULT_WAKE_HOUR


def _read_json(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, FileNotFoundError, json.JSONDecodeError, ValueError):
        return None


def _read_jsonl_tail(path: Path, max_lines: int) -> list[dict]:
    """Last max_lines parseable dict rows of a JSONL file. Missing file or
    corrupt lines are skipped — one bad line never blanks a brief section."""
    try:
        lines = path.read_text().splitlines()[-max_lines:]
    except (OSError, FileNotFoundError):
        return []
    out: list[dict] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def _row_epoch(row: dict):
    e = row.get("epoch")
    if isinstance(e, (int, float)):
        return float(e)
    return decisions.parse_iso(row.get("ts"))


def _read_jsonl_window(path: Path, cutoff: float, now: float) -> list[dict]:
    """Every parseable dict row whose epoch/ts falls in [cutoff, now], scanned
    line-by-line with NO fixed line cap (F11). A LEDGER_TAIL_LINES tail taken
    before the timestamp filter clips in-window rows on a huge incident day
    (>4000 denials/receipts in 24h) and freezes that undercount into the
    metrics forever; scanning to the window boundary counts them all. Corrupt
    lines are skipped — one bad line never blanks a section."""
    try:
        fh = path.open()
    except (OSError, FileNotFoundError):
        return []
    out: list[dict] = []
    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict):
                continue
            epoch = _row_epoch(row)
            if epoch is None or epoch < cutoff or epoch > now:
                continue
            out.append(row)
    return out


# ─── scoring ─────────────────────────────────────────────────────────────────

def brief_score(rec: dict, created_epoch: float, now: float,
                config: dict | None = None, goal_boost: float = 0.0) -> float:
    """Deterministic brief rank for one open decision. Pure function of the
    record, its creation time and `now` — replayable, no LLM, unit-tested
    against SCORE_CONFIG. goal_boost (Keel M4) is the per-decision rank-based
    term the caller precomputes via _record_goal_boost; it defaults to 0.0 so
    every M3 caller scores identically."""
    cfg = config if config is not None else SCORE_CONFIG
    # Type-guard the lane/urgency keys: a hand-edited or torn record can carry
    # an unhashable list/dict where a lane string belongs, and dict.get() on an
    # unhashable key raises TypeError — one bad row must never blank the whole
    # brief (F3; the module docstring's "one bad line" contract). A non-string
    # lane/urgency simply scores 0, the same as a missing one.
    lane_key = rec.get("lane")
    urg_key = rec.get("urgency")
    lane = cfg["lane_base"].get(lane_key if isinstance(lane_key, str) else "", 0)
    urgency = cfg["urgency"].get(urg_key if isinstance(urg_key, str) else "", 0)
    age_days = max(0.0, (now - created_epoch) / 86400.0)
    freshness = max(0.0, cfg["age_decay_cap"]
                    - cfg["age_decay_per_day"] * age_days)
    return round(lane + urgency + cfg["goal_boost"] + goal_boost + freshness, 3)


# ─── section builders (each pure over its store) ─────────────────────────────

def _created_epochs(records: list[dict]) -> dict[str, float]:
    """The CREATION epoch per decision id — the age/TTL anchor. Prefers the
    record's `created_epoch` field (stamped at creation and preserved across
    log compaction, F15) so a compacted log that keeps only the latest
    transition record still reports the true birth time; falls back to the
    earliest `epoch` seen for legacy rows. Type-guarded so an ISO-string or
    otherwise non-numeric epoch defaults to 0 instead of crashing the whole
    brief (F3)."""
    out: dict[str, float] = {}
    for rec in records:
        rid = rec.get("id")
        if not isinstance(rid, str):
            continue
        ce = rec.get("created_epoch")
        if isinstance(ce, (int, float)):
            out[rid] = float(ce)
            continue
        epoch = rec.get("epoch")
        out.setdefault(rid, float(epoch) if isinstance(epoch, (int, float))
                       else 0.0)
    return out


def _build_queue(records: list[dict], now: float,
                 goal_ranks: dict | None = None) -> list[dict]:
    created = _created_epochs(records)
    rows: list[dict] = []
    for dec_id, rec in decisions.fold(records).items():
        if rec.get("status") != decisions.OPEN:
            continue
        rec_epoch = rec.get("epoch")
        c_epoch = created.get(dec_id, float(rec_epoch)
                              if isinstance(rec_epoch, (int, float)) else now)
        recommended = rec.get("recommended")
        default_action = "accept"
        default_label = "Accept"
        if isinstance(recommended, dict) and recommended.get("class"):
            default_label = f"Accept: {recommended['class']}"
        gboost = _record_goal_boost(rec, goal_ranks)
        rows.append({
            "id": dec_id,
            "title": rec.get("title") or dec_id,
            "source": rec.get("source"),
            "kind": rec.get("kind"),
            "lane": rec.get("lane"),
            "policy_id": rec.get("policy_id"),
            "urgency": rec.get("urgency"),
            "ttl_h": rec.get("ttl_h"),
            "goal_refs": rec.get("goal_refs") or [],
            "goal_boost": gboost,
            "created_ts": utc_iso(c_epoch),
            "age_h": round((now - c_epoch) / 3600.0, 1),
            "score": brief_score(rec, c_epoch, now, goal_boost=gboost),
            "recommended": recommended,
            "default_action": default_action,
            "default_label": default_label,
            "triage": rec.get("triage"),
            "ws_ref": (rec.get("refs") or {}).get("ws_ref"),
            "snippet": (rec.get("snippet") or "")[:200],
        })
    # Deterministic order, PARTITIONED BY LANE so the escalate fail-safe lane
    # is always at the top of the queue (design section 4 invariant). The
    # partition — not the raw score — enforces it: urgency + freshness can
    # otherwise lift a staged+now row above an escalate+low one across the
    # 40-point band and bury the fail-safe lane (F6). Within a lane: score
    # desc, then older first, then id.
    rows.sort(key=lambda r: (decisions.lane_rank(r.get("lane")),
                             -r["score"], r["created_ts"], r["id"]))
    return rows


def _build_receipts(now: float) -> list[dict]:
    """Handled-overnight receipts: the last 24h of ledger rows that represent
    work the machine finished without a human tap, plus closure tombstones
    when the A1 store exists."""
    cutoff = now - WINDOW_SEC
    out: list[dict] = []
    for row in _read_jsonl_window(decisions.ledger_path(), cutoff, now):
        if row.get("kind") not in RECEIPT_KINDS:
            continue
        out.append({
            "ts": row.get("ts"),
            "kind": row.get("kind"),
            "key": row.get("key"),
            "ws_ref": row.get("ws_ref"),
            "outcome": row.get("outcome"),
            "evidence": (row.get("evidence") or "")[:300],
        })
    for row in _read_jsonl_tail(tombstones_path(), LEDGER_TAIL_LINES):
        epoch = decisions.parse_iso(row.get("closed_ts"))
        if epoch is None or epoch < cutoff or epoch > now:
            continue
        out.append({
            "ts": row.get("closed_ts"),
            "kind": "ws-closed",
            "key": f"tombstone:{row.get('ws_ref')}",
            "ws_ref": row.get("ws_ref"),
            "outcome": row.get("outcome"),
            "evidence": (f"{row.get('title') or ''} — resurrect: "
                         f"{row.get('resurrect') or '?'}")[:300],
        })
    out.sort(key=lambda r: (r.get("ts") or "", r.get("key") or ""),
             reverse=True)
    return out


def _build_digest(now: float) -> dict[str, list[dict]]:
    """FYI rows from the last 24h of the daily digest files (today +
    yesterday, UTC-keyed like triage writes them), grouped by source."""
    cutoff = now - WINDOW_SEC
    grouped: dict[str, list[dict]] = {}
    for day_epoch in (now - 86400, now):
        day_file = triage.digest_dir() / f"{utc_iso(day_epoch)[:10]}.jsonl"
        for row in _read_jsonl_tail(day_file, LEDGER_TAIL_LINES):
            epoch = _row_epoch(row)
            if epoch is None or epoch < cutoff or epoch > now:
                continue
            src = row.get("source") or "(unknown)"
            grouped.setdefault(src, []).append({
                "ts": row.get("ts"),
                "kind": row.get("kind"),
                "title": (row.get("title") or "")[:200],
                "policy_id": row.get("policy_id"),
            })
    for src in grouped:
        grouped[src].sort(key=lambda r: r.get("ts") or "", reverse=True)
        grouped[src] = grouped[src][:50]
    return dict(sorted(grouped.items()))


def _interrupt_counts(now: float) -> dict:
    """Delivered/denied interrupt counts from the gate's ledger rows (last
    24h) + the live budget. The gate ledgers every decision it makes, so the
    brief can PROVE silence instead of assuming it."""
    cutoff = now - WINDOW_SEC
    delivered = denied = 0
    for row in _read_jsonl_window(decisions.ledger_path(), cutoff, now):
        kind = row.get("kind")
        if kind not in ("interrupt-delivered", "interrupt-denied"):
            continue
        if kind == "interrupt-delivered":
            delivered += 1
        else:
            denied += 1
    budget = _read_json(noise_budget_path())
    return {
        "delivered_24h": delivered,
        "denied_24h": denied,
        "budget": (budget or {}).get("budget") or {"page": 0, "notify": 0},
    }


def expired_unseen_count(records: list[dict], now: float) -> int:
    """Decisions that expired in the last 24h with zero view: TTL'd or
    unseen-degraded, excluding the digest lane (digest decisions TTL out in
    24h BY DESIGN — counting them would drown the mis-laning signal this
    metric exists to surface)."""
    cutoff = now - WINDOW_SEC
    n = 0
    for rec in decisions.fold(records).values():
        if rec.get("status") != "expired" or rec.get("lane") == "digest":
            continue
        res = rec.get("resolution") or {}
        if res.get("via") not in ("ttl", "brief-unseen"):
            continue
        epoch = decisions.parse_iso(res.get("ts"))
        if epoch is not None and cutoff <= epoch <= now:
            n += 1
    return n


def _connector_heartbeats(now: float) -> dict[str, dict]:
    """heartbeat.json per connector (M5), with the health VERDICT derived here
    (pure function of the heartbeat + `now`, so it is unit-testable and the
    renderer stays presentation-only). A stale last_poll or a past/near
    token_expiry marks the connector unhealthy, so a dead connector or an
    expiring token surfaces in the brief within one morning (design 4/9).
    Before M5 the directory doesn't exist and this returns {} — fleet sources
    ride the spine and show staleness via world.json's events section."""
    out: dict[str, dict] = {}
    d = connectors_dir()
    try:
        entries = sorted(p for p in d.iterdir() if p.is_dir())
    except (OSError, FileNotFoundError):
        return out
    for sub in entries:
        hb = _read_json(sub / "heartbeat.json")
        if not isinstance(hb, dict):
            continue
        last = hb.get("last_poll_epoch")
        stale_after = hb.get("stale_after_sec") or 900
        age = int(now - last) if isinstance(last, (int, float)) else None
        stale = age is None or age > stale_after
        texp = hb.get("token_expiry_epoch")
        token_expired = isinstance(texp, (int, float)) and now >= texp
        out[sub.name] = {
            "source": hb.get("source"),
            "last_poll": hb.get("last_poll"),
            "age_sec": age,
            "stale": stale,
            "token_expiry": hb.get("token_expiry"),
            "token_expired": bool(token_expired),
            "errors": hb.get("errors") or [],
            "ok": bool(hb.get("ok", True)) and not stale and not token_expired,
        }
    return out


def _cost_health(now: float) -> dict:
    """$/day from the metering data (bin/metering.py owns the math; import
    by path like the renderer does). Any failure degrades to zeros — cost
    visibility must never block the brief."""
    try:
        bin_dir = str(_REPO / "bin")
        if bin_dir not in sys.path:
            sys.path.insert(0, bin_dir)
        import metering  # noqa: PLC0415
        agg = metering.aggregate(
            metering.read_metrics(), now=int(now), window_days=7,
            cost_rows=metering.read_cost_ledger())
        return {
            "cost_per_day_usd": round(float(agg.get("cost_per_day_usd") or 0.0), 4),
            "cost_ledger_per_day_usd": round(
                float(agg.get("cost_ledger_per_day_usd") or 0.0), 4),
            "n_pulses_7d": int(agg.get("n_pulses") or 0),
        }
    except Exception:  # noqa: BLE001 — never load-bearing
        return {"cost_per_day_usd": 0.0, "cost_ledger_per_day_usd": 0.0,
                "n_pulses_7d": 0}


def _build_health(records: list[dict], now: float) -> dict:
    world = _read_json(world_path()) or {}
    events = world.get("events") or {}
    return {
        "event_sources": events.get("by_source") or {},
        "events_24h": events.get("total_24h"),
        "quarantine_pending": events.get("quarantine_pending"),
        "world_built_at": (world.get("_meta") or {}).get("built_at"),
        "interrupts": _interrupt_counts(now),
        "cost": _cost_health(now),
        "expired_unseen_24h": expired_unseen_count(records, now),
        "connectors": _connector_heartbeats(now),
    }


def _build_proposals(now: float) -> dict:
    """Confirmation-gated proposals awaiting Mukul (design section 5: "policy-
    proposal confirmation is itself a one-tap brief item"). Surfaces the
    low-volume human-confirmation channel — BOTH policy proposals and
    goal_update proposals (m15: an automation-filed goal status/rank/'looks
    done' change must be visible and confirmable, mirroring policy proposals —
    it must never silently apply, nor silently sit unseen). Folded by id so a
    confirmed/vetoed proposal drops off; only pending survives."""
    folded: dict = {}
    for row in _read_jsonl_tail(proposals_path(), 1000):
        if isinstance(row, dict) and row.get("id"):
            folded[row["id"]] = row
    out = {"goal_update": [], "policy": [], "n": 0}
    for row in folded.values():
        typ = row.get("type")
        if typ not in ("goal_update", "policy"):
            continue
        if row.get("status") not in ("pending", None):
            continue
        item = {"id": row.get("id"), "type": typ,
                "reason": (row.get("reason") or "")[:200],
                "source": row.get("source")}
        if typ == "goal_update":
            item["goal_id"] = row.get("goal_id")
            item["changes"] = row.get("changes")
        out[typ].append(item)
    out["n"] = len(out["goal_update"]) + len(out["policy"])
    return out


# ─── the brief ───────────────────────────────────────────────────────────────

def build_brief(now: float | None = None) -> dict:
    """The whole brief, as a dict. Pure derivation: no store is mutated, and
    the same stores + the same `now` produce the identical document."""
    now = now if now is not None else time.time()
    records = decisions.read_log()
    # Goal ranks feed the deterministic goal_boost ranking term. Read straight
    # from the store file (not an import of goals.py) so the brief stays a pure
    # derivation with no dependency on the planner module; a malformed/absent
    # store simply yields no boosts.
    goals = _read_json(goals_path())
    goal_ranks: dict = {}
    if isinstance(goals, dict) and isinstance(goals.get("goals"), list):
        for g in goals["goals"]:
            if isinstance(g, dict) and isinstance(g.get("id"), str) \
                    and isinstance(g.get("rank"), int):
                goal_ranks[g["id"]] = g["rank"]
    queue = _build_queue(records, now, goal_ranks=goal_ranks)
    counts_by_lane: dict[str, int] = {}
    for row in queue:
        lane = row.get("lane")
        if not isinstance(lane, str):  # unhashable/absent lane → bucket (F3)
            lane = "?"
        counts_by_lane[lane] = counts_by_lane.get(lane, 0) + 1
    if isinstance(goals, dict) and isinstance(goals.get("goals"), list):
        active = sum(1 for g in goals["goals"]
                     if isinstance(g, dict) and g.get("status") == "active")
        goals_section = {"available": True, "n_goals": len(goals["goals"]),
                         "n_active": active,
                         "paused": bool(goals.get("_paused", False))}
    else:
        goals_section = {"available": False,
                         "note": "goals store absent until M4"}
    digest = _build_digest(now)
    receipts = _build_receipts(now)
    return {
        "schema": BRIEF_SCHEMA,
        "date": local_date(now),
        "ts": utc_iso(now),
        "epoch": int(now),
        "wake_hour": wake_hour(),
        "queue": queue,
        "handled_overnight": receipts,
        "digest": digest,
        "health": _build_health(records, now),
        "goals": goals_section,
        "proposals": _build_proposals(now),
        "counts": {
            "open_decisions": len(queue),
            "by_lane": dict(sorted(counts_by_lane.items())),
            "handled_overnight": len(receipts),
            "digest_rows": sum(len(v) for v in digest.values()),
        },
    }


def write_brief(brief: dict) -> Path:
    """Atomic write of the brief file (tmp + os.replace, repo idiom). The tmp
    name is unique per writer (pid + uuid) so two builders racing on the same
    date — a pre-wake CLI rebuild and the pulse step — can't clobber each
    other's half-written tmp and blow up os.replace with FileNotFoundError
    (F5b)."""
    p = brief_path(brief["date"])
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f"{p.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(brief, indent=2, ensure_ascii=False) + "\n")
    os.replace(tmp, p)
    return p


def mark_seen(date_str: str | None = None,
              now: float | None = None) -> tuple[bool, str]:
    """Record that the Brief tab was viewed: seen_ts lands in the SIDECAR
    (brief-<date>.seen.json), never in the brief file itself — the brief
    stays a pure, delete-safe derivation. Returns (ok, message)."""
    now = now if now is not None else time.time()
    date_str = date_str or latest_brief_date()
    if not date_str:
        return False, "no brief exists yet"
    if not brief_path(date_str).exists():
        return False, f"no brief for {date_str}"
    p = seen_path(date_str)
    prior = _read_json(p)
    if isinstance(prior, dict) and prior.get("seen_ts"):
        return True, f"brief {date_str} already seen at {prior['seen_ts']}"
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({
        "date": date_str,
        "seen_ts": utc_iso(now),
        "seen_epoch": int(now),
        "via": "dashboard",
    }, indent=2) + "\n")
    os.replace(tmp, p)
    return True, f"brief {date_str} marked seen"


def latest_brief_date() -> str | None:
    dates = []
    try:
        for p in brief_dir().glob("brief-????-??-??.json"):
            dates.append(p.name[len("brief-"):-len(".json")])
    except OSError:
        return None
    return max(dates) if dates else None


# ─── daily metrics row (north star) ──────────────────────────────────────────

def compute_daily_metrics(brief: dict, records: list[dict] | None = None,
                          now: float | None = None) -> dict:
    """The north-star row (design section 11), computed mechanically:
      decisions_pending_at_brief  open human-required decisions at build time
      decisions_accepted_unedited accepted-with-default in the last 24h
      auto_coverage_pct           % of decisions created in the last 24h that
                                  terminated auto_done (zero human touch)
      expired_unseen              expired with zero view, last 24h (see
                                  expired_unseen_count for the digest carve-out)
      interrupts_delivered/denied the gate's last-24h ledger counts
    """
    now = now if now is not None else float(brief.get("epoch") or time.time())
    records = records if records is not None else decisions.read_log()
    cutoff = now - WINDOW_SEC
    # decisions_accepted_unedited is a RATIO, not a count (design section 11:
    # "accepted-with-default / all resolved" — the decision-theater detector).
    # Numerator: decisions accepted at their DEFAULT action (status "accepted",
    # never "edited") in the last 24h. Denominator: all human-resolved
    # decisions in the window — accepted, edited, rejected (the states a human
    # tap produces). auto_done (machine, no human touch) and expired (a
    # timeout, not a decision) are excluded: the metric measures whether the
    # human is really deciding or just rubber-stamping. Day-one guard: no
    # resolutions → 0.0, never a div-by-zero.
    accepted = edited = rejected = 0
    for rec in decisions.fold(records).values():
        status = rec.get("status")
        if status not in ("accepted", "edited", "rejected"):
            continue
        epoch = decisions.parse_iso((rec.get("resolution") or {}).get("ts"))
        if epoch is None or not (cutoff <= epoch <= now):
            continue
        if status == "accepted":
            accepted += 1
        elif status == "edited":
            edited += 1
        else:
            rejected += 1
    resolved = accepted + edited + rejected
    accepted_ratio = round(accepted / resolved, 4) if resolved else 0.0
    created = _created_epochs(records)
    folded = decisions.fold(records)
    new_ids = [i for i, e in created.items() if cutoff <= e <= now]
    auto = sum(1 for i in new_ids if folded[i].get("status") == "auto_done")
    interrupts = (brief.get("health") or {}).get("interrupts") or {}
    # Goal metrics (Keel M4). Fenced import: a broken/absent goals module must
    # never blank the north-star row (the row predates M4). goals.py is pure
    # stdlib and imports decisions (already loaded), so this cannot cycle.
    goals_progressed = 0
    staged_accept = 0.0
    try:
        from . import goals as _goals  # noqa: PLC0415
        goals_progressed = _goals.goals_progressed_overnight(now)
        staged_accept = _goals.staged_accept_rate(records, now)
    except Exception:  # noqa: BLE001 — never load-bearing
        pass
    # The north star counts only ACTIONABLE open decisions. digest-lane rows
    # are FYI churn that no human tap reduces (design section 5 keeps FYI a
    # separate collapsed section); counting them here would inflate the one
    # number the whole system optimizes downward (F14). They stay in the
    # rendered queue for actionability but are carved out of THIS metric,
    # mirroring expired_unseen_count's identical digest carve-out.
    pending = sum(1 for r in (brief.get("queue") or [])
                  if r.get("lane") != "digest")
    return {
        "date": brief["date"],
        "ts": utc_iso(now),
        "epoch": int(now),
        "decisions_pending_at_brief": pending,
        "decisions_accepted_unedited": accepted_ratio,
        "auto_coverage_pct": round(100.0 * auto / len(new_ids), 1) if new_ids else 0.0,
        "expired_unseen": int((brief.get("health") or {})
                              .get("expired_unseen_24h") or 0),
        "interrupts_delivered": int(interrupts.get("delivered_24h") or 0),
        "interrupts_denied": int(interrupts.get("denied_24h") or 0),
        "goals_progressed_overnight": int(goals_progressed),
        "staged_accept_rate": float(staged_accept),
    }


def metrics_lock_path() -> Path:
    return brief_dir() / "brief-metrics.lock"


@contextlib.contextmanager
def _metrics_lock():
    """The single-writer flock for brief-metrics.jsonl. The once-per-date
    check-then-append MUST happen inside ONE lock hold or two concurrent
    builders (a pulse step racing a CLI rebuild) both pass the check and
    double-book the north-star row (F5) — same idiom as decisions._writer_lock,
    and delete-safe: the lock file carries no state."""
    d = brief_dir()
    d.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(metrics_lock_path()), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


def append_daily_metrics(brief: dict, now: float | None = None) -> dict | None:
    """Append the day's row to brief-metrics.jsonl — at most once per date,
    so on-demand rebuilds never double-book the north star. The dedup read and
    the append run under one flock so concurrent builders can't both win the
    once-per-date race (F5)."""
    p = metrics_path()
    with _metrics_lock():
        for row in _read_jsonl_tail(p, LEDGER_TAIL_LINES):
            if row.get("date") == brief["date"]:
                return None
        row = compute_daily_metrics(brief, now=now)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a") as f:
            f.write(json.dumps(row) + "\n")
    return row


def read_daily_metrics() -> list[dict]:
    return _read_jsonl_tail(metrics_path(), LEDGER_TAIL_LINES)


# ─── neglect degradation ─────────────────────────────────────────────────────

def degrade_unseen(now: float | None = None,
                   ttl_h: float = UNSEEN_TTL_H) -> dict:
    """The pressure valve is rules, not taps (design section 5): every brief
    unseen for > ttl_h has its non-escalate OPEN decisions moved to expired
    (via decisions.transition — appended + ledgered like any transition) and
    mirrored into today's digest; the expiries then feed
    policy.mine_unseen_expiry_proposals so repeat offenders graduate into a
    confirmed digest rule instead of re-nagging. Escalate decisions are never
    degraded — neglect must not silently bury the fail-safe lane. Idempotent:
    transitions only fire on still-open decisions, digest appends dedup by
    event id, the miner dedups against pending proposals."""
    now = now if now is not None else time.time()
    out = {"briefs_checked": 0, "briefs_unseen": 0, "expired": [],
           "proposals": 0}
    try:
        briefs = sorted(brief_dir().glob("brief-????-??-??.json"))
    except OSError:
        return out
    # Seen-ness is PER DECISION, not per brief-date (F4). A decision seen in
    # ANY brief the user actually viewed is seen — so neglect of a later brief
    # can never expire something already reviewed, and an older superseded
    # brief (whose sidecar the renderer can no longer stamp) can never expire a
    # decision the user saw in today's brief. The set is derived purely from
    # the existing stores (brief queues + seen sidecars), so it stays
    # delete-safe/rebuildable. First pass: collect every decision id that
    # appeared in a viewed brief's queue.
    parsed: list[tuple[str, dict, bool]] = []
    seen_dec_ids: set[str] = set()
    for p in briefs:
        date_str = p.name[len("brief-"):-len(".json")]
        doc = _read_json(p)
        if not isinstance(doc, dict):
            continue
        seen = _read_json(seen_path(date_str))
        is_seen = isinstance(seen, dict) and bool(seen.get("seen_ts"))
        parsed.append((date_str, doc, is_seen))
        if is_seen:
            for row in doc.get("queue") or []:
                did = row.get("id")
                if isinstance(did, str):
                    seen_dec_ids.add(did)
    folded = None
    for date_str, doc, is_seen in parsed:
        out["briefs_checked"] += 1
        built_epoch = doc.get("epoch")
        if not isinstance(built_epoch, (int, float)):
            continue
        if now - built_epoch <= ttl_h * 3600:
            continue
        if is_seen:
            continue
        out["briefs_unseen"] += 1
        if folded is None:
            folded = decisions.fold(decisions.read_log())
        for row in doc.get("queue") or []:
            dec_id = row.get("id")
            if dec_id in seen_dec_ids:
                continue  # the user viewed this decision in some brief (F4)
            rec = folded.get(dec_id)
            if rec is None or rec.get("status") != decisions.OPEN:
                continue
            if rec.get("lane") == "escalate":
                continue
            new_rec, err = decisions.transition(
                dec_id, "expired", via="brief-unseen",
                note=f"brief {date_str} unseen >{int(ttl_h)}h", now=now)
            if err is not None:
                continue
            folded[dec_id] = new_rec
            out["expired"].append(dec_id)
            triage.append_digest({
                "id": rec.get("event_ref") or dec_id,
                "source": rec.get("source"),
                "kind": rec.get("kind"),
                "title": rec.get("title"),
            }, "brief-unseen", now=now)
    if out["briefs_unseen"]:
        try:
            rules, _invalid, _err = policy.load_policies()
            mined = policy.mine_unseen_expiry_proposals(
                decisions.read_log(), now=now, rules=rules)
            out["proposals"] = len(mined)
        except Exception:  # noqa: BLE001 — mining must never block degradation
            pass
    return out


# ─── pulse step ──────────────────────────────────────────────────────────────

def degrade_stamp_path(date_str: str) -> Path:
    return brief_dir() / f"degrade-{date_str}.done"


def pulse_step(now: float | None = None, log=None) -> dict:
    """The pulse's brief step: on the FIRST pulse at/after wake_hour (local
    time) it runs the daily unseen-degradation pass and, if today has no brief
    yet, builds + writes the brief and appends the daily metrics row. Every
    later pulse that day is a cheap no-op. On-demand rebuilds go through
    bin/build-morning-brief.py instead.

    Two ordering guarantees (F9/F10):
      - Degradation runs BEFORE build_brief, so the brief reflects the
        post-degrade state and never ships a queue row with a live one-tap
        button that degrade is about to expire out from under the user.
      - Degradation is gated by its OWN per-date stamp, NOT by the brief file
        existing. A pre-wake CLI build (or a crash after the brief is written)
        must not suppress the day's degradation; the two are decoupled. The
        stamp is delete-safe — degrade_unseen is idempotent, so a lost stamp
        only costs one harmless re-run."""
    now = now if now is not None else time.time()
    summary = {"built": False, "path": None, "metrics_row": False,
               "degrade": None}
    if datetime.fromtimestamp(now).hour < wake_hour():
        return summary
    date_str = local_date(now)
    stamp = degrade_stamp_path(date_str)
    if not stamp.exists():
        summary["degrade"] = degrade_unseen(now=now)
        try:
            stamp.parent.mkdir(parents=True, exist_ok=True)
            tmp = stamp.with_name(f"{stamp.name}.{os.getpid()}.tmp")
            tmp.write_text(utc_iso(now) + "\n")
            os.replace(tmp, stamp)
        except OSError:
            pass
    if brief_path(date_str).exists():
        return summary
    brief = build_brief(now=now)
    path = write_brief(brief)
    summary["built"] = True
    summary["path"] = str(path)
    summary["open_decisions"] = len(brief.get("queue") or [])
    summary["metrics_row"] = append_daily_metrics(brief, now=now) is not None
    return summary
