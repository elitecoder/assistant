#!/usr/bin/env python3
"""
world-scanner.py — single observer that builds the canonical world snapshot.

Reads every input the Evaluator + Renderer need, joins them, writes one
JSON file: ~/.claude/cache/world.json. Pure observer — no proposals, no
side effects beyond writing world.json.

Inputs:
  - cmux tree (live workspaces + surfaces + tty)
  - ps (which Claude PIDs are alive)
  - ~/.claude/cmux-registry.json (session_id ↔ tab_id ↔ cwd ↔ transcript_path)
  - ~/.architect/orchestrator-registry.json (workers list — for is_cron tag)
  - ~/.claude/cache/session-context.json (transcript turns; maintained by watcher)
  - ~/.claude/cache/dashboard-state.json (workspace classifications + screen hashes)
  - ~/.claude/assistant-todo.json (TODO board)
  - ~/.architect/orchestrator-proposals/*.json (current proposal set)
  - ~/.architect/orchestrator-ledger/*.json (recent fires)
  - ~/.architect/orchestrator-inbox-archive/<today>/*.json (recent worker events,
                                                            for activity feed)
  - vm_stat (memory pressure)

Cadence: 30s via LaunchAgent. Stdlib only.
"""

import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HOME = Path(os.environ["HOME"])
OUT_PATH = HOME / ".claude/cache/world.json"
CMUX_REGISTRY = HOME / ".claude/cmux-registry.json"
ORCH_REGISTRY = HOME / ".architect/orchestrator-registry.json"
SESSION_CTX = HOME / ".claude/cache/session-context.json"
DASHBOARD_STATE = HOME / ".claude/cache/dashboard-state.json"
TODO_PATH = HOME / ".claude/assistant-todo.json"
PROPOSALS_DIR = HOME / ".architect/orchestrator-proposals"
LEDGER_DIR = HOME / ".architect/orchestrator-ledger"
INBOX_ARCHIVE = HOME / ".architect/orchestrator-inbox-archive"
LOG_DIR = HOME / ".assistant/logs"
CMUX_BIN = shutil.which("cmux") or "/Applications/cmux.app/Contents/Resources/bin/cmux"

ACTIVITY_HOURS = 24


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt):
    return dt.isoformat().replace("+00:00", "Z")


def parse_iso(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, TypeError, AttributeError):
        return None


def log(msg, level="info"):
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOG_DIR / "world-scanner.out", "a") as f:
        f.write(f"[{iso(utc_now())}] [{level}] {msg}\n")


def load_json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return default if default is not None else {}


def load_json_dir(d):
    p = Path(d)
    if not p.exists():
        return []
    out = []
    for f in p.glob("*.json"):
        try:
            out.append(json.loads(f.read_text()))
        except Exception:
            continue
    return out


def pid_alive(pid):
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except (ProcessLookupError, ValueError, PermissionError, TypeError):
        return False


def ps_tty(pid):
    try:
        return subprocess.run(
            ["ps", "-p", str(pid), "-o", "tty="],
            capture_output=True, text=True, timeout=3,
        ).stdout.strip() or None
    except Exception:
        return None


def cmux_tree():
    try:
        r = subprocess.run(
            [CMUX_BIN, "tree", "--all", "--json"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0:
            return None
        return json.loads(r.stdout)
    except Exception as e:
        log(f"cmux tree failed: {e}", "warn")
        return None


def read_mem_pct():
    """Return memory pressure as 0-100. macOS vm_stat: pages used vs total."""
    try:
        r = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=3)
        m = {}
        page_size = 4096
        for line in r.stdout.splitlines():
            if "page size of" in line:
                pm = re.search(r"page size of (\d+)", line)
                if pm:
                    page_size = int(pm.group(1))
            mm = re.match(r"^([^:]+):\s+(\d+)", line.strip())
            if mm:
                m[mm.group(1).strip()] = int(mm.group(2))
        free = m.get("Pages free", 0) + m.get("Pages inactive", 0) + m.get("Pages speculative", 0)
        total = sum(v for k, v in m.items() if k.startswith("Pages "))
        if total <= 0:
            return None
        used_pct = 100.0 * (1.0 - free / total)
        return round(used_pct, 1)
    except Exception:
        return None


def build_workspace_index(tree):
    """Return list of {ws_ref, title, surfaces:[{ref, tty, type, title}]}."""
    out = []
    if not tree:
        return out
    for win in tree.get("windows", []) or []:
        for ws in win.get("workspaces", []) or []:
            entry = {
                "ws_ref": ws.get("ref"),
                "title": ws.get("title") or "",
                "index": ws.get("index", 0),
                "surfaces": [],
            }
            for pane in ws.get("panes", []) or []:
                for surf in pane.get("surfaces", []) or []:
                    entry["surfaces"].append({
                        "ref": surf.get("ref"),
                        "tty": surf.get("tty"),
                        "type": surf.get("type"),
                        "title": surf.get("title") or "",
                    })
            out.append(entry)
    return out


def build_live_sessions():
    """Build the canonical live-session list by joining cmux-registry (records
    every Claude session ever) with pid-alive filter. Each entry carries
    session_id, pid, cwd, tty, transcript_path, and the workspace_ref it lives
    in (best-effort via tty join with cmux tree)."""
    reg = load_json(CMUX_REGISTRY, {})
    out = {}
    for tab_id, e in reg.items():
        pid = e.get("claude_pid")
        if not pid_alive(pid):
            continue
        sid = e.get("session_id")
        if not sid:
            continue
        prev = out.get(sid)
        # Multiple registry entries can share session_id (resume); keep most recent.
        if prev and prev.get("ts", 0) > e.get("ts", 0):
            continue
        out[sid] = {
            "session_id": sid,
            "pid": int(pid),
            "cwd": e.get("cwd"),
            "transcript_path": e.get("transcript_path"),
            "tab_id": tab_id,
            "ts": e.get("ts"),
        }
    return out


def join_workspaces_to_sessions(workspaces, live_sessions):
    """Add ws_ref / surface_ref / surface_title to each live session by tty.
    Add session_id list to each workspace entry."""
    # Build tty → ws/surface map
    tty_to_ws = {}
    for ws in workspaces:
        for surf in ws.get("surfaces", []):
            tty = surf.get("tty")
            if tty:
                tty_to_ws[tty] = {
                    "ws_ref": ws["ws_ref"],
                    "surface_ref": surf["ref"],
                    "surface_title": surf.get("title", ""),
                    "ws_title": ws.get("title", ""),
                }

    for sid, sess in live_sessions.items():
        tty = ps_tty(sess["pid"])
        sess["tty"] = tty
        info = tty_to_ws.get(tty) if tty else None
        if info:
            sess["ws_ref"] = info["ws_ref"]
            sess["surface_ref"] = info["surface_ref"]
            sess["ws_title"] = info["ws_title"]
            sess["surface_title"] = info["surface_title"]

    # Reverse-index ws_ref → session_ids
    for ws in workspaces:
        ws["session_ids"] = [
            s["session_id"] for s in live_sessions.values()
            if s.get("ws_ref") == ws["ws_ref"]
        ]


def tag_cron_workers(live_sessions):
    """Tag sessions running in known orchestrator worker workspaces as is_cron."""
    oreg = load_json(ORCH_REGISTRY, {})
    cron_ws_refs = set()
    for name, w in (oreg.get("workers") or {}).items():
        ref = w.get("workspace_ref")
        if ref:
            cron_ws_refs.add(ref)
    cron_cwds = {"/Users/mukuls/.architect"}
    for sid, sess in live_sessions.items():
        ws_ref = sess.get("ws_ref")
        cwd = sess.get("cwd") or ""
        sess["is_cron"] = (
            ws_ref in cron_ws_refs
            or cwd.rstrip("/") in {c.rstrip("/") for c in cron_cwds}
        )


def merge_session_context(live_sessions):
    """Pull last_user / last_assistant / queue_pending from session-context.json
    (maintained event-driven by the watcher)."""
    ctx = load_json(SESSION_CTX, {})
    by_sess = ctx.get("by_session") or {}
    for sid, sess in live_sessions.items():
        c = by_sess.get(sid)
        if c:
            sess["last_user"] = c.get("last_user")
            sess["last_assistant"] = c.get("last_assistant")
            sess["queue_pending"] = c.get("queue_pending", 0)
            sess["user_unanswered"] = c.get("user_unanswered", False)
            sess["recent_turns"] = c.get("recent_turns", [])


def compute_session_age(sess, now):
    cands = []
    for k in ("last_user", "last_assistant"):
        t = (sess.get(k) or {}).get("ts")
        ts = parse_iso(t)
        if ts:
            cands.append(ts)
    if not cands:
        return None, None
    last = max(cands)
    return int((now - last).total_seconds()), iso(last)


def load_inbox_recent(now):
    cutoff = now - timedelta(hours=ACTIVITY_HOURS)
    out = []
    for offset in (0, 1):
        date_str = (now - timedelta(days=offset)).strftime("%Y-%m-%d")
        d = INBOX_ARCHIVE / date_str
        if not d.exists():
            continue
        for p in d.glob("*.json"):
            try:
                e = json.loads(p.read_text())
                ts = parse_iso(e.get("ts"))
                if ts and ts >= cutoff:
                    out.append(e)
            except Exception:
                continue
    # Also current inbox (not yet archived).
    inbox = HOME / ".architect/orchestrator-inbox"
    if inbox.exists():
        for p in inbox.glob("*.json"):
            try:
                e = json.loads(p.read_text())
                ts = parse_iso(e.get("ts"))
                if ts and ts >= cutoff:
                    out.append(e)
            except Exception:
                continue
    return out


def build():
    now = utc_now()
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    tree = cmux_tree()
    workspaces = build_workspace_index(tree)
    live_sessions = build_live_sessions()
    join_workspaces_to_sessions(workspaces, live_sessions)
    tag_cron_workers(live_sessions)
    merge_session_context(live_sessions)

    # Compute per-session activity age and bucket.
    for sid, sess in live_sessions.items():
        age_sec, last_ts = compute_session_age(sess, now)
        sess["last_turn_age_sec"] = age_sec
        sess["last_turn_ts"] = last_ts

    # Recent ledger entries (last 24h).
    ledger_recent = []
    cutoff_24h = now - timedelta(hours=ACTIVITY_HOURS)
    for entry in load_json_dir(LEDGER_DIR):
        ts = parse_iso(entry.get("ts"))
        if ts and ts >= cutoff_24h:
            ledger_recent.append(entry)
    ledger_recent.sort(key=lambda e: parse_iso(e.get("ts")) or now, reverse=True)

    proposals = load_json_dir(PROPOSALS_DIR)
    todo = load_json(TODO_PATH, {"items": [], "completed": []})
    inbox_events = load_inbox_recent(now)
    dashboard_state = load_json(DASHBOARD_STATE, {})

    # Counts for the summary block.
    cron = sum(1 for s in live_sessions.values() if s.get("is_cron"))
    human = sum(1 for s in live_sessions.values() if not s.get("is_cron"))
    truly_active = sum(
        1 for s in live_sessions.values()
        if not s.get("is_cron")
        and s.get("last_turn_age_sec") is not None
        and s["last_turn_age_sec"] < 1800
    )
    awaiting = [
        p for p in proposals
        if (p.get("needs_you") or p.get("status") == "needs_you" or p.get("held"))
        and p.get("status") not in {"done", "expired", "vetoed"}
    ]

    payload = {
        "_meta": {
            "built_at": iso(now),
            "scanner_version": 1,
            "memory_pct": read_mem_pct(),
        },
        "counts": {
            "workspaces": len(workspaces),
            "live_sessions": len(live_sessions),
            "human_sessions": human,
            "cron_sessions": cron,
            "truly_active_30m": truly_active,
            "proposals_open": sum(1 for p in proposals if p.get("status") not in {"done", "expired", "vetoed"}),
            "proposals_awaiting": len(awaiting),
            "ledger_24h": len(ledger_recent),
            "todo_open": len(todo.get("items", [])),
            "todo_p0_p1": sum(1 for i in todo.get("items", []) if i.get("priority") in {"P0", "P1"}),
        },
        "workspaces": workspaces,
        "live_sessions": list(live_sessions.values()),
        "proposals": proposals,
        "ledger_recent": ledger_recent,
        "inbox_events_recent": inbox_events,
        "todo": todo,
        "dashboard_state_meta": dashboard_state.get("_meta", {}),
    }

    OUT_PATH.write_text(json.dumps(payload, indent=2, default=str))
    log(
        f"scan: ws={len(workspaces)} live={len(live_sessions)} "
        f"(human={human} cron={cron} active30m={truly_active}) "
        f"proposals_open={payload['counts']['proposals_open']} "
        f"awaiting={len(awaiting)} ledger24h={len(ledger_recent)} "
        f"todo_open={payload['counts']['todo_open']} mem={payload['_meta']['memory_pct']}%"
    )


if __name__ == "__main__":
    build()
