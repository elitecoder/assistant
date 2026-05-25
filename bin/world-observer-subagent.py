#!/usr/bin/env python3
"""world-observer-subagent — fan out one fresh LLM call per workspace.

Architecture (rewritten 2026-05-24, third pass):

The MAIN pulse must NOT read world.json/transcripts/gh — that rots its
context. Observer used to do regex-classification in Python; that
required adding a new branch every time a new pattern showed up
(test-only auto-merge, refactor auto-merge, investigative-no-PR, stale
cards, ...). Whack-a-mole.

This rewrite removes ALL regex/keyword decision-making. Python only:
  - lists cmux workspaces
  - resolves ws_ref → JSONL transcript (via cmux-registry, with a
    title-marker fallback for sessions whose registry entry is stale)
  - reads JSONL turns since `last_seen_ts` (a cheap delta)
  - runs `gh pr view` and caches results 5min
  - fans out N parallel `claude --print` subprocesses (ThreadPoolExecutor)
  - aggregates the per-ws JSON results

Decisions live in the LLM. Per-ws subprocess receives:
  - workspace title + cwd
  - JSONL tail (turns since last_seen_ts, or all turns if first time)
  - PR data (state/title/files) for any PRs the tail cites
  - the Assistant policies excerpt (what cards/actions are valid here)
  - CLAUDE.md auto-loads (lessons + global rules)

Per-ws subprocess emits one JSON object:
  {
    "ws_ref": "...",
    "classification": "ACTIVE|DONE|AWAITING_USER|BROKEN|STRANDED|UNKNOWN",
    "proposed_actions": [
       {"kind":"cleanup|merge-pr|nudge|status-flip|emit-card|purge-awaiting|...",
        "summary":"...", "params":{...}, "evidence":"..."}
    ],
    "draft_card": null | {key, tier, title, detail, alt_actions, confidence},
    "summary_for_next_pulse": "...",
    "last_seen_ts": <epoch>
  }

Steady state: only ws with new JSONL bytes since last call get an LLM
subprocess. Unchanged ws reuse the prior verdict from disk. Cold start
fans out 30 parallel subprocesses; warm pulses fan out 0-3.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

HOME = Path(os.environ["HOME"])
CACHE_DIR = HOME / ".assistant/observer-summaries"
PR_CACHE = HOME / ".assistant/observer-pr-cache.json"
LOG_DIR = HOME / ".assistant/world-observer-log"
CMUX = "/Applications/cmux.app/Contents/Resources/bin/cmux"

# Tunables
PR_CACHE_TTL_SEC = 5 * 60
SUMMARY_MAX_AGE_SEC = 30 * 60   # re-call LLM if a ws hasn't been re-classified for >30min
MAX_PARALLEL = 4                # parallel `claude --print` subprocesses (Bedrock saturates above ~6)
PER_WS_TIMEOUT_SEC = 150
ASSISTANT_PROMPT = HOME / ".claude/spawn-prompts/prompt-assistant-agent.md"

# These ws_refs are operator-protected — never propose actions against them.
PROTECTED_REFS = {"workspace:3", "workspace:108", "workspace:7"}


# ---------- Pure mechanical helpers (no decisions) -------------------------

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
                "title": (w.get("title") or "").strip(),
                "cwd": w.get("current_directory") or "",
            })
    return rows


def find_transcript_for_ws(ws_ref: str, title: str, cwd: str | None = None) -> str | None:
    """Resolve ws_ref → transcript_path.

    PRIMARY: cmux-registry.json keys by panel UUID. Resolve title → panel_ids
             → registry → freshest transcript.

    FALLBACK: when registry yields nothing (claudes started by `cmux send`
              text rather than `cmux new-workspace --command claude` don't
              register), scan the cwd's project directory for a JSONL whose
              first ~5 user turns mention a workspace-unique signature
              (P0-3 / td-074 / W2B / sq-ws8 / AC-008 — case-insensitive).
    """
    try:
        state = json.load(open(HOME / "Library/Application Support/cmux/session-com.cmuxterm.app.json"))
    except Exception:
        state = None
    panel_ids = []
    if state:
        for w in state.get("windows", []):
            for ws in w.get("tabManager", {}).get("workspaces", []):
                if (ws.get("customTitle", "") or "") == title:
                    for p in ws.get("panels", []):
                        if p.get("id"):
                            panel_ids.append(p["id"])
    try:
        reg = json.load(open(HOME / ".claude/cmux-registry.json"))
    except Exception:
        reg = {}
    paths = []
    for tab_id, ent in reg.items():
        if tab_id in panel_ids or ent.get("panel_id") in panel_ids:
            tp = ent.get("transcript_path")
            if tp and os.path.exists(tp):
                paths.append(tp)
    if paths:
        return max(paths, key=os.path.getmtime)

    if not cwd:
        return None
    slug = cwd.replace("/", "-")
    pdir = HOME / ".claude/projects" / slug
    if not pdir.exists():
        return None

    sig_candidates = set()
    for m in re.finditer(r"\b(P\d-\d+|W\d[A-Z]?|td-\d+|sq-ws\d+|AC-\d+)\b", title or "", re.I):
        s = m.group(1)
        sig_candidates.update({s, s.lower(), s.upper()})
    if not sig_candidates:
        return None

    for jsonl in sorted(pdir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            with open(jsonl, "rb") as f:
                head = f.read(65536).decode("utf-8", errors="replace")
        except Exception:
            continue
        n_user_seen = 0
        for line in head.splitlines():
            if not line.strip():
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            msg = d.get("message")
            if not isinstance(msg, dict):
                continue
            content = msg.get("content")
            text = ""
            if isinstance(content, list):
                for c in content:
                    if isinstance(c, dict) and c.get("type") == "text":
                        text += c.get("text", "")
            elif isinstance(content, str):
                text = content
            if msg.get("role") == "user":
                n_user_seen += 1
            if any(sig in text for sig in sig_candidates):
                return str(jsonl)
            if n_user_seen >= 5:
                break
    return None


def read_transcript_tail(path: str | None, since_epoch: int, max_turns: int = 30) -> tuple[list[dict], int]:
    """Read JSONL turns whose ts > since_epoch. Returns (turns, new_last_seen_ts).

    No keyword counting. Just turn extraction. Decisions happen in the LLM.
    """
    if not path or not os.path.exists(path):
        return [], since_epoch
    turns = []
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
            if ts <= since_epoch:
                continue
            msg = d.get("message")
            if not isinstance(msg, dict):
                continue
            role = msg.get("role")
            if role not in ("user", "assistant"):
                continue
            content = msg.get("content")
            text_parts = []
            if isinstance(content, list):
                for c in content:
                    if isinstance(c, dict):
                        t = c.get("type")
                        if t == "text":
                            text_parts.append(c.get("text", ""))
                        elif t == "tool_use":
                            text_parts.append(f"[tool_use:{c.get('name','')}]")
                        elif t == "tool_result":
                            text_parts.append("[tool_result]")
            elif isinstance(content, str):
                text_parts.append(content)
            text = "".join(text_parts).strip()
            if not text:
                continue
            turns.append({"ts": ts, "role": role, "text": text[:1500]})
            last_ts = max(last_ts, ts)
    except Exception:
        pass
    return turns[-max_turns:], last_ts


PR_RE = re.compile(r"(?:pull/|PR\s*#)(\d{4,5})")


def extract_pr_refs_from_turns(turns: list[dict], prior: list[int]) -> list[int]:
    refs = set(prior)
    for t in turns:
        for m in PR_RE.finditer(t.get("text", "")):
            try:
                refs.add(int(m.group(1)))
            except Exception:
                pass
    return sorted(refs)


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


def gh_pr_view(pr_num: int, cache: dict) -> dict:
    key = str(pr_num)
    now_ts = now()
    if key in cache and (now_ts - cache[key].get("ts", 0)) < PR_CACHE_TTL_SEC:
        return cache[key].get("data", {})
    try:
        out = subprocess.check_output(
            ["gh", "pr", "view", str(pr_num),
             "--repo", "Adobe-Firefly/firefly-platform",
             "--json", "state,title,reviewDecision,mergedAt,statusCheckRollup,files"],
            text=True, timeout=10,
        )
        data = json.loads(out)
        cache[key] = {"ts": now_ts, "data": data}
        return data
    except Exception:
        return {"state": "UNKNOWN", "_error": "gh-pr-view-failed"}


def load_summary(ws_ref: str) -> dict | None:
    p = CACHE_DIR / f"{ws_ref.replace(':', '_')}.json"
    if p.exists():
        try:
            return json.load(open(p))
        except Exception:
            return None
    return None


def save_summary(ws_ref: str, summary: dict) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    p = CACHE_DIR / f"{ws_ref.replace(':', '_')}.json"
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(summary, indent=2))
    tmp.replace(p)


def load_assistant_policies() -> str:
    """Pull the `## Assistant policies` section from the prompt file.

    The LLM reads this so it knows what decisions are valid for a dispatcher
    pulse (auto-merge-test-only, workspace cap, model-selection policy, etc).
    """
    if not ASSISTANT_PROMPT.exists():
        return "(Assistant policies not available)"
    try:
        text = ASSISTANT_PROMPT.read_text()
    except Exception:
        return "(read failed)"
    m = re.search(r"^## Assistant policies\b.*", text, re.MULTILINE)
    if not m:
        return "(section not found)"
    start = m.start()
    nxt = re.search(r"^## ", text[m.end():], re.MULTILINE)
    end = m.end() + nxt.start() if nxt else len(text)
    return text[start:end]


# ---------- Per-workspace LLM call -----------------------------------------

PER_WS_SYSTEM_PROMPT = """You are the **Per-Workspace Observer** for the Assistant dispatcher.

You receive ONE workspace's data and must emit ONE JSON object describing
its state and what the Assistant should do about it.

Sources of truth:
  - `~/.claude/CLAUDE.md` is auto-loaded — read its `## Lessons` section
    for global rules.
  - The user message includes the `## Assistant policies` excerpt — those
    are dispatcher-specific rules (workspace cap, auto-merge-test-only-pr,
    spawn model policy, etc).
  - The user message includes JSONL turn tail + PR data for THIS workspace.
    That's the ground truth for what's happening in the session.

Output schema (exact, on stdout, JSON-only):

```json
{
  "ws_ref": "<echo input>",
  "classification": "ACTIVE | DONE | AWAITING_USER | BROKEN | STRANDED | UNKNOWN",
  "proposed_actions": [
    {
      "kind": "cleanup | close-workspace | merge-pr | nudge | status-flip | emit-card | purge-awaiting",
      "summary": "<one sentence>",
      "params": { "ws_ref": "...", "pr": 12345, "td": "td-NNN", "send_text": "...", "key": "..." },
      "evidence": "<verbatim quote from transcript or PR data — the WHY>"
    }
  ],
  "draft_card": null | {
    "key": "assistant:<kind>:<ref>",
    "tier": "T1|T2|T3",
    "title": "<short>",
    "detail": "<2-3 sentences with verbatim evidence>",
    "alt_actions": ["...", "..."],
    "confidence": 0.0
  },
  "summary_for_next_pulse": "<3-5 sentence running summary the next pulse can read as quick context>",
  "last_seen_ts": <int — pass through from input unchanged>
}
```

## Hard rules

- **Ground every proposed action in evidence from the input.** If you can't
  quote a transcript line or PR field that justifies it, leave the action
  out.
- **Default to a draft_card over an action when unsure.** The card surfaces
  to the user; an action is auto-fired by the dispatcher.
- **Do not propose any action against `workspace:3`, `workspace:108`, or
  `workspace:7`** — those are operator-protected (dispatcher itself,
  Assistant agent, E2E reliability watcher).
- **Apply the auto-merge-test-only-pr / auto-merge-refactor-pr policies
  when the conditions are met** — read the policy text, look at the PR
  files list, look for test-pass signals in the transcript, and propose
  `merge-pr` if everything matches. Otherwise propose cleanup-gated card
  or whatever else is appropriate.
- **Output exactly ONE JSON object on stdout. No prose before or after.**
"""


def call_per_ws(payload: dict, model: str) -> dict:
    """Call `claude --print` once for one workspace. Returns parsed JSON or
    an error stub."""
    user_msg = json.dumps(payload, indent=2)
    cmd = [
        os.environ.get("CLAUDE_BIN", str(HOME / ".local/bin/claude")),
        "--print",
        "--model", model,
        "--append-system-prompt", PER_WS_SYSTEM_PROMPT,
        "--output-format", "text",
        "--add-dir", str(HOME / ".claude"),
        "--add-dir", str(HOME / ".assistant"),
        "--dangerously-skip-permissions",
    ]
    started = time.time()
    try:
        proc = subprocess.run(
            cmd, input=user_msg, capture_output=True, text=True,
            timeout=PER_WS_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        return {
            "ws_ref": payload.get("ws_ref"),
            "classification": "UNKNOWN",
            "proposed_actions": [],
            "draft_card": None,
            "summary_for_next_pulse": "(per-ws LLM call timed out)",
            "last_seen_ts": payload.get("last_seen_ts", 0),
            "_error": "timeout",
            "_duration_sec": round(time.time() - started, 1),
        }
    duration = round(time.time() - started, 2)
    if proc.returncode != 0:
        return {
            "ws_ref": payload.get("ws_ref"),
            "classification": "UNKNOWN",
            "proposed_actions": [],
            "draft_card": None,
            "summary_for_next_pulse": f"(per-ws LLM call failed rc={proc.returncode})",
            "last_seen_ts": payload.get("last_seen_ts", 0),
            "_error": f"rc={proc.returncode}",
            "_stderr": (proc.stderr or "")[:400],
            "_duration_sec": duration,
        }

    s = (proc.stdout or "").strip()
    parsed = None
    try:
        parsed = json.loads(s)
    except Exception:
        start = s.find("{")
        if start >= 0:
            try:
                parsed, _ = json.JSONDecoder().raw_decode(s[start:])
            except Exception:
                parsed = None
    if not isinstance(parsed, dict):
        return {
            "ws_ref": payload.get("ws_ref"),
            "classification": "UNKNOWN",
            "proposed_actions": [],
            "draft_card": None,
            "summary_for_next_pulse": "(per-ws LLM output unparseable)",
            "last_seen_ts": payload.get("last_seen_ts", 0),
            "_error": "parse-fail",
            "_stdout_head": s[:600],
            "_duration_sec": duration,
        }
    parsed["_duration_sec"] = duration
    return parsed


def build_per_ws_payload(ws: dict, prior: dict | None, pr_cache: dict, policies_excerpt: str) -> dict:
    """Assemble the input dict for one per-ws LLM call."""
    ws_ref = ws["ref"]
    title = ws["title"]
    cwd = ws.get("cwd", "")
    transcript = find_transcript_for_ws(ws_ref, title, cwd)
    since = int((prior or {}).get("last_seen_ts", 0))
    turns, new_last_ts = read_transcript_tail(transcript, since)

    # PRs cited in the new turns OR in the prior summary
    prior_prs = (prior or {}).get("pr_refs", []) or []
    pr_refs = extract_pr_refs_from_turns(turns, prior_prs)

    pr_data = {}
    for pr in pr_refs:
        pr_data[str(pr)] = gh_pr_view(pr, pr_cache)

    return {
        "ws_ref": ws_ref,
        "title": title,
        "cwd": cwd,
        "transcript_path": transcript,
        "transcript_tail": turns,
        "n_new_turns": len(turns),
        "last_seen_ts": new_last_ts,
        "pr_data": pr_data,
        "prior_summary": (prior or {}).get("summary_for_next_pulse", ""),
        "prior_classification": (prior or {}).get("classification"),
        "prior_pr_refs": prior_prs,
        "is_protected": ws_ref in PROTECTED_REFS,
        "assistant_policies_excerpt": policies_excerpt,
    }


# ---------- Aggregator (no decisions) --------------------------------------

def merge_per_ws_results(results: list[dict]) -> dict:
    """Concatenate the per-ws results into the report shape the main pulse
    expects. NO decision-making here — this is a flatten + meta-tally."""
    candidate_actions = []
    draft_cards = []
    classification_counts = {}
    duration_total = 0.0
    errors = []

    for r in results:
        cls = r.get("classification", "UNKNOWN")
        classification_counts[cls] = classification_counts.get(cls, 0) + 1
        for action in r.get("proposed_actions") or []:
            # Stamp ws_ref + classification onto each action so the main
            # pulse + judgement subagent know the source ws.
            params = dict(action.get("params") or {})
            if "ws_ref" not in params:
                params["ws_ref"] = r.get("ws_ref")
            action_with_meta = {
                **action,
                "params": params,
                "_source_ws": r.get("ws_ref"),
                "_classification": cls,
            }
            candidate_actions.append(action_with_meta)
        card = r.get("draft_card")
        if card:
            draft_cards.append(card)
        if "_error" in r:
            errors.append({"ws_ref": r.get("ws_ref"), "error": r["_error"]})
        if isinstance(r.get("_duration_sec"), (int, float)):
            duration_total += r["_duration_sec"]

    return {
        "candidate_actions": candidate_actions,
        "draft_awaiting_cards": draft_cards,
        "classification_counts": classification_counts,
        "errors": errors,
        "per_ws_duration_total_sec": round(duration_total, 2),
    }


# ---------- Main -----------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--pulse-idx", type=int, required=True)
    ap.add_argument("--model",
                    default=os.environ.get("OBSERVER_MODEL",
                                           "us.anthropic.claude-sonnet-4-6[1m]"))
    ap.add_argument("--state-path", default=str(HOME / ".claude/cache/assistant-state.json"))
    ap.add_argument("--max-parallel", type=int, default=MAX_PARALLEL)
    ap.add_argument("--force-refresh", action="store_true",
                    help="ignore cached summaries; call LLM for every ws")
    args = ap.parse_args()

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    pulse_started = time.time()

    # 1. List workspaces (cheap)
    workspaces = list_cmux_workspaces()
    total = len(workspaces)

    # 2. Decide which workspaces need an LLM call this pulse:
    #    - new ws (no prior summary)
    #    - ws with new JSONL bytes since prior last_seen_ts
    #    - ws whose prior summary is older than SUMMARY_MAX_AGE_SEC
    pr_cache = load_pr_cache()
    policies_excerpt = load_assistant_policies()

    to_call = []     # workspaces needing fresh LLM call this pulse
    cached = []      # workspaces whose prior verdict we reuse

    for ws in workspaces:
        prior = load_summary(ws["ref"])
        if args.force_refresh or not prior:
            to_call.append((ws, prior))
            continue

        # Is there new JSONL since last seen?
        transcript = find_transcript_for_ws(ws["ref"], ws["title"], ws.get("cwd", ""))
        if transcript:
            try:
                tx_mtime = int(os.path.getmtime(transcript))
            except Exception:
                tx_mtime = 0
            if tx_mtime > int(prior.get("last_seen_ts", 0)):
                to_call.append((ws, prior))
                continue

        # Stale-summary policy: re-verdict periodically even with no new bytes
        if (now() - int(prior.get("last_updated_ts", 0))) > SUMMARY_MAX_AGE_SEC:
            to_call.append((ws, prior))
            continue

        cached.append((ws, prior))

    # 3. Build payloads + fan-out
    results = []
    if to_call:
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.max_parallel) as ex:
            futures = {}
            for ws, prior in to_call:
                payload = build_per_ws_payload(ws, prior, pr_cache, policies_excerpt)
                fut = ex.submit(call_per_ws, payload, args.model)
                futures[fut] = (ws, payload)
            for fut in concurrent.futures.as_completed(futures):
                ws, payload = futures[fut]
                try:
                    res = fut.result()
                except Exception as exc:
                    res = {
                        "ws_ref": ws["ref"],
                        "classification": "UNKNOWN",
                        "proposed_actions": [],
                        "draft_card": None,
                        "summary_for_next_pulse": "(per-ws future raised)",
                        "last_seen_ts": payload.get("last_seen_ts", 0),
                        "_error": str(exc)[:200],
                    }
                # Persist
                to_save = {
                    **res,
                    "title": ws["title"],
                    "cwd": ws.get("cwd", ""),
                    "pr_refs": payload.get("prior_pr_refs", []),
                    "last_updated_ts": now(),
                }
                save_summary(ws["ref"], to_save)
                results.append(to_save)

    # 4. Reuse cached results unchanged
    for ws, prior in cached:
        results.append(prior)

    save_pr_cache(pr_cache)

    # 5. Aggregate
    agg = merge_per_ws_results(results)
    duration = round(time.time() - pulse_started, 2)

    report = {
        "_meta": {
            "pulse_idx": args.pulse_idx,
            "total_workspace_count": total,
            "n_llm_calls": len(to_call),
            "n_reused_cached": len(cached),
            "duration_sec": duration,
            "classification_counts": agg["classification_counts"],
            "errors": agg["errors"],
        },
        "candidate_actions": agg["candidate_actions"],
        "draft_awaiting_cards": agg["draft_awaiting_cards"],
    }

    # 6. Log
    log_path = LOG_DIR / f"observer-{int(time.time())}.json"
    try:
        log_path.write_text(json.dumps({
            "ts": time.time(), "report": report,
        }, indent=2))
        report["_log"] = str(log_path)
    except Exception:
        pass

    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
