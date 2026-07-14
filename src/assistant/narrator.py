"""narrator — the morning brief's editorial voice (Keel M7).

The brief (brief.py) is a PURE, no-LLM derivation and STAYS one. The narrator
is a SEPARATE, suggestion-only draft layer OVER that pure brief: an LLM phrases
a "good morning" summary sentence and one short per-decision recommendation,
grounded STRICTLY in facts the brief already derived. It writes to a SIDECAR
(brief-<date>.narrative.json), NEVER into the brief file — so the brief stays
byte-reproducible and delete-safe, and a missing / failed / stale narrative
degrades to a deterministic templated summary the renderer computes itself.

Structural guarantees (mirroring strategist.upgrade_step_text /
triage's suggestion-only LLM):

  • No live LLM in this module. The caller INJECTS `llm_narrate(facts) -> dict`;
    every test injects a fake. bin/narrate-brief.py is the ONE subprocess site.
  • WHAT-not-WHETHER, TEXT ONLY. The narrator can only PHRASE — it never acts,
    never dispatches, and cannot reorder the queue (the queue order is brief.py's
    lane-partitioned deterministic scorer; the narrator never touches it).
  • GROUNDED. validate_narrative drops any recommendation whose key is not a
    decision id the brief actually surfaced — the LLM cannot invent an item, the
    same way validate_draft rejects an out-of-playbook step_class.
  • FALLBACK on EVERY failure. There is always a narrative: if the LLM errors or
    returns junk, the deterministic template summary + per-decision lines are
    returned instead, marked source="template". The editorial LAYOUT therefore
    ships even with the narrator never once run; only the VOICE waits on the LLM.
  • EPOCH-TIED. The sidecar carries the brief_epoch it summarized; the renderer
    only overlays a narrative whose brief_epoch matches the current brief, so a
    stale narrative can never misdescribe a rebuilt/changed queue.

After the Droid GLM-5.2 migration the narrator is a lightweight, once-per-day
call routed through llm_runner (Droid). It no longer rides the Strategist's
cost ceiling — the $5/day ceiling was designed for the Strategist's expensive
per-goal research calls, not a single morning summary. The narrator always
proceeds to the LLM; a per-date stamp prevents re-fires, and the template floor
covers any LLM failure. Pure stdlib; never closes a workspace.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from pathlib import Path

from . import brief

NARR_SCHEMA = "brief-narrative/1"

# The narrator only phrases the decisions the Brief tab actually renders at the
# top of the queue — a bounded prompt in, a bounded overlay out. Matches the
# renderer's editorial focus (the long tail lives on the Decisions tab).
TOP_N_DECISIONS = 6
# The few receipts / health flags worth giving the summarizer as grounding.
FACTS_RECEIPTS = 6

# Hard caps on the LLM's returned prose (defence-in-depth against a runaway
# draft bloating the sidecar; the renderer also escapes every string via e()).
MAX_SUMMARY_CHARS = 600
MAX_REC_CHARS = 280


# ─── paths (sidecar lives beside the brief; brief stays pure) ────────────────

def narrative_path(date_str: str) -> Path:
    return brief.brief_dir() / f"brief-{date_str}.narrative.json"


def narrate_stamp_path(date_str: str) -> Path:
    """Per-date spend stamp — the caller writes it so the once-per-day narrator
    spend can't re-fire every pulse (delete-safe, like brief.degrade_stamp)."""
    return brief.brief_dir() / f"narrate-{date_str}.done"


# ─── the closed world of facts the LLM may phrase ────────────────────────────

def _lane_counts(brief_doc: dict) -> dict[str, int]:
    by_lane = ((brief_doc.get("counts") or {}).get("by_lane") or {})
    return {k: int(v) for k, v in by_lane.items()
            if isinstance(v, (int, float))}


def brief_facts(brief_doc: dict) -> dict:
    """The ONLY facts handed to the narrator — a slim, closed projection of the
    already-derived brief. The LLM is told to phrase THESE and invent nothing;
    validate_narrative then enforces the decision-id whitelist structurally, so
    grounding does not rely on the model's goodwill."""
    queue = brief_doc.get("queue") or []
    receipts = brief_doc.get("handled_overnight") or []
    health = brief_doc.get("health") or {}
    cost = health.get("cost") or {}
    interrupts = health.get("interrupts") or {}
    # Factory bulletproofing A5: name any LLM provider whose driver is dark so the
    # narrator can call it out instead of narrating a green fleet over a blind one.
    failing_providers = sorted(
        prov for prov, ph in (health.get("providers") or {}).items()
        if isinstance(ph, dict) and ph.get("failing"))
    decs = []
    for d in queue[:TOP_N_DECISIONS]:
        if not isinstance(d, dict):
            continue
        decs.append({
            "id": d.get("id"),
            "title": (d.get("title") or "")[:200],
            "lane": d.get("lane"),
            "urgency": d.get("urgency"),
            "default_label": d.get("default_label"),
            "age_h": d.get("age_h"),
            "snippet": (d.get("snippet") or "")[:200],
            # the Strategist's pre-research prose, if any — the narrator may
            # COMPRESS it into the one-line recommendation, never expand past it.
            "strategist_context": (d.get("strategist_context") or "")[:1200],
        })
    recs = []
    for r in receipts[:FACTS_RECEIPTS]:
        if isinstance(r, dict):
            recs.append({"kind": r.get("kind"),
                         "evidence": (r.get("evidence") or "")[:200]})
    return {
        "date": brief_doc.get("date"),
        "counts": {
            "open_decisions": len(queue),
            "by_lane": _lane_counts(brief_doc),
            "handled_overnight": len(receipts),
            "digest_rows": (brief_doc.get("counts") or {}).get("digest_rows", 0),
        },
        "cost_per_day_usd": cost.get("cost_per_day_usd"),
        "interrupts_delivered_24h": interrupts.get("delivered_24h"),
        "interrupts_denied_24h": interrupts.get("denied_24h"),
        "expired_unseen_24h": health.get("expired_unseen_24h"),
        "failing_providers": failing_providers,
        "decisions": decs,
        "receipts": recs,
    }


# ─── deterministic floor (no LLM; always available) ──────────────────────────


def deterministic_summary(brief_doc: dict) -> str:
    """The template summary sentence, from counts alone — the floor the whole
    editorial layout renders against before (and without) any LLM voice.
    Deterministic and grounded: every clause is a count the brief derived."""
    lanes = _lane_counts(brief_doc)
    n_receipts = len(brief_doc.get("handled_overnight") or [])
    esc = lanes.get("escalate", 0)
    staged = lanes.get("staged", 0)
    digest_rows = (brief_doc.get("counts") or {}).get("digest_rows") or 0
    parts: list[str] = []
    if n_receipts:
        parts.append(f"{n_receipts} handled overnight")
    if esc:
        parts.append(f"{esc} need your attention")
    if staged:
        parts.append(f"{staged} staged for review")
    if digest_rows:
        parts.append(f"{digest_rows} FYI")
    if not parts:
        parts.append("all clear — nothing needs a decision")
    # Add cost signal when notable
    cost = (brief_doc.get("health") or {}).get("cost") or {}
    cpd = cost.get("cost_per_day_usd")
    if isinstance(cpd, (int, float)) and cpd > 1.0:
        parts.append(f"${cpd:.0f}/day")
    return " · ".join(parts) + "."


def deterministic_recommendation(row: dict) -> str:
    """The template one-liner for a decision when the LLM did not phrase it.
    Grounded in the row's mechanical fields — tries to say something useful
    about the kind of decision, not just 'Accept — today.'"""
    if not isinstance(row, dict):
        return ""
    label = row.get("default_label") or "Accept"
    urg = row.get("urgency")
    kind = row.get("kind") or ""
    title = row.get("title") or ""
    ws_ref = row.get("ws_ref") or ""
    when = {"now": "today", "high": "today", "low": "when you get to it"}.get(
        urg if isinstance(urg, str) else "", "when you get to it")
    # Build a more specific recommendation based on kind
    if kind == "needs_input":
        if ws_ref:
            return f"Check {ws_ref} — agent is waiting for input."
        return "Agent is waiting for your input — check the workspace."
    if kind == "workspace_closed":
        if ws_ref:
            return f"Acknowledge closure of {ws_ref}."
        return "Acknowledge workspace closure."
    if "pr" in title.lower() or "merge" in title.lower():
        return f"Review and {label.lower()} — {when}."
    return f"{label} — {when}."


def _floor_recommendations(brief_doc: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    for d in (brief_doc.get("queue") or [])[:TOP_N_DECISIONS]:
        if isinstance(d, dict) and isinstance(d.get("id"), str):
            out[d["id"]] = deterministic_recommendation(d)
    return out


def _floor(brief_doc: dict, *, source: str = "template",
           reason: str | None = None) -> dict:
    narr = {
        "schema": NARR_SCHEMA,
        "date": brief_doc.get("date"),
        "brief_epoch": brief_doc.get("epoch"),
        "generated_ts": brief.utc_iso(float(brief_doc.get("epoch") or 0)),
        "source": source,
        "summary": deterministic_summary(brief_doc),
        "recommendations": _floor_recommendations(brief_doc),
    }
    if reason:
        narr["reason"] = reason
    return narr


# ─── strict validation of the injected LLM draft (grounding guard) ───────────

def validate_narrative(raw, brief_doc: dict) -> dict | None:
    """Validate one narrator draft against the schema + the brief's ground
    truth. Returns {summary, recommendations} on success, else None (→ floor).

    Rejections / sanitisation:
      • not a dict, or `summary` missing / not a non-empty string → None
        (a narrative with no summary is no narrative);
      • `recommendations` not a dict → treated as {} (summary can stand alone);
      • any recommendation whose KEY is not a decision id present in this brief's
        queue is DROPPED — the structural grounding guard: the LLM cannot narrate
        an item the brief never surfaced (mirrors validate_draft's playbook-enum
        reject). A non-string / empty value is dropped too.
    Strings are trimmed and hard-capped. TEXT ONLY — there is no field here that
    can carry an action, a lane, or a dispatch."""
    if not isinstance(raw, dict):
        return None
    summary = raw.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        return None
    valid_ids = {d.get("id") for d in (brief_doc.get("queue") or [])
                 if isinstance(d, dict) and isinstance(d.get("id"), str)}
    recs_in = raw.get("recommendations")
    recs_out: dict[str, str] = {}
    if isinstance(recs_in, dict):
        for k, v in recs_in.items():
            if k in valid_ids and isinstance(v, str) and v.strip():
                recs_out[k] = v.strip()[:MAX_REC_CHARS]
    return {"summary": summary.strip()[:MAX_SUMMARY_CHARS],
            "recommendations": recs_out}


# ─── the entrypoint: build the narrative (floor + optional LLM overlay) ───────

def build_narrative(brief_doc: dict, *, llm_narrate, now: float,
                    log=None) -> dict:
    """Build the narrative for one brief. ALWAYS returns a narrative dict.

    `llm_narrate(facts) -> dict | None` is INJECTED by the caller
    (bin/narrate-brief.py spawns the subprocess). This module never talks to an
    LLM. The floor (deterministic summary + per-decision template lines) is
    computed FIRST and is the return value on every failure:
      • llm_narrate raises / returns non-dict / fails validation → floor.
    On success the validated summary REPLACES the template summary and each
    validated recommendation REPLACES that decision's template line; decisions
    the LLM omitted keep their deterministic line. source="llm"."""
    log = log or logging.getLogger("narrator")
    floor = _floor(brief_doc)

    try:
        raw = llm_narrate(brief_facts(brief_doc))
    except Exception as exc:  # noqa: BLE001 — LLM output is never load-bearing
        log.warning("narrator llm call failed → template floor: %s", exc)
        floor["reason"] = "llm-error"
        return floor

    valid = validate_narrative(raw, brief_doc)
    if valid is None:
        floor["reason"] = "llm-invalid"
        return floor

    # Overlay: LLM summary wins; LLM recs win per-id, template line fills gaps.
    recs = dict(floor["recommendations"])
    recs.update(valid["recommendations"])
    return {
        "schema": NARR_SCHEMA,
        "date": brief_doc.get("date"),
        "brief_epoch": brief_doc.get("epoch"),
        "generated_ts": brief.utc_iso(now),
        "source": "llm",
        "summary": valid["summary"],
        "recommendations": recs,
    }


# ─── sidecar io ──────────────────────────────────────────────────────────────

def write_narrative(narr: dict) -> Path:
    """Atomic write of the narrative sidecar (tmp + os.replace, brief idiom).
    Unique tmp name per writer so a CLI rebuild racing the pulse can't clobber a
    half-written tmp."""
    date_str = narr.get("date") or brief.latest_brief_date() or "unknown"
    p = narrative_path(date_str)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f"{p.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(narr, indent=2, ensure_ascii=False) + "\n")
    os.replace(tmp, p)
    return p


def read_narrative(date_str: str) -> dict | None:
    return brief._read_json(narrative_path(date_str))


def narrative_for_brief(brief_doc: dict) -> dict:
    """Renderer helper: the narrative to render for THIS brief. Returns the
    sidecar ONLY if it exists and its brief_epoch matches the brief instant it
    describes (a rebuilt brief with a changed queue must not wear a stale
    voice); otherwise the deterministic floor, computed on the fly. So the
    editorial layout ALWAYS has a summary + a line per decision — the LLM voice
    is a pure enhancement, never a prerequisite."""
    date_str = brief_doc.get("date")
    if isinstance(date_str, str):
        side = read_narrative(date_str)
        if (isinstance(side, dict)
                and side.get("brief_epoch") == brief_doc.get("epoch")
                and isinstance(side.get("summary"), str)):
            recs = side.get("recommendations")
            if not isinstance(recs, dict):
                side["recommendations"] = {}
            # Backfill any decision the sidecar didn't cover with its template
            # line, so every rendered row has a recommendation.
            merged = _floor_recommendations(brief_doc)
            merged.update({k: v for k, v in side["recommendations"].items()
                           if isinstance(v, str)})
            side["recommendations"] = merged
            return side
    return _floor(brief_doc)
