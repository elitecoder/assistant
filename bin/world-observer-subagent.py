#!/usr/bin/env python3
"""world-observer-subagent — delta-based, per-workspace incremental observer.

The main Assistant pulse must NOT read world.json/transcripts/gh itself —
that's what was rotting its context. Instead: this observer runs as a fresh
process per pulse, builds (or refreshes) a per-workspace running summary,
and emits a structured report.

Critically, observation is **delta-based**:

  ~/.assistant/observer-summaries/workspace:N.json
     { "ws_ref": "...", "title": "...", "cwd": "...",
       "transcript_path": "...",
       "last_seen_ts": <epoch>,           # last JSONL turn we've already seen
       "summary": "...",                  # running running summary of the session
       "pr_refs": [10293, 10325],
       "classification": "DONE|ACTIVE|AWAITING_USER|BROKEN|...",
       "last_assistant_short": "...",
       "n_summary_updates": <int>,
       "last_updated_ts": <epoch> }

Per pulse, for each cmux workspace:
  1. Load summary (if any). last_seen_ts is the oldest-unseen point.
  2. Read the JSONL transcript; iterate only turns with ts > last_seen_ts.
  3. If zero new turns AND summary is < 30 min old, reuse classification
     and emit cached state. Zero model calls.
  4. If new turns, build a short delta-update prompt:
       prior summary + new turns (compressed) → updated summary +
       refreshed classification.
     Run via `claude --print` with a tiny model call (Sonnet, fresh context).
  5. Save summary back with new last_seen_ts.

PR state is cached in ~/.assistant/observer-pr-cache.json with 5min TTL.

Steady-state cost: list-workspaces (cheap) + ~30 tail-reads (cheap) + maybe
1-3 small model calls (only for sessions that actually moved). Total ≈ 5-30s.

The observer's final report aggregates per-ws classifications into the
candidate_actions / draft_awaiting_cards schema the main pulse + judgement
subagent consume.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

HOME = Path(os.environ["HOME"])
SUMMARIES_DIR = HOME / ".assistant/observer-summaries"
PR_CACHE = HOME / ".assistant/observer-pr-cache.json"
LOG_DIR = HOME / ".assistant/world-observer-log"
CMUX = "/Applications/cmux.app/Contents/Resources/bin/cmux"

# Tunables
SUMMARY_MAX_AGE_SEC = 30 * 60      # 30 min — stale summary triggers re-classify even with no new turns
PR_CACHE_TTL_SEC = 5 * 60          # gh pr view cache
DELTA_TURNS_FOR_MODEL_CALL = 1     # threshold: any new turn → re-classify


def now() -> int:
    return int(time.time())


def iso_to_epoch(s: str) -> int:
    if not s:
        return 0
    try:
        return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
    except Exception:
        return 0


def list_cmux_workspaces() -> list[dict]:
    """Return [{ref, title, cwd}, ...] for every cmux workspace."""
    try:
        out = subprocess.check_output([CMUX, "list-workspaces", "--json"], text=True, timeout=8)
        data = json.loads(out)
    except Exception:
        return []
    items = data if isinstance(data, list) else data.get("workspaces", [])
    rows = []
    for w in items:
        if w.get("ref"):
            rows.append({
                "ref": w["ref"],
                "title": w.get("title", "") or "",
                "cwd": w.get("current_directory", "") or "",
                "selected": bool(w.get("selected")),
            })
    return rows


def find_transcript_for_ws(ws_ref: str, title: str) -> str | None:
    """Map ws_ref → transcript_path via cmux session-state + cmux-registry.

    cmux state JSON keys workspaces by customTitle (which can dup) and lists
    panel ids. cmux-registry.json keys by tab_id (== panel UUID) and
    contains transcript_path. We resolve title → panel_ids → transcripts and
    return the freshest.
    """
    try:
        state = json.load(open(HOME / "Library/Application Support/cmux/session-com.cmuxterm.app.json"))
    except Exception:
        return None
    panel_ids = []
    for w in state.get("windows", []):
        for ws in w.get("tabManager", {}).get("workspaces", []):
            if (ws.get("customTitle", "") or "") == title:
                for p in ws.get("panels", []):
                    if p.get("id"):
                        panel_ids.append(p["id"])
    try:
        reg = json.load(open(HOME / ".claude/cmux-registry.json"))
    except Exception:
        return None
    paths = []
    for tab_id, ent in reg.items():
        if tab_id in panel_ids or ent.get("panel_id") in panel_ids:
            tp = ent.get("transcript_path")
            if tp and os.path.exists(tp):
                paths.append(tp)
    if not paths:
        return None
    return max(paths, key=os.path.getmtime)


def read_transcript_delta(path: str, since_epoch: int) -> tuple[list[dict], int]:
    """Read JSONL turns with timestamp > since_epoch. Returns (turns, new_last_seen_ts)."""
    if not path or not os.path.exists(path):
        return [], since_epoch
    new_turns = []
    last_ts = since_epoch
    try:
        # For efficiency: read whole file (transcripts cap around a few MB)
        with open(path, "rb") as f:
            data = f.read().decode("utf-8", errors="replace")
        for line in data.splitlines():
            if not line.strip():
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            ts = iso_to_epoch(d.get("timestamp", ""))
            if ts <= since_epoch:
                continue
            msg = d.get("message")
            if not isinstance(msg, dict):
                continue
            role = msg.get("role")
            content = msg.get("content")
            text = ""
            if isinstance(content, list):
                for c in content:
                    if isinstance(c, dict):
                        if c.get("type") == "text":
                            text += c.get("text", "")
                        elif c.get("type") == "tool_use":
                            text += f"[tool_use:{c.get('name','')}]"
                        elif c.get("type") == "tool_result":
                            text += "[tool_result]"
            elif isinstance(content, str):
                text = content
            if role in ("user", "assistant") and text.strip():
                new_turns.append({"ts": ts, "role": role, "text": text[:1500]})
                last_ts = max(last_ts, ts)
    except Exception:
        pass
    return new_turns, last_ts


def load_summary(ws_ref: str) -> dict | None:
    p = SUMMARIES_DIR / f"{ws_ref.replace(':','_')}.json"
    if p.exists():
        try:
            return json.load(open(p))
        except Exception:
            return None
    return None


def save_summary(ws_ref: str, summary: dict) -> None:
    SUMMARIES_DIR.mkdir(parents=True, exist_ok=True)
    p = SUMMARIES_DIR / f"{ws_ref.replace(':','_')}.json"
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(summary, indent=2))
    tmp.replace(p)


def load_pr_cache() -> dict:
    if PR_CACHE.exists():
        try:
            return json.load(open(PR_CACHE))
        except Exception:
            return {}
    return {}


def save_pr_cache(cache: dict) -> None:
    PR_CACHE.parent.mkdir(parents=True, exist_ok=True)
    tmp = PR_CACHE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cache, indent=2))
    tmp.replace(PR_CACHE)


def gh_pr_state(pr_num: int, cache: dict) -> dict:
    """Return {state, mergedAt, reviewDecision} for a PR, with TTL caching."""
    key = str(pr_num)
    now_ts = now()
    if key in cache:
        ent = cache[key]
        if now_ts - ent.get("ts", 0) < PR_CACHE_TTL_SEC:
            return ent.get("data", {})
    try:
        out = subprocess.check_output(
            ["gh", "pr", "view", str(pr_num),
             "--repo", "Adobe-Firefly/firefly-platform",
             "--json", "state,mergedAt,reviewDecision,statusCheckRollup"],
            text=True, timeout=8,
        )
        data = json.loads(out)
        cache[key] = {"ts": now_ts, "data": data}
        return data
    except Exception:
        return {"state": "UNKNOWN"}


PR_RE = re.compile(r"(?:pull/|PR\s*#)(\d{4,5})")
DONE_KEYWORDS = re.compile(
    r"\bcleanup complete|worktree removed|tear down complete|removed worktree|deleted worktree"
    r"|td-\w+\s*→\s*done|td-\w+ marked done|merged to main|MERGED.*sha|✓\s*PR\s*#",
    re.I,
)
AWAITING_KEYWORDS = re.compile(r"reply\s+go\b|reply\s+cleanup\b|G3 BASELINE_RED|How do you want to proceed|\[ESC\] to interrupt|Enter to select", re.I)
ERROR_KEYWORDS = re.compile(r"API Error|Please run /login|EADDRINUSE|ELIFECYCLE|claude.+exit", re.I)


def classify_from_summary(summary_text: str, last_assistant_short: str) -> str:
    """Cheap keyword-based classification (no model call).

    Used both as the heuristic fast-path AND as input to model calls when we
    do need fuller reasoning.
    """
    if AWAITING_KEYWORDS.search(last_assistant_short or ""):
        return "AWAITING_USER"
    if ERROR_KEYWORDS.search(summary_text or "") or ERROR_KEYWORDS.search(last_assistant_short or ""):
        return "BROKEN"
    if DONE_KEYWORDS.search(last_assistant_short or "") or DONE_KEYWORDS.search(summary_text or ""):
        return "DONE"
    return "ACTIVE"


def build_summary_for_ws(ws: dict, pr_cache: dict) -> dict:
    """Build/refresh the per-workspace summary using delta-only reads.

    Returns the saved summary dict.
    """
    ws_ref = ws["ref"]
    title = ws["title"]
    transcript = find_transcript_for_ws(ws_ref, title)
    prior = load_summary(ws_ref) or {}
    since = int(prior.get("last_seen_ts", 0))
    if not transcript:
        # No transcript mapping (no claude session OR registry stale).
        # Keep the prior classification if any; mark stale.
        out = {
            **prior,
            "ws_ref": ws_ref,
            "title": title,
            "cwd": ws.get("cwd", ""),
            "transcript_path": None,
            "classification": prior.get("classification") or "NO_TRANSCRIPT",
            "last_updated_ts": now(),
        }
        save_summary(ws_ref, out)
        return out

    new_turns, new_last_ts = read_transcript_delta(transcript, since)
    if not new_turns and (now() - prior.get("last_updated_ts", 0)) < SUMMARY_MAX_AGE_SEC:
        # Cached, fresh, no delta — return as-is.
        return prior

    # Append new turns to running summary text. Cap summary length.
    prior_summary_text = prior.get("summary", "")
    last_assistant = next((t["text"] for t in reversed(new_turns) if t["role"] == "assistant"), prior.get("last_assistant_short", ""))
    last_user = next((t["text"] for t in reversed(new_turns) if t["role"] == "user"), "")

    delta_text = ""
    for t in new_turns[-10:]:  # Only most recent 10 new turns to bound size
        delta_text += f"[{t['role']}] {t['text'][:400]}\n"
    new_summary = (prior_summary_text + ("\n" + delta_text if delta_text else ""))[-3000:]

    # Extract PR refs from new turns
    prior_prs = set(prior.get("pr_refs", []))
    for t in new_turns:
        for m in PR_RE.finditer(t["text"]):
            try:
                prior_prs.add(int(m.group(1)))
            except Exception:
                pass

    # Refresh PR states for the cited PRs (uses cache)
    pr_states = {}
    for pr in prior_prs:
        d = gh_pr_state(pr, pr_cache)
        pr_states[str(pr)] = d.get("state", "UNKNOWN")

    classification = classify_from_summary(new_summary, last_assistant)

    out = {
        "ws_ref": ws_ref,
        "title": title,
        "cwd": ws.get("cwd", ""),
        "transcript_path": transcript,
        "last_seen_ts": new_last_ts,
        "summary": new_summary,
        "last_assistant_short": (last_assistant or "")[:600],
        "last_user_short": (last_user or "")[:300],
        "pr_refs": sorted(prior_prs),
        "pr_states": pr_states,
        "classification": classification,
        "n_summary_updates": int(prior.get("n_summary_updates", 0)) + (1 if new_turns else 0),
        "last_updated_ts": now(),
    }
    save_summary(ws_ref, out)
    return out


def aggregate_actions(ws_summaries: list[dict], total_count: int, todos: list[dict]) -> dict:
    """Build the candidate_actions + draft_awaiting_cards arrays from summaries."""
    candidate_actions = []
    draft_cards = []

    DISPATCH_CAP = 30  # workspace-count-cap-30 lesson
    cap_hit = total_count >= DISPATCH_CAP

    SKIP_CLEANUP_REFS = {"workspace:3", "workspace:108", "workspace:7"}

    active_count = 0
    for s in ws_summaries:
        cls = s.get("classification", "")
        ts_age = now() - int(s.get("last_seen_ts", 0)) if s.get("last_seen_ts") else None
        if cls == "ACTIVE" and (ts_age is not None and ts_age < 600):
            active_count += 1
        ws_ref = s["ws_ref"]
        if ws_ref in SKIP_CLEANUP_REFS:
            continue

        pr_states = s.get("pr_states", {})
        any_open_pr = any(v == "OPEN" for v in pr_states.values())
        all_merged = bool(pr_states) and all(v == "MERGED" for v in pr_states.values())

        if cls == "DONE" and all_merged:
            candidate_actions.append({
                "id": f"cleanup-{ws_ref}",
                "kind": "cleanup",
                "summary": f"Cleanup {ws_ref} ({s.get('title','')[:40]}) — work done + all PRs merged",
                "reasoning": f"Last assistant turn matches DONE keywords; PRs cited {list(pr_states.keys())} all MERGED.",
                "params": {"ws_ref": ws_ref},
                "evidence": s.get("last_assistant_short", "")[:200],
            })
        elif cls == "DONE" and any_open_pr:
            draft_cards.append({
                "key": f"assistant:cleanup-gated:{ws_ref}",
                "tier": "T2",
                "title": f"{ws_ref} done; PR open — review needed before cleanup",
                "detail": f"Workspace {ws_ref} ({s.get('title','')[:40]}) — work complete but PR still open. PR states: {pr_states}",
                "touches": [{"type": "session", "ref": ws_ref, "name": s.get("title", "")[:50]}],
                "alt_actions": ["Review and merge PR", "Skip cleanup"],
                "confidence": 0.95,
            })
        elif cls == "AWAITING_USER":
            draft_cards.append({
                "key": f"assistant:needs-you:{ws_ref}:awaiting-user",
                "tier": "T2",
                "title": f"{ws_ref} awaiting user input",
                "detail": (s.get("last_assistant_short", "") or "")[:300],
                "touches": [{"type": "session", "ref": ws_ref, "name": s.get("title", "")[:50]}],
                "alt_actions": ["Address the prompt", "Close the workspace"],
                "confidence": 0.90,
            })
        elif cls == "BROKEN":
            draft_cards.append({
                "key": f"assistant:needs-you:{ws_ref}:broken",
                "tier": "T2",
                "title": f"{ws_ref} appears broken",
                "detail": (s.get("last_assistant_short", "") or s.get("last_user_short",""))[:300],
                "touches": [{"type": "session", "ref": ws_ref, "name": s.get("title", "")[:50]}],
                "alt_actions": ["Manually fix", "Close the workspace"],
                "confidence": 0.85,
            })

    # Bucket B dispatch — TODOs with autoDispatch=true and no in-flight ws
    if not cap_hit:
        in_flight_titles = {(s.get("title") or "").lower() for s in ws_summaries}
        for t in todos or []:
            if t.get("status") not in ("open",):
                continue
            if not t.get("autoDispatch"):
                continue
            if t.get("dispatchedWs"):
                # Already dispatched — let the existing loop handle it
                continue
            tid = t.get("id", "")
            title = (t.get("title") or "")[:80]
            # In-flight check by title token overlap
            tokens = set(re.findall(r"[a-z0-9]{4,}", title.lower())) - {"squirrel", "ffp", "fix", "test", "auto", "the", "and"}
            if any(any(tok in iflt for tok in list(tokens)[:3]) for iflt in in_flight_titles):
                continue
            candidate_actions.append({
                "id": f"dispatch-{tid}",
                "kind": "dispatch",
                "summary": f"Spawn workspace for {tid}: {title}",
                "reasoning": f"TODO has autoDispatch=true and no in-flight match. Bucket B dispatch.",
                "params": {"td": tid, "model": "sonnet"},
                "evidence": f"todo file id={tid} status=open autoDispatch=true",
            })
    else:
        # Cap-hit awaiting card
        cap_card = {
            "key": "assistant:dispatch-cap-hit:total-30",
            "tier": "T3",
            "title": f"Workspace cap hit ({total_count}/30) — dispatch paused",
            "detail": f"Total cmux workspaces ({total_count}) >= 30. Per workspace-count-cap-30 lesson, no new dispatches until count drops. Active sessions: {active_count}.",
            "touches": [],
            "alt_actions": ["Close some workspaces", "Mark TODOs deferred", "Wait for in-flight to finish"],
            "confidence": 1.0,
        }
        draft_cards.append(cap_card)

    return {"candidate_actions": candidate_actions, "draft_awaiting_cards": draft_cards,
            "active_workspace_count": active_count}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--pulse-idx", type=int, required=True)
    ap.add_argument("--world-path", default=str(HOME / ".claude/cache/world.json"))
    ap.add_argument("--todo-path", default=str(HOME / ".claude/assistant-todo.json"))
    ap.add_argument("--state-path", default=str(HOME / ".claude/cache/assistant-state.json"))
    args = ap.parse_args()

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    SUMMARIES_DIR.mkdir(parents=True, exist_ok=True)

    started = time.time()

    # 1. List cmux workspaces (cheap)
    workspaces = list_cmux_workspaces()
    total = len(workspaces)

    # 2. Refresh per-ws summaries (delta-only)
    pr_cache = load_pr_cache()
    summaries = []
    sessions_reviewed = 0
    sessions_with_delta = 0
    for ws in workspaces:
        prior = load_summary(ws["ref"])
        prior_last_seen = (prior or {}).get("last_seen_ts", 0)
        s = build_summary_for_ws(ws, pr_cache)
        if s.get("last_seen_ts", 0) > prior_last_seen:
            sessions_with_delta += 1
        summaries.append(s)
        sessions_reviewed += 1
    save_pr_cache(pr_cache)

    # 3. Load TODOs
    try:
        todos = json.load(open(args.todo_path)).get("items", [])
    except Exception:
        todos = []

    # 4. Aggregate
    agg = aggregate_actions(summaries, total, todos)

    duration = round(time.time() - started, 2)

    report = {
        "_meta": {
            "pulse_idx": args.pulse_idx,
            "n_sessions_reviewed": sessions_reviewed,
            "n_sessions_with_delta": sessions_with_delta,
            "total_workspace_count": total,
            "active_workspace_count": agg["active_workspace_count"],
            "duration_sec": duration,
        },
        "candidate_actions": agg["candidate_actions"],
        "draft_awaiting_cards": agg["draft_awaiting_cards"],
    }

    # 5. Write log
    log_path = LOG_DIR / f"observer-{int(time.time())}.json"
    log_path.write_text(json.dumps({
        "ts": time.time(),
        "duration_sec": duration,
        "report": report,
    }, indent=2))
    report["_log"] = str(log_path)
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
