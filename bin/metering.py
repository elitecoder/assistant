#!/usr/bin/env python3
"""metering — per-pulse cost/behavior metrics for the Assistant fleet.

Makes the fleet's spend and Observer behavior MEASURABLE so a regression like
"cadence quadrupled" or "cost tripled" is visible on the dashboard the next
morning instead of a month later on a bill. Pure stdlib, no LLM, never
load-bearing: pulse.py wraps every call in try/except so a broken metering
module can never stop a pulse.

One JSONL record is appended to ~/.assistant/metrics.jsonl per pulse:

    {
      "ts": "2026-07-09T12:00:00Z", "epoch": 1783000000, "pulse_idx": 42,
      "observer_called": true, "batch_size": 8,
      "model": "us.anthropic.claude-sonnet-4-6[1m]", "duration_s": 41.2,
      "tokens_in": 91000, "tokens_out": 1800, "cost_usd_est": 0.30,
      "usage_source": "cli",            # "cli" (real) | "estimated" | "mixed"
      "verdicts": {"active": 6, "needs_user": 2},
      "verdict_changes": 1,             # ws whose verdict differs from last pulse
                                        # (null when the prev-verdict snapshot failed)
      "synthesized": 0,                 # ws given a fallback 'active' verdict this
                                        # pulse (Observer timeout/batch error)
      "actions": {"noop": 6, "emit-card": 2}
    }

Token/cost capture: the Observer subprocess runs `claude --print
--output-format json`, whose stdout is a single result envelope carrying
`usage` (input/output/cache token counts) and `total_cost_usd` — those are the
REAL numbers and are used when parseable. When the envelope is missing or
unparsable (old CLI, timeout, killed run) we fall back to a chars/4 estimate
from the prompt and stdout sizes and mark the record "estimated".

Previous-verdict comparison reuses the observer-summaries store
(~/.assistant/observer-summaries/<ws>.json, same files build-ws-context.py
reads) — the summary on disk before this pulse's save IS the previous verdict,
so no extra state file is needed.

Paths are computed per-call (not module constants) so tests that reload
callers with a different $HOME see fresh paths even when this module stays
cached in sys.modules.
"""
from __future__ import annotations

import json
import os
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

# Rotate metrics.jsonl once it exceeds this (one ~350-byte record per 5-min
# pulse ≈ 100KB/day, so 5MB is ~50 days of history). Old file is kept as
# metrics.jsonl.1 and read_metrics() reads both, so the 7-day dashboard
# window survives a rotation with margin to spare.
MAX_METRICS_BYTES = 5_000_000

# $ per 1M tokens (input, output), matched by substring against the model id.
# NOTE: estimates only — the real number comes from the CLI's total_cost_usd
# when available. The [1m] long-context tier can bill input above 200K ctx at
# a premium, so the estimated cost is a floor, not an exact figure.
_PRICE_PER_MTOK = (
    ("opus", (5.0, 25.0)),
    ("haiku", (1.0, 5.0)),
    ("sonnet", (3.0, 15.0)),
)
_DEFAULT_PRICE = (3.0, 15.0)  # sonnet rates — the fleet's Observer default


def _home() -> Path:
    return Path(os.environ.get("HOME", str(Path.home())))


def metrics_path() -> Path:
    return _home() / ".assistant/metrics.jsonl"


def cost_ledger_path() -> Path:
    return _home() / ".assistant/cost-ledger.jsonl"


def summaries_dir() -> Path:
    return _home() / ".assistant/observer-summaries"


def utc_iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ─── token/cost capture ─────────────────────────────────────────────────────

def estimate_tokens(n_chars: int) -> int:
    """Crude chars/4 token estimate — only used when the CLI envelope is
    unavailable; the record is marked usage_source='estimated'."""
    return max(0, int(n_chars) // 4)


def price_per_mtok(model: str) -> tuple[float, float]:
    m = (model or "").lower()
    for needle, price in _PRICE_PER_MTOK:
        if needle in m:
            return price
    return _DEFAULT_PRICE


def estimate_cost_usd(tokens_in: int, tokens_out: int, model: str) -> float:
    p_in, p_out = price_per_mtok(model)
    return tokens_in / 1e6 * p_in + tokens_out / 1e6 * p_out


def parse_cli_result(stdout: str) -> dict | None:
    """Parse the `claude --print --output-format json` result envelope.

    Returns {"tokens_in", "tokens_out", "cost_usd"} or None when stdout is
    not a usage-bearing envelope (plain-text CLI, timeout, empty). tokens_in
    includes cache-creation/read tokens — it measures how much context the
    Observer consumed, which is the cadence signal we care about."""
    try:
        d = json.loads((stdout or "").strip())
    except Exception:
        return None
    if not isinstance(d, dict) or not isinstance(d.get("usage"), dict):
        return None
    u = d["usage"]

    def _i(key: str) -> int:
        v = u.get(key)
        return int(v) if isinstance(v, (int, float)) else 0

    tokens_in = _i("input_tokens") + _i("cache_creation_input_tokens") + _i("cache_read_input_tokens")
    tokens_out = _i("output_tokens")
    cost = d.get("total_cost_usd")
    return {
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "cost_usd": float(cost) if isinstance(cost, (int, float)) else None,
    }


def observer_usage(stdout: str, prompt_chars: int, model: str) -> dict:
    """Usage for ONE Observer subprocess: real CLI numbers when the stdout
    envelope parses, chars/4 estimate otherwise (and says so in `source`)."""
    parsed = parse_cli_result(stdout)
    if parsed is not None:
        cost = parsed["cost_usd"]
        if cost is None:
            cost = estimate_cost_usd(parsed["tokens_in"], parsed["tokens_out"], model)
        return {"tokens_in": parsed["tokens_in"], "tokens_out": parsed["tokens_out"],
                "cost_usd": round(cost, 6), "source": "cli"}
    tokens_in = estimate_tokens(prompt_chars)
    tokens_out = estimate_tokens(len(stdout or ""))
    return {"tokens_in": tokens_in, "tokens_out": tokens_out,
            "cost_usd": round(estimate_cost_usd(tokens_in, tokens_out, model), 6),
            "source": "estimated"}


def sum_usage(usages: list[dict]) -> dict:
    """Combine per-batch usages into one pulse total. source is 'cli' only
    when every batch reported real numbers."""
    if not usages:
        return {"tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0, "source": "estimated"}
    sources = {u.get("source", "estimated") for u in usages}
    source = "cli" if sources == {"cli"} else ("estimated" if sources == {"estimated"} else "mixed")
    return {
        "tokens_in": sum(int(u.get("tokens_in") or 0) for u in usages),
        "tokens_out": sum(int(u.get("tokens_out") or 0) for u in usages),
        "cost_usd": round(sum(float(u.get("cost_usd") or 0.0) for u in usages), 6),
        "source": source,
    }


def append_cost_row(*, caller: str, model: str, usage: dict, wall_ms: int,
                    path: Path | None = None) -> None:
    """One per-call row in ~/.assistant/cost-ledger.jsonl (design section 3:
    {ts, caller, model, tokens_in, tokens_out, est_usd, wall_ms}). Every LLM
    caller beyond the Observer (triage, later strategist/drafter) appends here
    so $/day per caller is derivable. Single-write append like the actions
    ledger; the read side must tolerate a torn line."""
    p = path if path is not None else cost_ledger_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "ts": utc_iso(int(time.time())),
        "caller": caller,
        "model": model,
        "tokens_in": int(usage.get("tokens_in") or 0),
        "tokens_out": int(usage.get("tokens_out") or 0),
        "est_usd": float(usage.get("cost_usd") or 0.0),
        "usage_source": usage.get("source", "estimated"),
        "wall_ms": int(wall_ms),
    }
    with open(p, "a") as f:
        f.write(json.dumps(row) + "\n")


# ─── previous-verdict comparison ────────────────────────────────────────────

def load_prev_verdicts(ws_refs: list[str], directory: Path | None = None) -> dict[str, str]:
    """Verdict-per-ws from the observer-summaries store as it stands BEFORE
    this pulse's save_summary overwrites it. Missing/corrupt/verdict-less
    files are skipped — a ws with no prior verdict can't count as changed."""
    d = directory if directory is not None else summaries_dir()
    out: dict[str, str] = {}
    for ws_ref in ws_refs:
        p = d / f"{ws_ref.replace(':', '_')}.json"
        try:
            data = json.loads(p.read_text())
        except Exception:
            continue
        verdict = data.get("verdict")
        if isinstance(verdict, str) and verdict:
            out[ws_ref] = verdict
    return out


def count_verdict_changes(prev: dict[str, str], new: dict[str, str]) -> int:
    """Workspaces judged this pulse whose verdict differs from last pulse.
    Only ws present in BOTH maps count — a brand-new ws is not a 'change'."""
    return sum(1 for ws, v in new.items() if ws in prev and prev[ws] != v)


# ─── record build/append/read ───────────────────────────────────────────────

def build_pulse_record(*, epoch: int, pulse_idx: int, observer_called: bool,
                       batch_size: int, model: str | None, duration_s: float,
                       usage: dict, new_verdicts: dict[str, str],
                       verdict_changes: int | None, actions: list[dict],
                       synthesized: int = 0, skipped: int = 0) -> dict:
    """One metrics.jsonl record. `verdict_changes` is None when the
    previous-verdict snapshot failed — the comparison is degraded but the
    cost/usage numbers are still real and still recorded. `synthesized`
    counts workspaces whose verdict this pulse was a synthesized 'active'
    fallback (Observer timeout/batch error), so failure pulses are visible
    in the data; synthesized verdicts never count as verdict changes.
    `skipped` counts workspaces the no-change skip carried forward without
    an Observer call this pulse (batch_size already excludes them)."""
    return {
        "ts": utc_iso(epoch),
        "epoch": epoch,
        "pulse_idx": pulse_idx,
        "observer_called": bool(observer_called),
        "batch_size": int(batch_size),
        "model": model,
        "duration_s": round(float(duration_s), 2),
        "tokens_in": int(usage.get("tokens_in") or 0),
        "tokens_out": int(usage.get("tokens_out") or 0),
        "cost_usd_est": float(usage.get("cost_usd") or 0.0),
        "usage_source": usage.get("source", "estimated"),
        "verdicts": dict(Counter(new_verdicts.values())),
        "verdict_changes": None if verdict_changes is None else int(verdict_changes),
        "synthesized": int(synthesized),
        "skipped": int(skipped),
        "actions": dict(Counter(a.get("kind", "unknown") for a in actions)),
    }


def append_metric(record: dict, path: Path | None = None,
                  max_bytes: int = MAX_METRICS_BYTES) -> None:
    """Append one record; rotate to <name>.1 first when oversized. Appends
    are single-write (like the actions ledger) — the READ side tolerates a
    torn/corrupt line, so no tmpfile dance is needed for an append-only log."""
    p = path if path is not None else metrics_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        if p.exists() and p.stat().st_size > max_bytes:
            os.replace(p, p.with_name(p.name + ".1"))
    except OSError:
        pass  # rotation is best-effort; the append below still lands
    with open(p, "a") as f:
        f.write(json.dumps(record) + "\n")


def _record_epoch(rec: dict) -> int | None:
    e = rec.get("epoch")
    if isinstance(e, (int, float)):
        return int(e)
    ts = rec.get("ts")
    if isinstance(ts, str):
        try:
            return int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp())
        except Exception:
            return None
    return None


def read_metrics(path: Path | None = None) -> list[dict]:
    """All parseable records, oldest-first, spanning BOTH the rotated file
    (<name>.1, read first — it holds the older records) and the active file,
    so a rotation never truncates the dashboard's 7-day window. A missing or
    unreadable file is skipped; corrupt lines (torn append, manual edit) are
    skipped — one bad line never blanks the dashboard."""
    p = path if path is not None else metrics_path()
    out: list[dict] = []
    for f in (p.with_name(p.name + ".1"), p):
        if not f.exists():
            continue
        try:
            lines = f.read_text().splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict):
                out.append(rec)
    return out


# ─── dashboard aggregation ──────────────────────────────────────────────────

def aggregate(records: list[dict], now: int, window_days: int = 7) -> dict:
    """Roll the last `window_days` of records into the four dashboard tiles.

    Per-day figures divide by the ACTUAL span the records cover (floored at
    one hour) rather than the full window, so a fleet that's only been
    metered for a day still shows a truthful calls/day the next morning.

    Records stamped slightly in the future (clock skew, DST weirdness) are
    kept — dropping them would silently hide their cost. The only upper
    bound is a now+1-day sanity cap against absurdly future garbage.
    """
    cutoff = now - window_days * 86400
    future_cap = now + 86400  # sanity cap only — tolerate ordinary clock skew
    recs = []
    for r in records:
        e = _record_epoch(r)
        if e is not None and cutoff <= e <= future_cap:
            recs.append((e, r))
    n = len(recs)
    if n == 0:
        return {"n_pulses": 0, "observer_calls_per_day": 0.0, "cost_per_day_usd": 0.0,
                "verdict_change_rate": 0.0, "skip_rate": 0.0}
    oldest = min(e for e, _ in recs)
    span_days = max((now - oldest) / 86400.0, 1.0 / 24.0)
    observer_calls = sum(1 for _, r in recs if r.get("observer_called"))
    total_cost = sum(float(r.get("cost_usd_est") or 0.0) for _, r in recs)
    total_judged = sum(int(r.get("batch_size") or 0) for _, r in recs if r.get("observer_called"))
    total_changes = sum(int(r.get("verdict_changes") or 0) for _, r in recs)
    return {
        "n_pulses": n,
        "observer_calls_per_day": observer_calls / span_days,
        "cost_per_day_usd": total_cost / span_days,
        "verdict_change_rate": (total_changes / total_judged) if total_judged else 0.0,
        "skip_rate": (n - observer_calls) / n,
    }
