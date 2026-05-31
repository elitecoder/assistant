#!/usr/bin/env python3
"""workspace-watcher.py — detect cmux workspace crashes and auto-resume them.

Subscribes to `cmux events --category workspace --reconnect`. On every
`workspace.closed` frame, classifies cause as one of:

  - "crash"        — positive evidence: a com.cmuxterm.app-coalition .ips
                     (spindump) with captureTime within [-60s, +10s] of the
                     close event. macOS writes an .ips when a child process
                     inside cmux dies hard (SIGSEGV/SIGBUS/uncaught exception).
                     Cause precedes effect: the spindump's captureTime should
                     sit at-or-just-before the close.
  - "intentional"  — default. The cmux daemon delivered workspace.closed,
                     which means cmux itself is alive; absent crash evidence
                     we trust the close as user-driven (UI button, the
                     `cmux close-workspace` CLI, the /close-workspace skill,
                     /cleanup, etc.). A cleanup-ledger match in
                     ~/.architect/orchestrator-ledger/ is recorded as evidence
                     but does not influence classification.

Policy:

  1. Always write a structured JSON file drop to
     ~/.claude/cmux-crash-events/<ref>-<epoch>.json so the dashboard / manager
     session can see every close.
  2. Notify and auto-resume ONLY on cause=crash. Default-intentional means an
     ordinary close never bounces back as a phantom workspace.
  3. Resume safety caps: per-workspace 2/h, daemon-wide 5/h, 3-strike retry
     cap with bash -lc fallback on second strike.

Known false-negative gap: a workspace that dies without leaving a
cmux-coalition .ips (e.g. SIGKILL of a child, OOM-killer, cmux internal state
corruption) is classified as intentional — auto-resume will not fire. Future
work: subscribe to a wider cmux event category (pane / terminal / process
exits) to catch upstream of the .ips signal. Probe with
`cmux events --category '*' --reconnect | jq -r .name | sort -u` while
exercising controlled deaths.

Designed to be crash-resistant itself: KeepAlive=true under launchd, recovers
from cmux socket disconnects via --reconnect, persists cursor + resume ledger
across restarts.

Stdlib only, Python 3.11+.
"""
from __future__ import annotations

import collections
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ---------- paths ----------
HOME = Path(os.environ["HOME"])
WATCHER_DIR = HOME / ".assistant" / "workspace-watcher"
CURSOR_FILE = WATCHER_DIR / "cursor.seq"
RESUME_LEDGER = WATCHER_DIR / "resume-ledger.jsonl"
WS_CACHE = WATCHER_DIR / "ws-cache.json"
CRASH_EVENTS_DIR = HOME / ".claude" / "cmux-crash-events"
LOG_PATH = HOME / ".claude" / "logs" / "workspace-watcher.log"

CMUX_SESSION_STATE = HOME / "Library" / "Application Support" / "cmux" / "session-com.cmuxterm.app.json"
CMUX_PREV_STATE = HOME / "Library" / "Application Support" / "cmux" / "session-com.cmuxterm.app-previous.json"
ORCH_LEDGER = HOME / ".architect" / "orchestrator-ledger"
DIAG_REPORTS = HOME / "Library" / "Logs" / "DiagnosticReports"

CMUX_BIN = shutil.which("cmux") or "/Applications/cmux.app/Contents/Resources/bin/cmux"

# ---------- timing knobs ----------
LEDGER_MATCH_WINDOW_SEC = 60          # cleanup ledger evidence is recorded if within ±60s (informational only)
# Crash window is asymmetric: the .ips captureTime is when the child died,
# and the workspace.closed event arrives shortly after. macOS sometimes
# delays the .ips write by several seconds, so we accept anything from
# 60s before the close up to 10s after.
IPS_LOOKBACK_SEC = 60
IPS_LOOKAHEAD_SEC = 10
PER_WS_CAP = 2                         # max resumes per workspace per hour
DAEMON_CAP = 5                         # max resumes daemon-wide per hour
CAP_WINDOW_SEC = 3600

# ---------- logging ----------
LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    filename=str(LOG_PATH),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("workspace-watcher")


# ---------- helpers ----------
def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def run(cmd: list[str], timeout: int = 10) -> tuple[int, str, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return -1, "", str(e)


def notify(title: str, message: str, sound: str = "Sosumi") -> None:
    """Fire macOS Notification Center alert via osascript."""
    # Escape backslashes first, then double-quotes, to prevent AppleScript injection.
    safe_title = title.replace("\\", "\\\\").replace('"', '\\"')
    safe_msg = message.replace("\\", "\\\\").replace('"', '\\"')
    script = (
        f'display notification "{safe_msg}" '
        f'with title "{safe_title}" '
        f'sound name "{sound}"'
    )
    run(["osascript", "-e", script], timeout=5)


# ---------- workspace registry ----------
class WorkspaceRegistry:
    """Caches per-workspace metadata so we can map a `workspace.closed` UUID
    back to title, cwd, resumeBinding.command, etc.

    Refreshed on every workspace.* event; persisted to disk so restarts have
    immediate state."""

    def __init__(self) -> None:
        self.by_uuid: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        if WS_CACHE.exists():
            try:
                self.by_uuid = json.loads(WS_CACHE.read_text())
            except Exception:
                self.by_uuid = {}

    def save(self) -> None:
        WATCHER_DIR.mkdir(parents=True, exist_ok=True)
        WS_CACHE.write_text(json.dumps(self.by_uuid, indent=2))

    def refresh(self) -> None:
        """Pull live workspace list + session-state file; merge into cache."""
        # 1. cmux RPC for ref/UUID/title/cwd
        rc, stdout, _ = run([CMUX_BIN, "rpc", "workspace.list", "{}"])
        if rc == 0 and stdout:
            try:
                d = json.loads(stdout)
                for w in d.get("workspaces", []) or []:
                    uuid = w.get("id")
                    if not uuid:
                        continue
                    # cmux returns "ref" as either "workspace:N" (string) or N (int).
                    raw_ref = w.get("ref")
                    if isinstance(raw_ref, str) and raw_ref.startswith("workspace:"):
                        ref_int = int(raw_ref.split(":", 1)[1])
                    elif isinstance(raw_ref, int):
                        ref_int = raw_ref
                    else:
                        ref_int = None
                    self.by_uuid.setdefault(uuid, {})
                    self.by_uuid[uuid].update({
                        "uuid": uuid,
                        "ref": ref_int,
                        "title": w.get("title"),
                        "cwd": w.get("current_directory"),
                        "selected": w.get("selected"),
                        "last_seen_alive_ts": iso(utcnow()),
                    })
            except Exception:
                log.exception("workspace.list parse failed")
        # 2. session-com.cmuxterm.app.json for resumeBinding + agent metadata
        for path in (CMUX_SESSION_STATE, CMUX_PREV_STATE):
            self._merge_session_state(path)
        self.save()

    def _merge_session_state(self, path: Path) -> None:
        if not path.exists():
            return
        try:
            doc = json.loads(path.read_text())
        except Exception:
            return
        for window in doc.get("windows", []) or []:
            tm = window.get("tabManager", {})
            for w in tm.get("workspaces", []) or []:
                # session-com keys workspaces by index, not UUID. Map via title+cwd.
                title = w.get("customTitle") or w.get("title")
                cwd = w.get("currentDirectory")
                # Pull resume binding from any panel that has one.
                panels = w.get("panels", []) or []
                resume_cmd = None
                resume_cwd = None
                checkpoint_id = None
                was_agent_running = False
                process_title = w.get("processTitle")
                last_pr = None
                for p in panels:
                    rb = p.get("terminal", {}).get("resumeBinding")
                    if rb:
                        resume_cmd = rb.get("command")
                        resume_cwd = rb.get("cwd")
                        checkpoint_id = rb.get("checkpointId")
                        was_agent_running = bool(p.get("terminal", {}).get("wasAgentRunning"))
                    # First browser panel URL is usually the PR.
                    burl = p.get("browser", {}).get("urlString")
                    if burl and last_pr is None and "/pull/" in burl:
                        last_pr = burl
                # Find the UUID by matching title+cwd in the live RPC cache.
                target_uuid = None
                for uuid, meta in self.by_uuid.items():
                    if meta.get("title") == title and (
                        not cwd or meta.get("cwd") == cwd
                    ):
                        target_uuid = uuid
                        break
                if not target_uuid:
                    # Workspace already gone from RPC list; remember by synthetic key.
                    target_uuid = f"by-title:{title}"
                    self.by_uuid.setdefault(target_uuid, {
                        "uuid": target_uuid,
                        "ref": None,
                        "title": title,
                        "cwd": cwd,
                    })
                self.by_uuid[target_uuid].update({
                    "title": title,
                    "cwd": cwd,
                    "process_title": process_title,
                    "resume_command": resume_cmd,
                    "resume_cwd": resume_cwd,
                    "checkpoint_id": checkpoint_id,
                    "was_agent_running": was_agent_running,
                    "last_pr": last_pr,
                })

    def snapshot(self, uuid: str) -> dict | None:
        return self.by_uuid.get(uuid)


# ---------- cause classification ----------
_NUMBER_SUFFIX_RE = re.compile(r"\s*\[\d+\]\s*$")


def _strip_number_suffix(s: str | None) -> str:
    """Remove the trailing ' [N]' that cmux-ws-numberer appends to titles."""
    if not s:
        return ""
    return _NUMBER_SUFFIX_RE.sub("", s).strip()


def find_recent_cleanup_ledger(uuid: str | None, ref: int | None,
                               title: str | None,
                               window_sec: int = LEDGER_MATCH_WINDOW_SEC) -> Path | None:
    """Return the matching cleanup-ledger path if we find one within ±window_sec."""
    if not ORCH_LEDGER.exists():
        return None
    cutoff = time.time() - window_sec
    title_bare = _strip_number_suffix(title)
    for p in ORCH_LEDGER.glob("cleanup-*.json"):
        try:
            if p.stat().st_mtime < cutoff:
                continue
            doc = json.loads(p.read_text())
            wsr = doc.get("workspace_ref") or ""
            if ref is not None and wsr == f"workspace:{ref}":
                return p
            ledger_title_bare = _strip_number_suffix(doc.get("workspace_title"))
            if title_bare and ledger_title_bare and ledger_title_bare == title_bare:
                return p
        except Exception:
            continue
    return None


_CAPTURE_TIME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")


def _parse_capture_time(raw: str | None) -> float | None:
    """captureTime in .ips files looks like '2026-05-27 12:45:02.0269 -0700'.
    Return epoch seconds (best effort)."""
    if not raw:
        return None
    m = _CAPTURE_TIME_RE.match(raw)
    if not m:
        return None
    try:
        # Ignore subseconds + tz suffix; use the local-clock seconds. The .ips
        # is written by the local crash reporter so the tz matches the system.
        # That's good enough for a ±60s window match against close events,
        # which we also key off the local clock via time.time().
        from datetime import datetime as _dt
        # Parse the "YYYY-MM-DD HH:MM:SS" prefix; take the rest of the line as tz.
        prefix = m.group(1)
        tz_part = raw[m.end():].strip().split()
        # tz_part may be ["0269", "-0700"] or ["-0700"]; the offset is the last token.
        tz_token = tz_part[-1] if tz_part else ""
        if re.fullmatch(r"[+-]\d{4}", tz_token):
            ts = _dt.strptime(f"{prefix} {tz_token}", "%Y-%m-%d %H:%M:%S %z")
            return ts.timestamp()
        # Fallback: assume local tz.
        ts = _dt.strptime(prefix, "%Y-%m-%d %H:%M:%S").astimezone()
        return ts.timestamp()
    except Exception:
        return None


def _read_ips(path: Path) -> dict | None:
    """An .ips file is a one-line metadata header followed by a JSON body."""
    try:
        with open(path) as f:
            f.readline()
            return json.loads(f.read())
    except Exception:
        return None


def find_recent_cmux_ips(close_epoch: float, ws_cwd: str | None = None,
                         lookback: int = IPS_LOOKBACK_SEC,
                         lookahead: int = IPS_LOOKAHEAD_SEC) -> Path | None:
    """Find a com.cmuxterm.app-coalition .ips whose captureTime is within
    [close_epoch - lookback, close_epoch + lookahead]. If `ws_cwd` is given,
    prefer .ips files whose triggered process belongs to that cwd subtree
    (procPath prefix or the workspace title appearing in argv) — but accept
    a coalition-match-only hit if no cwd-binding candidate is found, since
    cmux's spindumps for child processes often replace the cwd with /USER/*.

    Filtering is on captureTime (when the process actually died), not mtime,
    because macOS sometimes delays writing the .ips by several seconds.
    """
    if not DIAG_REPORTS.exists():
        return None
    earliest = close_epoch - lookback
    latest = close_epoch + lookahead
    # mtime is a coarse pre-filter — the file has to exist by the time we look.
    coalition_hits: list[tuple[float, Path, dict]] = []
    cwd_hits: list[tuple[float, Path, dict]] = []
    for p in DIAG_REPORTS.glob("*.ips"):
        try:
            if p.stat().st_mtime < earliest - 5:
                continue
        except FileNotFoundError:
            continue
        doc = _read_ips(p)
        if not doc:
            continue
        if doc.get("coalitionName") != "com.cmuxterm.app" and doc.get("responsibleProc") != "cmux":
            continue
        cap_epoch = _parse_capture_time(doc.get("captureTime"))
        if cap_epoch is None or not (earliest <= cap_epoch <= latest):
            continue
        coalition_hits.append((cap_epoch, p, doc))
        # Prefer hits whose proc lived in the workspace's cwd. macOS scrubs
        # paths to /Users/USER/*/proc — we still get the leaf; check argv too.
        if ws_cwd:
            proc_path = doc.get("procPath") or ""
            argv = " ".join(doc.get("processByPid", {}).get("processList", [])) if isinstance(doc.get("processByPid"), dict) else ""
            blob = f"{proc_path} {argv}".lower()
            cwd_low = ws_cwd.lower()
            # Match either the leaf directory or any path component.
            leaf = Path(ws_cwd).name.lower() if ws_cwd else ""
            if (cwd_low in blob) or (leaf and leaf in blob):
                cwd_hits.append((cap_epoch, p, doc))
    pool = cwd_hits or coalition_hits
    if not pool:
        return None
    # Closest-in-time wins.
    pool.sort(key=lambda x: abs(x[0] - close_epoch))
    return pool[0][1]


def parse_ips_summary(path: Path) -> dict:
    """Pull signal + top frame from an .ips file (best-effort)."""
    doc = _read_ips(path)
    if not doc:
        return {}
    out = {
        "path": str(path),
        "proc": doc.get("procName"),
        "responsible": doc.get("responsibleProc"),
        "coalition": doc.get("coalitionName"),
        "captureTime": doc.get("captureTime"),
        "signal": None,
        "top": None,
    }
    ex = doc.get("exception", {}) or {}
    out["signal"] = ex.get("signal") or ex.get("type")
    for t in doc.get("threads", []) or []:
        if t.get("triggered"):
            frames = t.get("frames", []) or []
            if frames:
                out["top"] = frames[0].get("symbol") or "?"
            break
    return out


# ---------- resume policy ----------
class ResumeGovernor:
    """Tracks resume attempts to enforce per-workspace and daemon-wide caps."""

    def __init__(self) -> None:
        self.attempts: collections.deque[tuple[float, str]] = collections.deque()
        self._load()

    def _load(self) -> None:
        if not RESUME_LEDGER.exists():
            return
        try:
            for line in RESUME_LEDGER.read_text().splitlines():
                if not line.strip():
                    continue
                e = json.loads(line)
                ts = datetime.fromisoformat(e["ts"].replace("Z", "+00:00")).timestamp()
                self.attempts.append((ts, e.get("workspace_uuid", "")))
        except Exception:
            pass

    def _gc(self) -> None:
        cutoff = time.time() - CAP_WINDOW_SEC
        while self.attempts and self.attempts[0][0] < cutoff:
            self.attempts.popleft()

    def can_resume(self, uuid: str) -> tuple[bool, str]:
        self._gc()
        per_ws = sum(1 for ts, u in self.attempts if u == uuid)
        if per_ws >= PER_WS_CAP:
            return False, f"per-workspace cap reached ({per_ws}/{PER_WS_CAP} in {CAP_WINDOW_SEC}s)"
        if len(self.attempts) >= DAEMON_CAP:
            return False, f"daemon-wide cap reached ({len(self.attempts)}/{DAEMON_CAP} in {CAP_WINDOW_SEC}s)"
        return True, "ok"

    def record(self, uuid: str, title: str, cause: str, command: str,
               new_ref: str | None, new_uuid: str | None,
               attempt_no: int, used_bash_lc: bool, ok: bool, error: str | None) -> None:
        self.attempts.append((time.time(), uuid))
        WATCHER_DIR.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": iso(utcnow()),
            "workspace_uuid": uuid,
            "workspace_title": title,
            "cause": cause,
            "attempt_no": attempt_no,
            "used_bash_lc": used_bash_lc,
            "command_preview": command[:200],
            "new_workspace_ref": new_ref,
            "new_workspace_uuid": new_uuid,
            "ok": ok,
            "error": error,
        }
        with open(RESUME_LEDGER, "a") as f:
            f.write(json.dumps(entry) + "\n")
        log.info("resume_record %s", entry)


def resume_workspace(meta: dict, governor: ResumeGovernor, attempt_no: int = 1,
                     used_bash_lc: bool = False) -> dict:
    """Spawn a fresh cmux workspace running the prior resumeBinding command.

    Returns a structured result dict {ok, new_ref, new_uuid, error}."""
    uuid = meta.get("uuid", "?")
    title = meta.get("title") or meta.get("process_title") or "?"
    cwd = meta.get("resume_cwd") or meta.get("cwd") or str(HOME)
    cmd_str = meta.get("resume_command")
    if not cmd_str:
        return {"ok": False, "error": "no resume_command in cache"}
    if used_bash_lc:
        # Wrap in bash -lc to dodge the .zprofile segfault per
        # ~/.claude/CLAUDE.md feedback_workspace_keeps_getting_killed lesson.
        cmd_str = f"bash -lc {json_quote(cmd_str)}"
    new_title = f"{title} (auto-resumed)"
    rc, stdout, stderr = run([
        CMUX_BIN, "new-workspace",
        "--name", new_title,
        "--cwd", cwd,
        "--command", cmd_str,
        "--focus", "false",
    ], timeout=20)
    if rc != 0:
        err = stderr.strip() or stdout.strip() or f"exit {rc}"
        governor.record(uuid, title, meta.get("cause", "unknown"), cmd_str,
                        None, None, attempt_no, used_bash_lc, False, err)
        return {"ok": False, "error": err}
    # cmux new-workspace usually prints the new ref/uuid to stdout. Best-effort parse.
    new_ref = None
    new_uuid = None
    m = re.search(r"workspace:(\d+)", stdout)
    if m:
        new_ref = f"workspace:{m.group(1)}"
    m = re.search(r"\b([0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12})\b", stdout)
    if m:
        new_uuid = m.group(1)
    governor.record(uuid, title, meta.get("cause", "unknown"), cmd_str,
                    new_ref, new_uuid, attempt_no, used_bash_lc, True, None)
    return {"ok": True, "new_ref": new_ref, "new_uuid": new_uuid}


def json_quote(s: str) -> str:
    """Quote a string for safe inclusion as a single shell argument."""
    return "'" + s.replace("'", "'\\''") + "'"


# ---------- close handler ----------
def handle_close(evt: dict, registry: WorkspaceRegistry,
                 governor: ResumeGovernor) -> None:
    payload = evt.get("payload") or {}
    uuid = (
        evt.get("workspace_id")
        or payload.get("workspace_id")
        or (payload.get("workspace") or {}).get("id")
    )
    # Refresh registry BEFORE the close fully propagates so we can still see the row.
    registry.refresh()
    meta = registry.snapshot(uuid) if uuid else None
    if not meta:
        # Fall back to anything we have by-title from the previous session-state.
        log.warning("close event for unknown workspace_id=%s; payload=%s", uuid, payload)
        meta = {"uuid": uuid or "unknown", "title": "(unknown)"}
    title = meta.get("title") or "(unknown)"
    ref_int = meta.get("ref")
    ws_ref = f"workspace:{ref_int}" if ref_int else "workspace:?"

    # Classify cause.
    #
    # Default is INTENTIONAL: the cmux daemon delivered workspace.closed,
    # which means cmux is alive and processed the close — the user (or some
    # tool acting on their behalf) asked it to close. The classifier flips
    # to CRASH only on positive evidence, currently a com.cmuxterm.app-
    # coalition .ips spindump within the asymmetric window around the close.
    #
    # cleanup-ledger matches are recorded as evidence but do NOT influence
    # classification — a missing ledger entry is no longer evidence of
    # anything (the /close-workspace skill, the UI, the CLI all close
    # without writing one).
    close_epoch = time.time()
    cleanup_path = find_recent_cleanup_ledger(uuid, ref_int, title)
    ips_path = find_recent_cmux_ips(close_epoch, ws_cwd=meta.get("cwd"))
    cause = "crash" if ips_path else "intentional"

    # File drop.
    CRASH_EVENTS_DIR.mkdir(parents=True, exist_ok=True)
    epoch = int(close_epoch)
    drop = {
        "schema_version": 2,
        "workspace_ref": ws_ref,
        "workspace_id": meta.get("uuid"),
        "name": title,
        "cwd": meta.get("cwd"),
        "died_at": iso(utcnow()),
        "cause": cause,
        "evidence": {
            "matching_ips": str(ips_path) if ips_path else None,
            "matching_cleanup_ledger": str(cleanup_path) if cleanup_path else None,
            "ips_window": {
                "lookback_sec": IPS_LOOKBACK_SEC,
                "lookahead_sec": IPS_LOOKAHEAD_SEC,
                "close_epoch": close_epoch,
            },
        },
        "last_session_uuid": meta.get("checkpoint_id"),
        "last_pr": meta.get("last_pr"),
        "git_branch": None,
        "was_agent_running": meta.get("was_agent_running", False),
        "process_title_at_close": meta.get("process_title"),
        "resume": None,  # filled below if we attempt one
    }
    if ips_path:
        drop["evidence"]["ips_summary"] = parse_ips_summary(ips_path)

    safe_ref = ws_ref.replace(":", "-").replace("?", "unknown")
    drop_path = CRASH_EVENTS_DIR / f"{safe_ref}-{epoch}.json"
    drop_path.write_text(json.dumps(drop, indent=2))
    log.info("close cause=%s ws=%s title=%r ips=%s drop=%s",
             cause, ws_ref, title, ips_path.name if ips_path else "-",
             drop_path.name)

    # Auto-resume fires ONLY on cause=crash. was_agent_running is no longer
    # a vote — it's metadata in the file drop. Without positive crash
    # evidence we trust the close and stay out of the way.
    if cause != "crash":
        return

    notify(f"cmux: {title} crashed",
           f"{ws_ref} — auto-resuming…",
           sound="Sosumi")

    # Resume policy.
    if not meta.get("resume_command"):
        notify(f"cmux: {title} auto-resume blocked",
               "no resumeBinding.command captured",
               sound="Funk")
        drop["resume"] = {"attempted": False, "reason": "no resume_command"}
        drop_path.write_text(json.dumps(drop, indent=2))
        return
    ok, why = governor.can_resume(meta.get("uuid", ""))
    if not ok:
        notify(f"cmux: {title} auto-resume blocked",
               f"{why} — needs you",
               sound="Funk")
        drop["resume"] = {"attempted": False, "reason": why}
        drop_path.write_text(json.dumps(drop, indent=2))
        return

    # First attempt: the prior resumeBinding.command verbatim.
    meta["cause"] = cause
    result = resume_workspace(meta, governor, attempt_no=1, used_bash_lc=False)
    if result.get("ok"):
        notify(f"cmux: {title} resumed",
               f"→ {result.get('new_ref') or 'new workspace'}",
               sound="Glass")
    else:
        notify(f"cmux: {title} auto-resume failed",
               f"{result.get('error', '?')} — needs you",
               sound="Funk")
    drop["resume"] = result | {"attempted": True, "attempt_no": 1, "used_bash_lc": False}
    drop_path.write_text(json.dumps(drop, indent=2))


# ---------- event loop ----------
def stream(registry: WorkspaceRegistry, governor: ResumeGovernor) -> None:
    WATCHER_DIR.mkdir(parents=True, exist_ok=True)
    cmd = [
        CMUX_BIN, "events",
        "--reconnect",
        "--category", "workspace",
        "--no-heartbeat",
        "--no-ack",
        "--cursor-file", str(CURSOR_FILE),
    ]
    log.info("starting event stream: %s", " ".join(cmd))
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True, bufsize=1)
    if proc.stdout is None:
        raise RuntimeError("cmux events: no stdout")
    for line in proc.stdout:
        line = line.strip()
        if not line:
            continue
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            continue
        name = evt.get("name", "")
        if name in ("workspace.created", "workspace.renamed", "workspace.selected"):
            registry.refresh()
            continue
        if name == "workspace.closed":
            try:
                handle_close(evt, registry, governor)
            except Exception:
                log.exception("handle_close failed")
            continue


def main() -> int:
    log.info("=== workspace-watcher pid=%d ===", os.getpid())
    WATCHER_DIR.mkdir(parents=True, exist_ok=True)
    registry = WorkspaceRegistry()
    registry.refresh()
    governor = ResumeGovernor()
    while True:
        try:
            stream(registry, governor)
        except KeyboardInterrupt:
            log.info("interrupted, exiting")
            return 0
        except Exception:
            log.exception("stream crashed; sleeping 5s and retrying")
            time.sleep(5)


if __name__ == "__main__":
    sys.exit(main())
