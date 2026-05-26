#!/usr/bin/env python3
"""Build per-workspace context for Observer.

The Observer reads the workspace's transcript JSONL directly — this script
just gives it the path + a few mechanically-computed signals about cwd
state. No PR fetching, no rule excerpts, no curated turns. The Observer
decides what to read.

Output JSON to stdout:
  {
    "ws_ref": "workspace:N",
    "title": "...",
    "cwd": "...",
    "transcript_path": "/Users/.../<session>.jsonl",
    "last_turn_age_sec": <int|null>,
    "agent_status": "working" | "idle",
    "cwd_dirty": <bool>,
    "cwd_unpushed": <bool>,
    "is_protected": <bool>
  }
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

HOME = Path(os.environ["HOME"])
PROTECTED_REFS = {"workspace:3", "workspace:108", "workspace:7"}


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


def transcript_signals(path: str | None) -> tuple[int | None, str]:
    """Returns (last_turn_age_sec, agent_status) by scanning the JSONL.

    agent_status='working' means a tool_use is in flight (last entry is
    assistant emitting tool_use, no matching tool_result yet). Otherwise 'idle'.
    """
    if not path or not os.path.exists(path):
        return None, "idle"
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None, "idle"
    age = max(0, int(time.time() - mtime))

    # Cheap scan of last ~64KB for pending tool_use detection.
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 65536))
            tail_text = f.read().decode("utf-8", errors="replace")
    except Exception:
        return age, "idle"

    pending_tool_ids: set[str] = set()
    for line in tail_text.splitlines():
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
        if not isinstance(content, list):
            continue
        for c in content:
            if not isinstance(c, dict):
                continue
            t = c.get("type")
            if t == "tool_use":
                tid = c.get("id")
                if tid:
                    pending_tool_ids.add(tid)
            elif t == "tool_result":
                tid = c.get("tool_use_id")
                pending_tool_ids.discard(tid)
    return age, ("working" if pending_tool_ids else "idle")


def cwd_state(cwd: str | None) -> tuple[bool, bool]:
    if not cwd or not os.path.isdir(cwd):
        return False, False
    try:
        r = subprocess.run(
            ["git", "-C", cwd, "status", "--porcelain"],
            capture_output=True, text=True, timeout=5,
        )
        dirty = bool(r.stdout.strip()) if r.returncode == 0 else False
    except Exception:
        dirty = False
    try:
        r = subprocess.run(
            ["git", "-C", cwd, "log", "@{u}..", "--oneline"],
            capture_output=True, text=True, timeout=5,
        )
        unpushed = bool(r.stdout.strip()) if r.returncode == 0 else False
    except Exception:
        unpushed = False
    return dirty, unpushed


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--ws-ref", required=True)
    ap.add_argument("--title", required=True)
    ap.add_argument("--cwd", default="")
    args = ap.parse_args()

    transcript = find_transcript(args.ws_ref, args.title, args.cwd or None)
    age, agent_status = transcript_signals(transcript)
    dirty, unpushed = cwd_state(args.cwd)

    payload = {
        "ws_ref": args.ws_ref,
        "title": args.title,
        "cwd": args.cwd,
        "transcript_path": transcript,
        "last_turn_age_sec": age,
        "agent_status": agent_status,
        "cwd_dirty": dirty,
        "cwd_unpushed": unpushed,
        "is_protected": args.ws_ref in PROTECTED_REFS,
    }
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
