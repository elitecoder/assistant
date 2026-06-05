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
    "transcript_path": "/Users/.../<session>.jsonl" | null,
    "transcript_source": "screen_session_id" | "registry_live_pid" | null,
    "session_id8": "<8hex>" | null,
    "agent_surface": "surface:N" | null,
    "last_turn_age_sec": <int|null>,
    "agent_status": "working" | "idle",
    "cwd_dirty": <bool>,
    "cwd_unpushed": <bool>,
    "is_protected": <bool>,
    "screen_text": "<live AGENT-pane viewport+scrollback>",
    "screen_shows_error": <bool>
  }

ABSOLUTE INVARIANT (operator, 2026-06-05): never attach a transcript from an
old or wrong workspace — a wrong transcript is worse than none. `transcript_path`
is emitted ONLY when its identity is VERIFIED against this workspace's live
agent; otherwise it is `null` and the Observer falls back to `screen_text`.

How identity is established (no guessing — the old mtime/cwd/title heuristic
that caused the ws:12 misattribution has been DELETED):

  - The CLAUDE AGENT pane is found by enumerating ALL panes (not the focused
    one — a split workspace's focused pane is often a shell) and picking the
    one carrying a Claude status bar / boot banner. Its screen is `screen_text`.
  - `transcript_path` resolves from the session-id the agent stamps on its OWN
    status bar (`… │ #<8hex>`), then confirms the file's internal `sessionId`
    matches — exact, self-verifying. Fallback: a cmux-registry row for the
    agent pane's surface UUID, but ONLY when its `claude_pid` is still alive
    AND it agrees with the live screen (the registry goes stale on surface
    reuse — the ws:12 trap). `transcript_source` records which gate passed.

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


# ─── Agent-pane + transcript identification ──────────────────────────────────
#
# INVARIANT (operator, 2026-06-05): never attach a transcript from an old or
# wrong workspace. A wrong transcript is worse than none. So transcript_path is
# emitted ONLY when its identity is VERIFIED to belong to this workspace's live
# agent. Every signal below is either verified or discarded — never guessed.
#
# Empirically-confirmed traps this guards against (all reproduced on live/test
# workspaces 2026-06-05):
#   - `rpc surface.list {workspace:N}` IGNORES the param and returns the CALLER's
#     workspace → never used for targeting. CLI `--workspace` flags target right.
#   - A naive `#<8hex>` search matches session ids printed in CONVERSATION
#     CONTENT, not just the status bar (a session discussing other sessions —
#     like this one — self-misattributes). The id is read ONLY from a status-bar
#     -shaped line (`… │ #<8hex>` at end-of-line).
#   - `read-screen --workspace` reads only the FOCUSED pane; in a split
#     workspace the focused pane is often a shell, not the agent. We enumerate
#     ALL panes and pick the agent.
#   - The cmux registry's surface→session map goes STALE when a surface is
#     reused (ws:12: registry pointed at a dead headless pulse's session while
#     the live agent ran a different one). Registry entries are trusted only
#     when their claude_pid is still alive AND they agree with the live screen.

# The Claude Code status bar's last cell is the running session-id prefix,
# rendered as `… │ #<8hex>` at the END of a box-drawing line. Anchoring to that
# shape (not a bare `#<8hex>`) is what stops content/quote false matches.
_STATUS_BAR_SID_RE = re.compile(r"│[^│\n]*#([0-9a-f]{8})\b\s*$")
# A pane is the Claude agent if its screen carries any of these. The status-bar
# sid is strongest; the others catch a pane still booting (banner up, status bar
# not yet rendered) so we still identify the agent pane and read its screen.
_CLAUDE_PANE_MARKERS = (
    "bypass permissions on",
    "Claude Code v",
    "esc to interrupt",
    "⏵⏵",
    "Opus 4",          # boot banner model line
    "Sonnet 4",
    "Haiku 4",
)
_SURFACE_LINE_RE = re.compile(r"(surface:\d+)\s+([0-9A-Fa-f-]{36})")


def _cmux(args: list[str], timeout: int = 15):
    """Run a cmux CLI command, return CompletedProcess or None on failure."""
    try:
        return subprocess.run([CMUX_BIN, *args], capture_output=True,
                              text=True, timeout=timeout)
    except Exception:
        return None


def _bound(text: str) -> str:
    # Bound the payload: 120 lines of terminal is plenty for state detection,
    # but a wide terminal can still be large, and several go inline into the
    # Observer batch prompt. Keep the TAIL (most recent output — where the
    # recap / error / spinner lives) if it's oversized.
    MAX_CHARS = 12000
    if len(text) > MAX_CHARS:
        return "…[earlier screen truncated]…\n" + text[-MAX_CHARS:]
    return text


def status_bar_session_id(screen_text: str) -> str | None:
    """The 8-hex session id from the agent's status bar, or None.

    Reads ONLY status-bar-shaped lines (`… │ #<8hex>` at end of line) and
    returns the LAST one — the live bar is rendered at the bottom of the
    screen. A `#<8hex>` appearing in conversation text or a quoted log does
    not match, because it isn't in that line shape."""
    if not screen_text:
        return None
    found = None
    for line in screen_text.splitlines():
        m = _STATUS_BAR_SID_RE.search(line)
        if m:
            found = m.group(1)  # keep walking → last match wins
    return found


def _is_claude_pane(text: str) -> bool:
    if not text:
        return False
    if status_bar_session_id(text):
        return True
    return any(m in text for m in _CLAUDE_PANE_MARKERS)


def _list_panes(ws_ref: str) -> list[str]:
    """All pane refs in a workspace (NOT just the focused one — that's the
    split-pane trap). `list-pane-surfaces` defaults to the focused pane, so we
    must enumerate panes explicitly and read each."""
    r = _cmux(["list-panes", "--workspace", ws_ref])
    if not r or r.returncode != 0:
        return []
    return re.findall(r"\bpane:\d+\b", r.stdout)


def _pane_surfaces(ws_ref: str, pane_ref: str) -> list[tuple[str, str]]:
    """[(surface_ref, surface_uuid), …] for one pane, UUIDs included."""
    r = _cmux(["--id-format", "both", "list-pane-surfaces",
               "--workspace", ws_ref, "--pane", pane_ref])
    if not r or r.returncode != 0:
        return []
    return [(m.group(1), m.group(2).upper())
            for line in r.stdout.splitlines()
            for m in [_SURFACE_LINE_RE.search(line)] if m]


def _read_surface(surface_ref: str, ws_ref: str, lines: int = SCREEN_LINES) -> str:
    """Read one surface. A surface ref needs a window context to resolve, and
    the workspace ref supplies it (`read-screen --surface S --workspace W`).
    Returns '' for a non-terminal surface (browser/markdown) or any error."""
    if not surface_ref:
        return ""
    r = _cmux(["read-screen", "--surface", surface_ref, "--workspace", ws_ref,
               "--scrollback", "--lines", str(lines)])
    return (r.stdout or "").strip() if (r and r.returncode == 0) else ""


def find_agent_pane(ws_ref: str, lines: int = SCREEN_LINES) -> dict | None:
    """Locate the Claude AGENT pane in a (possibly multi-pane) workspace.

    Enumerates every pane → every surface, reads each, and returns the surface
    that is the Claude agent — preferring one whose status bar carries a live
    session id, falling back to a pane that self-identifies as Claude (e.g.
    still booting). A shell / dev-server / browser pane is never returned.

    Returns {surface_ref, surface_uuid, screen_text, sid8} or None when no
    agent pane is found (headless one-shot already exited, cmux down, etc.) —
    None is the safe answer: no agent ⇒ no transcript attached.
    """
    if not ws_ref:
        return None
    panes = _list_panes(ws_ref)
    pairs: list[tuple[str, str]] = []
    if panes:
        for p in panes:
            pairs.extend(_pane_surfaces(ws_ref, p))
    if not pairs:
        # Older cmux / no pane enumeration: fall back to the focused pane's
        # surfaces (still correct for the common single-pane workspace).
        r = _cmux(["--id-format", "both", "list-pane-surfaces",
                   "--workspace", ws_ref])
        if r and r.returncode == 0:
            pairs = [(m.group(1), m.group(2).upper())
                     for line in r.stdout.splitlines()
                     for m in [_SURFACE_LINE_RE.search(line)] if m]

    booting: dict | None = None
    for sref, suuid in pairs:
        text = _read_surface(sref, ws_ref, lines)
        if not text:
            continue
        sid = status_bar_session_id(text)
        if sid:
            return {"surface_ref": sref, "surface_uuid": suuid,
                    "screen_text": _bound(text), "sid8": sid}
        if booting is None and _is_claude_pane(text):
            booting = {"surface_ref": sref, "surface_uuid": suuid,
                       "screen_text": _bound(text), "sid8": None}
    return booting


def _pid_alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError, TypeError):
        return False


def _transcript_internal_sid(path: str) -> str | None:
    """The sessionId recorded INSIDE the transcript (Claude Code writes it on
    every line). Used to confirm a file's identity matches the id we resolved
    from — the last verification gate before we trust a path."""
    try:
        with open(path, "rb") as f:
            head = f.read(65536).decode("utf-8", errors="replace")
    except Exception:
        return None
    for line in head.splitlines():
        if not line.strip():
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        sid = d.get("sessionId") or d.get("session_id")
        if sid:
            return sid
    return None


def transcript_from_session_id(sid8: str | None) -> str | None:
    """Resolve an 8-hex session-id prefix to its transcript jsonl, VERIFIED.

    Globs `*/{sid}*.jsonl` across project dirs, then confirms the file's own
    internal sessionId starts with the prefix — so a coincidental filename
    can't slip through. Returns None unless a file both matches the name and
    self-identifies as that session. Newest verified match wins on the
    (astronomically unlikely) 8-hex prefix collision."""
    if not sid8:
        return None
    projects = HOME / ".claude/projects"
    if not projects.is_dir():
        return None
    candidates = sorted(
        (str(p) for p in projects.glob(f"*/{sid8}*.jsonl")),
        key=os.path.getmtime, reverse=True,
    )
    for path in candidates:
        internal = _transcript_internal_sid(path)
        # Verified iff the file's own sessionId agrees with the prefix. If the
        # file carries no sessionId at all (legacy), accept the filename match
        # — the glob already constrained it to <sid>*.jsonl.
        if internal is None or internal.startswith(sid8):
            return path
    return None


def registry_transcript_for_surface(surface_uuid: str, expect_sid8: str | None = None) -> dict | None:
    """Transcript for a surface via the cmux registry — SAFE variant.

    The registry maps surface UUID → {session_id, claude_pid, transcript_path},
    but the mapping goes stale when a surface is reused. We therefore return an
    entry ONLY when its claude_pid is still ALIVE (a dead pid means the entry
    describes a session that no longer occupies the surface — the ws:12 trap).
    When `expect_sid8` is given (a live status-bar id), the registry must AGREE
    with it; on disagreement we trust the screen and return None here.

    Returns {transcript_path, session_id8} or None.
    """
    if not surface_uuid:
        return None
    try:
        reg = json.load(open(HOME / ".claude/cmux-registry.json"))
    except Exception:
        return None
    want = surface_uuid.upper()
    entries = [e for e in reg.values()
               if (e.get("surface_id") or "").upper() == want]
    if not entries:
        return None
    entry = max(entries, key=lambda e: e.get("ts", 0))
    if not _pid_alive(entry.get("claude_pid")):
        return None  # stale registry row — do NOT trust
    sid = (entry.get("session_id") or "")
    sid8 = sid[:8] if sid else None
    if expect_sid8 and sid8 and sid8 != expect_sid8:
        return None  # registry disagrees with the live screen → screen wins
    tp = entry.get("transcript_path")
    if not tp or not os.path.exists(tp):
        return None
    # Final identity gate: the file must self-identify as this session.
    internal = _transcript_internal_sid(tp)
    if internal and sid8 and not internal.startswith(sid8):
        return None
    return {"transcript_path": tp, "session_id8": sid8}


def resolve_workspace_screen_and_transcript(ws_ref: str) -> dict:
    """Single entry point: find the agent pane, read its screen, and resolve
    its transcript ONLY via verified signals. Returns the dict of screen/
    transcript fields for the payload. transcript_path is None unless verified.

    Resolution priority (first verified wins; otherwise None):
      1. screen_session_id — id from the agent's own status bar, glob+verified.
         Strongest: it's what the running agent says about itself, right now.
      2. registry_live_pid — registry row for the agent pane's surface UUID,
         but only with a LIVE pid and agreement with the screen. Covers a
         booting agent whose status bar hasn't rendered yet.
      3. None — no verified signal. The Observer falls back to screen_text.
         (We deliberately do NOT guess by mtime/cwd/title — that was the bug.)
    """
    pane = find_agent_pane(ws_ref)
    screen = pane["screen_text"] if pane else ""
    sid8 = pane["sid8"] if pane else None
    surface_uuid = pane["surface_uuid"] if pane else None

    transcript = None
    source = None

    if sid8:
        transcript = transcript_from_session_id(sid8)
        if transcript:
            source = "screen_session_id"

    if transcript is None and surface_uuid:
        reg = registry_transcript_for_surface(surface_uuid, expect_sid8=sid8)
        if reg:
            transcript = reg["transcript_path"]
            sid8 = sid8 or reg["session_id8"]
            source = "registry_live_pid"

    return {
        "screen_text": screen,
        "screen_shows_error": screen_shows_error(screen),
        "session_id8": sid8,
        "transcript_path": transcript,
        "transcript_source": source,
        "agent_surface": pane["surface_ref"] if pane else None,
    }


def screen_shows_error(screen_text: str) -> bool:
    """True when a LINE of the screen is an assistant turn that ended on an
    error banner (`⏺ API Error: …`). Deliberately does NOT match the words
    appearing inside prose or edited code — only the rendered halt — so a
    session discussing errors isn't flagged as stranded on one."""
    if not screen_text:
        return False
    return any(_ERROR_LINE_RE.match(line) for line in screen_text.splitlines())


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

    # Find the agent pane, read its screen, and resolve its transcript using
    # ONLY verified signals (status-bar session id, or a live-pid registry row
    # that agrees with the screen). transcript_path is None when nothing
    # verifies — by design: a wrong transcript is worse than none.
    resolved = resolve_workspace_screen_and_transcript(args.ws_ref)
    transcript = resolved["transcript_path"]
    age, agent_status = transcript_signals(transcript)
    dirty, unpushed = cwd_state(args.cwd)

    payload = {
        "ws_ref": args.ws_ref,
        "title": args.title,
        "cwd": args.cwd,
        "transcript_path": transcript,
        "transcript_source": resolved["transcript_source"],
        "session_id8": resolved["session_id8"],
        "agent_surface": resolved["agent_surface"],
        "last_turn_age_sec": age,
        "agent_status": agent_status,
        "cwd_dirty": dirty,
        "cwd_unpushed": unpushed,
        "is_protected": args.ws_ref in PROTECTED_REFS,
        "screen_text": resolved["screen_text"],
        "screen_shows_error": resolved["screen_shows_error"],
    }
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
