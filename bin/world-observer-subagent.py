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


def read_transcript_delta(path: str, since_epoch: int) -> tuple[list[dict], int, dict]:
    """Read JSONL turns with timestamp > since_epoch.

    Returns (turns, new_last_seen_ts, signals) where signals is a dict
    with three independent stranded-detection signals (extracted from
    the WHOLE file, not just delta — STRANDED detection needs lifetime
    counts not deltas).
    """
    signals = {
        "n_user_turns": 0,
        "n_assistant_turns": 0,
        "n_tool_uses": 0,
        "first_user_text": "",
        "first_assistant_ts": 0,
        "last_assistant_ts": 0,
    }
    if not path or not os.path.exists(path):
        return [], since_epoch, signals
    new_turns = []
    last_ts = since_epoch
    try:
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
            msg = d.get("message")
            if not isinstance(msg, dict):
                continue
            role = msg.get("role")
            content = msg.get("content")
            text = ""
            tool_uses_in_turn = 0
            if isinstance(content, list):
                for c in content:
                    if isinstance(c, dict):
                        if c.get("type") == "text":
                            text += c.get("text", "")
                        elif c.get("type") == "tool_use":
                            text += f"[tool_use:{c.get('name','')}]"
                            tool_uses_in_turn += 1
                        elif c.get("type") == "tool_result":
                            text += "[tool_result]"
            elif isinstance(content, str):
                text = content

            # Lifetime signal counters (count BEFORE the since_epoch filter — these
            # are about the session as a whole, not just the new delta).
            if role == "user" and text.strip():
                signals["n_user_turns"] += 1
                if not signals["first_user_text"]:
                    signals["first_user_text"] = text[:300]
            elif role == "assistant":
                signals["n_assistant_turns"] += 1
                if not signals["first_assistant_ts"]:
                    signals["first_assistant_ts"] = ts
                signals["last_assistant_ts"] = max(signals["last_assistant_ts"], ts)
                signals["n_tool_uses"] += tool_uses_in_turn

            # Delta turns for the running summary
            if ts <= since_epoch:
                continue
            if role in ("user", "assistant") and text.strip():
                new_turns.append({"ts": ts, "role": role, "text": text[:1500]})
                last_ts = max(last_ts, ts)
    except Exception:
        pass
    return new_turns, last_ts, signals


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
        # No transcript mapping. cmux-registry didn't index a transcript for
        # any of this workspace's panels. Two real situations:
        #   (a) the workspace has no claude process at all (bare shell — eg.
        #       a `pnpm dev` shell), OR
        #   (b) claude was launched but the prompt was never delivered, so
        #       claude is sitting idle waiting for input (the ws:81 case
        #       from the cmux-saturation incident).
        #
        # Distinguishing (a) from (b) is hard from cmux state alone (the
        # session-state JSON only marks `agent` for cmux-launched claudes,
        # not for shells where someone typed `claude` by hand). We err on
        # the side of recovery: any workspace whose title matches the
        # Auto:* / W2A / W2B / P0-* / "td-NN ..." dispatch-naming convention
        # should have had claude launched, so missing transcript = STRANDED.
        # Workspaces titled like a manual session (not matching dispatch
        # naming) get NO_TRANSCRIPT and are left alone.
        DISPATCH_TITLE_RE = re.compile(
            r"^(Auto:\s*|W\d[A-Z]?\s+|P\d-\d+\s+|sq-ws\d+|sq-w\d+|fix-|squirrel-)",
            re.I,
        )
        is_dispatch_titled = bool(DISPATCH_TITLE_RE.match(title or "")) or "td-" in (title or "").lower()
        if is_dispatch_titled:
            cls = "STRANDED"
            stranded_reason = "no-transcript-mapping"
        else:
            cls = "NO_TRANSCRIPT"
            stranded_reason = None

        out = {
            **prior,
            "ws_ref": ws_ref,
            "title": title,
            "cwd": ws.get("cwd", ""),
            "transcript_path": None,
            "classification": cls,
            "stranded_reason": stranded_reason,
            "signals": {"n_user_turns": 0, "n_assistant_turns": 0, "n_tool_uses": 0},
            "transcript_age_sec": None,
            "recovery_attempts": int(prior.get("recovery_attempts", 0)),
            "last_updated_ts": now(),
        }
        save_summary(ws_ref, out)
        return out

    new_turns, new_last_ts, signals = read_transcript_delta(transcript, since)
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

    # === Stranded detection (3 transcript-grounded signals) ===
    #
    # A workspace is STRANDED when claude is up but no real work has happened.
    # We check three independent signals, all derived from the JSONL transcript
    # (no surface scraping):
    #
    #   Signal 1: at least one user turn (the prompt was delivered + submitted)
    #   Signal 2: at least one assistant turn (the model produced output)
    #   Signal 3: at least one tool_use OR very-recent assistant activity
    #             (proves the model is actually doing something, not just a
    #             one-shot text reply that never engaged the agent loop)
    #
    # Sub-reasons per failure mode:
    NOW = now()
    stranded_reason = None
    SPAWN_GRACE_SEC = 90  # don't classify a fresh spawn as stranded
    PROMPT_PATH_RE = re.compile(r"~?/?(?:Users/\w+/)?\.claude/spawn-prompts/")
    looks_like_dispatched = bool(PROMPT_PATH_RE.search(signals.get("first_user_text", "") or ""))

    # Age of the freshest transcript activity (epoch). For STRANDED detection
    # we want "how long has the workspace been alive without making progress?"
    # The transcript file's mtime is a reasonable proxy when no assistant turn
    # has ever happened.
    try:
        transcript_mtime = int(os.path.getmtime(transcript))
    except Exception:
        transcript_mtime = NOW
    transcript_age_sec = NOW - transcript_mtime
    last_assistant_age_sec = NOW - signals["last_assistant_ts"] if signals["last_assistant_ts"] else None

    if looks_like_dispatched or signals["n_user_turns"] > 0 or transcript_age_sec > SPAWN_GRACE_SEC:
        if signals["n_user_turns"] == 0 and transcript_age_sec > SPAWN_GRACE_SEC:
            stranded_reason = "no-user-turn"   # prompt was never delivered
        elif signals["n_assistant_turns"] == 0 and signals["n_user_turns"] > 0 and transcript_age_sec > 300:
            stranded_reason = "no-assistant-turn"  # model never started
        elif (signals["n_tool_uses"] == 0 and signals["n_assistant_turns"] > 0
              and last_assistant_age_sec is not None and last_assistant_age_sec > 1800):
            # Model thunked once + did nothing for >30 min. Real "thinking but
            # not acting" pattern (rare but observed).
            stranded_reason = "no-tool-use-stale"

    classification = classify_from_summary(new_summary, last_assistant)
    if stranded_reason:
        classification = "STRANDED"

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
        "stranded_reason": stranded_reason,
        "signals": signals,
        "transcript_age_sec": transcript_age_sec,
        "recovery_attempts": int(prior.get("recovery_attempts", 0)),
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
        elif cls == "STRANDED":
            attempts = int(s.get("recovery_attempts", 0))
            reason = s.get("stranded_reason", "unknown")
            sigs = s.get("signals", {})
            sig_summary = (
                f"signals: n_user={sigs.get('n_user_turns',0)} "
                f"n_assistant={sigs.get('n_assistant_turns',0)} "
                f"n_tool_uses={sigs.get('n_tool_uses',0)} "
                f"transcript_age={s.get('transcript_age_sec',0)}s"
            )
            if attempts < 3:
                # Find the most recent prompt file in ~/.claude/spawn-prompts/
                # for THIS workspace, by mtime. The first user turn (if any)
                # gives us the prompt path; otherwise fall back to mtime-newest.
                prompt_path = None
                first_user = sigs.get("first_user_text", "") or ""
                m = re.search(r"(/Users/\w+/\.claude/spawn-prompts/[^\s\"']+)", first_user)
                if m:
                    prompt_path = m.group(1)
                else:
                    # No-user-turn case — pick the freshest prompt file system-wide
                    # that's older than the transcript file (i.e. was the intended
                    # prompt for this spawn).
                    spawn_dir = HOME / ".claude/spawn-prompts"
                    if spawn_dir.exists():
                        candidates = sorted(spawn_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
                        for p in candidates[:10]:
                            if p.stat().st_mtime <= (now() - 30):  # at least 30s before now
                                prompt_path = str(p)
                                break
                candidate_actions.append({
                    "id": f"recover-stranded-{ws_ref}-attempt-{attempts+1}",
                    "kind": "nudge",
                    "summary": f"Recover stranded {ws_ref} (attempt {attempts+1}/3): {reason}",
                    "reasoning": f"Workspace classified STRANDED. {sig_summary}. Re-paste prompt + Enter.",
                    "params": {
                        "ws_ref": ws_ref,
                        "send_text": (
                            f"Read {prompt_path} in full and execute every instruction in it."
                            if prompt_path
                            else "Continue."
                        ),
                        "send_enter": True,
                        "increment_recovery_attempts": True,
                    },
                    "evidence": f"signals from JSONL transcript: {json.dumps(sigs)}",
                })
            else:
                # 3 strikes — escalate to needs-you and stop trying.
                draft_cards.append({
                    "key": f"assistant:needs-you:{ws_ref}:dispatch-broken",
                    "tier": "T2",
                    "title": f"{ws_ref} dispatch broken — manual rescue needed",
                    "detail": (
                        f"Stranded for {attempts} consecutive recovery attempts. "
                        f"Reason: {reason}. {sig_summary}. "
                        f"Title: {s.get('title','')[:60]}"
                    ),
                    "touches": [{"type": "session", "ref": ws_ref, "name": s.get("title", "")[:50]}],
                    "alt_actions": ["Manually paste prompt", "Close the workspace", "Investigate why claude isn't receiving input"],
                    "confidence": 0.95,
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
