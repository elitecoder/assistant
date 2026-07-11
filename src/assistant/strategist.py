"""strategist — the throttled, metered LLM DRAFTER that upgrades the M4
planner's template staging (Keel M6, the FINAL milestone + the FIRST new LLM
caller since M2).

Two design constraints dominate every line here (safety + cost governance are
paramount — this is the first new LLM caller since M2):

  1. DRAFTS WHAT, NEVER WHETHER. The Python planner (goals.plan_pass) has
     ALREADY decided to stage a step — a stalled goal, with capacity, whose
     step_class the deterministic select_next_step chose from the goal's
     playbook. Only AFTER that decision does the Strategist get consulted, and
     its ONLY job is to draft better TEXT: a sharper TODO title/detail, or a
     decision-context markdown. `upgrade_step_text` therefore RETURNS ONLY
     (title, detail) STRINGS — it has no return path to an action class, a
     lane, a dispatch, or autoDispatch. The step_class stays a Python-owned
     INPUT; the downstream _stage_todo/_stage_decision always stage with THAT
     class, never anything the LLM echoed. A structural test asserts there is
     no code path from Strategist output to an action class (the M6 twin of
     the M2 "triage lane map has no auto" invariant).

  2. LLM output is STRICT-JSON, schema-validated, and NEVER load-bearing.
     - The LLM's echoed `step_class` MUST be within the goal's playbook enum
       (unattended + gated) or the whole draft is REJECTED — the LLM cannot
       invent an unattended action class.
     - Malformed/invalid JSON, an exception, a missing field, or an empty
       string → a logged no-op that FALLS BACK to the M4 deterministic
       template. Never a TODO from bad LLM output, never a crash, never a
       blocked pulse.

INJECTABLE LLM (mirrors triage.py exactly): this module NEVER talks to an LLM
itself. The caller (bin/strategist.py, spawned by pulse.py like the Observer)
injects the draft result via the `llm_draft`/`llm_context` callables. Every
test injects a fake draft — NO live LLM, NO network, ever.

GOVERNANCE, all config-driven, all ledgered, all reversible:
  • THROTTLE ≤1 call/goal/day (budget.maxStrategistCallsPerDay, default 1),
    persisted in the actions ledger so it survives pulses AND restarts (the M4
    week-key pattern, narrowed to per-goal-per-day). A second call the same day
    → skip, use the template.
  • DAILY COST CEILING: when the day's whole LLM spend is over the ceiling the
    Strategist is SHED FIRST (skipped, LEDGERED). The Observer/triage never
    consult the ceiling, so they are structurally un-sheddable by it — the
    token-audit governance lesson made real and testable.
  • AUTO-PAUSE (mechanical twins for the human backstops), two triggers, both
    config-driven, both ledgered, both reversible: (a) staged-work acceptance
    rate <50% (accepted/edited vs rejected/expired of goal-linked decisions);
    (b) expired-unseen meter growth (the M3 metric) over a limit. Auto-pause
    stops the Strategist DRAFTING only — the planner keeps staging templates.
    Distinct from the goal `_paused` kill switch (which stops the WHOLE
    planner).

Paths are computed per-call (not module constants) so tests that point $HOME
at a tmp dir see fresh paths even when this module stays cached in sys.modules.
Pure stdlib; NEVER closes a workspace; no launchctl from code.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from . import decisions, goals

# ─── config defaults (a mangled config never breaks the drafter) ─────────────

DEFAULT_ENABLED = True
# The day's whole-LLM-spend ceiling; over it, the Strategist is shed FIRST. A
# real, testable number (the token-audit lesson) — not infinity.
DEFAULT_DAILY_COST_CEILING_USD = 5.0
# Auto-pause (a): acceptance-rate floor + the minimum resolved sample before
# the floor is allowed to fire (so a day-one 0/0 can never auto-pause).
DEFAULT_ACCEPT_RATE_FLOOR = 0.5
DEFAULT_ACCEPT_MIN_SAMPLE = 4
# Auto-pause (b): expired-unseen count over the last 24h that trips the pause.
DEFAULT_EXPIRED_UNSEEN_LIMIT = 5
# Pre-research bounds: at most this many OPEN decisions researched per pass.
DEFAULT_MAX_CONTEXT_PER_PASS = 1

WINDOW_SEC = 24 * 3600


# ─── paths (per-call, $HOME-rooted) ──────────────────────────────────────────

def _home() -> Path:
    return Path(os.environ.get("HOME", str(Path.home())))


def ledger_path() -> Path:
    return _home() / ".assistant" / "actions-ledger.jsonl"


def cost_ledger_path() -> Path:
    return _home() / ".assistant" / "cost-ledger.jsonl"


def metrics_path() -> Path:
    return _home() / ".assistant" / "metrics.jsonl"


def decision_context_dir() -> Path:
    return _home() / ".assistant" / "decision-context"


def config_path() -> Path:
    return _home() / ".assistant" / "comms" / "config.json"


def utc_iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def _day_key(epoch: float) -> str:
    """UTC calendar day (YYYY-MM-DD) — the per-goal-per-day throttle grain."""
    return utc_iso(epoch)[:10]


# ─── config knobs ────────────────────────────────────────────────────────────

def _read_config() -> dict:
    try:
        raw = json.loads(config_path().read_text())
        return raw if isinstance(raw, dict) else {}
    except (OSError, FileNotFoundError, json.JSONDecodeError, ValueError):
        return {}


def _cfg() -> dict:
    s = _read_config().get("strategist")
    return s if isinstance(s, dict) else {}


def _num(key: str, default: float) -> float:
    v = _cfg().get(key)
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return default
    return float(v)


def _int(key: str, default: int) -> int:
    v = _cfg().get(key)
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return default
    return int(v)


def enabled() -> bool:
    v = _cfg().get("enabled", DEFAULT_ENABLED)
    return bool(v) if isinstance(v, bool) else DEFAULT_ENABLED


def daily_cost_ceiling() -> float:
    return _num("dailyCostCeilingUsd", DEFAULT_DAILY_COST_CEILING_USD)


def accept_rate_floor() -> float:
    return _num("acceptRateFloor", DEFAULT_ACCEPT_RATE_FLOOR)


def accept_min_sample() -> int:
    return _int("acceptMinSample", DEFAULT_ACCEPT_MIN_SAMPLE)


def expired_unseen_limit() -> int:
    return _int("expiredUnseenLimit", DEFAULT_EXPIRED_UNSEEN_LIMIT)


def max_context_per_pass() -> int:
    return _int("maxContextPerPass", DEFAULT_MAX_CONTEXT_PER_PASS)


# ─── ledger (best-effort; a ledger failure never blocks the drafter) ─────────

def _append_ledger(entry: dict) -> None:
    try:
        p = ledger_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _read_jsonl(path: Path) -> list[dict]:
    try:
        lines = path.read_text().splitlines()
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


def _ledger_count_key(key: str) -> int:
    """How many times `key` has been ledgered — the per-goal-per-day throttle
    counts calls, not just presence, so budget.maxStrategistCallsPerDay>1 is
    honored (default 1 → the first call trips it)."""
    return sum(1 for r in _read_jsonl(ledger_path()) if r.get("key") == key)


def _ledger_has_key_today(key: str) -> bool:
    return _ledger_count_key(key) > 0


def _ledger_skip(reason: str, now: float, *, ref: str = "(goals)",
                 extra: str = "") -> None:
    """A NON-draft outcome (paused / throttled / ceiling-shed / invalid draft)
    on the actions ledger, so every reason the Strategist did NOT upgrade a
    step is auditable in the brief — never a silent drop."""
    _append_ledger({
        "ts": utc_iso(now), "epoch": int(now),
        "key": f"strategist:skip:{reason}:{ref}", "kind": "strategist-skip",
        "ws_ref": "(strategist)", "outcome": "skipped",
        "evidence": f"strategist {reason} for {ref}"
                    + (f"; {extra}" if extra else ""),
    })


# ─── throttle keys (per-goal-per-day, per-decision-per-day) ──────────────────

def call_key(goal_id: str, now: float) -> str:
    return f"strategist:call:{goal_id}:{_day_key(now)}"


def context_key(dec_id: str, now: float) -> str:
    return f"strategist:context:{dec_id}:{_day_key(now)}"


def record_call(goal_id: str, now: float, *, drafted: bool) -> None:
    """Spend one of the goal's daily Strategist calls. Recorded whether or not
    the draft VALIDATED — a malformed reply still consumed the call, so it must
    count against the throttle (else a bad LLM could be hammered all day)."""
    _append_ledger({
        "ts": utc_iso(now), "epoch": int(now),
        "key": call_key(goal_id, now), "kind": "strategist-call",
        "ws_ref": "(strategist)", "outcome": "verified" if drafted else "skipped",
        "evidence": f"strategist call for {goal_id} "
                    f"(draft {'used' if drafted else 'rejected→template'})",
    })


def record_context_call(dec_id: str, now: float, *, wrote: bool) -> None:
    _append_ledger({
        "ts": utc_iso(now), "epoch": int(now),
        "key": context_key(dec_id, now), "kind": "strategist-context",
        "ws_ref": "(strategist)", "outcome": "verified" if wrote else "skipped",
        "evidence": f"strategist pre-research for {dec_id} "
                    f"({'wrote context' if wrote else 'no-op'})",
    })


def goal_call_budget(goal: dict) -> int:
    budget = goal.get("budget") if isinstance(goal.get("budget"), dict) else {}
    v = budget.get("maxStrategistCallsPerDay",
                   goals.DEFAULT_BUDGET["maxStrategistCallsPerDay"])
    return goals._safe_int(v, goals.DEFAULT_BUDGET["maxStrategistCallsPerDay"])


def throttled(goal: dict, now: float) -> bool:
    """True when this goal has already spent its daily Strategist calls. Reads
    the actions ledger (durable) so the throttle survives pulses AND restarts
    with zero new locking — exactly the M4 week-key discipline, narrowed to
    per-goal-per-day."""
    gid = goal.get("id")
    if gid is None:
        return True
    return _ledger_count_key(call_key(gid, now)) >= max(1, goal_call_budget(goal))


# ─── daily cost ceiling (sheds Strategist FIRST, never Observer/triage) ──────

def day_spend_usd(now: float) -> float:
    """The day's WHOLE LLM bill: every cost-ledger row (observer/triage/
    strategist/drafter) stamped today PLUS the Observer's per-pulse cost from
    metrics.jsonl (its usage lives there, not in the cost ledger — the two
    never double-count). This is the number the ceiling caps."""
    day = _day_key(now)
    total = 0.0
    for row in _read_jsonl(cost_ledger_path()):
        ts = row.get("ts")
        if isinstance(ts, str) and ts[:10] == day:
            total += float(row.get("est_usd") or 0.0)
    for row in _read_jsonl(metrics_path()):
        ts = row.get("ts")
        if isinstance(ts, str) and ts[:10] == day:
            total += float(row.get("cost_usd_est") or 0.0)
    return round(total, 6)


def over_ceiling(now: float) -> bool:
    """The day's spend has reached the ceiling → shed the Strategist. ONLY the
    Strategist calls this; the Observer/triage never do, so the ceiling can
    never shed them (design section 2: 'sheds Strategist/Drafter, never
    Observer')."""
    return day_spend_usd(now) >= daily_cost_ceiling()


# ─── auto-pause (mechanical twins) ───────────────────────────────────────────

def staged_accept_rate(now: float, records: list[dict] | None = None
                       ) -> tuple[float, int]:
    """Acceptance rate of goal-linked staged work over the last 24h, counting
    EXPIRED as a non-accept (design item 4a: 'accepted vs rejected/expired').
    Returns (rate, resolved_count). resolved_count gates the floor so a tiny
    sample can't auto-pause."""
    now = now if now is not None else 0.0
    records = records if records is not None else decisions.read_log()
    cutoff = now - WINDOW_SEC
    kept = resolved = 0
    for rec in decisions.fold(records).values():
        if not (rec.get("goal_refs") or []):
            continue
        status = rec.get("status")
        if status not in ("accepted", "edited", "rejected", "expired"):
            continue
        ep = decisions.parse_iso((rec.get("resolution") or {}).get("ts"))
        if ep is None:
            ep = rec.get("epoch")
        if not isinstance(ep, (int, float)) or not (cutoff <= ep <= now):
            continue
        resolved += 1
        if status in ("accepted", "edited"):
            kept += 1
    rate = round(kept / resolved, 4) if resolved else 0.0
    return rate, resolved


def expired_unseen(now: float, records: list[dict] | None = None) -> int:
    """Reuse the M3 expired-unseen metric (brief.expired_unseen_count) — the
    second auto-pause trigger. Imported lazily to keep the import graph acyclic
    (brief imports goals/decisions/triage, never strategist)."""
    from . import brief  # noqa: PLC0415
    records = records if records is not None else decisions.read_log()
    return brief.expired_unseen_count(records, now)


def auto_pause_reason(now: float, records: list[dict] | None = None
                      ) -> str | None:
    """Which mechanical twin (if any) says pause. Returns the trigger name or
    None. Pure over the decisions log, so it is reversible: when the metrics
    recover, the pause lifts on the next pulse with no manual un-pause."""
    records = records if records is not None else decisions.read_log()
    rate, resolved = staged_accept_rate(now, records)
    if resolved >= accept_min_sample() and rate < accept_rate_floor():
        return "accept-rate"
    if expired_unseen(now, records) >= expired_unseen_limit():
        return "expired-unseen"
    return None


def auto_paused(now: float, records: list[dict] | None = None) -> bool:
    """True when EITHER mechanical twin trips. Ledgers the pause once per day
    per trigger (day-keyed dedup, like the M4 stall week-key) so a persistently
    bad metric doesn't spam the ledger every pulse, while the pause itself is
    recomputed fresh (reversible)."""
    reason = auto_pause_reason(now, records)
    if reason is None:
        return False
    key = f"strategist:autopause:{reason}:{_day_key(now)}"
    if not _ledger_has_key_today(key):
        _append_ledger({
            "ts": utc_iso(now), "epoch": int(now),
            "key": key, "kind": "strategist-autopause",
            "ws_ref": "(strategist)", "outcome": "verified",
            "evidence": f"strategist auto-paused: {reason} tripped "
                        f"(drafting off, planner falls back to templates)",
        })
    return True


def active(now: float, records: list[dict] | None = None) -> tuple[bool, str | None]:
    """Is the Strategist allowed to draft AT ALL right now (independent of any
    per-goal throttle)? Returns (ok, blocking_reason). The single gate every
    entrypoint consults before spending a call."""
    if not enabled():
        return False, "disabled"
    if auto_paused(now, records):
        return False, "auto-paused"
    if over_ceiling(now):
        return False, "ceiling-shed"
    return True, None


# ─── draft-output schema validation (strict; LLM output never load-bearing) ──

def playbook_classes(goal: dict) -> set[str]:
    """The goal's whole playbook enum (unattended + gated). A drafted
    step_class OUTSIDE this set is rejected — the LLM cannot invent an
    action class."""
    pb = goal.get("playbook") if isinstance(goal.get("playbook"), dict) else {}
    return set(pb.get("unattended") or []) | set(pb.get("gated") or [])


def validate_draft(raw, goal: dict, step_class: str) -> tuple[str, str] | None:
    """Validate one strict-JSON draft against the schema. Returns (title,
    detail) ONLY on success, else None (→ template fallback). Rejections:
      • not a dict / missing title|detail;
      • title or detail not a non-empty string;
      • echoed step_class OUTSIDE the goal's playbook enum (the LLM cannot
        invent an unattended action class — the WHAT-not-WHETHER guard).
    NOTE the return is TEXT ONLY — this function has no way to emit an action
    class, a lane, or a dispatch. That is the structural WHAT-not-WHETHER
    guarantee, unit-asserted."""
    if not isinstance(raw, dict):
        return None
    echoed = raw.get("step_class")
    # An echoed class is optional, but if present it MUST be a real playbook
    # class — a class the LLM invented (or one outside this goal's playbook) is
    # a hard reject, never silently coerced.
    if echoed is not None:
        if not isinstance(echoed, str) or echoed not in playbook_classes(goal):
            return None
    title = raw.get("title")
    detail = raw.get("detail")
    if not isinstance(title, str) or not title.strip():
        return None
    if not isinstance(detail, str) or not detail.strip():
        return None
    return title.strip()[:200], detail.strip()[:2000]


# ─── the WHAT-not-WHETHER entrypoint (returns TEXT ONLY) ─────────────────────

def upgrade_step_text(goal: dict, step_class: str,
                      template_title: str, template_detail: str, *,
                      llm_draft, now: float, log=None) -> tuple[str, str]:
    """Upgrade a step the PLANNER already decided to stage from templates to an
    LLM draft. Called ONLY after goals.plan_pass chose (stalled goal + capacity
    + step_class ∈ playbook) — the Strategist never decides WHETHER.

    Returns the (title, detail) to stage — TEXT ONLY, NEVER an action class.
    The caller stages with the Python-owned `step_class`, never anything the
    LLM echoed. Falls back to the template (template_title, template_detail) on
    EVERY gate and EVERY invalid/malformed draft:
      • Strategist not active (disabled / auto-paused / over-ceiling) → template
        (ledgered skip; ceiling-shed is the 'shed Strategist first' path);
      • per-goal daily throttle already spent → template (ledgered skip);
      • llm_draft raises / returns non-dict / fails schema validation →
        template (ledgered no-op — NEVER a TODO from bad LLM output, never a
        crash, never a blocked pulse).

    `llm_draft(goal, step_class, template_title, template_detail) -> dict|None`
    is INJECTED by the caller (bin/strategist.py spawns the subprocess and
    injects the parsed result). This module never talks to an LLM — every test
    injects a fake draft, so no live LLM/network is ever reachable from here."""
    log = log or logging.getLogger("strategist")
    template = (template_title, template_detail)
    gid = goal.get("id")

    ok, reason = active(now)
    if not ok:
        _ledger_skip(reason, now, ref=str(gid))
        return template
    if throttled(goal, now):
        _ledger_skip("throttled", now, ref=str(gid))
        return template

    # A call is about to be spent — invoke the injected LLM. Any failure is
    # non-load-bearing: the template is the floor, always.
    try:
        raw = llm_draft(goal, step_class, template_title, template_detail)
    except Exception as e:  # noqa: BLE001 — a broken LLM never blocks staging
        log.warning("strategist: llm_draft raised for %s (template): %s", gid, e)
        record_call(str(gid), now, drafted=False)
        _ledger_skip("draft-error", now, ref=str(gid))
        return template

    drafted = validate_draft(raw, goal, step_class)
    record_call(str(gid), now, drafted=drafted is not None)
    if drafted is None:
        _ledger_skip("invalid-draft", now, ref=str(gid),
                     extra="malformed/out-of-playbook → template")
        return template
    return drafted


# ─── nightly decision-context pre-research (draft-only; never acts) ──────────

def context_path(dec_id: str) -> Path:
    return decision_context_dir() / f"{dec_id}.md"


def has_context(dec_id: str) -> bool:
    try:
        return context_path(dec_id).stat().st_size > 0
    except (OSError, FileNotFoundError):
        return False


def write_context(dec_id: str, markdown: str, now: float) -> bool:
    """Atomically write a decision-context markdown (tmp+os.replace, the repo
    idiom). Draft-only: this file is surfaced inline in the brief's decision
    row — it NEVER becomes an action. Returns True when written."""
    if not isinstance(markdown, str) or not markdown.strip():
        return False
    d = decision_context_dir()
    d.mkdir(parents=True, exist_ok=True)
    p = context_path(dec_id)
    tmp = p.with_suffix(".md.tmp")
    tmp.write_text(markdown.strip() + "\n")
    os.replace(tmp, p)
    return True


def read_context(dec_id: str) -> str | None:
    try:
        text = context_path(dec_id).read_text()
    except (OSError, FileNotFoundError):
        return None
    return text if text.strip() else None


def _idle_capacity(now: float) -> tuple[bool, str]:
    """Reuse the M4 headroom / world-active / world-staleness checks so
    pre-research NEVER steals human or goal dispatch capacity. Idle == fresh
    world.json AND leftover ACTIVE_WS_CAP headroom after live ws + pending
    human TODOs. Returns (idle, reason_when_not)."""
    world = goals._read_json(goals.world_path())
    if goals._world_is_stale(world, now):
        return False, "stale-world"
    todo_data = goals._read_json(goals.todo_path()) or {}
    active_ws = goals._world_active_ws(world)
    human_pending = goals._pending_human_todos(todo_data)
    headroom = goals.ACTIVE_WS_CAP - active_ws - human_pending
    if headroom <= 0:
        return False, "no-idle-headroom"
    return True, ""


def pre_research_pass(now: float, *, llm_context, log=None) -> dict:
    """On IDLE capacity, pre-research queued OPEN decisions into
    ~/.assistant/decision-context/<dec-id>.md (draft-only, surfaced inline in
    the brief). Throttled (per-decision-per-day), ceiling-gated, auto-pausable
    exactly like the staging drafts — the same `active()` gate + a per-pass cap.

    `llm_context(decision) -> str|None` is INJECTED by the caller; this module
    never talks to an LLM. Returns a summary; every skip is ledgered."""
    log = log or logging.getLogger("strategist")
    summary = {"researched": [], "skipped": [], "idle": True}

    ok, reason = active(now)
    if not ok:
        summary["idle"] = False
        _ledger_skip(reason, now, ref="(pre-research)")
        summary["skipped"].append({"reason": reason})
        return summary

    idle, why = _idle_capacity(now)
    if not idle:
        summary["idle"] = False
        _ledger_skip(why, now, ref="(pre-research)")
        summary["skipped"].append({"reason": why})
        return summary

    cap = max(0, max_context_per_pass())
    done = 0
    for dec in decisions.open_decisions():
        if done >= cap:
            break
        dec_id = dec.get("id")
        if not dec_id or has_context(dec_id):
            continue
        # Per-decision-per-day throttle (survives pulses/restarts, ledger-based).
        if _ledger_has_key_today(context_key(dec_id, now)):
            continue
        try:
            markdown = llm_context(dec)
        except Exception as e:  # noqa: BLE001 — a broken LLM never blocks the pulse
            log.warning("strategist: llm_context raised for %s: %s", dec_id, e)
            record_context_call(dec_id, now, wrote=False)
            _ledger_skip("context-error", now, ref=str(dec_id))
            continue
        wrote = write_context(dec_id, markdown, now) if markdown else False
        record_context_call(dec_id, now, wrote=wrote)
        if wrote:
            summary["researched"].append(dec_id)
            _append_ledger({
                "ts": utc_iso(now), "epoch": int(now),
                "key": f"strategist:context-wrote:{dec_id}",
                "kind": "strategist-context-wrote", "ws_ref": "(strategist)",
                "outcome": "verified",
                "evidence": f"pre-researched decision {dec_id} context "
                            f"(draft-only, surfaced in brief)",
            })
        else:
            summary["skipped"].append({"dec": dec_id, "reason": "empty-context"})
        done += 1
    return summary
