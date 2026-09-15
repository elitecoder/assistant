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
import re
import shlex
import sys
import time
from pathlib import Path
from typing import Any

import agent_session
import comms_lib

HOME = Path(os.environ["HOME"])
CLEAR_THRESHOLD = float(os.environ.get("COMMS_CLEAR_FRACTION", "0.5"))
# Droid transcripts carry NO usage/token block (verified live 2026-07-25 — only
# session_start/message/todo_state records), so the Claude usage-fraction path
# would peg should_clear() at False forever. Instead we proxy context growth by
# on-disk transcript size. The default approximates ~50% of the 1M-token window:
# a JSONL turn is roughly 4 bytes per context token once JSON framing + repeated
# content are counted, so ~500k tokens ≈ 2 MB. Env-overridable per box.
DROID_CLEAR_BYTES = int(os.environ.get("COMMS_DROID_CLEAR_BYTES", str(2_000_000)))
SESSION_TITLE = "assistant-comms (warm)"
# The warm session's cwd = this repo checkout (its own code + boot prompt), NOT
# a hardcoded ~/dev/assistant — derive it from this file (bin/comms_session.py →
# repo root is parent-of-bin) so a checkout elsewhere still works.
REPO_ROOT = Path(__file__).resolve().parent.parent
DISPATCH_CWD = REPO_ROOT

# Semantic model tiers (Keel M8): resolve a TIER to the id the LIVE backend
# expects instead of hardcoding one provider's id shape. See model_tiers.py.
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
from assistant import model_tiers  # noqa: E402

# Comms is a narrow conversational role — Sonnet, not the Opus the ~/.zprofile
# `claude` alias bakes in. We bypass the alias by invoking the binary at its
# full path with explicit flags; an alias only expands for the bare word
# `claude`, so this session must declare its OWN backend rather than assume
# whatever the ambient shell happens to carry (2026-09-05: comms_session was
# the one spawn site in this repo still hardcoding a Bedrock-shaped id,
# instead of going through model_tiers like pulse.py/strategist.py/
# lesson-extractor.py/narrate-brief.py already do — it broke the moment the
# operator's alias stopped defaulting to Bedrock).
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", str(HOME / ".local/bin/claude"))


def _resolve_warm_model_and_backend() -> tuple[str, str, bool]:
    """(model_id, backend, pinned) for the warm session's --model + explicit
    CLAUDE_CODE_USE_BEDROCK prefix — resolved FRESH on every call, not frozen
    at import. `comms-listen.py` is a long-lived daemon; freezing these as
    module constants meant a mid-run `claude-backend bedrock|sub` toggle was
    invisible until the daemon itself restarted (2026-09-05 review finding).

    `pinned=True` means COMMS_MODEL is an explicit operator override. In that
    case the caller must NOT also declare CLAUDE_CODE_USE_BEDROCK: an operator
    pinning a specific id already knows which backend it targets, and
    auto-declaring a flag from ambient detection could contradict that pin
    (e.g. COMMS_MODEL set to a Bedrock id on a box whose zprofile currently
    reads non-Bedrock) — worse than the pre-fix ambient-inheritance behavior
    the override previously fell back to (2026-09-05 review finding)."""
    override = os.environ.get("COMMS_MODEL")
    if override:
        return override, model_tiers.provider(), True
    backend = model_tiers.provider()
    return model_tiers.model_for("balanced", long_context=True), backend, False

def _positive_int_env(name: str, default: int) -> int:
    """Env override parsed as a positive int, falling back to ``default`` on a
    missing/malformed/non-positive value. A bad tunable must never crash the
    daemon at import — it degrades to the safe default instead."""
    try:
        value = int(os.environ[name])
    except (KeyError, ValueError):
        return default
    return value if value > 0 else default


# Boot-screen readiness budget: how many poll iterations (~1s each) to wait for
# the ready marker before giving up on a spawn. A cold Claude-on-Bedrock launch
# (plus the login-shell nvm preamble) can render its banner well past the old
# 30-poll window, so the budget is generous and env-overridable. Each poll also
# answers the first-launch trust prompt if it is showing (see await_ready), so a
# slow boot no longer stalls. This is a ceiling: a healthy boot breaks out as
# soon as the marker appears.
READY_ATTEMPTS = _positive_int_env("COMMS_READY_ATTEMPTS", 90)


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
                  cwd: str, transcript_path: str | None,
                  agent: str | None = None, clock=None) -> None:
    """Persist the warm-session registry. `agent` records which provider owns
    the session so post-restart reads pick the right transcript root + schema.
    agent=None means "preserve the persisted choice" — used by the transcript-
    refresh call path, which must NOT silently reset a droid session to claude;
    it reuses the prior record's agent, falling back to the coexistence default."""
    paths.comms_dir.mkdir(parents=True, exist_ok=True)
    if agent is None:
        agent = (read_session(paths) or {}).get("agent") or agent_session.CLAUDE
    rec = {
        "ws_ref": ws_ref,
        "surface_ref": surface_ref,
        "cwd": cwd,
        "transcript_path": transcript_path,
        "agent": agent,
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

def project_dir_for_cwd(cwd: str, agent: str = agent_session.CLAUDE) -> Path:
    """Per-cwd transcript dir a warm `agent` session writes into. Claude:
    ~/.claude/projects/<slug>; Droid: ~/.factory/sessions/<slug>. slug = the
    realpath with '/' → '-'. Delegates to agent_session.confirm_dir (the single
    source of truth for both roots), rooted at this module's HOME so a tmp-home
    test resolves against its own tree."""
    return agent_session.confirm_dir(agent, cwd, home=HOME)


def newest_transcript(cwd: str, agent: str = agent_session.CLAUDE) -> str | None:
    pdir = project_dir_for_cwd(cwd, agent)
    if not pdir.is_dir():
        return None
    jsonls = sorted(pdir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    return str(jsonls[0]) if jsonls else None


def last_assistant_text(transcript_path: str | Path) -> str | None:
    """Extract the most recent assistant turn's text content from a transcript.
    Returns None if there is no assistant turn yet. Schema-agnostic: the role is
    resolved via agent_session.record_role, which normalizes both the Claude
    (type=="assistant") and Droid (type=="message" + message.role) schemas; the
    content-block shape is identical across agents."""
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
            if agent_session.record_role(rec) != "assistant":
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
                 threshold: float = CLEAR_THRESHOLD,
                 agent: str = agent_session.CLAUDE,
                 droid_clear_bytes: int = DROID_CLEAR_BYTES) -> bool:
    """True when the warm session's context has grown enough to warrant a clear.

    claude: use the per-turn usage block Claude Code records — live context
    fraction >= threshold (default 50% of the 1M window).

    droid: Droid transcripts have NO usage block, so read_context_tokens returns
    None and the fraction path would be perpetually False. Proxy with on-disk
    transcript size instead: >= droid_clear_bytes (COMMS_DROID_CLEAR_BYTES,
    default ~2 MB ≈ 50% window). Deterministic and unit-testable; the "clear" it
    triggers is a lossless respawn (durable memory lives in conversation.jsonl)."""
    if agent == agent_session.DROID:
        p = Path(transcript_path)
        return p.exists() and p.stat().st_size >= droid_clear_bytes
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


def close_own_workspace(paths: comms_lib.Paths, ws_ref: str, log=lambda m: None) -> None:  # pragma: no cover - live cmux I/O
    """Close a warm workspace THIS daemon spawned (tracked in session.json).

    Scope of the 2026-05-26 close-workspace ban: automation must never close a
    workspace that could hold USER WORK (ws:97/ws:99 were live agents killed
    mid-task). This call site is the deliberate, narrow exception — it closes
    ONLY comms's own throwaway warm session, and ONLY after verifying the target
    is titled SESSION_TITLE ("assistant-comms (warm)"). User work never carries
    that title, so this can never touch it. Without this, every respawn (daemon
    restart, dead session) leaks a live Claude process — 6 piled up during
    2026-06-05 testing.

    test_no_close_workspace.py allowlists exactly this one guarded invocation and
    still bans every other close-workspace call in production code."""
    rc, out, _ = comms_lib.run_cmd([str(paths.cmux_bin), "list-workspaces"], timeout=10)
    if rc != 0:
        return
    # Title guard: only ever close our own warm session, never an arbitrary ref.
    is_warm = any(ws_ref in line and SESSION_TITLE in line for line in out.splitlines())
    if not is_warm:
        log(f"skip close {ws_ref}: not a '{SESSION_TITLE}' workspace (ref reissued?)")
        return
    rc, _, err = comms_lib.run_cmd(
        [str(paths.cmux_bin), "close-workspace", "--workspace", ws_ref], timeout=15)
    log(f"closed prior warm workspace {ws_ref}" if rc == 0
        else f"close {ws_ref} rc={rc}: {err.strip()[:120]}")


def feed(paths: comms_lib.Paths, surface_ref: str, text: str) -> None:  # pragma: no cover - live cmux I/O
    """Type text into the warm session and submit. Strip trailing newline first
    (send_text streams keystrokes; a trailing \\n auto-submits mid-paste), then
    an explicit Enter — exactly pulse.py's delivery sequence."""
    _cmux_rpc(paths, "surface.send_text", {"surface_id": surface_ref, "text": text.rstrip("\n")})
    time.sleep(0.5)
    _cmux_rpc(paths, "surface.send_key", {"surface_id": surface_ref, "key": "enter"})


def clear_session(paths: comms_lib.Paths, sess: dict, boot_prompt: Path,
                  agent: str = agent_session.CLAUDE,
                  log=lambda m: None) -> dict:  # pragma: no cover - live cmux I/O
    """Clear-AND-resume: reset the context window losslessly, then return the
    refreshed session record. Per-message thread continuity comes from
    conversation.jsonl (the boot prompt tells the session to reconstruct it), so
    a reset loses nothing.

    claude — in-place /clear + resume: send /clear (as text, then an explicit
    Enter keystroke; a trailing newline inside send_text does NOT reliably submit
    a slash command), POLL for the post-clear "Welcome back" screen (feeding
    during the ~2s reset window gets keystrokes swallowed), re-deliver the boot
    prompt, then update the registry with the new transcript. The workspace and
    surface are unchanged.

    droid — respawn: Droid has no /clear slash command with the same semantics,
    so the lossless equivalent is to close this warm workspace and spawn a fresh
    one (a brand-new ws/surface/transcript). close_own_workspace is title-guarded
    to the warm-session title, so it never touches user work."""
    if agent == agent_session.DROID:
        close_own_workspace(paths, sess["ws_ref"], log=log)
        clear_session_registry(paths)
        return spawn_session(paths, boot_prompt, log=log, agent=agent) or sess

    surface_ref = sess["surface_ref"]
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

    new_t = newest_transcript(sess["cwd"], agent)
    if new_t:
        write_session(paths, sess["ws_ref"], surface_ref, sess["cwd"], new_t,
                      agent=agent)
    return read_session(paths) or sess


def spawned_ledger_path(paths: comms_lib.Paths) -> Path:
    """Per-INSTANCE record of every warm workspace THIS comms instance spawned.
    Lives under comms_dir (derived from COMMS_HOME), so two instances with
    distinct COMMS_HOMEs never see each other's refs — the reconcile scope is the
    instance, not the machine."""
    return paths.comms_dir / "spawned-workspaces.json"


def read_spawned_refs(paths: comms_lib.Paths) -> list[str]:
    """The warm workspace refs this instance has spawned (may include dead ones —
    reconcile prunes them). Empty on missing / malformed ledger."""
    p = spawned_ledger_path(paths)
    try:
        refs = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    return [r for r in refs if isinstance(r, str)] if isinstance(refs, list) else []


def record_spawned_ref(paths: comms_lib.Paths, ws_ref: str) -> None:
    """Append `ws_ref` to this instance's spawned-workspaces ledger (idempotent,
    order-preserving). Written atomically so a crash mid-write can't corrupt it."""
    refs = read_spawned_refs(paths)
    if ws_ref in refs:
        return
    refs.append(ws_ref)
    paths.comms_dir.mkdir(parents=True, exist_ok=True)
    p = spawned_ledger_path(paths)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(refs))
    os.replace(tmp, p)


def refs_to_reconcile(spawned: list[str], keep: str | None) -> list[str]:
    """Pure policy: which of THIS instance's spawned refs to close on reconcile —
    every spawned ref except `keep`, de-duplicated in first-seen order. Scoping to
    the instance's OWN ledger (not a machine-wide title scan) is the fix for a
    second comms instance / a live validation run closing the production session."""
    seen: set[str] = set()
    out: list[str] = []
    for ws in spawned:
        if ws == keep or ws in seen:
            continue
        seen.add(ws)
        out.append(ws)
    return out


def parse_ws_ref_from_output(out: str, err: str) -> str | None:
    """Extract the first ``workspace:\\d+`` ref from combined stdout+stderr.
    Used after a failed/timed-out new-workspace call: cmux may have assigned a
    ref and printed it before the timeout fired, leaving an untracked orphan."""
    m = re.search(r"workspace:\d+", out + err)
    return m.group(0) if m else None


def untracked_warm_refs(warm_refs: list[str], spawned_refs: list[str],
                        keep: str | None) -> list[str]:
    """Warm-titled workspaces (from a machine-wide title scan) that are NOT in
    this instance's spawned ledger and are NOT the kept survivor.  These are
    orphans that slipped through record_spawned_ref — e.g. a timed-out
    new-workspace that created a workspace before the daemon got rc=1."""
    known = set(spawned_refs)
    return [ws for ws in warm_refs if ws != keep and ws not in known]


def list_warm_workspaces(paths: comms_lib.Paths) -> list[str]:  # pragma: no cover - live cmux I/O
    """All workspace refs whose title is the warm-session title, machine-wide."""
    rc, out, _ = comms_lib.run_cmd([str(paths.cmux_bin), "list-workspaces"], timeout=10)
    if rc != 0:
        return []
    refs = []
    for line in out.splitlines():
        if SESSION_TITLE in line:
            m = re.search(r"workspace:\d+", line)
            if m:
                refs.append(m.group(0))
    return refs


def reconcile_warm_workspaces(paths: comms_lib.Paths, keep: str | None, log=lambda m: None) -> None:  # pragma: no cover - live cmux I/O
    """Close every warm workspace THIS instance spawned except `keep`, plus any
    untracked orphan found by a machine-wide title scan.

    Two-pass cleanup:
    1. Ledger pass — close every ref in spawned-workspaces.json that isn't keep.
       Instance-scoped: a second comms instance (distinct COMMS_HOME) cannot
       accidentally close the production session this way.
    2. Title-scan pass — close any SESSION_TITLE workspace NOT in the spawned
       ledger and NOT keep.  Catches orphans from timed-out spawns that created a
       workspace before new-workspace returned rc=1 and record_spawned_ref was
       never reached.  close_own_workspace is title-guarded as defence-in-depth.

    `keep` is rewritten as the sole entry in the spawned ledger afterward."""
    spawned = read_spawned_refs(paths)
    for ws in refs_to_reconcile(spawned, keep):
        close_own_workspace(paths, ws, log=log)
    for ws in untracked_warm_refs(list_warm_workspaces(paths), spawned, keep):
        log(f"closing untracked orphan {ws} (not in spawned ledger)")
        close_own_workspace(paths, ws, log=log)
    # Rewrite the ledger to just the kept ref (the survivor); closed refs are gone.
    paths.comms_dir.mkdir(parents=True, exist_ok=True)
    p = spawned_ledger_path(paths)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps([keep] if keep else []))
    os.replace(tmp, p)


def _warm_launch(agent: str) -> str:
    """The cmux `--command` string for a warm `agent` session.

    claude: the explicit binary + flags (NOT the bare `claude` alias, which is
    Opus). The full path means the login shell's alias doesn't apply — so this
    command must declare its OWN backend rather than assume one. Unless the
    model is an explicit COMMS_MODEL pin (see _resolve_warm_model_and_backend),
    it prefixes CLAUDE_CODE_USE_BEDROCK=<0|1> from the SAME model_tiers
    resolution that picked the model id, so the two can never disagree.

    Deliberately NOT prefixed: AWS_REGION / AWS_BEARER_TOKEN_BEDROCK. Those are
    secrets — baking them into a `--command` string would put them in the
    cmux process's argv, visible to any other local user via `ps`, which the
    global credential-handling rule forbids regardless of how convenient it
    would be. They still reach the child correctly: the pane `--command` types
    into is itself a fresh login shell (cmux spawns one per workspace) that
    already sourced ~/.zprofile — the SAME mechanism that makes the AWS vars
    (and the `claude` alias, and every --add-dir root) available to a spawn
    that invokes the alias directly. Only the ONE non-secret routing flag,
    CLAUDE_CODE_USE_BEDROCK, is worth declaring explicitly here, because it is
    the one value this command's own id-shape choice can silently disagree
    with if left to ambient inheritance (2026-09-05 review finding) — the AWS
    vars have no such shape-matching hazard, only a delivery mechanism, and
    that mechanism already works.

    Quote the model slug — the [1m] brackets are shell glob chars. Scope to
    its OWN surface: ~/dev/assistant (its code + boot prompt — so it can
    evolve its own behavior) + ~/.assistant (runtime state: conversation.jsonl,
    session.json) + ~/.architect (reads Assistant's proposals/ledger) + /tmp.
    Deliberately NOT ~/.claude (global CLAUDE.md + settings.json — a session
    must not widen its own rules/permissions) and NOT all of ~/dev.
    Lesson-writing still works via a subprocess (assistant-curator.py) gated
    by an explicit human `y`.

    droid: the single-source launch_command(DROID) — settings + --auto high +
    optional --append-system-prompt-file. Droid scopes via --cwd + its settings,
    so we invent NO --add-dir here; the caller passes --cwd."""
    if agent == agent_session.DROID:
        return agent_session.launch_command(agent, home=HOME)
    model, backend, pinned = _resolve_warm_model_and_backend()
    prefix = "" if pinned else f"CLAUDE_CODE_USE_BEDROCK={'1' if backend == 'bedrock' else '0'} "
    return (
        f"{prefix}"
        f"{shlex.quote(CLAUDE_BIN)} --model {shlex.quote(model)} "
        f"--dangerously-skip-permissions "
        f"--add-dir {shlex.quote(str(REPO_ROOT))} --add-dir {shlex.quote(str(HOME / '.assistant'))} "
        f"--add-dir {shlex.quote(str(HOME / '.architect'))} --add-dir /tmp"
    )


def await_ready(read_screen, ready_re, trust_marker, answer_trust,
                attempts, sleep=time.sleep):
    """Poll the boot screen until the ready marker appears, auto-answering the
    first-launch trust prompt the moment it is seen.

    Pure control flow (all I/O is injected) so the readiness/trust ordering is
    unit-testable without a live cmux. Returns ``(ready, trust_answered)``.

    The trust-answer is folded INTO the poll rather than fired once before it:
    a slow cold boot can render the prompt after any fixed pre-loop wait, and a
    missed answer stalls the session forever — the 2026-08-21 comms outage.
    ``answer_trust`` fires at most once (Claude re-renders the same prompt on
    every frame until it is answered; sending "1"+Enter repeatedly would leak
    keystrokes into the REPL once accepted). ``trust_marker`` is None for agents
    with no known auto-answerable gate (droid), so the branch never misfires.
    Readiness is checked before trust each iteration so an already-ready screen
    short-circuits without touching the surface."""
    trust_answered = False
    for _ in range(attempts):
        screen = read_screen()
        if ready_re.search(screen):
            return True, trust_answered
        if trust_marker and not trust_answered and trust_marker in screen:
            answer_trust()
            trust_answered = True
        sleep(1)
    return False, trust_answered


def _abandon_failed_spawn(paths: comms_lib.Paths, log=lambda m: None) -> None:  # pragma: no cover - live cmux I/O
    """Close the workspace a failed spawn just created, plus any warm orphans.

    Every spawn_session failure path AFTER new-workspace succeeds used to return
    None without closing the workspace it made. When claude never reached the
    ready marker (a bad boot, a slow machine), the watchdog respawned every tick
    and each attempt leaked one live workspace — ~200 piled up on 2026-09-14 and
    killed cmux. Reconciling with keep=None here closes this attempt's workspace
    (recorded in the spawned ledger) AND title-scans for orphans from earlier
    failed ticks, so a persistent boot failure holds at most one transient
    workspace instead of leaking one per minute."""
    try:
        reconcile_warm_workspaces(paths, keep=None, log=log)
    except Exception as e:  # noqa: BLE001 — cleanup is best-effort; never mask the spawn failure
        log(f"cleanup after failed spawn errored: {e}")


def spawn_session(paths: comms_lib.Paths, boot_prompt: Path, log=lambda m: None,
                  agent: str | None = None) -> dict | None:  # pragma: no cover - live cmux I/O
    """Spawn a fresh warm cmux session and deliver the responder boot prompt.
    Returns the session record on success, None on failure. Mirrors pulse.py's
    proven dispatch sequence.

    Provider comes from agent_session.warm_agent() (env → comms pin → llm.provider
    → claude) unless the caller pins `agent`. The launch, readiness gate, and
    trust-prompt auto-answer are all resolved per-agent through agent_session so
    Claude's behavior is byte-identical to before while Droid gets its own."""
    agent = agent or agent_session.warm_agent()
    cmux = str(paths.cmux_bin)
    rc, _, _ = comms_lib.run_cmd([cmux, "ping"], timeout=10)
    if rc != 0:
        log("cmux not running — cannot spawn warm session")
        return None

    cwd = str(DISPATCH_CWD)
    launch = _warm_launch(agent)
    rc, out, err = comms_lib.run_cmd(
        [cmux, "new-workspace", "--cwd", cwd, "--name", SESSION_TITLE,
         "--focus", "false", "--command", launch], timeout=30)
    if rc != 0:
        log(f"new-workspace failed rc={rc}: {err.strip()[:200]}")
        # cmux may have assigned a workspace ref before timing out — record it
        # immediately so the next reconcile (on the following successful spawn)
        # closes it rather than leaving it as a permanent orphan.
        partial = parse_ws_ref_from_output(out, err)
        if partial:
            record_spawned_ref(paths, partial)
            log(f"recorded partial spawn {partial} for cleanup on next reconcile")
        return None
    m = re.search(r"workspace:\d+", out)
    if not m:
        log(f"no workspace ref in: {out.strip()[:200]}")
        _abandon_failed_spawn(paths, log=log)
        return None
    ws_ref = m.group(0)
    # Record the ref BEFORE the surface lookup: if any later step early-returns,
    # this instance's next reconcile still knows to clean up the workspace it made.
    record_spawned_ref(paths, ws_ref)

    rc, out, _ = comms_lib.run_cmd([cmux, "list-pane-surfaces", "--workspace", ws_ref], timeout=15)
    sm = re.search(r"surface:\d+", out)
    if not sm:
        log(f"no surface for {ws_ref}")
        _abandon_failed_spawn(paths, log=log)
        return None
    surface_ref = sm.group(0)

    project_dir = project_dir_for_cwd(cwd, agent)
    project_dir.mkdir(parents=True, exist_ok=True)
    before = {p.name for p in project_dir.glob("*.jsonl")}

    # Readiness gate: poll the boot screen for the per-agent ready marker
    # (banner or status bar), answering the first-launch trust prompt if/when it
    # shows. Both are delegated to await_ready so the ordering is unit-tested.
    def _answer_trust() -> None:
        _cmux_rpc(paths, "surface.send_text", {"surface_id": surface_ref, "text": "1"})
        # send_text streams keystrokes; give "1" a beat to land before Enter so
        # the selection isn't submitted empty (mirrors feed()'s proven pattern).
        time.sleep(0.5)
        _cmux_rpc(paths, "surface.send_key", {"surface_id": surface_ref, "key": "enter"})

    ready, trust_answered = await_ready(
        read_screen=lambda: _surface_read_text(paths, surface_ref),
        ready_re=agent_session.ready_re(agent),
        trust_marker=agent_session.trust_marker(agent),
        answer_trust=_answer_trust,
        attempts=READY_ATTEMPTS,
    )
    if not ready:
        # "answer sent" — not "accepted": if the RPC dropped or the keystroke
        # raced acceptance, the prompt can still be up. Distinct from the
        # no-trust-seen case so the next outage triage isn't misled.
        detail = " (trust prompt seen; answer sent)" if trust_answered else ""
        log(f"{agent} never ready in {ws_ref}/{surface_ref}{detail}")
        _abandon_failed_spawn(paths, log=log)
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
        transcript = newest_transcript(cwd, agent)
        log(f"warm session {ws_ref} spawned but boot submission unconfirmed")

    write_session(paths, ws_ref, surface_ref, cwd, transcript, agent=agent)
    log(f"warm session ready: {ws_ref} / {surface_ref} (transcript={transcript})")
    reconcile_warm_workspaces(paths, keep=ws_ref, log=log)
    return read_session(paths)
