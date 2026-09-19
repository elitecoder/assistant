#!/usr/bin/env python3
"""save-ws-summary — atomic write of one workspace's verdict to disk.

Pure data persistence. The Assistant's main pulse calls this after each
per-ws Agent tool call returns its verdict, so subsequent pulses can reuse
the verdict (or skip the agent call entirely if no JSONL bytes have changed).

Usage:
    bin/save-ws-summary.py --ws-ref workspace:N \\
                           --title "..." \\
                           --cwd /Users/.../firefly-platform \\
                           --pr-refs '[10320, 10326]' \\
                           --json '{...verdict from agent...}'

The verdict JSON should match the per-ws agent's output schema:
    {classification, proposed_actions[], draft_card, summary_for_next_pulse, last_seen_ts}

Pass --observation-json with {ws_ref, workspace_id, observed_sessions, observed_at} captured
before the Observer runs. Each observed session contains surface_id, provider,
and the full session_id. Without this envelope, identity remains unverified.
Never look up current identity when saving an older observation.
observed_at is the epoch-seconds timestamp from before observation reads,
not the save time. Missing legacy timestamps stay null.
Pass --observation-complete true for a returned observation, false for a
failure fallback, or null when a legacy summary has no completion evidence.

This script merges in {title, cwd, pr_refs, last_updated_ts, workspace_id,
observed_sessions, observation_complete, observed_at} and writes
atomically to ~/.assistant/observer-summaries/<ws_ref>.json.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import time
from pathlib import Path

HOME = Path(os.environ["HOME"])
CACHE_DIR = HOME / ".assistant/observer-summaries"


def compute_state_hash(verdict: dict) -> str:
    """Hash of the fields that signal observable state.

    classification + summary_for_next_pulse + sorted proposed_action kinds.
    Stable across pulses when nothing meaningful has changed.
    """
    cls = str(verdict.get("classification", ""))
    summ = str(verdict.get("summary_for_next_pulse", ""))
    kinds = sorted(
        str((a or {}).get("kind", ""))
        for a in (verdict.get("proposed_actions") or [])
    )
    payload = json.dumps([cls, summ, kinds], sort_keys=True)
    return hashlib.sha1(payload.encode()).hexdigest()[:16]


def observation_identity_key(workspace_id: str | None, sessions: list[dict]) -> tuple:
    """Compare UUID spelling without changing provider session identifiers."""
    return (
        (workspace_id or "").casefold(),
        tuple((session["surface_id"].casefold(), session["provider"], session["session_id"])
              for session in sessions),
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--ws-ref", required=True)
    ap.add_argument("--title", default="")
    ap.add_argument("--cwd", default="")
    ap.add_argument("--pr-refs", default="[]", help="JSON array of PR numbers")
    ap.add_argument("--json", required=True, help="JSON verdict from per-ws agent")
    ap.add_argument("--observation-json", default="{}",
                    help="Observation-time identity envelope, not agent output")
    ap.add_argument("--observation-complete", choices=("true", "false", "null"),
                    default="null", help="Mechanically recorded observation outcome")
    args = ap.parse_args()

    try:
        verdict = json.loads(args.json)
    except json.JSONDecodeError as e:
        print(f"ERROR: --json failed to parse: {e}", file=sys.stderr)
        return 2
    if not isinstance(verdict, dict):
        print(f"ERROR: --json must be a JSON object, got {type(verdict).__name__}", file=sys.stderr)
        return 2

    try:
        observation = json.loads(args.observation_json)
        if not isinstance(observation, dict):
            raise ValueError("must be a JSON object")
        workspace_id = observation.get("workspace_id")
        observed_sessions = observation.get("observed_sessions", [])
        observed_at = observation.get("observed_at")
        if observed_at is not None and (
                isinstance(observed_at, bool) or not isinstance(observed_at, (int, float))
                or not math.isfinite(observed_at) or observed_at <= 0):
            raise ValueError("observed_at must be finite positive epoch seconds or null")
        if observation and observation.get("ws_ref") != args.ws_ref:
            raise ValueError("workspace reference differs from the observation")
        if workspace_id is not None and (
                not isinstance(workspace_id, str) or not workspace_id.strip()):
            raise ValueError("workspace_id must be a nonempty string or null")
        if not isinstance(observed_sessions, list):
            raise ValueError("observed_sessions must be a JSON array")
        for session in observed_sessions:
            if not isinstance(session, dict) or any(
                    not isinstance(session.get(key), str) or not session[key].strip()
                    for key in ("surface_id", "provider", "session_id")):
                raise ValueError("each observed session needs surface_id, provider, and session_id")
        if observed_sessions and not workspace_id:
            raise ValueError("observed sessions require a workspace_id")
    except (TypeError, ValueError, OverflowError) as e:
        print(f"ERROR: --observation-json invalid: {e}", file=sys.stderr)
        return 2

    # `next` is required for the dashboard's NEXT line. A missing `next` used
    # to hard-reject (return 2) — but that DROPPED the whole workspace from the
    # summaries dir, so a verdict-shape slip silently un-tracked a live ws
    # (ws:24/ws:4, 2026-06-15). A degraded row beats a vanished one: synthesize
    # a fallback `next`, warn on stderr (so the pulse log still flags the slip),
    # and persist. The Observer prompt remains the place that enforces shape.
    next_text = verdict.get("next")
    if not isinstance(next_text, str) or not next_text.strip():
        kind = str(verdict.get("verdict") or verdict.get("classification") or "")
        summary = verdict.get("summary")
        summary_text = summary.strip() if isinstance(summary, str) else ""
        if kind == "no_action":
            verdict["next"] = "User will close the workspace when ready."
        elif summary_text:
            verdict["next"] = f"(inferred) {summary_text[:140]}"
        else:
            verdict["next"] = "(unknown — Observer emitted no `next`; review workspace directly.)"
        print(
            f"WARN: verdict missing `next`; synthesized fallback rather than dropping ws. "
            f"verdict={kind!r} next={verdict['next']!r}",
            file=sys.stderr,
        )

    try:
        pr_refs = json.loads(args.pr_refs)
    except Exception:
        pr_refs = []

    now = int(time.time())
    new_hash = compute_state_hash(verdict)

    # Read prior summary to carry forward state-unchanged tracking.
    p = CACHE_DIR / f"{args.ws_ref.replace(':', '_')}.json"
    prior_hash = None
    state_unchanged_since_ts = now  # default: brand new entry
    if p.exists():
        try:
            prior = json.loads(p.read_text())
            prior_hash = prior.get("state_hash")
            same_identity = (
                observation_identity_key(
                    prior.get("workspace_id"), prior.get("observed_sessions", []))
                == observation_identity_key(workspace_id, observed_sessions)
            )
            if same_identity and prior_hash == new_hash and prior.get("state_unchanged_since_ts"):
                state_unchanged_since_ts = int(prior["state_unchanged_since_ts"])
        except Exception:
            pass

    out = {
        **verdict,
        "ws_ref": args.ws_ref,
        "title": args.title,
        "cwd": args.cwd,
        "workspace_id": workspace_id,
        "observed_sessions": observed_sessions,
        "observation_complete": json.loads(args.observation_complete),
        "observed_at": observed_at,
        "pr_refs": pr_refs,
        "last_updated_ts": now,
        "state_hash": new_hash,
        "state_unchanged_since_ts": state_unchanged_since_ts,
    }
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(out, indent=2))
    tmp.replace(p)
    stuck_for = now - state_unchanged_since_ts
    print(f"saved: {p} (state_hash={new_hash} stuck_for={stuck_for}s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
