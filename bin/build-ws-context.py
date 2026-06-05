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
    "transcript_source": "screen_session_id" | "heuristic" | null,
    "session_id8": "<8hex>" | null,
    "last_turn_age_sec": <int|null>,
    "agent_status": "working" | "idle",
    "cwd_dirty": <bool>,
    "cwd_unpushed": <bool>,
    "is_protected": <bool>,
    "screen_text": "<live AGENT-pane viewport+scrollback>",
    "screen_shows_error": <bool>
  }

Two signals harden this against the ws:12 (2026-06-05) misattribution bug,
where a workspace hosting both an interactive session and a headless comms
pulse made find_transcript()'s mtime/cwd heuristic pick a different stranger's
jsonl every pulse:

  - `screen_text` is the live terminal of the CLAUDE AGENT pane (selected by
    its status-bar signature, not just whatever pane is focused), so a split
    workspace's shell/dev-server pane can't masquerade as the agent.
  - `transcript_path` is resolved FIRST from the session-id the agent stamps
    on its own status bar (`#<8hex>`) — exact, no guessing — and only falls
    back to the old heuristic when that id isn't on screen. `transcript_source`
    records which path was used so the Observer (and audits) can see when a
    verdict rests on a guessed transcript.

The Observer is still told to trust the screen over transcript-derived signals
on conflict — the screen is what the agent is showing RIGHT NOW.
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
CMUX_BIN = os.environ.get(
    "CMUX_BIN", "/Applications/cmux.app/Contents/Resources/bin/cmux")

# How many lines of live terminal to hand the Observer. The visible viewport
# plus enough scrollback to capture the last recap/error and a few turns of
# context, without bloating the batch prompt. A stuck agent's tell (API error
# banner, "esc to interrupt", a half-finished tool call) lives in the last
# ~40 lines; 120 gives margin for a recap that scrolled up a little.
SCREEN_LINES = 120

# Patterns that mean "the live screen is showing a halted/error state the
# transcript may not reflect" — a Claude turn that ENDED on an error.
#
# These MUST be anchored to how Claude Code renders a halt, not loose
# substrings: an agent that is merely *discussing* or *editing* the words
# "API Error" / "Traceback" (e.g. this very session, debugging the detector)
# would otherwise trip a spurious `stranded` nudge. The real tell is the
# assistant-turn bullet `⏺ ` immediately followed by the error text — that's
# the agent's OWN last output being an error, which is what strands it.
# Matched per-line, case-insensitive, against screen_text.
_ERROR_LINE_RE = re.compile(
    r"""^\s*               # leading indent cmux may render
        [⏺·∙•*]\s*         # the assistant-turn bullet glyph
        (?:                # …immediately followed by an error envelope:
            api\ error\b
          | request\ timed\ out\b
          | (?:the\ )?system\ encountered\ an\ unexpected\ error
          | overloaded(?:_error)?\b
          | rate[\ _]limit(?:_error)?\b
          | connection\ error\b
        )
    """,
    re.IGNORECASE | re.VERBOSE,
)


# The Claude Code status bar prints the running session-id prefix as `#<8hex>`
# (e.g. `… │ $9.55 │ #6fb0c668`). This is the agent telling us its OWN session,
# so it's the ground truth for (a) which pane in a split workspace is the agent
# and (b) which transcript on disk is really this session's — both of which the
# mtime/cwd heuristics in find_transcript() get wrong when a workspace has more
# than one jsonl in scope (ws:12, 2026-06-05: a headless comms pulse kept
# writing transcripts into the same project dir, so find_transcript() picked a
# different stranger's jsonl every pulse). We trust the on-screen id over the
# heuristic whenever it's present.
_SESSION_ID_RE = re.compile(r"#([0-9a-f]{8})\b")
# A pane is the Claude agent if its screen carries any of these. The session-id
# stamp is the strongest (present idle AND working); the others catch a pane
# that's still booting or whose status bar scrolled off the read window.
_CLAUDE_PANE_MARKERS = (
    "bypass permissions on",
    "Claude Code v",
    "esc to interrupt",
    "⏵⏵",
)


def _read_surface(surface_ref: str, ws_ref: str, lines: int = SCREEN_LINES) -> str:
    """Read one surface. A surface ref needs a window context to resolve, and
    the workspace ref supplies it (`read-screen --surface S --workspace W`).
    Returns '' for a non-terminal surface (browser/markdown pane) or any error
    — those carry no agent signal."""
    if not surface_ref:
        return ""
    try:
        r = subprocess.run(
            [CMUX_BIN, "read-screen", "--surface", surface_ref,
             "--workspace", ws_ref, "--scrollback", "--lines", str(lines)],
            capture_output=True, text=True, timeout=15,
        )
    except Exception:
        return ""
    return (r.stdout or "").strip() if r.returncode == 0 else ""


def _list_surfaces(ws_ref: str) -> list[str]:
    """Surface refs for a workspace, in cmux's listing order. The
    `[selected]`-marked one (if any) is moved to the front so single-read
    callers and tie-breaks prefer the focused pane."""
    if not ws_ref:
        return []
    try:
        r = subprocess.run(
            [CMUX_BIN, "list-pane-surfaces", "--workspace", ws_ref],
            capture_output=True, text=True, timeout=15,
        )
    except Exception:
        return []
    if r.returncode != 0:
        return []
    surfaces, selected = [], None
    for line in r.stdout.splitlines():
        m = re.search(r"surface:\d+", line)
        if not m:
            continue
        ref = m.group(0)
        surfaces.append(ref)
        if "[selected]" in line:
            selected = ref
    if selected and surfaces and surfaces[0] != selected:
        surfaces.remove(selected)
        surfaces.insert(0, selected)
    return surfaces


def _is_claude_pane(text: str) -> bool:
    if not text:
        return False
    if _SESSION_ID_RE.search(text):
        return True
    return any(m in text for m in _CLAUDE_PANE_MARKERS)


def _bound(text: str) -> str:
    # Bound the payload: 120 lines of terminal is plenty for state detection,
    # but a wide terminal can still be large, and several go inline into the
    # Observer batch prompt. Keep the TAIL (most recent output — where the
    # recap / error / spinner lives) if it's oversized.
    MAX_CHARS = 12000
    if len(text) > MAX_CHARS:
        return "…[earlier screen truncated]…\n" + text[-MAX_CHARS:]
    return text


def read_agent_screen(ws_ref: str, lines: int = SCREEN_LINES) -> tuple[str, str | None]:
    """Read the live terminal of the CLAUDE AGENT pane for ws_ref.

    A workspace can hold more than one pane (agent + a shell/dev-server). We
    enumerate surfaces and return the one that looks like a Claude session, so
    the screen we hand the Observer is the AGENT's, not whatever pane happens
    to be selected. Returns (screen_text, session_id8):

      - screen_text: bounded agent-pane text, or '' if no pane could be read.
      - session_id8: the 8-hex session-id prefix the agent stamps on its
        status bar, or None if not visible (e.g. headless / booting).

    Falls back to the first/selected surface when no pane self-identifies as
    Claude, so a still-booting agent (banner not yet rendered) isn't dropped.
    """
    if not ws_ref:
        return "", None
    surfaces = _list_surfaces(ws_ref)
    if not surfaces:
        # cmux too old for list-pane-surfaces, or no panes: fall back to the
        # workspace-level read (the original behavior).
        text = _read_surface_via_workspace(ws_ref, lines)
        return _bound(text), _session_id_from(text)

    first_text = ""
    for ref in surfaces:
        text = _read_surface(ref, ws_ref, lines)
        if not first_text and text:
            first_text = text
        if _is_claude_pane(text):
            return _bound(text), _session_id_from(text)
    # No pane self-identified as Claude — use the first non-empty (selected
    # is already at index 0). Better a maybe-wrong screen than none, but it
    # won't carry a session id, so transcript resolution falls back too.
    return _bound(first_text), _session_id_from(first_text)


def _read_surface_via_workspace(ws_ref: str, lines: int = SCREEN_LINES) -> str:
    """Workspace-scoped read (selected surface) — fallback when surface
    enumeration is unavailable."""
    try:
        r = subprocess.run(
            [CMUX_BIN, "read-screen", "--workspace", ws_ref,
             "--scrollback", "--lines", str(lines)],
            capture_output=True, text=True, timeout=15,
        )
    except Exception:
        return ""
    return (r.stdout or "").strip() if r.returncode == 0 else ""


def _session_id_from(screen_text: str) -> str | None:
    m = _SESSION_ID_RE.search(screen_text or "")
    return m.group(1) if m else None


def transcript_from_session_id(sid8: str | None) -> str | None:
    """Resolve a session-id prefix to its transcript jsonl by globbing every
    project dir. This is exact: the agent printed this id, so the file named
    <sid>*.jsonl IS its transcript — no mtime/cwd guessing. Newest match wins
    on the (vanishingly rare) 8-hex prefix collision."""
    if not sid8:
        return None
    projects = HOME / ".claude/projects"
    if not projects.is_dir():
        return None
    matches = [str(p) for p in projects.glob(f"*/{sid8}*.jsonl")]
    if not matches:
        return None
    return max(matches, key=os.path.getmtime)


def screen_shows_error(screen_text: str) -> bool:
    """True when a LINE of the screen is an assistant turn that ended on an
    error banner (`⏺ API Error: …`). Deliberately does NOT match the words
    appearing inside prose or edited code — only the rendered halt — so a
    session discussing errors isn't flagged as stranded on one."""
    if not screen_text:
        return False
    return any(_ERROR_LINE_RE.match(line) for line in screen_text.splitlines())


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

    # Read the AGENT pane's screen first — it carries the session-id stamp,
    # which is the ground truth for which transcript is really this session's.
    screen, sid8 = read_agent_screen(args.ws_ref)

    # Transcript resolution, best signal first:
    #   1. the session id the agent printed on its own status bar (exact), then
    #   2. the registry/cwd heuristic (mtime-guessing — wrong when a workspace
    #      has multiple jsonls in scope, which is exactly the ws:12 bug).
    transcript = transcript_from_session_id(sid8)
    transcript_source = "screen_session_id" if transcript else None
    if not transcript:
        transcript = find_transcript(args.ws_ref, args.title, args.cwd or None)
        transcript_source = "heuristic" if transcript else None

    age, agent_status = transcript_signals(transcript)
    dirty, unpushed = cwd_state(args.cwd)

    payload = {
        "ws_ref": args.ws_ref,
        "title": args.title,
        "cwd": args.cwd,
        "transcript_path": transcript,
        "transcript_source": transcript_source,
        "session_id8": sid8,
        "last_turn_age_sec": age,
        "agent_status": agent_status,
        "cwd_dirty": dirty,
        "cwd_unpushed": unpushed,
        "is_protected": args.ws_ref in PROTECTED_REFS,
        "screen_text": screen,
        "screen_shows_error": screen_shows_error(screen),
    }
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
