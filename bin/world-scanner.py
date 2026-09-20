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
  - ~/.assistant/events.jsonl (event-spine health: counts + latest-event age
                               per source, so a stalled spine is visible)
  - ~/.cmux-session-ledger.jsonl (first recorded start for a verified identity)

Cadence: 30s via LaunchAgent. Stdlib only.

Workspace and surface UUIDs identify current cmux objects. A session's
identity_status is verified when a live resume binding and process agree, or a
SessionStart registry entry matches both UUIDs and a foreground provider process
whose start predates that hook. Registry keys are workspace UUIDs, not surfaces.
context_status verifies the cached session/provider association, not freshness;
context_built_at supplies the cache clock. Missing bindings and unsupported
providers stay unknown. first_recorded_at is not a session creation timestamp.
Internal session keys include provider, full session ID, workspace UUID, and
surface UUID, so resumed sessions retain each binding in the exported list.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

# The canonical connector tri-state (not_configured|ok|error) + the known-
# connector registry live in the connector base, so world-scanner, the brief
# and the dashboard never drift on what "connected" means. Import it the way the
# standalone connectors do (this launchd script must put src/ on the path
# first); degrade to a None fallback so a broken import can NEVER take down the
# 30s world snapshot — build_connectors_summary just returns {} in that case.
_REPO = Path(__file__).resolve().parents[1]
if str(_REPO / "src") not in sys.path:
    sys.path.insert(0, str(_REPO / "src"))
try:
    from assistant import connector as _connector
    _classify_connector = _connector.classify_connector
    _read_and_classify = _connector.read_and_classify
    _KNOWN_CONNECTORS = _connector.KNOWN_CONNECTORS
except Exception:  # noqa: BLE001 — snapshot resilience over connector fidelity
    _connector = None
    _classify_connector = None
    _read_and_classify = None
    _KNOWN_CONNECTORS = ()

HOME = Path(os.environ["HOME"])
OUT_PATH = HOME / ".claude/cache/world.json"
CMUX_REGISTRY = HOME / ".claude/cmux-registry.json"
ORCH_REGISTRY = HOME / ".architect/orchestrator-registry.json"
SESSION_CTX = HOME / ".claude/cache/session-context.json"
SESSION_LEDGER = HOME / ".cmux-session-ledger.jsonl"
DASHBOARD_STATE = HOME / ".claude/cache/dashboard-state.json"
TODO_PATH = HOME / ".claude/assistant-todo.json"
PROPOSALS_DIR = HOME / ".architect/orchestrator-proposals"
LEDGER_DIR = HOME / ".architect/orchestrator-ledger"
INBOX_ARCHIVE = HOME / ".architect/orchestrator-inbox-archive"
LOG_DIR = HOME / ".assistant/logs"
EVENTS_PATH = HOME / ".assistant/events.jsonl"
EVENTS_QUARANTINE_DIR = HOME / ".assistant/eventspine/quarantine"
CONNECTORS_DIR = HOME / ".assistant/connectors"
CMUX_BIN = shutil.which("cmux") or "/Applications/cmux.app/Contents/Resources/bin/cmux"

ACTIVITY_HOURS = 24
# Tail window for the event-spine health scan — bounded so a fat log can
# never slow the 30s scanner.
EVENTS_TAIL_BYTES = 512_000


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
            [CMUX_BIN, "--id-format", "both", "tree", "--all", "--json"],
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
    """Retain cmux object IDs separately from reusable display references."""
    out = []
    if not tree:
        return out
    for win in tree.get("windows", []) or []:
        for ws in win.get("workspaces", []) or []:
            entry = {
                "ws_ref": ws.get("ref"),
                "workspace_id": ws.get("id"),
                "title": ws.get("title") or "",
                "index": ws.get("index", 0),
                "surfaces": [],
            }
            for pane in ws.get("panes", []) or []:
                for surf in pane.get("surfaces", []) or []:
                    entry["surfaces"].append({
                        "ref": surf.get("ref"),
                        "surface_id": surf.get("id"),
                        "tty": surf.get("tty"),
                        "type": surf.get("type"),
                        "title": surf.get("title") or "",
                        "session_id": None,
                        "provider": None,
                        "identity_status": "unknown",
                        "agent_status": "unknown",
                    })
            out.append(entry)
    return out


def surface_resume_binding(surface_ref, ws_ref):
    """Return a normalized live cmux agent binding for one terminal surface."""
    try:
        r = subprocess.run(
            [CMUX_BIN, "surface", "resume", "show", "--json",
             "--surface", surface_ref, "--workspace", ws_ref],
            capture_output=True, text=True, timeout=3,
        )
        if r.returncode != 0:
            return {"unverified": True}
        doc = json.loads(r.stdout)
    except Exception:
        return {"unverified": True}
    if not isinstance(doc, dict):
        return {"unverified": True}
    queue = [doc]
    binding = None
    has_kind = False
    while queue:
        value = queue.pop(0)
        if not isinstance(value, dict):
            continue
        has_kind = has_kind or bool(value.get("kind"))
        if value.get("checkpointId") or value.get("checkpoint_id"):
            binding = value
            break
        queue.extend(v for v in value.values() if isinstance(v, dict))
    if not binding:
        return {"unverified": True} if has_kind else None
    kind = str(binding.get("kind") or "").lower()
    if kind not in {"claude", "factory", "droid"}:
        return {"unverified": True}
    sid = binding.get("checkpointId") or binding.get("checkpoint_id")
    return {
        "session_id": str(sid),
        "provider": "droid" if kind in {"factory", "droid"} else "claude",
        "cwd": binding.get("cwd"),
    }


def transcript_for_session(provider, session_id):
    if provider not in {"claude", "droid"}:
        return None
    root = (HOME / ".factory/sessions" if provider == "droid"
            else HOME / ".claude/projects")
    if not root.is_dir() or not session_id:
        return None
    matches = list(root.glob(f"*/{session_id}*.jsonl"))
    if not matches:
        return None
    return str(max(matches, key=lambda p: p.stat().st_mtime))


def foreground_provider_process(tty, provider):
    """Return the unique foreground provider process and its start time."""
    if not tty or provider not in {"claude", "droid"}:
        return None
    try:
        result = subprocess.run(
            ["ps", "-t", tty, "-o", "pid=,pgid=,tpgid=,lstart=,comm="],
            capture_output=True, text=True, timeout=3,
            env={**os.environ, "LC_ALL": "C", "TZ": "UTC"},
        )
        if result.returncode != 0:
            return None
    except (OSError, subprocess.TimeoutExpired):
        return None
    processes = []
    for line in result.stdout.splitlines():
        fields = line.split(None, 8)
        if len(fields) != 9:
            continue
        process_provider = Path(fields[8]).name
        if process_provider not in {"claude", "droid", "copilot"}:
            continue
        try:
            pid, pgid, foreground = map(int, fields[:3])
            started = datetime.strptime(
                " ".join(fields[3:8]), "%a %b %d %H:%M:%S %Y",
            ).replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
        if pgid > 0 and pgid == foreground:
            processes.append((pid, started, process_provider))
    if len(processes) != 1 or processes[0][2] != provider:
        return None
    return processes[0][:2]


def agent_pid_on_tty(tty, provider):
    process = foreground_provider_process(tty, provider)
    return process[0] if process else None


def registry_binding_for_surface(registry, workspace, surface):
    """Verify the hook's workspace key, surface UUID, session, and process age."""
    workspace_id = workspace.get("workspace_id")
    surface_id = surface.get("surface_id")
    tty = surface.get("tty")
    if not workspace_id or not surface_id or not tty:
        return None
    matches = [
        entry for key, entry in registry.items()
        if key.lower() == str(workspace_id).lower()
        and str(entry.get("surface_id") or "").lower() == str(surface_id).lower()
    ]
    if len(matches) != 1:
        return None
    entry = matches[0]
    if entry.get("workspace_id") and str(entry["workspace_id"]).lower() != str(workspace_id).lower():
        return None
    sid = entry.get("session_id")
    provider = normalize_provider(entry.get("provider") or "claude")
    recorded = entry.get("ts")
    if not isinstance(sid, str) or not sid or provider not in {"claude", "droid"}:
        return None
    if isinstance(recorded, bool) or not isinstance(recorded, (int, float)):
        return None
    process = foreground_provider_process(tty, provider)
    if process is None:
        return None
    pid, started = process
    if entry.get("claude_pid") and str(entry["claude_pid"]) != str(pid):
        return None
    if not started <= recorded < utc_now().timestamp() + 1:
        return None
    return {
        "session_id": sid, "provider": provider, "cwd": entry.get("cwd"),
        "pid": pid, "ts": recorded, "tab_id": workspace_id,
        "identity_source": "session_start_registry",
    }


def session_identity_key(session):
    return (
        normalize_provider(session.get("provider")),
        session["session_id"],
        str(session.get("workspace_id") or "").lower(),
        str(session.get("surface_id") or "").lower(),
    )


def build_live_sessions(workspaces=None):
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
        session = {
            "session_id": sid,
            "pid": int(pid),
            "cwd": e.get("cwd"),
            "transcript_path": e.get("transcript_path"),
            "provider": normalize_provider(e.get("provider") or "claude"),
            "tab_id": tab_id,
            "ts": e.get("ts"),
            "workspace_id": e.get("workspace_id") or (tab_id if e.get("surface_id") else None),
            "surface_id": e.get("surface_id"),
            "identity_status": "unknown",
            "identity_source": "registry",
            "agent_status": "unknown",
        }
        key = session_identity_key(session)
        prev = out.get(key)
        if prev and prev.get("ts", 0) > e.get("ts", 0):
            continue
        out[key] = session
    for ws in workspaces or []:
        for surface in ws.get("surfaces", []):
            if surface.get("type") != "terminal":
                continue
            binding = surface_resume_binding(
                surface.get("surface_id") or surface.get("ref"),
                ws.get("workspace_id") or ws.get("ws_ref"),
            )
            if binding is None:
                binding = registry_binding_for_surface(reg, ws, surface)
            if not binding or binding.get("unverified"):
                continue
            sid = binding["session_id"]
            provider = binding["provider"]
            pid = binding.get("pid") or agent_pid_on_tty(surface.get("tty"), provider)
            if not pid:
                continue
            verified = bool(ws.get("workspace_id") and surface.get("surface_id"))
            surface.update({
                "session_id": sid,
                "provider": provider,
                "identity_status": "verified" if verified else "unknown",
            })
            session = {
                "session_id": sid,
                "pid": pid,
                "cwd": binding.get("cwd"),
                "transcript_path": transcript_for_session(provider, sid),
                "provider": provider,
                "tab_id": binding.get("tab_id"),
                "ts": binding.get("ts", time.time()),
                "tty": surface.get("tty"),
                "ws_ref": ws.get("ws_ref"),
                "surface_ref": surface.get("ref"),
                "ws_title": ws.get("title", ""),
                "surface_title": surface.get("title", ""),
                "workspace_id": ws.get("workspace_id"),
                "surface_id": surface.get("surface_id"),
                "identity_status": "verified" if verified else "unknown",
                "identity_source": binding.get("identity_source", "cmux_resume_binding"),
                "agent_status": "unknown",
            }
            out[session_identity_key(session)] = session
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
                tty_to_ws[tty.removeprefix("/dev/")] = {
                    "ws_ref": ws["ws_ref"],
                    "workspace_id": ws.get("workspace_id"),
                    "surface_ref": surf["ref"],
                    "surface_id": surf.get("surface_id"),
                    "surface_title": surf.get("title", ""),
                    "ws_title": ws.get("title", ""),
                    "bound_session_id": surf.get("session_id"),
                    "bound_provider": surf.get("provider"),
                }

    for sess in live_sessions.values():
        sid = sess["session_id"]
        tty = sess.get("tty") or ps_tty(sess["pid"])
        sess["tty"] = tty
        info = tty_to_ws.get(tty.removeprefix("/dev/")) if tty else None
        if info:
            if info["bound_session_id"] and info["bound_session_id"] != sid:
                continue
            if info["bound_provider"] and info["bound_provider"] != normalize_provider(sess.get("provider")):
                continue
            if any(
                sess.get(key) and info.get(key)
                and str(sess[key]).lower() != str(info[key]).lower()
                for key in ("workspace_id", "surface_id")
            ):
                continue
            if info["surface_id"] and not sess.get("surface_id"):
                continue
            sess["ws_ref"] = info["ws_ref"]
            sess["workspace_id"] = info["workspace_id"]
            sess["surface_ref"] = info["surface_ref"]
            sess["surface_id"] = info["surface_id"]
            sess["ws_title"] = info["ws_title"]
            sess["surface_title"] = info["surface_title"]

    # Reverse-index ws_ref → session_ids
    for ws in workspaces:
        ws["session_ids"] = list(dict.fromkeys(
            s["session_id"] for s in live_sessions.values()
            if s.get("ws_ref") == ws["ws_ref"]
            and (
                not ws.get("workspace_id")
                or (
                    s.get("identity_status") == "verified"
                    and str(s.get("workspace_id") or "").lower() == str(ws["workspace_id"]).lower()
                )
            )
        ))


def tag_cron_workers(live_sessions):
    """Tag sessions running in known orchestrator worker workspaces as is_cron."""
    oreg = load_json(ORCH_REGISTRY, {})
    cron_ws_refs = set()
    for name, w in (oreg.get("workers") or {}).items():
        ref = w.get("workspace_ref")
        if ref:
            cron_ws_refs.add(ref)
    cron_cwds = {str(HOME / ".architect")}
    for sess in live_sessions.values():
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
    for sess in live_sessions.values():
        sid = sess["session_id"]
        sess["context_status"] = "unknown"
        sess["pending_tool_use"] = None
        sess["guidance_context"] = None
        sess["context_checked_at"] = None
        sess["context_built_at"] = (ctx.get("_meta") or {}).get("built_at")
        c = by_sess.get(sid)
        if c:
            provider = normalize_provider(sess.get("provider") or "claude")
            if provider not in {"claude", "droid"}:
                continue
            if normalize_provider(c.get("provider") or "claude") != provider:
                continue
            if c.get("session_id", sid) != sid:
                continue
            if any(
                c.get(key) and str(c[key]).lower() != str(sess.get(key) or "").lower()
                for key in ("workspace_id", "surface_id")
            ):
                continue
            sess["last_user"] = c.get("last_user")
            sess["last_assistant"] = c.get("last_assistant")
            sess["queue_pending"] = c.get("queue_pending", 0)
            sess["user_unanswered"] = c.get("user_unanswered", False)
            sess["recent_turns"] = c.get("recent_turns", [])
            pending = c.get("pending_tool_use")
            sess["pending_tool_use"] = pending if isinstance(pending, bool) else None
            if sess.get("identity_status") == "verified":
                sess["context_status"] = "verified"
                guidance = c.get("guidance_context")
                if isinstance(guidance, dict):
                    sess["context_status"] = "unknown"
                    evidence = c.get("transcript_state")
                    path = sess.get("transcript_path")
                    if not isinstance(evidence, dict) or not isinstance(path, str) or not path:
                        continue
                    try:
                        current = Path(path).stat()
                    except (OSError, ValueError):
                        continue
                    expected = {
                        "device": current.st_dev, "inode": current.st_ino,
                        "size_read": current.st_size, "mtime_ns": current.st_mtime_ns,
                    }
                    if all(type(evidence.get(key)) is int and evidence[key] == value
                           for key, value in expected.items()):
                        sess["guidance_context"] = guidance
                        sess["context_status"] = "verified"
                        sess["context_checked_at"] = iso(utc_now())


def normalize_provider(provider):
    provider = str(provider or "").lower()
    return "droid" if provider in {"factory", "droid"} else provider


def merge_first_recorded(live_sessions, now):
    """Join recorded starts by workspace UUID, provider, and full session ID."""
    targets = {}
    for session in live_sessions.values():
        session["first_recorded_at"] = None
        if session.get("identity_status") != "verified":
            continue
        key = (
            str(session.get("workspace_id") or "").lower(),
            normalize_provider(session.get("provider")),
            session["session_id"],
        )
        if all(key):
            targets.setdefault(key, []).append(session)
    if not targets:
        return
    first = {}
    try:
        with SESSION_LEDGER.open(errors="replace") as ledger:
            for line in ledger:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(row, dict) or row.get("event") != "start":
                    continue
                key = (
                    str(row.get("workspace_id") or "").lower(),
                    normalize_provider(row.get("provider")),
                    row.get("session_id"),
                )
                if not isinstance(key[2], str) or key not in targets:
                    continue
                timestamp = row.get("ts")
                if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)):
                    continue
                if not 0 < timestamp <= now.timestamp():
                    continue
                if key not in first or timestamp < first[key]:
                    first[key] = timestamp
    except OSError:
        return
    for key, timestamp in first.items():
        recorded = datetime.fromtimestamp(timestamp, tz=timezone.utc).replace(microsecond=0)
        for session in targets[key]:
            session["first_recorded_at"] = iso(recorded)


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


def build_events_summary(now):
    """Event-spine health: per-source counts + latest-event age (Keel M1).

    A stalled spine (producer alive, consumer dead — the pre-M1 failure mode)
    is visible here: the source's latest_age_sec grows while the fleet keeps
    signalling. `latest_*` deliberately ignores the 24h window so a source
    that went quiet days ago still shows how stale it is. quarantine_pending
    counts malformed drops awaiting a human look."""
    out = {"total_24h": 0, "by_source": {}, "quarantine_pending": 0}
    try:
        out["quarantine_pending"] = sum(
            1 for _ in EVENTS_QUARANTINE_DIR.glob("*.json"))
    except OSError:
        pass
    if not EVENTS_PATH.exists():
        return out
    try:
        with open(EVENTS_PATH, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - EVENTS_TAIL_BYTES))
            tail = f.read().decode("utf-8", errors="replace")
    except OSError:
        return out
    now_epoch = now.timestamp()
    cutoff = now_epoch - ACTIVITY_HOURS * 3600
    for line in tail.splitlines():
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(d, dict) or not d.get("source"):
            continue
        epoch = d.get("epoch")
        if not isinstance(epoch, (int, float)):
            ts = parse_iso(d.get("ts"))
            epoch = ts.timestamp() if ts else None
        src = out["by_source"].setdefault(
            d["source"], {"count_24h": 0, "latest_ts": None,
                          "latest_age_sec": None})
        if epoch is None:
            continue
        if epoch >= cutoff:
            src["count_24h"] += 1
            out["total_24h"] += 1
        if src["latest_age_sec"] is None or epoch > now_epoch - src["latest_age_sec"]:
            src["latest_ts"] = d.get("ts")
            src["latest_age_sec"] = max(0, int(now_epoch - epoch))
    return out


def build_connectors_summary(now):
    """Join each connector's heartbeat into world.json with its canonical
    tri-state (Keel M5). The status/stale/token verdict is computed by the
    connector base's classify_connector — the ONE place the not_configured|ok|
    error model lives, shared with the brief so both agree. We enumerate the
    UNION of the known-connector registry and whatever heartbeat dirs exist, so:

      * a KNOWN connector that has never run (no heartbeat file at all — fresh
        install, daemon never started) still appears, as not_configured
        ("available, not connected"), NOT as an error/stale alarm; and
      * an unknown/extra connector that DOES have a heartbeat is never dropped.

    So the dashboard Connections panel can render every connector from this one
    block. Pure read — connectors own their heartbeat files; we only observe. If
    the connector base could not be imported this degrades to {} (the dashboard
    then shows its own honest empty state)."""
    if _classify_connector is None:
        return {}
    now_epoch = now.timestamp()
    names = [c["name"] for c in _KNOWN_CONNECTORS]
    if CONNECTORS_DIR.exists():
        try:
            for p in sorted(CONNECTORS_DIR.iterdir()):
                if p.is_dir() and p.name not in names:
                    names.append(p.name)
        except OSError:
            pass
    out = {}
    for name in names:
        # F5: read_and_classify distinguishes an ABSENT heartbeat (never ran →
        # not_configured) from a PRESENT-but-corrupt one (ran before, now broken
        # → error) — the SAME verdict the brief derives, so a corrupt beat can
        # never read "available" here while the brief drops it. F1: fence per
        # connector so one bad heartbeat can never take down the whole snapshot
        # (world.json must still be written — the M3 one-bad-row contract).
        try:
            out[name] = _read_and_classify(
                CONNECTORS_DIR / name / "heartbeat.json", now_epoch)
        except Exception:  # noqa: BLE001 — one bad row degrades only itself
            out[name] = _connector._corrupt_connector_view(now_epoch)
    return out


def build():
    now = utc_now()
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    tree = cmux_tree()
    workspaces = build_workspace_index(tree)
    live_sessions = build_live_sessions(workspaces)
    join_workspaces_to_sessions(workspaces, live_sessions)
    tag_cron_workers(live_sessions)
    merge_session_context(live_sessions)
    merge_first_recorded(live_sessions, now)

    # Compute per-session activity age and bucket.
    for sess in live_sessions.values():
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
    events_summary = build_events_summary(now)
    connectors_summary = build_connectors_summary(now)

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
            "events_24h": events_summary["total_24h"],
        },
        "events": events_summary,
        "connectors": connectors_summary,
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
