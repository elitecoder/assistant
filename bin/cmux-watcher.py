#!/usr/bin/env python3
"""cmux-watcher — tap the cmux agent event stream and drop inbox signals.

Mukul's assistant polls workspaces every few minutes via pulse.py. cmux already
emits Claude Code lifecycle events (via its hook wrapper) and streams them over
`cmux events`. This watcher subscribes to that stream and turns two classes of
event into inbox items under ~/.assistant/inbox within seconds:

  needs_input   — the agent is waiting on the user (a permission Notification or
                  an AskUserQuestion). Always dropped.
  work_complete — a turn ended (agent.hook.Stop). The terminal screen is read and
                  matched against pattern_bank.json; an item is dropped ONLY when
                  a non-muted pattern fires (PR opened, CI green, awaiting review,
                  …). A bare turn-end with nothing notable is never pinged — that
                  is the noise floor.

REALITY NOTE (verified 2026-06-06 against the installed cmux build): this build
does NOT emit `agent.session.lifecycle` running/idle/needsInput events. The real
signal carriers are the `agent.hook.*` events:

    agent.hook.Stop             → a turn finished (the idle / running→idle signal)
    agent.hook.Notification     → the agent posted a notification (needs input)
    agent.hook.AskUserQuestion  → the agent asked the user a question
    agent.hook.SessionEnd       → the session ended

Each event's `payload.workspace_id` is a UUID, not a `workspace:NN` ref, and the
tool input / screen text is redacted from the event itself — so we read the live
terminal with `cmux read-screen --workspace <uuid>` to pattern-match. Events
arrive as `phase: "received"` then `phase: "completed"` pairs sharing one
`_opencode_request_id`; we de-dup on that id so each turn is handled once.

The watcher reconnects forever (exponential backoff, max 60s) on stream EOF,
never crashes on malformed JSON, creates a default pattern_bank.json on first
run, hot-reloads the bank when it changes (lazy mtime check at point of use —
event-driven, not a poll loop), and exits cleanly if cmux is not running. It
never calls `launchctl load`.
"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path

HOME = Path(os.environ.get("HOME", str(Path.home())))
ASSISTANT_DIR = Path(os.environ.get("CMUX_WATCHER_ASSISTANT_DIR",
                                    str(HOME / ".assistant")))
INBOX_DIR = ASSISTANT_DIR / "inbox"
PATTERN_BANK_PATH = Path(os.environ.get("CMUX_PATTERN_BANK",
                                        str(ASSISTANT_DIR / "pattern_bank.json")))
LOG_PATH = ASSISTANT_DIR / "cmux-watcher.log"
# Audit trail of every pattern that fired — pattern-feedback.py / lesson
# discovery correlate user response against these rows.
FIRED_LOG = ASSISTANT_DIR / "cmux-fired-patterns.jsonl"

CMUX_BIN = os.environ.get("CMUX_BIN",
                          "/Applications/cmux.app/Contents/Resources/bin/cmux")

# Don't re-ping the same workspace for the same signal more than once per window.
# Notifications in particular can fire repeatedly while the agent waits.
COOLDOWN_SEC = int(os.environ.get("CMUX_WATCHER_COOLDOWN_SEC", "120"))
# How many recent request-ids to remember for de-dup (received/completed pairs +
# bursty repeats). Bounded so memory never grows without limit.
SEEN_IDS_MAX = 4096
# Workspace UUID→ref map is cached this long before a refresh.
WS_MAP_TTL_SEC = 30
# Screen read window for pattern matching.
SCREEN_LINES = 50

# Event names we care about. Everything else (PreToolUse, UserPromptSubmit,
# heartbeats, acks) is ignored.
NEEDS_INPUT_EVENTS = {
    "agent.hook.Notification",
    "agent.hook.AskUserQuestion",
    # Feed events — fired when the agent presents a multi-choice dialog
    "feed.item.received",
}
TURN_END_EVENTS = {"agent.hook.Stop"}

DEFAULT_PATTERN_BANK = {
    "version": 1,
    "patterns": [
        {"id": "pr-opened", "regex": r"PR #\d+ (opened|created|shipped)",
         "signal": "work_complete", "priority": "high"},
        {"id": "awaiting-review",
         "regex": r"(awaiting|waiting for|needs).{0,30}(review|approval|your input)",
         "signal": "needs_input", "priority": "high"},
        {"id": "ci-green",
         "regex": r"(all CI (checks )?green|CI (is )?green|✓.*CI|Jenkins.*SUCCESS)",
         "signal": "work_complete", "priority": "medium", "suppress": True},
        {"id": "ci-red",
         "regex": r"(CI (is )?red|CI fail|Jenkins.*FAIL|OURS.*failure)",
         "signal": "needs_input", "priority": "high"},
        {"id": "emit-card", "regex": r"(needs_user|emit.card|awaiting your)",
         "signal": "needs_input", "priority": "high"},
        {"id": "stranded", "regex": r"(stranded|stuck|blocked|timed out|API error)",
         "signal": "needs_input", "priority": "medium"},
        {"id": "tests-pass", "regex": r"\d+ (tests?|specs?) (passing|passed|green)",
         "signal": "work_complete", "priority": "low"},
        {"id": "committed", "regex": r"\[main [a-f0-9]{7}\]",
         "signal": "work_complete", "priority": "low"},
    ],
}


def utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(msg: str) -> None:
    """One line to the watcher log and stderr. Never raises."""
    line = f"[{utc_iso()}] {msg}"
    try:
        ASSISTANT_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass
    print(line, file=sys.stderr, flush=True)


# ─── pattern bank ─────────────────────────────────────────────────────────────

class PatternBank:
    """The compiled pattern set, hot-reloaded by mtime.

    Reload is lazy: maybe_reload() stats the file and re-reads only when the
    mtime advances. This is point-of-use invalidation, not a background poll —
    no timer thread, and the next match after a write picks up the change. A
    missing bank file is created from DEFAULT_PATTERN_BANK on first load.
    """

    def __init__(self, path: Path = PATTERN_BANK_PATH):
        self.path = path
        self._mtime: float | None = None
        self.patterns: list[dict] = []
        self._compiled: list[tuple[dict, re.Pattern]] = []
        self.load()

    def _ensure_default(self) -> None:
        if self.path.exists():
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(DEFAULT_PATTERN_BANK, indent=2))
            os.replace(tmp, self.path)
            log(f"pattern_bank: created default at {self.path}")
        except OSError as e:
            log(f"pattern_bank: could not write default: {e}")

    def load(self) -> None:
        self._ensure_default()
        try:
            raw = self.path.read_text()
            data = json.loads(raw)
            self._mtime = self.path.stat().st_mtime
        except (OSError, json.JSONDecodeError) as e:
            log(f"pattern_bank: load failed ({e}); using defaults in-memory")
            data = DEFAULT_PATTERN_BANK
            self._mtime = None
        self.patterns = list(data.get("patterns", []))
        self._compile()

    def _compile(self) -> None:
        compiled: list[tuple[dict, re.Pattern]] = []
        for p in self.patterns:
            regex = p.get("regex")
            if not regex:
                continue
            try:
                compiled.append((p, re.compile(regex, re.IGNORECASE)))
            except re.error as e:
                log(f"pattern_bank: bad regex id={p.get('id')!r}: {e}")
        self._compiled = compiled

    def maybe_reload(self) -> bool:
        """Re-read the bank if the file changed on disk. Returns True on reload."""
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            return False
        if self._mtime is None or mtime != self._mtime:
            self.load()
            log(f"pattern_bank: hot-reloaded {len(self.patterns)} pattern(s)")
            return True
        return False

    def match(self, text: str) -> list[dict]:
        """Return every non-muted pattern whose regex hits `text`, highest
        priority first. Muted patterns never match — that is how feedback
        silences a noisy pattern without deleting it."""
        self.maybe_reload()
        order = {"high": 0, "medium": 1, "low": 2}
        hits: list[dict] = []
        for p, rx in self._compiled:
            if p.get("priority") == "muted":
                continue
            if rx.search(text or ""):
                hits.append(p)
        hits.sort(key=lambda p: order.get(p.get("priority", "low"), 3))
        return hits


# ─── cmux helpers ─────────────────────────────────────────────────────────────

def _run(argv: list[str], timeout: int = 15) -> tuple[int, str, str]:
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired as e:
        return -1, e.stdout or "", f"timeout after {timeout}s"
    except FileNotFoundError as e:
        return -1, "", str(e)
    except Exception as e:  # noqa: BLE001
        return -1, "", str(e)


def cmux_available() -> bool:
    rc, _, _ = _run([CMUX_BIN, "ping"], timeout=10)
    return rc == 0


class WsRefResolver:
    """Maps a workspace UUID → its `workspace:NN` ref, cached with a short TTL.

    Events carry UUIDs; the inbox payload reads nicer with the ref. A miss
    forces one refresh (a freshly-spawned workspace), then falls back to the
    UUID so a drop is never blocked on resolution."""

    def __init__(self, ttl: int = WS_MAP_TTL_SEC, clock=time.time):
        self.ttl = ttl
        self._clock = clock
        self._map: dict[str, str] = {}
        self._fetched_at = 0.0

    def _refresh(self) -> None:
        rc, out, _ = _run([CMUX_BIN, "rpc", "workspace.list", "{}"], timeout=15)
        if rc != 0:
            return
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            return
        new_map: dict[str, str] = {}
        for w in data.get("workspaces", []):
            wid = (w.get("id") or "").upper()
            ref = w.get("ref")
            if wid and ref:
                new_map[wid] = ref
        if new_map:
            self._map = new_map
            self._fetched_at = self._clock()

    def resolve(self, uuid: str | None) -> str | None:
        if not uuid:
            return None
        key = uuid.upper()
        now = self._clock()
        if now - self._fetched_at > self.ttl:
            self._refresh()
        ref = self._map.get(key)
        if ref:
            return ref
        # Miss on a fresh cache → one forced refresh (a new workspace).
        if now - self._fetched_at <= 1:
            return uuid
        self._refresh()
        return self._map.get(key, uuid)


def read_screen(workspace: str, lines: int = SCREEN_LINES) -> str:
    """Read the live terminal for a workspace UUID/ref. Returns "" for a dead or
    headless workspace (cmux prints `Error: not_found` for those — a natural
    filter that keeps us from pinging about `claude --print` subprocesses)."""
    rc, out, _ = _run(
        [CMUX_BIN, "read-screen", "--workspace", workspace,
         "--lines", str(lines)], timeout=15)
    if rc != 0:
        return ""
    text = out or ""
    if text.lstrip().startswith("Error:"):
        return ""
    return text


# Lines that are pure TUI chrome — box-drawing rules, the status bar, the
# bypass-permissions hint — carry no signal and just bloat the phone snippet.
_CHROME_RE = re.compile(r"^[\s│─╭╮╰╯▔▕>·•⏵◀▶]+$")
_STATUS_BAR_RE = re.compile(r"bypass permissions on|shift\+tab to cycle")


def last_lines(text: str, n: int = 3) -> str:
    """Last n content-bearing lines of the screen, joined — the inbox snippet.

    Drops blank lines, pure box-drawing / separator rules, and the cmux status
    bar so the snippet reflects what the agent actually printed, not TUI chrome.
    Falls back to the raw tail if filtering leaves nothing (rare)."""
    rows = []
    for ln in (text or "").splitlines():
        s = ln.rstrip()
        if not s.strip():
            continue
        if _CHROME_RE.match(s) or _STATUS_BAR_RE.search(s):
            continue
        rows.append(s)
    if not rows:
        rows = [ln.rstrip() for ln in (text or "").splitlines() if ln.strip()]
    return "\n".join(rows[-n:])


# ─── event classification (pure) ──────────────────────────────────────────────

def classify_event(evt: dict) -> dict | None:
    """Decide what an event means, independent of side effects.

    Returns None for frames we ignore (acks, heartbeats, PreToolUse, …) or for
    the `received` half of a paired hook (we act on `completed`, or on the lone
    phase if only one is present — handled by request-id de-dup upstream).

    On a relevant event returns:
        {"signal": "needs_input"|"turn_end",
         "request_id": <str|None>, "workspace_id": <uuid|None>,
         "cwd": <str|None>, "event_name": <str>}
    `turn_end` still needs a screen read + pattern match before any drop;
    `needs_input` is dropped unconditionally (subject to cooldown).
    """
    if not isinstance(evt, dict):
        return None
    if evt.get("type") != "event":
        return None  # ack / heartbeat / unknown frame
    name = evt.get("name")
    if name not in NEEDS_INPUT_EVENTS and name not in TURN_END_EVENTS:
        return None
    payload = evt.get("payload") or {}
    if not isinstance(payload, dict):
        payload = {}
    # Workspace id can live on the frame or in the payload; the frame wins.
    workspace_id = evt.get("workspace_id") or payload.get("workspace_id")
    request_id = payload.get("_opencode_request_id")
    signal_kind = "needs_input" if name in NEEDS_INPUT_EVENTS else "turn_end"
    return {
        "signal": signal_kind,
        "request_id": request_id,
        "workspace_id": workspace_id,
        "cwd": payload.get("cwd"),
        "event_name": name,
    }


# ─── inbox drop ───────────────────────────────────────────────────────────────

def _slug(ws_ref: str | None) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", (ws_ref or "unknown")).strip("-") or "unknown"


def drop_inbox_item(ws_ref: str | None, signal_type: str,
                    pattern_matched: str, screen_snippet: str,
                    inbox_dir: Path = INBOX_DIR) -> Path:
    """Atomically write one inbox item. Returns the final path.

    The shape is:
        {ts, event, ws_ref, signal_type, pattern_matched, screen_snippet}
    Written to a unique temp file then os.replace'd so a reader never sees a
    half-written file (a kqueue watcher wakes on the rename)."""
    inbox_dir.mkdir(parents=True, exist_ok=True)
    item = {
        "ts": utc_iso(),
        "event": "workspace_signal",
        "ws_ref": ws_ref,
        "signal_type": signal_type,
        "pattern_matched": pattern_matched,
        "screen_snippet": screen_snippet,
    }
    # Unique name: ws slug + monotonic-ish stamp + pid so concurrent drops never
    # collide. The temp file carries the pid too so two watchers can't clobber.
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    final = inbox_dir / f"cmux-{_slug(ws_ref)}-{stamp}.json"
    tmp = inbox_dir / f".cmux-{_slug(ws_ref)}-{stamp}.{os.getpid()}.tmp"
    tmp.write_text(json.dumps(item, indent=2))
    os.replace(tmp, final)
    return final


def record_fired(pattern_id: str, ws_ref: str | None, signal_type: str) -> None:
    """Append one row to the fired-patterns log (best-effort audit trail)."""
    try:
        FIRED_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(FIRED_LOG, "a") as f:
            f.write(json.dumps({
                "ts": utc_iso(),
                "pattern_id": pattern_id,
                "ws_ref": ws_ref,
                "signal_type": signal_type,
            }) + "\n")
    except OSError:
        pass


# ─── watcher state + handling ──────────────────────────────────────────────────

class WatcherState:
    """Per-process de-dup + cooldown memory. Not persisted: on restart we begin
    fresh from `now`, so a restart never replays a backlog of old turn-ends."""

    def __init__(self, cooldown_sec: int = COOLDOWN_SEC, clock=time.time):
        self.cooldown_sec = cooldown_sec
        self._clock = clock
        self._seen_ids: "OrderedDict[str, bool]" = OrderedDict()
        self._last_drop: dict[tuple[str, str], float] = {}

    def already_seen(self, request_id: str | None) -> bool:
        """True if this request-id was handled before. Bounded LRU. A None id
        (some hooks omit it) is never de-duped — cooldown still guards spam."""
        if not request_id:
            return False
        if request_id in self._seen_ids:
            return True
        self._seen_ids[request_id] = True
        while len(self._seen_ids) > SEEN_IDS_MAX:
            self._seen_ids.popitem(last=False)
        return False

    def cooled_down(self, ws_key: str, signal_type: str) -> bool:
        """True if we may drop now (outside the cooldown window for this
        workspace+signal). Records the drop time when it returns True."""
        key = (ws_key, signal_type)
        now = self._clock()
        last = self._last_drop.get(key, 0.0)
        if now - last < self.cooldown_sec:
            return False
        self._last_drop[key] = now
        return True


def handle_event(evt: dict, bank: PatternBank, state: WatcherState,
                 resolver: WsRefResolver, *, screen_reader=read_screen) -> dict | None:
    """Process one parsed event end-to-end. Returns the dropped item dict (for
    tests/logging) or None when nothing was dropped. `screen_reader` is
    injectable so tests don't shell out to cmux."""
    cls = classify_event(evt)
    if cls is None:
        return None
    if state.already_seen(cls["request_id"]):
        return None

    workspace_id = cls["workspace_id"]
    ws_ref = resolver.resolve(workspace_id)
    ws_key = ws_ref or workspace_id or "unknown"

    if cls["signal"] == "needs_input":
        # Always a signal — the agent is blocked on the user. Cooldown only.
        if not state.cooled_down(ws_key, "needs_input"):
            return None
        snippet = last_lines(screen_reader(workspace_id or ws_ref or ""))
        pattern_matched = cls["event_name"].split(".")[-1]  # Notification / AskUserQuestion
        item = drop_inbox_item(ws_ref, "needs_input", pattern_matched, snippet)
        record_fired(pattern_matched, ws_ref, "needs_input")
        log(f"drop needs_input ws={ws_ref or workspace_id} via={pattern_matched} → {item.name}")
        return {"path": str(item), "signal_type": "needs_input",
                "pattern_matched": pattern_matched, "ws_ref": ws_ref}

    # turn_end: read the screen and pattern-match. Drop only on a non-muted hit.
    screen = screen_reader(workspace_id or ws_ref or "")
    if not screen:
        return None  # dead/headless workspace or read failure — nothing to judge
    hits = bank.match(screen)
    if not hits:
        return None  # plain turn-end with nothing notable — the noise floor
    top = hits[0]
    if top.get("suppress"):
        return None
    pat_signal = top.get("signal")
    signal_type = pat_signal if pat_signal in ("needs_input", "work_complete") else "pattern_match"
    if not state.cooled_down(ws_key, signal_type):
        return None
    snippet = last_lines(screen)
    item = drop_inbox_item(ws_ref, signal_type, top.get("id", ""), snippet)
    record_fired(top.get("id", ""), ws_ref, signal_type)
    log(f"drop {signal_type} ws={ws_ref or workspace_id} pattern={top.get('id')} → {item.name}")
    return {"path": str(item), "signal_type": signal_type,
            "pattern_matched": top.get("id"), "ws_ref": ws_ref}


# ─── stream loop ───────────────────────────────────────────────────────────────

def stream_events(stop_flag=None, on_proc=None):
    """Yield parsed event dicts from `cmux events --category agent --reconnect`.

    Reconnects with exponential backoff (max 60s) on EOF/crash. Malformed lines
    are skipped (logged sparsely). Stops when stop_flag() returns True. Exits
    the generator if the cmux binary cannot be spawned at all.

    `on_proc(proc_or_None)` is called with the live Popen as soon as it spawns
    (and with None when it exits). The signal handler uses this to terminate the
    subprocess on SIGTERM/SIGINT, which unblocks the `for line in proc.stdout`
    read instantly — otherwise shutdown waits for the next event line to arrive
    (a stuck watcher on a quiet stream, observed 2026-06-06)."""
    backoff = 1.0
    while not (stop_flag and stop_flag()):
        try:
            proc = subprocess.Popen(
                [CMUX_BIN, "events", "--category", "agent", "--category", "feed",
                 "--reconnect", "--no-heartbeat"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            )
        except FileNotFoundError:
            log(f"cmux binary not found at {CMUX_BIN}; exiting")
            return
        if on_proc:
            on_proc(proc)
        log("listening on cmux events --category agent --reconnect")
        backoff = 1.0  # reset on a successful connect
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                if stop_flag and stop_flag():
                    proc.terminate()
                    return
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue  # never crash on a malformed frame
        finally:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                pass
            if on_proc:
                on_proc(None)
        if stop_flag and stop_flag():
            return
        log(f"event stream ended; reconnecting in {backoff:.0f}s")
        time.sleep(backoff)
        backoff = min(backoff * 2, 60.0)


def main() -> int:
    ASSISTANT_DIR.mkdir(parents=True, exist_ok=True)
    INBOX_DIR.mkdir(parents=True, exist_ok=True)

    if not cmux_available():
        log("cmux not available (ping failed) — exiting")
        return 0

    stopping = {"v": False}
    live_proc: dict = {"p": None}

    def handle_sig(signum, frame):  # noqa: ARG001
        log(f"signal {signum} — shutting down")
        stopping["v"] = True
        # Terminate the running `cmux events` subprocess so the blocking
        # `for line in proc.stdout` read returns immediately, instead of waiting
        # for the next event line on a quiet stream.
        proc = live_proc.get("p")
        if proc is not None:
            try:
                proc.terminate()
            except Exception:  # noqa: BLE001
                pass
    signal.signal(signal.SIGTERM, handle_sig)
    signal.signal(signal.SIGINT, handle_sig)

    bank = PatternBank()
    state = WatcherState()
    resolver = WsRefResolver()
    log(f"cmux-watcher started (pid={os.getpid()}) — "
        f"{len(bank.patterns)} pattern(s), inbox={INBOX_DIR}")

    n_seen = 0
    n_dropped = 0
    for evt in stream_events(stop_flag=lambda: stopping["v"],
                             on_proc=lambda p: live_proc.__setitem__("p", p)):
        n_seen += 1
        try:
            result = handle_event(evt, bank, state, resolver)
            if result:
                n_dropped += 1
        except Exception as e:  # noqa: BLE001 — one bad event must never kill the watcher
            log(f"handle_event error (ignored): {e}")
    log(f"cmux-watcher stopped (events_seen={n_seen}, dropped={n_dropped})")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
