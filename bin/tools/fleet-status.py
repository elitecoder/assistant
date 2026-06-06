#!/usr/bin/env python3
"""fleet-status — current state of all workspaces, for the warm comms session.

Joins two on-disk sources:
  - ~/.assistant/heartbeat.json          → pulse_idx, age, alive/status
  - ~/.assistant/observer-latest-report.json → classification counts + the
                                               actionable "needs attention" set

Pulse liveness (idx / age / status) comes from the heartbeat, which the
mechanical pulse rewrites every run; the observer report supplies the
per-workspace picture. Both are read defensively — a missing or malformed file
degrades to empty rather than raising.

Returns JSON to stdout:
  {
    "pulse_idx": 1413,
    "pulse_age_seconds": 42,
    "heartbeat_status": "running",
    "workspace_counts": {"AWAITING_USER": 6, "ACTIVE": 3, ...},
    "needs_attention": [
      {"ws_ref": "workspace:4", "project": "...", "classification": "AWAITING_USER",
       "action_needed": "<evidence, <=120 chars>", "kind": "emit-card"}
    ]
  }
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

HOME = Path.home()
ASSISTANT_DIR = HOME / ".assistant"
HEARTBEAT_PATH = ASSISTANT_DIR / "heartbeat.json"
OBSERVER_REPORT_PATH = ASSISTANT_DIR / "observer-latest-report.json"

# Cap the attention list — the warm session wants the live worry-set, not a
# full dump. It can call recent_actions / workspace_peek to drill in.
MAX_NEEDS_ATTENTION = 25
ACTION_NEEDED_MAX = 120


def _load_json(path: Path) -> dict[str, Any]:
    """Read a JSON object, or {} on any read/parse failure."""
    try:
        data = json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _needs_attention(report: dict[str, Any]) -> list[dict[str, Any]]:
    """Build the attention list from the observer report's candidate actions.

    Each candidate action carries a kind, an evidence string, the source
    workspace, and its classification. We surface ws_ref + project (the
    workspace title if the report has one, else the ws_ref) + classification +
    a trimmed action_needed + kind.
    """
    out: list[dict[str, Any]] = []
    actions = report.get("candidate_actions")
    if isinstance(actions, list):
        for a in actions:
            if not isinstance(a, dict):
                continue
            params = a.get("params") if isinstance(a.get("params"), dict) else {}
            ws_ref = params.get("ws_ref") or a.get("_source_ws") or a.get("ws_ref") or ""
            # Title fallback chain: explicit title on the action/params, else
            # the ws_ref itself (the report's candidate actions carry no title).
            project = a.get("title") or params.get("title") or ws_ref
            evidence = (a.get("evidence") or a.get("summary") or "").strip()
            out.append({
                "ws_ref": ws_ref,
                "project": project,
                "classification": a.get("_classification") or a.get("classification") or "",
                "action_needed": evidence[:ACTION_NEEDED_MAX],
                "kind": a.get("kind") or "",
            })
        return out[:MAX_NEEDS_ATTENTION]

    # Fallback for the newer state shape: draft awaiting cards carry a title +
    # detail directly.
    cards = report.get("draft_awaiting_cards")
    if isinstance(cards, list):
        for c in cards:
            if not isinstance(c, dict):
                continue
            detail = (c.get("detail") or c.get("title") or "").strip()
            out.append({
                "ws_ref": c.get("ws_ref") or "",
                "project": c.get("title") or c.get("ws_ref") or "",
                "classification": c.get("tier") or "",
                "action_needed": detail[:ACTION_NEEDED_MAX],
                "kind": "emit-card",
            })
    return out[:MAX_NEEDS_ATTENTION]


def fleet_status() -> dict[str, Any]:
    hb = _load_json(HEARTBEAT_PATH)
    report = _load_json(OBSERVER_REPORT_PATH)

    last_ts = hb.get("last_pulse_ts")
    age = int(time.time() - last_ts) if isinstance(last_ts, (int, float)) else None

    meta = report.get("_meta") if isinstance(report.get("_meta"), dict) else {}
    counts = meta.get("classification_counts")
    if not isinstance(counts, dict):
        counts = {}

    return {
        "pulse_idx": hb.get("pulse_idx"),
        "pulse_age_seconds": age,
        "heartbeat_status": hb.get("status"),
        "workspace_counts": counts,
        "needs_attention": _needs_attention(report),
    }


def main() -> int:
    argparse.ArgumentParser(
        description="Current state of all workspaces — classifications, what "
                    "needs attention, and pulse age. Returns JSON.").parse_args()
    print(json.dumps(fleet_status()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
