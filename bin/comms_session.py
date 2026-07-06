"""comms_session — warm cmux Claude session manager for assistant-comms.

A persistent cmux workspace running `claude`, kept warm so inbound Slack
messages get seconds-fast replies instead of a ~46s cold `claude --print`. The
daemon (comms-listen.py) feeds each message to this session and reads the reply
back from the session transcript.

Context management: after each reply, measure context % from the transcript
usage block (comms_lib.read_context_tokens). At >=50% of the 1M window, send
`/clear`. Because all durable memory lives in conversation.jsonl, a /clear loses
nothing — the session reconstructs from disk on the next message.

This module splits cleanly:
  - PURE logic (registry r/w, transcript reply-extraction, should_clear,
    newest-transcript resolution) — unit-tested, no cmux.
  - cmux I/O (spawn, feed, clear) — thin wrappers over the same RPC pattern
    pulse.py uses to drive Assistant. Validated live, not mocked.

Transport-agnostic: this file knows nothing about Slack vs any other transport.
The daemon composes the per-message feed string; this only manages the session.
"""
from __future__ import annotations

import json
import os
import shlex
import time
from pathlib import Path
from typing import Any

import comms_lib

HOME = Path(os.environ["HOME"])
CLEAR_THRESHOLD = float(os.environ.get("COMMS_CLEAR_FRACTION", "0.5"))
SESSION_TITLE = "assistant-comms (warm)"
DISPATCH_CWD = HOME / "dev" / "assistant"

# Comms is a narrow conversational role — Sonnet, not the Opus the ~/.zprofile
# `claude` alias bakes in. We bypass the alias by invoking the binary at its
# full path with explicit flags; an alias only expands for the bare word
# `claude`. Bedrock prefix matches pulse.py.
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", str(HOME / ".local/bin/claude"))
WARM_MODEL = os.environ.get("COMMS_MODEL", "us.anthropic.claude-sonnet-4-6[1m]")


# --------------------------------------------------------------------------- registry (pure)

def session_registry_path(paths: comms_lib.Paths) -> Path:
    return paths.comms_dir / "session.json"


def read_session(paths: comms_lib.Paths) -> dict[str, Any] | None:
    p = session_registry_path(paths)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError:
        return None


def write_session(paths: comms_lib.Paths, ws_ref: str, surface_ref: str,
                  cwd: str, transcript_path: str | None, clock=None) -> None:
    paths.comms_dir.mkdir(parents=True, exist_ok=True)
    rec = {
        "ws_ref": ws_ref,
        "surface_ref": surface_ref,
        "cwd": cwd,
        "transcript_path": transcript_path,
        "spawned_ts": (clock() if clock else int(time.time())),
    }
    p = session_registry_path(paths)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(rec, indent=2))
    os.replace(tmp, p)


def clear_session_registry(paths: comms_lib.Paths) -> None:
    p = session_registry_path(paths)
    if p.exists():
        p.unlink()


# --------------------------------------------------------------------------- transcript (pure)

def project_dir_for_cwd(cwd: str) -> Path:
    """Claude Code stores a session's JSONL under ~/.claude/projects/<slug>
    where slug = the realpath with '/' → '-'."""
    cwd_real = os.path.realpath(cwd)
    return HOME / ".claude/projects" / cwd_real.replace("/", "-")


def newest_transcript(cwd: str) -> str | None:
    pdir = project_dir_for_cwd(cwd)
    if not pdir.is_dir():
        return None
    jsonls = sorted(pdir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    return str(jsonls[0]) if jsonls else None


def last_assistant_text(transcript_path: str | Path) -> str | None:
    """Extract the most recent assistant turn's text content from a transcript.
    Returns None if there is no assistant turn yet."""
    p = Path(transcript_path)
    if not p.exists():
        return None
    last_text: str | None = None
    with open(p) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("type") != "assistant":
                continue
            msg = rec.get("message")
            if not isinstance(msg, dict):
                continue
            content = msg.get("content")
            if isinstance(content, str):
                last_text = content
            elif isinstance(content, list):
                parts = [
                    c.get("text", "") for c in content
                    if isinstance(c, dict) and c.get("type") == "text"
                ]
                joined = "".join(parts).strip()
                if joined:
                    last_text = joined
    return last_text


def transcript_line_count(transcript_path: str | Path) -> int:
    """Count non-blank lines — a cheap 'has the transcript grown?' signal used
    to detect that the session produced a new turn after we fed it."""
    p = Path(transcript_path)
    if not p.exists():
        return 0
    n = 0
    with open(p) as f:
        for line in f:
            if line.strip():
                n += 1
    return n


def should_clear(transcript_path: str | Path,
                 threshold: float = CLEAR_THRESHOLD) -> bool:
    """True when context usage has reached the /clear threshold (default 50%)."""
    tokens = comms_lib.read_context_tokens(transcript_path)
    return comms_lib.context_fraction(tokens) >= threshold


# --------------------------------------------------------------------------- cmux I/O (live)
#
# Same RPC pattern pulse.py uses. Kept thin; validated live, not mocked.

def _cmux_rpc(paths: comms_lib.Paths, method: str, params: dict, timeout: int = 15) -> dict | None:  # pragma: no cover - live cmux I/O
    rc, out, _ = comms_lib.run_cmd(
        [str(paths.cmux_bin), "rpc", method, json.dumps(params)], timeout=timeout)
    if rc != 0:
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return None


def _surface_read_text(paths: comms_lib.Paths, surface_ref: str, lines: int = 200) -> str:  # pragma: no cover - live cmux I/O
    d = _cmux_rpc(paths, "surface.read_text", {"surface_id": surface_ref, "lines": lines})
    if not d or d.get("surface_ref") != surface_ref:
        return ""
    return d.get("text", "") or ""


def cmux_alive(paths: comms_lib.Paths, ws_ref: str) -> bool:  # pragma: no cover - live cmux I/O
    rc, _, _ = comms_lib.run_cmd(
        [str(paths.cmux_bin), "tree", "--workspace", ws_ref, "--json"], timeout=10)
    return rc == 0


def flag_orphan_workspace(paths: comms_lib.Paths, ws_ref: str, log=lambda m: None) -> None:  # pragma: no cover - live cmux I/O
    """Surface a stale warm workspace for the OPERATOR to close — never close it
    from code.

    Production code must never shell out `cmux close-workspace`: the 2026-05-26
    work-loss incident (workspaces auto-closed mid-work) made that an absolute
    rule, pinned by tests/test_no_close_workspace.py. So when the daemon replaces
    a dead warm session, it does NOT close the old cmux workspace — it logs the
    orphan ref so the operator can close it by hand (the same discipline as
    /cleanup, which also leaves the workspace for the user).

    Tradeoff: a respawn leaves the prior warm workspace open (a defunct Claude
    pane). That's a visible, recoverable leak the operator clears manually —
    strictly preferable to any code path that can close a workspace."""
    rc, out, _ = comms_lib.run_cmd([str(paths.cmux_bin), "list-workspaces"], timeout=10)
    if rc != 0:
        return
    is_warm = any(ws_ref in line and SESSION_TITLE in line for line in out.splitlines())
    if is_warm:
        log(f"orphan warm workspace {ws_ref} left open — close it manually "
            f"(cmux, the '{SESSION_TITLE}' pane); code never auto-closes workspaces")


def feed(paths: comms_lib.Paths, surface_ref: str, text: str) -> None:  # pragma: no cover - live cmux I/O
    """Type text into the warm session and submit. Strip trailing newline first
    (send_text streams keystrokes; a trailing \\n auto-submits mid-paste), then
    an explicit Enter — exactly pulse.py's delivery sequence."""
    _cmux_rpc(paths, "surface.send_text", {"surface_id": surface_ref, "text": text.rstrip("\n")})
    time.sleep(0.5)
    _cmux_rpc(paths, "surface.send_key", {"surface_id": surface_ref, "key": "enter"})


def clear_session(paths: comms_lib.Paths, surface_ref: str, boot_prompt: Path) -> None:  # pragma: no cover - live cmux I/O
    """Clear-AND-resume: reset the context window, then immediately re-deliver
    the boot prompt so the fresh session reloads its identity + tools.

    A bare /clear would wipe the warm session's briefing (delivered as a
    conversation turn at spawn), leaving a generic un-briefed Claude that no
    longer knows it's the assistant. We always resume with the SAME prompt —
    per-message thread continuity comes from conversation.jsonl, which the boot
    prompt tells it to reconstruct. So this is lossless.

    Send /clear as text, then an explicit Enter keystroke. A trailing newline
    inside send_text does NOT reliably submit a slash command. Then POLL for the
    post-clear "Welcome back" screen before re-feeding — feeding during the ~2s
    reset window gets keystrokes swallowed."""
    _cmux_rpc(paths, "surface.send_text", {"surface_id": surface_ref, "text": "/clear"})
    time.sleep(0.5)
    _cmux_rpc(paths, "surface.send_key", {"surface_id": surface_ref, "key": "enter"})

    for _ in range(15):
        time.sleep(1)
        screen = _surface_read_text(paths, surface_ref)
        if "Welcome back" in screen or "Tips for getting started" in screen:
            break

    time.sleep(1)
    instruction = f"Read {boot_prompt} in full and execute every instruction in it."
    feed(paths, surface_ref, instruction)


def list_warm_workspaces(paths: comms_lib.Paths) -> list[str]:  # pragma: no cover - live cmux I/O
    """All workspace refs whose title is the warm-session title."""
    rc, out, _ = comms_lib.run_cmd([str(paths.cmux_bin), "list-workspaces"], timeout=10)
    if rc != 0:
        return []
    import re
    refs = []
    for line in out.splitlines():
        if SESSION_TITLE in line:
            m = re.search(r"workspace:\d+", line)
            if m:
                refs.append(m.group(0))
    return refs


def reconcile_warm_workspaces(paths: comms_lib.Paths, keep: str | None, log=lambda m: None) -> None:  # pragma: no cover - live cmux I/O
    """Flag every warm-titled workspace except `keep` for manual close. The
    daemon is a singleton, so at most one warm session should exist; any others
    are orphans from a prior daemon that died. Code never closes them (see
    flag_orphan_workspace) — it logs each so the operator clears them by hand."""
    for ws in list_warm_workspaces(paths):
        if ws == keep:
            continue
        flag_orphan_workspace(paths, ws, log=log)


def spawn_session(paths: comms_lib.Paths, boot_prompt: Path, log=lambda m: None) -> dict | None:  # pragma: no cover - live cmux I/O
    """Spawn a fresh warm cmux Claude session and deliver the responder boot
    prompt. Returns the session record on success, None on failure. Mirrors
    pulse.py's proven dispatch sequence."""
    cmux = str(paths.cmux_bin)
    rc, _, _ = comms_lib.run_cmd([cmux, "ping"], timeout=10)
    if rc != 0:
        log("cmux not running — cannot spawn warm session")
        return None

    cwd = str(DISPATCH_CWD)
    # Explicit binary + flags (NOT the bare `claude` alias, which is Opus). The
    # full path means the login shell's alias doesn't apply. Quote the model
    # slug — the [1m] brackets are shell glob chars.
    # Scope to its OWN surface: ~/dev/assistant (its code + boot prompt — so it
    # can evolve its own behavior) + ~/.assistant (runtime state it writes:
    # conversation.jsonl, session.json) + ~/.architect (reads Assistant's
    # proposals/ledger) + /tmp. Deliberately NOT ~/.claude (global CLAUDE.md
    # rules + settings.json — a session must not widen its own rules/permissions)
    # and NOT all of ~/dev. The lesson-writing path still works via a subprocess
    # (assistant-curator.py) gated by an explicit human `y`.
    launch = (
        f"{shlex.quote(CLAUDE_BIN)} --model {shlex.quote(WARM_MODEL)} "
        f"--dangerously-skip-permissions "
        f"--add-dir ~/dev/assistant --add-dir ~/.assistant "
        f"--add-dir ~/.architect --add-dir /tmp"
    )
    rc, out, err = comms_lib.run_cmd(
        [cmux, "new-workspace", "--cwd", cwd, "--name", SESSION_TITLE,
         "--focus", "false", "--command", launch], timeout=30)
    if rc != 0:
        log(f"new-workspace failed rc={rc}: {err.strip()[:200]}")
        return None
    import re
    m = re.search(r"workspace:\d+", out)
    if not m:
        log(f"no workspace ref in: {out.strip()[:200]}")
        return None
    ws_ref = m.group(0)

    rc, out, _ = comms_lib.run_cmd([cmux, "list-pane-surfaces", "--workspace", ws_ref], timeout=15)
    sm = re.search(r"surface:\d+", out)
    if not sm:
        log(f"no surface for {ws_ref}")
        return None
    surface_ref = sm.group(0)

    project_dir = project_dir_for_cwd(cwd)
    project_dir.mkdir(parents=True, exist_ok=True)
    before = {p.name for p in project_dir.glob("*.jsonl")}

    # Trust prompt (first launch in a never-used cwd).
    time.sleep(2)
    if "1. Yes, I trust this folder" in _surface_read_text(paths, surface_ref):
        _cmux_rpc(paths, "surface.send_text", {"surface_id": surface_ref, "text": "1"})
        _cmux_rpc(paths, "surface.send_key", {"surface_id": surface_ref, "key": "enter"})

    # Readiness: banner OR bottom status bar (mode-independent).
    ready = False
    for _ in range(30):
        screen = _surface_read_text(paths, surface_ref)
        if "Claude Code v" in screen or "bypass permissions on" in screen:
            ready = True
            break
        time.sleep(1)
    if not ready:
        log(f"claude never ready in {ws_ref}/{surface_ref}")
        return None

    # Deliver the responder boot prompt by reference.
    instruction = f"Read {boot_prompt} in full and execute every instruction in it."
    feed(paths, surface_ref, instruction)

    # Confirm submission via a new transcript carrying the prompt path.
    sig = str(boot_prompt)[:60]
    transcript = None
    for _ in range(30):
        for name in {p.name for p in project_dir.glob("*.jsonl")} - before:
            try:
                if sig in (project_dir / name).read_text():
                    transcript = str(project_dir / name)
                    break
            except OSError:
                continue
        if transcript:
            break
        time.sleep(1)

    if not transcript:
        transcript = newest_transcript(cwd)
        log(f"warm session {ws_ref} spawned but boot submission unconfirmed")

    write_session(paths, ws_ref, surface_ref, cwd, transcript)
    log(f"warm session ready: {ws_ref} / {surface_ref} (transcript={transcript})")
    reconcile_warm_workspaces(paths, keep=ws_ref, log=log)
    return read_session(paths)
