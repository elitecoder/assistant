#!/usr/bin/env python3
"""build-ws-context — assemble the JSON payload for one per-workspace agent.

Pure data assembly, no LLM. The Assistant's main pulse calls this once per
workspace it wants to re-classify, then passes the JSON to a parallel Agent
tool call. The agent applies CLAUDE.md lessons + the Assistant policies
excerpt + the data from this script and returns a verdict.

Usage:
    bin/build-ws-context.py --ws-ref workspace:N \\
                            --title "..." \\
                            --cwd /Users/.../firefly-platform

Output: one JSON object on stdout containing:
    - workspace ref / title / cwd
    - transcript_path (resolved via cmux-registry, with title-marker fallback)
    - transcript_tail (turns since prior last_seen_ts, ≤30 turns ≤1500ch each)
    - pr_data (gh pr view {state,title,body,reviewDecision,statusCheckRollup,files})
    - prior_summary + prior_classification (running context)
    - is_protected (workspace:3/108/7 are off-limits)
    - assistant_policies_excerpt (verbatim from prompt)
    - last_seen_ts (epoch — agent should pass this back unchanged)
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
CACHE_DIR = HOME / ".assistant/observer-summaries"
PR_CACHE = HOME / ".assistant/observer-pr-cache.json"
ASSISTANT_PROMPT = HOME / ".claude/spawn-prompts/prompt-assistant-agent.md"
PR_CACHE_TTL_SEC = 5 * 60
PROTECTED_REFS = {"workspace:3", "workspace:108", "workspace:7"}


def now() -> int:
    return int(time.time())


def iso_to_epoch(s: str) -> int:
    if not s:
        return 0
    try:
        return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
    except Exception:
        return 0


def find_transcript(ws_ref: str, title: str, cwd: str | None) -> str | None:
    """Resolve ws_ref → transcript_path via cmux-registry (primary) + a
    title-marker scan of the cwd's project dir (fallback)."""
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


def extract_pr_refs(turns: list[dict], prior: list[int]) -> list[int]:
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
             "--json", "state,title,body,reviewDecision,mergedAt,statusCheckRollup,files"],
            text=True, timeout=10,
        )
        data = json.loads(out)
        cache[key] = {"ts": now_ts, "data": data}
        return data
    except Exception:
        return {"state": "UNKNOWN", "_error": "gh-pr-view-failed"}


def load_summary(ws_ref: str) -> dict:
    p = CACHE_DIR / f"{ws_ref.replace(':', '_')}.json"
    if p.exists():
        try:
            return json.load(open(p))
        except Exception:
            pass
    return {}


def load_assistant_policies() -> str:
    if not ASSISTANT_PROMPT.exists():
        return "(policies file not available)"
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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--ws-ref", required=True)
    ap.add_argument("--title", required=True)
    ap.add_argument("--cwd", default="")
    args = ap.parse_args()

    prior = load_summary(args.ws_ref)
    transcript = find_transcript(args.ws_ref, args.title, args.cwd or None)
    since = int(prior.get("last_seen_ts", 0))
    turns, new_last_ts = read_transcript_tail(transcript, since)

    prior_prs = prior.get("pr_refs", []) or []
    # Also pull PR refs out of prior_summary text — the LLM agent's summary
    # often mentions PRs that the cached pr_refs list missed (cache wasn't
    # always populated correctly in older observer code paths).
    prior_summary_text = prior.get("summary_for_next_pulse", "") or ""
    for m in PR_RE.finditer(prior_summary_text):
        try:
            n = int(m.group(1))
            if n not in prior_prs:
                prior_prs.append(n)
        except Exception:
            pass
    pr_refs = extract_pr_refs(turns, prior_prs)
    # Always include prior PR refs even when no new turns — the PR may have
    # changed state (e.g. CI completed, reviewer approved) since last pulse,
    # and the agent needs to see the freshest PR data for cleanup-gating /
    # auto-merge decisions. The 5min cache means this is cheap.
    for pr in prior_prs:
        if pr not in pr_refs:
            pr_refs.append(pr)
    pr_refs = sorted(set(pr_refs))
    pr_cache = load_pr_cache()
    pr_data = {}
    for pr in pr_refs:
        pr_data[str(pr)] = gh_pr_view(pr, pr_cache)
    save_pr_cache(pr_cache)

    payload = {
        "ws_ref": args.ws_ref,
        "title": args.title,
        "cwd": args.cwd,
        "transcript_path": transcript,
        "transcript_tail": turns,
        "n_new_turns": len(turns),
        "last_seen_ts": new_last_ts,
        "pr_data": pr_data,
        "pr_refs": pr_refs,
        "prior_summary": prior.get("summary_for_next_pulse", ""),
        "prior_classification": prior.get("classification"),
        "prior_proposed_actions": prior.get("proposed_actions", []),
        "is_protected": args.ws_ref in PROTECTED_REFS,
        "assistant_policies_excerpt": load_assistant_policies(),
    }
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
