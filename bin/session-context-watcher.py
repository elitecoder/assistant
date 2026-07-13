#!/usr/bin/env python3
"""
session-context-watcher.py — event-driven session transcript watcher.

Uses macOS kqueue (stdlib `select.kqueue`) to react to writes on every
recently-active Claude transcript JSONL. When any transcript grows,
incrementally updates ~/.claude/cache/session-context.json with the new
turns. Pure stdlib. No polling — kqueue blocks until a real fs event.

A 30s "discovery" wakeup picks up brand-new session files (kqueue doesn't
give us new-file notifications efficiently for a dir with hundreds of
entries — directory-level kqueue events tell us *something* changed but
not what, so we re-list active transcripts when stat() suggests change).

Cache schema matches build-session-context.py (drop-in replacement).

Usage:
  session-context-watcher.py [--daemon]   long-lived watcher (default)
  session-context-watcher.py --once       full scan + exit (no watch)
"""

import argparse
import json
import os
import select
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

HOME = Path(os.environ["HOME"])
PROJECTS_DIR = HOME / ".claude/projects"
CMUX_REGISTRY = HOME / ".claude/cmux-registry.json"
WORLD_PATH = HOME / ".claude/cache/world.json"
ORCHESTRATOR_REGISTRY = HOME / ".architect/orchestrator-registry.json"
OUT_PATH = HOME / ".claude/cache/session-context.json"
LOG_DIR = HOME / ".assistant/logs"
LOCK_FILE = HOME / ".architect/.session-context-watcher.lock"

ACTIVITY_HOURS = 24
TURNS_PER_SESSION = 6
TEXT_TRUNCATE = 800
RECENT_INPUTS_LIMIT = 30
DISCOVERY_INTERVAL_SEC = 30  # how often we look for brand-new transcript files
FLUSH_DEBOUNCE_SEC = 0.5     # batch writes during a burst of fs events
MAX_WATCHED_FDS = 256        # cap on simultaneously-watched files

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_session


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
    with open(LOG_DIR / "session-context-watcher.out", "a") as f:
        f.write(f"[{iso(utc_now())}] [{level}] {msg}\n")
    if level in {"warn", "error"}:
        with open(LOG_DIR / "session-context-watcher.err", "a") as f:
            f.write(f"[{iso(utc_now())}] [{level}] {msg}\n")


def cwd_from_project_dir(name):
    if name.startswith("-"):
        return "/" + name[1:].replace("-", "/")
    return name


def pid_alive(pid):
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except (ProcessLookupError, ValueError, PermissionError, TypeError):
        return False


def load_live_agent_sessions():
    """Return verified live Claude and Droid sessions keyed by session id."""
    try:
        reg = json.loads(CMUX_REGISTRY.read_text())
    except Exception:
        reg = {}
    out = {}
    for tab_id, entry in reg.items():
        pid = entry.get("claude_pid")
        if not pid_alive(pid):
            continue
        sid = entry.get("session_id")
        if not sid:
            continue
        # Multiple registry entries can share a session_id (re-registration on resume).
        # Keep the most recent.
        prev = out.get(sid)
        if prev and prev.get("ts", 0) > entry.get("ts", 0):
            continue
        out[sid] = {
            "pid": int(pid),
            "cwd": entry.get("cwd"),
            "transcript_path": entry.get("transcript_path"),
            "provider": entry.get("provider") or "claude",
            "ts": entry.get("ts"),
            "tab_id": tab_id,
        }
    try:
        world = json.loads(WORLD_PATH.read_text())
    except Exception:
        world = {}
    for entry in world.get("live_sessions", []) or []:
        if not isinstance(entry, dict):
            continue
        sid = entry.get("session_id")
        pid = entry.get("pid")
        if not sid or not pid_alive(pid):
            continue
        out[sid] = {
            "pid": int(pid),
            "cwd": entry.get("cwd"),
            "transcript_path": entry.get("transcript_path"),
            "provider": entry.get("provider") or "claude",
            "ts": entry.get("ts"),
            "tab_id": entry.get("tab_id"),
        }
    return out


def load_live_claude_sessions():
    """Compatibility alias for older callers and tests."""
    return load_live_agent_sessions()


def load_cron_workers():
    """Return a dict {session_id_or_workspace_ref: worker_name} for known
    cron-fired orchestrator workers. We tag them so the dashboard can
    hide them by default."""
    try:
        oreg = json.loads(ORCHESTRATOR_REGISTRY.read_text())
    except Exception:
        return {}
    out = {}
    workers = oreg.get("workers", {}) or {}
    for name, w in workers.items():
        ws_ref = w.get("workspace_ref")
        if ws_ref:
            out[ws_ref] = name
    return out


def text_from_message(msg):
    if not isinstance(msg, dict):
        return ""
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if not isinstance(item, dict):
                continue
            t = item.get("type")
            if t == "text":
                parts.append(item.get("text", ""))
            elif t == "tool_use":
                parts.append(f"[tool_use:{item.get('name', '?')}]")
            elif t == "tool_result":
                parts.append("[tool_result]")
        return "\n".join(p for p in parts if p)
    return ""


def truncate(s, n=TEXT_TRUNCATE):
    if not s:
        return ""
    s = s.strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def acquire_lock():
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    if LOCK_FILE.exists():
        try:
            pid = int(LOCK_FILE.read_text().strip())
            os.kill(pid, 0)
            return False
        except (ProcessLookupError, ValueError, OSError):
            pass
    LOCK_FILE.write_text(str(os.getpid()))
    return True


def release_lock():
    try:
        LOCK_FILE.unlink()
    except FileNotFoundError:
        pass


class TranscriptState:
    """Per-file incremental parser state."""
    __slots__ = ("path", "cwd", "session_id", "pos", "turns", "queue_pending",
                 "last_user", "last_assistant", "mtime", "pid", "is_cron",
                 "cron_label", "tab_id", "provider")

    def __init__(self, path, cwd, pid=None, is_cron=False, cron_label=None,
                 tab_id=None, provider="claude"):
        self.path = path
        self.cwd = cwd
        self.session_id = path.stem
        self.pos = 0
        self.turns = []  # rolling window of recent (role, ts, text) dicts
        self.queue_pending = 0
        self.last_user = None
        self.last_assistant = None
        self.mtime = 0.0
        self.pid = pid
        self.is_cron = is_cron
        self.cron_label = cron_label
        self.tab_id = tab_id
        self.provider = provider

    def read_new(self):
        """Read bytes since self.pos, parse JSONL turns, update state."""
        try:
            st = self.path.stat()
        except FileNotFoundError:
            return False
        self.mtime = st.st_mtime
        if st.st_size < self.pos:
            # File truncated/rotated — start over.
            self.pos = 0
            self.turns.clear()
            self.queue_pending = 0
            self.last_user = None
            self.last_assistant = None
        if st.st_size == self.pos:
            return False
        try:
            with open(self.path, "rb") as f:
                f.seek(self.pos)
                data = f.read().decode("utf-8", errors="replace")
                self.pos = f.tell()
        except OSError as e:
            log(f"read {self.path.name}: {e}", "warn")
            return False
        changed = False
        for line in data.splitlines():
            if not line.strip():
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            t = d.get("type")
            if t == "queue-operation":
                self.queue_pending += 1
                continue
            role = agent_session.record_role(d)
            if role not in ("user", "assistant"):
                continue
            msg = d.get("message", {})
            ts = d.get("timestamp") or d.get("ts")
            text = text_from_message(msg)
            if not text:
                continue
            entry = {"role": role, "ts": ts, "text": truncate(text)}
            self.turns.append(entry)
            if role == "user":
                self.last_user = entry
                self.queue_pending = 0  # processed
            elif role == "assistant":
                self.last_assistant = entry
            changed = True
        if len(self.turns) > TURNS_PER_SESSION * 4:
            self.turns = self.turns[-TURNS_PER_SESSION * 2:]
        return changed

    def to_dict(self, now):
        recent = self.turns[-TURNS_PER_SESSION:]
        user_unanswered = (recent[-1]["role"] == "user") if recent else False
        if self.queue_pending > 0:
            user_unanswered = True
        return {
            "session_id": self.session_id,
            "transcript_path": str(self.path),
            "cwd": self.cwd,
            "pid": self.pid,
            "is_cron": self.is_cron,
            "cron_label": self.cron_label,
            "tab_id": self.tab_id,
            "provider": self.provider,
            "last_modified": iso(datetime.fromtimestamp(self.mtime, tz=timezone.utc).replace(microsecond=0)),
            "age_sec": int(now.timestamp() - self.mtime) if self.mtime else None,
            "last_user": self.last_user,
            "last_assistant": self.last_assistant,
            "user_unanswered": user_unanswered,
            "queue_pending": self.queue_pending,
            "recent_turns": recent,
        }


class Watcher:
    def __init__(self):
        self.kq = select.kqueue()
        self.fd_to_state = {}     # fd -> TranscriptState
        self.path_to_fd = {}      # str(path) -> fd
        self.dirty = False
        self.last_flush = 0.0
        self.last_discovery = 0.0

    def find_active_transcripts(self, cutoff):
        """Return ONLY transcripts whose Claude process is currently alive AND
        registered in cmux. This is the real set of 'sessions inside open
        cmux workspaces'. Skips the hundreds of closed-workspace ghost
        transcripts that mtime alone can't filter out."""
        live = load_live_agent_sessions()
        # Tag the cron workers so we can mark them. We need session_id for that
        # join. The orchestrator-registry has workspace_ref; correlate by the
        # cmux-registry tab→workspace mapping. Easier: for now, infer by the
        # known cmux workspaces of the cron workers (workspace:104/105/106/97).
        # The cmux-registry doesn't store workspace_ref directly, so we tag
        # cron sessions by matching the live session's transcript path stem
        # against known orchestrator workspace session IDs we've seen recently.
        # Simpler approach: read orchestrator-registry → workspace_ref names, but
        # to map workspace_ref → session_id we need cmux tree. Skip the precise
        # mapping; just tag by cwd heuristic for now.
        cron_cwds = {"/Users/mukuls/.architect"}
        out = []
        for sid, meta in live.items():
            tpath = meta.get("transcript_path")
            if not tpath:
                continue
            p = Path(tpath)
            try:
                if not p.exists():
                    continue
                if p.stat().st_mtime < cutoff:
                    continue
            except OSError:
                continue
            cwd = meta.get("cwd") or ""
            is_cron = cwd.rstrip("/") in {c.rstrip("/") for c in cron_cwds}
            cron_label = "orchestrator-worker" if is_cron else None
            out.append({
                "path": p,
                "cwd": cwd,
                "pid": meta.get("pid"),
                "is_cron": is_cron,
                "cron_label": cron_label,
                "tab_id": meta.get("tab_id"),
                "provider": meta.get("provider") or "claude",
            })
        return out

    def add_watch(self, path, cwd, pid=None, is_cron=False, cron_label=None,
                  tab_id=None, provider="claude"):
        key = str(path)
        if key in self.path_to_fd:
            # Already watching — refresh metadata in case pid/cron tag changed.
            existing = self.fd_to_state.get(self.path_to_fd[key])
            if existing:
                existing.pid = pid
                existing.is_cron = is_cron
                existing.cron_label = cron_label
                existing.tab_id = tab_id
                existing.provider = provider
            return
        if len(self.fd_to_state) >= MAX_WATCHED_FDS:
            oldest_fd = min(self.fd_to_state, key=lambda fd: self.fd_to_state[fd].mtime)
            self.drop_watch(oldest_fd)
        try:
            fd = os.open(key, os.O_RDONLY)
        except OSError as e:
            log(f"open {path.name}: {e}", "warn")
            return
        try:
            kev = select.kevent(
                fd,
                filter=select.KQ_FILTER_VNODE,
                flags=select.KQ_EV_ADD | select.KQ_EV_ENABLE | select.KQ_EV_CLEAR,
                fflags=(
                    select.KQ_NOTE_WRITE
                    | select.KQ_NOTE_EXTEND
                    | select.KQ_NOTE_DELETE
                    | select.KQ_NOTE_RENAME
                ),
            )
            self.kq.control([kev], 0)
        except OSError as e:
            os.close(fd)
            log(f"kqueue add {path.name}: {e}", "warn")
            return
        state = TranscriptState(path, cwd, pid=pid, is_cron=is_cron,
                                cron_label=cron_label, tab_id=tab_id,
                                provider=provider)
        state.read_new()
        self.fd_to_state[fd] = state
        self.path_to_fd[key] = fd
        self.dirty = True

    def drop_watch(self, fd):
        state = self.fd_to_state.pop(fd, None)
        if state:
            self.path_to_fd.pop(str(state.path), None)
        try:
            os.close(fd)
        except OSError:
            pass

    def handle_event(self, ev):
        state = self.fd_to_state.get(ev.ident)
        if not state:
            return
        if ev.fflags & (select.KQ_NOTE_DELETE | select.KQ_NOTE_RENAME):
            log(f"drop {state.path.name} (deleted/renamed)")
            self.drop_watch(ev.ident)
            self.dirty = True
            return
        if ev.fflags & (select.KQ_NOTE_WRITE | select.KQ_NOTE_EXTEND):
            if state.read_new():
                self.dirty = True

    def discover(self):
        now = utc_now()
        cutoff = (now - timedelta(hours=ACTIVITY_HOURS)).timestamp()
        live_paths = set()
        for entry in self.find_active_transcripts(cutoff):
            self.add_watch(
                entry["path"], entry["cwd"],
                pid=entry["pid"], is_cron=entry["is_cron"],
                cron_label=entry["cron_label"], tab_id=entry["tab_id"],
                provider=entry["provider"],
            )
            live_paths.add(str(entry["path"]))
        # Drop watches for sessions whose Claude pid died since last discovery.
        for fd in list(self.fd_to_state.keys()):
            state = self.fd_to_state[fd]
            if str(state.path) not in live_paths:
                log(f"drop {state.path.name} (pid {state.pid} no longer live)")
                self.drop_watch(fd)
                self.dirty = True
        self.last_discovery = time.monotonic()

    def flush(self):
        now = utc_now()
        cutoff = now - timedelta(hours=ACTIVITY_HOURS)
        by_session = {}
        recent_inputs = []
        cron_count = 0
        human_count = 0
        for state in self.fd_to_state.values():
            d = state.to_dict(now)
            by_session[state.session_id] = d
            if state.is_cron:
                cron_count += 1
            else:
                human_count += 1
            # Cron sessions never count as "direct talk" — Mukul never typed there.
            if state.is_cron:
                continue
            lu = state.last_user
            if lu:
                lu_ts = parse_iso(lu.get("ts"))
                if lu_ts and lu_ts >= cutoff:
                    has_reply = (
                        state.last_assistant is not None
                        and parse_iso(state.last_assistant.get("ts"))
                        and parse_iso(state.last_assistant.get("ts")) > lu_ts
                    )
                    recent_inputs.append({
                        "session_id": state.session_id,
                        "cwd": state.cwd,
                        "ts": lu.get("ts"),
                        "age_sec": int((now - lu_ts).total_seconds()),
                        "text": lu.get("text"),
                        "has_assistant_reply": bool(has_reply),
                        "queue_pending": state.queue_pending,
                    })
        recent_inputs.sort(key=lambda x: parse_iso(x["ts"]) or now, reverse=True)
        recent_inputs = recent_inputs[:RECENT_INPUTS_LIMIT]
        payload = {
            "_meta": {
                "built_at": iso(now),
                "watched": len(self.fd_to_state),
                "watched_human": human_count,
                "watched_cron": cron_count,
                "activity_hours": ACTIVITY_HOURS,
                "mode": "event-driven (live cmux sessions only)",
            },
            "by_session": by_session,
            "recent_user_inputs": recent_inputs,
        }
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUT_PATH.write_text(json.dumps(payload, indent=2))
        self.dirty = False
        self.last_flush = time.monotonic()

    def run(self):
        log("starting; initial discovery")
        self.discover()
        self.flush()
        log(f"watching {len(self.fd_to_state)} transcripts")
        while True:
            timeout = max(0.1, DISCOVERY_INTERVAL_SEC - (time.monotonic() - self.last_discovery))
            try:
                events = self.kq.control(None, 32, timeout)
            except InterruptedError:
                continue
            for ev in events:
                self.handle_event(ev)
            now_mono = time.monotonic()
            if self.dirty and (now_mono - self.last_flush) >= FLUSH_DEBOUNCE_SEC:
                self.flush()
            if (now_mono - self.last_discovery) >= DISCOVERY_INTERVAL_SEC:
                self.discover()


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--daemon", action="store_true", default=True)
    g.add_argument("--once", action="store_true")
    args = ap.parse_args()
    if args.once:
        # One-shot: same as build-session-context.py but uses the watcher's flush path.
        w = Watcher()
        w.discover()
        w.flush()
        print(f"flushed {len(w.fd_to_state)} sessions → {OUT_PATH}")
        return
    if not acquire_lock():
        log("another watcher is already running — exiting", "warn")
        return
    try:
        Watcher().run()
    except KeyboardInterrupt:
        log("interrupted")
    except Exception as e:
        log(f"crash: {type(e).__name__}: {e}", "error")
        raise
    finally:
        release_lock()


if __name__ == "__main__":
    main()
