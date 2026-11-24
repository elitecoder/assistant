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
import re
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import agent_session

# Rotate metrics.jsonl once it exceeds this (one ~350-byte record per 5-min
# pulse ≈ 100KB/day, so 5MB is ~50 days of history). Old file is kept as
# metrics.jsonl.1 and read_metrics() reads both, so the 7-day dashboard
# window survives a rotation with margin to spare.
MAX_METRICS_BYTES = 5_000_000

# $ per 1M tokens (input, output), matched by substring against the model id.
# NOTE: estimates only — the real number comes from the CLI's total_cost_usd
# when available. The [1m] long-context tier can bill input above 200K ctx at
# a premium, so the estimated cost is a floor, not an exact figure.
# GLM-5.2 Z.ai official API pricing (July 2026): $1.40/M input, $4.40/M output.
# Factory Droid subscription covers this, but tracking the real API cost keeps
# the $/day tile honest about underlying usage.
_PRICE_PER_MTOK = (
    ("opus", (5.0, 25.0)),
    ("haiku", (1.0, 5.0)),
    ("sonnet-5", (2.0, 10.0)),
    ("sonnet", (3.0, 15.0)),
    ("glm-5.2", (1.40, 4.40)),
)
_DEFAULT_PRICE = (3.0, 15.0)  # sonnet rates — unknown models default to Claude pricing


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


def _usage(parsed: dict | None) -> dict | None:
    if parsed is None:
        return None
    return {k: parsed[k] for k in ("tokens_in", "tokens_out", "cost_usd")}


def parse_cli_usage(stdout: str) -> dict | None:
    """Usage from a Claude or Droid result envelope, or Copilot's JSONL event
    stream: {"tokens_in", "tokens_out", "cost_usd"}, or None when stdout
    carries no usage (plain text, timeout, empty). tokens_in includes
    cache-creation/read tokens — it measures how much context the Observer
    consumed, which is the cadence signal we care about."""
    return _usage(agent_session.parse_usage(agent_session.CLAUDE, stdout)
                  or agent_session.parse_usage(agent_session.COPILOT, stdout))


def parse_cli_result(stdout: str) -> dict | None:
    """parse_cli_usage, but None unless the run reports success."""
    if agent_session.parse_usage(agent_session.CLAUDE, stdout) is not None:
        return _usage(agent_session.parse_headless(agent_session.CLAUDE, stdout))
    return _usage(agent_session.parse_headless(agent_session.COPILOT, stdout))


def observer_usage(stdout: str, prompt_chars: int, model: str) -> dict:
    """Usage for ONE Observer subprocess: real CLI numbers when the stdout
    envelope parses, chars/4 estimate otherwise (and says so in `source`)."""
    parsed = parse_cli_usage(stdout)
    if parsed is not None:
        cost = parsed["cost_usd"]
        source = "cli"
        if cost is None:
            cost = estimate_cost_usd(parsed["tokens_in"], parsed["tokens_out"], model)
            source = "estimated"
        return {"tokens_in": parsed["tokens_in"], "tokens_out": parsed["tokens_out"],
                "cost_usd": round(cost, 6), "source": source}
    tokens_in = estimate_tokens(prompt_chars)
    tokens_out = estimate_tokens(len(stdout or ""))
    return {"tokens_in": tokens_in, "tokens_out": tokens_out,
            "cost_usd": round(estimate_cost_usd(tokens_in, tokens_out, model), 6),
            "source": "estimated"}


def sum_usage(usages: list[dict]) -> dict:
    """Combine per-batch usage and preserve actual provider/model routing."""
    if not usages:
        return {
            "tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0,
            "source": "estimated", "provider": None, "model": None,
            "routes": [],
        }
    sources = {u.get("source", "estimated") for u in usages}
    source = "cli" if sources == {"cli"} else ("estimated" if sources == {"estimated"} else "mixed")
    route_counts = Counter(
        (str(u.get("provider") or "unknown"), str(u.get("model") or "unknown"))
        for u in usages
    )
    routes = [
        {"provider": provider, "model": model, "calls": calls}
        for (provider, model), calls in sorted(route_counts.items())
    ]
    providers = {route["provider"] for route in routes}
    models = {route["model"] for route in routes}
    return {
        "tokens_in": sum(int(u.get("tokens_in") or 0) for u in usages),
        "tokens_out": sum(int(u.get("tokens_out") or 0) for u in usages),
        "cost_usd": round(sum(float(u.get("cost_usd") or 0.0) for u in usages), 6),
        "source": source,
        "provider": next(iter(providers)) if len(providers) == 1 else "mixed",
        "model": next(iter(models)) if len(models) == 1 else "mixed",
        "routes": routes,
    }


# Jobs that run on their own, so a model call that keeps failing the same way
# can be held back. A call a person asked for (the Work queue refresh, the Slack
# tab refresh) is never held back: it always runs when asked.
BACKGROUND_CALLERS = ("lessons", "strategist", "narrator", "observer-audit", "triage")


def _failwatch():
    """The failure counter, or None if it can't be loaded."""
    try:
        src = str(Path(__file__).resolve().parent.parent / "src")
        if src not in sys.path:
            sys.path.insert(0, src)
        from assistant import failwatch  # noqa: PLC0415
        return failwatch
    except Exception:  # noqa: BLE001 — counting must never break a model call
        return None


def failure_reason(rc: int | None, stderr: str = "") -> str:
    """A short, stable reason for a failed model call: what the CLI said last,
    or the exit code in words."""
    if rc == 124:
        return "timed out"
    if rc == 127:
        return "not installed"
    lines = (stderr or "").strip().splitlines()
    return lines[-1][:150] if lines else f"exit code {rc}"


def _context(provider: str | None, model: str | None) -> str:
    return f"{provider}/{model}"


def paused_note(caller: str, provider: str | None = None, model: str | None = None) -> str | None:
    """For a background job about to call a model: a one-line reason to skip the
    call because the same failure has repeated, or None to go ahead. A job that
    knows its provider and model before the call passes them, so a pause set
    for another model (the wrong id was fixed) doesn't hold."""
    try:
        watch = _failwatch()
        context = _context(provider, model) if provider or model else None
        until = (watch.backed_off("model:" + caller, context=context)
                 if watch and caller in BACKGROUND_CALLERS else None)
        if until:
            return (f"{caller} model calls paused after repeating the same failure; "
                    f"next try after {time.strftime('%H:%M', time.localtime(until))}")
    except Exception:  # noqa: BLE001
        pass
    return None


def _count_call(caller: str, model: str | None, provider: str | None,
                status: str, error: str | None) -> None:
    """Tell failwatch how a model call went. Never raises."""
    try:
        watch = _failwatch()
        if watch is None:
            return
        component = "model:" + caller
        if status == "failed":
            text = f"{provider or 'unknown provider'} {model or 'unknown model'} call failed"
            if error:
                text += f": {error}"
            watch.record_failure(component, text, pause=caller in BACKGROUND_CALLERS,
                                 context=_context(provider, model))
        elif status == "ok":
            watch.record_ok(component)
    except Exception:  # noqa: BLE001
        pass


def _records():
    """assistant.records, or None when it can't be loaded. Writing a model call's record is never load-bearing."""
    try:
        src = str(Path(__file__).resolve().parent.parent / "src")
        if src not in sys.path:
            sys.path.insert(0, src)
        from assistant import records  # noqa: PLC0415
        return records
    except Exception:  # noqa: BLE001 — a record must never break a model call
        return None


_TRACE = re.compile(r"t[rl]-[0-9a-f]{16}")


def append_cost_row(*, caller: str, model: str | None, usage: dict,
                    wall_ms: int,
                    path: Path | None = None, status: str = "ok",
                    provider: str | None = None, error: str | None = None,
                    trace: str = "", batch: int | None = None) -> None:
    """One per-call row in ~/.assistant/cost-ledger.jsonl (design section 3:
    {ts, caller, model, tokens_in, tokens_out, est_usd, wall_ms}). Every LLM
    caller beyond the Observer (triage, later strategist/drafter) appends here
    so $/day per caller is derivable. Failed rows retain observed or estimated
    spend so repeated failures cannot evade cost accounting.

    `trace` is the trace id of what the call served: the run's for a batch, a row's or a task's for a call about
    one piece of work, or "" when it served none. It goes into the row and into the call's telemetry record, which
    also holds `batch`, which of the run's calls this was, when the caller knows."""
    trace = trace if isinstance(trace, str) and _TRACE.fullmatch(trace) else ""
    p = path if path is not None else cost_ledger_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "ts": utc_iso(int(time.time())),
        "caller": caller,
        "provider": provider,
        "model": model,
        "tokens_in": int(usage.get("tokens_in") or 0),
        "tokens_out": int(usage.get("tokens_out") or 0),
        "est_usd": float(usage.get("cost_usd") or 0.0),
        "usage_source": usage.get("source", "estimated"),
        "wall_ms": int(wall_ms),
        "status": status,
        "trace": trace,
    }
    with open(p, "a") as f:
        f.write(json.dumps(row) + "\n")
    _count_call(caller, model, provider, status, error)
    records = _records()
    if records is not None:
        records.model_call(caller, model, status, usage, wall_ms, trace, error, batch)


def read_cost_ledger(path: Path | None = None) -> list[dict]:
    """All parseable cost-ledger rows, oldest-first. Missing file or corrupt
    lines are skipped (same tolerance as read_metrics)."""
    p = path if path is not None else cost_ledger_path()
    out: list[dict] = []
    try:
        lines = p.read_text().splitlines()
    except (OSError, FileNotFoundError):
        return out
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
        "provider": usage.get("provider") if observer_called else None,
        "model": model,
        "routes": usage.get("routes", []) if observer_called else [],
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

def aggregate(records: list[dict], now: int, window_days: int = 7,
              cost_rows: list[dict] | None = None) -> dict:
    """Roll the last `window_days` of records into the dashboard tiles.

    Per-day figures divide by the ACTUAL span the records cover (floored at
    one hour) rather than the full window, so a fleet that's only been
    metered for a day still shows a truthful calls/day the next morning.

    Records stamped slightly in the future (clock skew, DST weirdness) are
    kept — dropping them would silently hide their cost. The only upper
    bound is a now+1-day sanity cap against absurdly future garbage.

    cost_rows (optional): cost-ledger rows (read_cost_ledger) from LLM
    callers beyond the Observer — triage today, strategist later. Their
    windowed spend lands in cost_ledger_per_day_usd AND in cost_per_day_usd,
    so the $/day tile is the fleet's whole LLM bill, not just the Observer's
    (pulse records and cost-ledger rows never double-count: observer usage is
    only ever in metrics.jsonl, other callers only ever in the cost ledger).

    skip_rate is WORKSPACE-level: skipped / (skipped + observed) summed over
    the window's records — the fraction of per-ws judgments the no-change
    carry saved. (The old pulse-level observer_called ratio understated
    mixed pulses: one observed ws out of ten skipped counted as 0% skip.)
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
                "cost_ledger_per_day_usd": 0.0,
                "verdict_change_rate": 0.0, "skip_rate": 0.0}
    oldest = min(e for e, _ in recs)
    span_days = max((now - oldest) / 86400.0, 1.0 / 24.0)
    observer_calls = sum(1 for _, r in recs if r.get("observer_called"))
    total_cost = sum(float(r.get("cost_usd_est") or 0.0) for _, r in recs)
    total_judged = sum(int(r.get("batch_size") or 0) for _, r in recs if r.get("observer_called"))
    total_changes = sum(int(r.get("verdict_changes") or 0) for _, r in recs)
    total_skipped = sum(int(r.get("skipped") or 0) for _, r in recs)
    ledger_cost = 0.0
    for row in cost_rows or []:
        e = _record_epoch(row)
        if e is not None and cutoff <= e <= future_cap:
            ledger_cost += float(row.get("est_usd") or 0.0)
    denom = total_skipped + total_judged
    return {
        "n_pulses": n,
        "observer_calls_per_day": observer_calls / span_days,
        "cost_per_day_usd": (total_cost + ledger_cost) / span_days,
        "cost_ledger_per_day_usd": ledger_cost / span_days,
        "verdict_change_rate": (total_changes / total_judged) if total_judged else 0.0,
        "skip_rate": (total_skipped / denom) if denom else 0.0,
    }
