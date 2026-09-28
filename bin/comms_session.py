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
    submission confirmation, prompt-box parsing, workspace liveness) —
    unit-tested, no cmux.
  - cmux I/O (spawn, submit, clear, liveness probes) — thin wrappers over the
    same RPC pattern pulse.py uses to drive Assistant. Validated live, not
    mocked.

Transport-agnostic: this file knows nothing about Slack vs any other transport.
The daemon composes the per-message feed string; this only manages the session.
"""
from __future__ import annotations

import hashlib
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


def _instance_tag(paths: comms_lib.Paths) -> str:
    """A short stable discriminator for THIS comms instance, derived from its
    COMMS_HOME (via comms_dir). Two daemons with distinct COMMS_HOME — a second
    box, or a live-validation spawn — get distinct tags."""
    return hashlib.sha1(str(paths.comms_dir).encode()).hexdigest()[:6]


def warm_workspace_title(paths: comms_lib.Paths) -> str:
    """The cmux workspace name for this instance's warm session: the shared
    SESSION_TITLE plus this instance's tag. The machine-wide orphan scan
    (list_warm_workspaces) filters on this full title, so a failing instance
    can only ever sweep ITS OWN orphans and never closes another instance's
    healthy warm session (which carries a different tag). close_own_workspace's
    guard still matches on the SESSION_TITLE substring, so it keeps working for
    both tagged and any legacy untagged session."""
    return f"{SESSION_TITLE} #{_instance_tag(paths)}"


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


def warm_session_model_is_current(paths: comms_lib.Paths, sess: dict) -> bool:
    """True if an alive warm session was spawned with the model id the CURRENT
    backend resolves to.

    A warm session is long-lived and kept alive by ref across daemon restarts
    (ensure_warm_session), so its model id is fixed at spawn time. A
    `claude-backend bedrock|sub` toggle AFTER spawn changes what
    _resolve_warm_model_and_backend returns, but the running session keeps the
    OLD provider's id — e.g. a Bedrock `us.anthropic.claude-sonnet-4-6[1m]`
    still running after the box switched to direct Anthropic (observed
    2026-09-16). Comparing the recorded id to the freshly resolved one lets the
    caller respawn on the correct id.

    Only the claude warm session carries such an id; a droid session has none,
    so it is always current. A pre-upgrade session with no recorded model reads
    as NOT current, so it respawns once onto the tracked id."""
    if (sess.get("agent") or agent_session.CLAUDE) != agent_session.CLAUDE:
        return True
    want, _backend, _pinned = _resolve_warm_model_and_backend()
    return sess.get("model") == want


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

# The boot banner is the first thing Claude paints, before the prompt box exists,
# and keys typed that early can be lost. After the banner, wait up to this many
# seconds for the bottom status bar — the sign the prompt box is drawn.
INPUT_READY_SEC = _positive_int_env("COMMS_INPUT_READY_SEC", 15)
_INPUT_READY_RE = {agent_session.CLAUDE: re.compile(r"⏵⏵ bypass permissions on")}

# Every prompt the daemon types is confirmed against the transcript. Enter can be
# lost or turn into a newline (2026-09-27: a boot prompt and a Slack message sat
# typed in the box for two hours), so up to SUBMIT_ATTEMPTS Enters are tried,
# waiting SUBMIT_WAIT_SEC after each for the transcript to record the prompt.
SUBMIT_ATTEMPTS = _positive_int_env("COMMS_SUBMIT_ATTEMPTS", 3)
SUBMIT_WAIT_SEC = _positive_int_env("COMMS_SUBMIT_WAIT_SEC", 15)

# How many times to look at a workspace cmux didn't answer about, and how long to
# wait between looks, before calling its state unknown.
LIVENESS_ATTEMPTS = _positive_int_env("COMMS_LIVENESS_ATTEMPTS", 3)
LIVENESS_RETRY_SEC = _positive_int_env("COMMS_LIVENESS_RETRY_SEC", 3)


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
                  agent: str | None = None, clock=None,
                  model: str | None = None) -> None:
    """Persist the warm-session registry. `agent` records which provider owns
    the session so post-restart reads pick the right transcript root + schema.
    `model` records the exact model id the session was spawned with, so
    ensure_warm_session can respawn it when a `claude-backend bedrock|sub`
    toggle changes the resolved id out from under a still-alive session.
    agent=None / model=None mean "preserve the persisted value" — used by the
    transcript-refresh call path, which must NOT silently reset a droid session
    to claude or drop the recorded model; each reuses the prior record's value."""
    paths.comms_dir.mkdir(parents=True, exist_ok=True)
    if agent is None or model is None:
        prior = read_session(paths) or {}
        if agent is None:
            agent = prior.get("agent") or agent_session.CLAUDE
        if model is None:
            model = prior.get("model")
    rec = {
        "ws_ref": ws_ref,
        "surface_ref": surface_ref,
        "cwd": cwd,
        "transcript_path": transcript_path,
        "agent": agent,
        "model": model,
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
    ~/.claude/projects/<slug>; Droid: ~/.factory/sessions/<slug>. Delegates to
    agent_session.confirm_dir (the single source of truth for both roots and
    slugs), rooted at this module's HOME so a tmp-home test resolves against
    its own tree."""
    return agent_session.confirm_dir(agent, cwd, home=HOME)


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


# --------------------------------------------------------------------------- submission (pure)

# Only the transcript tail is read: a submitted prompt is always among the
# newest records, and warm transcripts grow to megabytes.
TRANSCRIPT_TAIL_BYTES = 262_144
_RULE_RE = re.compile(r"^\s*─{8,}\s*$")
_WS_RE = re.compile(r"\s+")


def input_box_text(screen: str) -> str | None:
    """The text in Claude's prompt box, or None when the screen shows no box.

    The box is the region between the last two horizontal rules, and its first
    line starts with ❯. Earlier prompts in the scrollback also start with ❯ but
    aren't fenced by rules, so they never match. Wrapped lines are joined."""
    lines = screen.splitlines()
    rules = [i for i, line in enumerate(lines) if _RULE_RE.match(line)]
    if len(rules) < 2:
        return None
    body = lines[rules[-2] + 1:rules[-1]]
    if not body or not body[0].lstrip().startswith("❯"):
        return None
    first = body[0].lstrip()[1:]
    return " ".join(part.strip() for part in [first, *body[1:]]).strip()


def box_holds(box: str | None, marker: str) -> bool:
    """True if the prompt box still holds text the daemon typed: its marker
    (compared without whitespace, so a line wrap inside it still matches) or a
    collapsed paste."""
    if not box:
        return False
    return (_WS_RE.sub("", marker) in _WS_RE.sub("", box)
            or "[Pasted text" in box)


def _prompt_text(rec: dict) -> str | None:
    """The prompt text of a submitted user turn or a prompt queued while the
    session was busy; None for every other record, including tool results."""
    if rec.get("type") == "queue-operation":
        content = rec.get("content")
        return content if isinstance(content, str) else None
    if agent_session.record_role(rec) != "user":
        return None
    msg = rec.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(b.get("text", "") for b in content
                       if isinstance(b, dict) and b.get("type") == "text")
    return None


def transcript_has_submission(path: str | Path, marker: str) -> bool:
    """True if the transcript at `path` records a prompt containing `marker`.

    Headless `claude -p` transcripts never count: the proofgate Stop hook runs
    one in this cwd after every warm turn, and it quotes the warm session's
    prompts back."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - TRANSCRIPT_TAIL_BYTES))
            tail = f.read().decode("utf-8", "replace")
    except OSError:
        return False
    for line in tail.splitlines():
        if marker not in line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict) or rec.get("entrypoint") == "sdk-cli":
            continue
        text = _prompt_text(rec)
        if text and marker in text:
            return True
    return False


def find_submission(project_dir: Path, marker: str, since: float) -> str | None:
    """Path of the newest transcript in `project_dir` changed at or after
    `since` that records a prompt containing `marker`, or None.

    Searches instead of trusting a remembered path, so a session that was
    cleared, resumed, or bound to the wrong file is found again. Subagent
    transcripts are skipped."""
    if not project_dir.is_dir():
        return None
    candidates = []
    for p in project_dir.rglob("*.jsonl"):
        if "subagents" in p.parts:
            continue
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        if mtime >= since:
            candidates.append((mtime, p))
    for _mtime, p in sorted(candidates, reverse=True):
        if transcript_has_submission(p, marker):
            return str(p)
    return None


def submit_until_confirmed(send_text, press_enter, read_box, confirmed, marker: str,
                           attempts: int = SUBMIT_ATTEMPTS,
                           wait_sec: float = SUBMIT_WAIT_SEC,
                           sleep=time.sleep, clock=time.monotonic) -> bool:
    """Type a prompt once, press Enter, and wait until `confirmed()` sees it in
    the transcript. Returns whether it did.

    If confirmation doesn't come and the box still holds the daemon's marker —
    Enter was lost or became a newline — press Enter again, up to `attempts`
    presses in all. The text is never retyped and Enter is never pressed on a
    box that doesn't hold the marker, so a retry can't double-send a prompt or
    submit someone else's half-typed input. A prompt an earlier try already
    got recorded isn't typed again, and one an earlier try left sitting in the
    box only gets its Enter. All I/O is injected."""
    if confirmed():
        return True
    if not box_holds(read_box(), marker):
        send_text()
        sleep(0.5)
    press_enter()
    for attempt in range(attempts):
        deadline = clock() + wait_sec
        while clock() < deadline:
            if confirmed():
                return True
            sleep(1)
        if attempt + 1 < attempts and box_holds(read_box(), marker):
            press_enter()
    return confirmed()


def boot_instruction(boot_prompt: Path, nonce: str) -> str:
    """The prompt that boots a warm session. The nonce makes each boot's text
    unique, so confirmation can't match an earlier session's boot turn."""
    return f"Read {boot_prompt} in full and execute every instruction in it. [boot {nonce}]"


# --------------------------------------------------------------------------- liveness (pure)

ALIVE = "alive"
GONE = "gone"
UNKNOWN = "unknown"

# What cmux prints for a workspace ref it doesn't know. Every other failure — a
# refused socket connection, a timeout — means cmux didn't answer, which says
# nothing about the workspace (2026-09-27: a napping cmux refused connections
# for up to two hours, and each refusal used to close a healthy warm session).
_MISSING_WORKSPACE_MARKERS = ("invalid_params", "Missing or invalid workspace")


def classify_tree_result(rc: int, err: str) -> str:
    """ALIVE, GONE, or UNKNOWN for one `cmux tree --workspace` result."""
    if rc == 0:
        return ALIVE
    if any(m in (err or "") for m in _MISSING_WORKSPACE_MARKERS):
        return GONE
    return UNKNOWN


def ref_listed(text: str, ws_ref: str) -> bool:
    """True if `ws_ref` appears as a whole ref in `text`, so workspace:25
    doesn't match workspace:258."""
    return re.search(rf"{re.escape(ws_ref)}(?!\d)", text) is not None


def resolve_workspace_state(probe, attempts: int = LIVENESS_ATTEMPTS,
                            retry_sec: float = LIVENESS_RETRY_SEC,
                            sleep=time.sleep) -> str:
    """Run `probe` until it returns ALIVE or GONE, waiting `retry_sec` between
    tries; UNKNOWN if cmux never gives an answer. Short cmux blips clear within
    a few seconds, so one retry saves a session a single failed look would
    have closed."""
    for attempt in range(attempts):
        state = probe()
        if state != UNKNOWN:
            return state
        if attempt + 1 < attempts:
            sleep(retry_sec)
    return UNKNOWN


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


def probe_workspace(paths: comms_lib.Paths, ws_ref: str, run=None) -> str:
    """One look at a workspace. When `tree` fails without saying the ref is
    unknown, a successful `list-workspaces` still settles it either way; if
    that fails too, the answer is UNKNOWN. `run` defaults to comms_lib.run_cmd."""
    run = run or comms_lib.run_cmd
    rc, _, err = run([str(paths.cmux_bin), "tree", "--workspace", ws_ref, "--json"], timeout=10)
    state = classify_tree_result(rc, err)
    if state != UNKNOWN:
        return state
    rc, out, _ = run([str(paths.cmux_bin), "list-workspaces"], timeout=10)
    if rc != 0:
        return UNKNOWN
    return ALIVE if ref_listed(out, ws_ref) else GONE


def workspace_state(paths: comms_lib.Paths, ws_ref: str) -> str:  # pragma: no cover - live cmux I/O
    """ALIVE, GONE, or UNKNOWN (cmux didn't answer) for a warm workspace."""
    return resolve_workspace_state(lambda: probe_workspace(paths, ws_ref))


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
    is_warm = any(ref_listed(line, ws_ref) and SESSION_TITLE in line for line in out.splitlines())
    if not is_warm:
        log(f"skip close {ws_ref}: not a '{SESSION_TITLE}' workspace (ref reissued?)")
        return
    rc, _, err = comms_lib.run_cmd(
        [str(paths.cmux_bin), "close-workspace", "--workspace", ws_ref], timeout=15)
    log(f"closed prior warm workspace {ws_ref}" if rc == 0
        else f"close {ws_ref} rc={rc}: {err.strip()[:120]}")


def send_enter(paths: comms_lib.Paths, surface_ref: str) -> None:  # pragma: no cover - live cmux I/O
    """Submit whatever is in the prompt box by writing a carriage return to the
    terminal. `surface.send_key enter` reports success on a warm workspace that
    was never shown on screen, yet the prompt stays unsent (reproduced
    2026-09-28 on a --focus false workspace); a "\\r" through send_text submits
    it at once."""
    _cmux_rpc(paths, "surface.send_text", {"surface_id": surface_ref, "text": "\r"})


def submit(paths: comms_lib.Paths, surface_ref: str, text: str, marker: str,
           confirmed) -> bool:  # pragma: no cover - live cmux I/O
    """Type text into the warm session and press Enter until `confirmed()` sees
    it in the transcript (see submit_until_confirmed). The trailing newline is
    stripped: send_text streams keystrokes, so a trailing \\n would submit
    mid-paste."""
    return submit_until_confirmed(
        send_text=lambda: _cmux_rpc(paths, "surface.send_text",
                                    {"surface_id": surface_ref, "text": text.rstrip("\n")}),
        press_enter=lambda: send_enter(paths, surface_ref),
        read_box=lambda: input_box_text(_surface_read_text(paths, surface_ref, lines=60)),
        confirmed=confirmed,
        marker=marker,
    )


def deliver_boot(paths: comms_lib.Paths, surface_ref: str, cwd: str, boot_prompt: Path,
                 agent: str, submit_fn=None) -> str | None:
    """Type the boot prompt and return the transcript that recorded it, or None
    if it was never submitted. The session is bound to that exact file — never
    to whichever transcript happens to be newest, which on 2026-09-27/28 was
    often another session's. `submit_fn` defaults to submit."""
    submit_fn = submit_fn or submit
    nonce = f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{os.getpid()}"
    marker = f"[boot {nonce}]"
    project_dir = project_dir_for_cwd(cwd, agent)
    since = time.time() - 1
    found: list[str] = []

    def confirmed() -> bool:
        hit = find_submission(project_dir, marker, since)
        if hit:
            found.append(hit)
        return hit is not None

    if not submit_fn(paths, surface_ref, boot_instruction(boot_prompt, nonce), marker, confirmed):
        return None
    return found[-1]


def clear_session(paths: comms_lib.Paths, sess: dict, boot_prompt: Path,
                  agent: str = agent_session.CLAUDE,
                  log=lambda m: None) -> dict:  # pragma: no cover - live cmux I/O
    """Clear-AND-resume: reset the context window losslessly, then return the
    refreshed session record. Per-message thread continuity comes from
    conversation.jsonl (the boot prompt tells the session to reconstruct it), so
    a reset loses nothing.

    claude — in-place /clear + resume: send /clear (as text, then a separate
    carriage return; a trailing newline inside the same send_text does NOT
    reliably submit a slash command), POLL the bottom of the screen for the
    post-clear welcome and an empty prompt box (feeding during the ~2s reset
    window gets keystrokes swallowed; a full-history read could match the old
    banner), re-deliver the boot prompt, then bind the registry to the
    transcript that recorded it. The workspace and surface are unchanged. If
    the boot prompt never lands, fall back to a lossless respawn.

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
    send_enter(paths, surface_ref)

    for _ in range(15):
        time.sleep(1)
        screen = _surface_read_text(paths, surface_ref, lines=40)
        welcomed = "Welcome back" in screen or "Tips for getting started" in screen
        if welcomed and input_box_text(screen) == "":
            break

    time.sleep(1)
    transcript = deliver_boot(paths, surface_ref, sess["cwd"], boot_prompt, agent)
    if not transcript:
        log(f"boot prompt after /clear never submitted in {sess['ws_ref']} — respawning")
        close_own_workspace(paths, sess["ws_ref"], log=log)
        clear_session_registry(paths)
        return spawn_session(paths, boot_prompt, log=log, agent=agent) or sess
    write_session(paths, sess["ws_ref"], surface_ref, sess["cwd"], transcript, agent=agent)
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
    """This instance's warm workspaces (from list_warm_workspaces' instance-tagged
    scan) that are NOT in its spawned ledger and are NOT the kept survivor.  These
    are orphans that slipped through record_spawned_ref — e.g. a timed-out
    new-workspace that created a workspace before the daemon got rc=1."""
    known = set(spawned_refs)
    return [ws for ws in warm_refs if ws != keep and ws not in known]


def list_warm_workspaces(paths: comms_lib.Paths) -> list[str]:  # pragma: no cover - live cmux I/O
    """Workspace refs whose title is THIS instance's warm-session title.

    Filters on the instance-tagged warm_workspace_title, not the bare
    SESSION_TITLE, so the machine-wide list-workspaces output is narrowed to
    this instance's own warm workspaces. That keeps reconcile's orphan-sweep
    pass from ever returning a second comms instance's healthy session."""
    rc, out, _ = comms_lib.run_cmd([str(paths.cmux_bin), "list-workspaces"], timeout=10)
    if rc != 0:
        return []
    title = warm_workspace_title(paths)
    refs = []
    for line in out.splitlines():
        if title in line:
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
    2. Title-scan pass — close any of THIS instance's warm workspaces (matched by
       the instance-tagged warm_workspace_title, via list_warm_workspaces) NOT in
       the spawned ledger and NOT keep.  Catches orphans from timed-out spawns
       that created a workspace before record_spawned_ref was reached.  The
       instance tag keeps this from ever reaching a second comms instance's
       healthy warm session; close_own_workspace is title-guarded on top.

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


def _warm_launch(agent: str, resolved: tuple[str, str, bool] | None = None) -> str:
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
    model, backend, pinned = resolved or _resolve_warm_model_and_backend()
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


def _abandon_failed_spawn(paths: comms_lib.Paths, ws_ref: str | None,
                          log=lambda m: None) -> None:  # pragma: no cover - live cmux I/O
    """Close the single workspace a failed spawn just created.

    Every spawn_session failure path AFTER new-workspace made a workspace used to
    return None without closing it. When claude never reached the ready marker (a
    bad boot, a slow machine), the watchdog respawned every tick and each attempt
    leaked one live workspace — ~200 piled up on 2026-09-14 and killed cmux.

    Closes ONLY ws_ref, via title-guarded close_own_workspace — never a
    machine-wide title scan. A machine-wide scan (reconcile with keep=None) would
    also close a SECOND comms instance's healthy warm session (same SESSION_TITLE,
    different COMMS_HOME), so a failing instance would kill a healthy one on every
    tick. Targeting one ref keeps cleanup instance-safe. ws_ref is None only on
    the no-ref path (cmux returned success but printed no ref); there is nothing
    to target there, so warn and let the next successful spawn's reconcile sweep
    the orphan via its title-scan pass."""
    if not ws_ref:
        log("failed spawn produced no workspace ref — cannot target-close; "
            "next successful reconcile will sweep any orphan")
        return
    try:
        close_own_workspace(paths, ws_ref, log=log)
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
    rc, _, err = comms_lib.run_cmd([cmux, "ping"], timeout=10)
    if rc != 0:
        log(f"cmux isn't answering ({err.strip()[:120]}) — cannot spawn warm session")
        return None

    cwd = str(DISPATCH_CWD)
    # Resolve the model/backend ONCE and use it for BOTH the launch flag and the
    # recorded id, so a `claude-backend` toggle mid-boot can't make the record
    # disagree with what the session actually launched with (claude only; droid
    # has no such id).
    resolved = _resolve_warm_model_and_backend() if agent == agent_session.CLAUDE else None
    launch = _warm_launch(agent, resolved=resolved)
    rc, out, err = comms_lib.run_cmd(
        [cmux, "new-workspace", "--cwd", cwd, "--name", warm_workspace_title(paths),
         "--focus", "false", "--command", launch], timeout=30)
    if rc != 0:
        log(f"new-workspace failed rc={rc}: {err.strip()[:200]}")
        # cmux may have assigned a workspace ref before timing out — record it
        # immediately so the next reconcile (on the following successful spawn)
        # closes it rather than leaving it as a permanent orphan.
        # cmux may have assigned a workspace ref before timing out. Record it (so
        # a later reconcile still catches it if the close below fails), then close
        # it now — under a persistent new-workspace timeout no successful spawn
        # ever comes to run reconcile, so waiting for "next reconcile" leaks.
        partial = parse_ws_ref_from_output(out, err)
        if partial:
            record_spawned_ref(paths, partial)
            log(f"recorded partial spawn {partial}")
            _abandon_failed_spawn(paths, partial, log=log)
        return None
    m = re.search(r"workspace:\d+", out)
    if not m:
        log(f"no workspace ref in: {out.strip()[:200]}")
        _abandon_failed_spawn(paths, None, log=log)
        return None
    ws_ref = m.group(0)
    # Record the ref BEFORE the surface lookup: if any later step early-returns,
    # this instance's next reconcile still knows to clean up the workspace it made.
    record_spawned_ref(paths, ws_ref)

    rc, out, _ = comms_lib.run_cmd([cmux, "list-pane-surfaces", "--workspace", ws_ref], timeout=15)
    sm = re.search(r"surface:\d+", out)
    if not sm:
        log(f"no surface for {ws_ref}")
        _abandon_failed_spawn(paths, ws_ref, log=log)
        return None
    surface_ref = sm.group(0)

    # Readiness gate: poll the boot screen for the per-agent ready marker
    # (banner or status bar), answering the first-launch trust prompt if/when it
    # shows. Both are delegated to await_ready so the ordering is unit-tested.
    def _answer_trust() -> None:
        _cmux_rpc(paths, "surface.send_text", {"surface_id": surface_ref, "text": "1"})
        # send_text streams keystrokes; give "1" a beat to land before Enter so
        # the selection isn't submitted empty (mirrors submit()'s pattern).
        time.sleep(0.5)
        send_enter(paths, surface_ref)

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
        _abandon_failed_spawn(paths, ws_ref, log=log)
        return None

    input_ready_re = _INPUT_READY_RE.get(agent)
    if input_ready_re:
        await_ready(
            read_screen=lambda: _surface_read_text(paths, surface_ref, lines=40),
            ready_re=input_ready_re, trust_marker=None, answer_trust=lambda: None,
            attempts=INPUT_READY_SEC,
        )

    # Deliver the responder boot prompt by reference, and keep the session only
    # if its transcript shows the prompt was submitted.
    transcript = deliver_boot(paths, surface_ref, cwd, boot_prompt, agent)
    if not transcript:
        log(f"{agent} boot prompt never submitted in {ws_ref}/{surface_ref}")
        _abandon_failed_spawn(paths, ws_ref, log=log)
        return None

    # Record the EXACT id this session launched with — the same `resolved` tuple
    # _warm_launch used above, captured once so a mid-boot backend toggle can't
    # desync the record from the running session.
    spawn_model = resolved[0] if resolved else None
    write_session(paths, ws_ref, surface_ref, cwd, transcript, agent=agent, model=spawn_model)
    log(f"warm session ready: {ws_ref} / {surface_ref} (transcript={transcript})")
    reconcile_warm_workspaces(paths, keep=ws_ref, log=log)
    return read_session(paths)
