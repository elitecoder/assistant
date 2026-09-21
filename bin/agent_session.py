"""agent_session — the Claude Code / Droid coexistence seam for the fleet.

The fleet historically read Claude Code session transcripts under
``~/.claude/projects`` and spawned the bare ``claude`` REPL. Factory Droid is a
second coding agent on this machine: it writes transcripts under
``~/.factory/sessions`` and is launched as ``droid``. This module is the ONE
place that knows the per-agent differences, so every transcript reader
(``lesson-extractor``, ``build-ws-context``, ``session-context-watcher``,
``pulse``, ``tools/memory_seeds``) and the dispatcher (``pulse.dispatch_todo``)
can stay agent-agnostic.

Two coexisting transcript schemas, normalized to one (role, content) shape:

    Claude:  {"type": "user"|"assistant", "message": {"role", "content"}, ...}
    Droid:   {"type": "session_start" | "message",
              "message": {"role": "user"|"assistant", "content"}, ...}

``content`` is the SAME list-of-blocks (``[{type:"text",text}, ...]``) or string
in both schemas, so only the ROLE lives in a different field — top-level
``type`` for Claude, ``message.role`` for Droid. ``record_role()`` resolves
that; callers keep their existing text extraction. The per-cwd directory slug
(``/`` → ``-``) is identical across both agents, only the root dir differs.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path

HOME = Path(os.environ.get("HOME", str(Path.home())))

CLAUDE = "claude"
DROID = "droid"
AGENTS = (CLAUDE, DROID)

# Transcript roots. The per-cwd subdir slug is shared; only the root differs.
CLAUDE_PROJECTS = HOME / ".claude" / "projects"
DROID_SESSIONS = HOME / ".factory" / "sessions"


def transcript_root(agent: str, home: str | Path | None = None) -> Path:
    """Root dir under which `agent` writes one <slug>/<uuid>.jsonl per session."""
    if home is None:
        return DROID_SESSIONS if agent == DROID else CLAUDE_PROJECTS
    base = Path(home)
    return base / (".factory/sessions" if agent == DROID
                   else ".claude/projects")


def transcript_roots(agents: tuple[str, ...] = AGENTS) -> list[tuple[str, Path]]:
    """[(agent, root)] for each requested agent whose root exists on disk."""
    return [(a, transcript_root(a)) for a in agents if transcript_root(a).is_dir()]


def project_slug(cwd: str | Path) -> str:
    """The <cwd> → directory-name slug both agents use (real path, `/` → `-`)."""
    return os.path.realpath(str(cwd)).replace("/", "-")


def confirm_dir(agent: str, cwd: str | Path,
                home: str | Path | None = None) -> Path:
    """Per-cwd transcript dir a freshly-spawned `agent` session writes into."""
    return transcript_root(agent, home=home) / project_slug(cwd)


def record_role(obj: object) -> str | None:
    """Normalized turn role ('user' | 'assistant') for a transcript record of
    EITHER schema, or None for non-turn records (session_start, summaries,
    tool-only rows). Callers keep their own content/text extraction — the block
    shape is identical across agents; only the role's location differs."""
    if not isinstance(obj, dict):
        return None
    t = obj.get("type")
    if t in ("user", "assistant"):  # Claude: the role IS the top-level type
        return t
    if t == "message":  # Droid: the role rides on the message object
        m = obj.get("message")
        if isinstance(m, dict):
            r = m.get("role")
            return r if r in ("user", "assistant") else None
    return None


# ── spawn policy ──────────────────────────────────────────────────────────────

def launch_command(agent: str, home: str | Path | None = None) -> str:
    """Interactive REPL command to bake into cmux --command. The bare binary
    name only: ~/.zprofile's `claude` alias / `droid` on PATH carry the flags
    (model, permissions, --add-dir) — the single source of truth per machine."""
    if agent == DROID:
        base = HOME if home is None else Path(home)
        settings = base / ".assistant" / "droid-glm-settings.json"
        lessons = base / ".claude" / "CLAUDE.md"
        command = f"droid --settings '{settings}' --auto high"
        if lessons.is_file():
            command += f" --append-system-prompt-file '{lessons}'"
        return command
    return CLAUDE


# Readiness markers seen on the live boot screen via cmux `surface.read_text`.
#   Claude: the boot banner, or the bottom bypass-permissions status bar (always
#           visible regardless of /tui mode).
#   Droid:  the help hint / autonomy status bar / model badge from its banner
#           (observed: "v0.153.1", "Skills (63) ✓", "Opus 4.8 (High)",
#           "allow all commands", "? for help").
_READY_RE = {
    CLAUDE: re.compile(r"Claude Code v|⏵⏵ bypass permissions on"),
    DROID: re.compile(
        r"\? for help|allow all commands|Skills \(\d+\)|"
        r"(?:Opus 4\.\d|GLM-?5\.2)"),
}


def ready_re(agent: str) -> "re.Pattern[str]":
    """Compiled regex whose match on the boot screen means the REPL is ready."""
    return _READY_RE[DROID] if agent == DROID else _READY_RE[CLAUDE]


# First-launch folder-trust prompt. Claude's prompt is an arrow-selector whose
# DEFAULT highlighted option is "No, exit", with "Yes, I trust this folder" on
# the line below it (verified live 2026-09-21 against Claude Code v2.1.x). An
# earlier numbered UI ("1. Yes, I trust this folder") is gone; matching the
# unnumbered option text stays robust to both. Droid's trust UX is not pinned
# here, so it returns None (no auto-answer) — a missing answer only delays,
# never misfires.
_TRUST_MARKER = {
    CLAUDE: "Yes, I trust this folder",
    DROID: None,
}


def trust_marker(agent: str) -> str | None:
    """Screen substring of the first-launch trust prompt to auto-answer, or
    None when this agent has no known auto-answerable trust gate."""
    return _TRUST_MARKER.get(agent)


# Keystrokes that ACCEPT the trust prompt, sent in order once the marker shows.
# Claude's selector defaults to "No, exit"; the trusting option is one line
# below, so we move Down then confirm with Enter (verified live 2026-09-21).
# Sending "1" — correct for the RETIRED numbered UI — would now confirm the
# default "No, exit" and quit claude, so the keys MUST track the current UI.
# Droid has no auto-answerable gate → empty tuple (never fires). Single-sourced
# here so every call site (comms warm session, pulse dispatch) stays in sync.
_TRUST_ANSWER_KEYS = {
    CLAUDE: ("down", "enter"),
    DROID: (),
}


def trust_answer_keys(agent: str) -> tuple[str, ...]:
    """Ordered cmux key names that accept the first-launch trust prompt for
    `agent`, or an empty tuple when there is no auto-answerable gate."""
    return _TRUST_ANSWER_KEYS.get(agent, ())


def _read_comms_config() -> dict | None:
    """The comms config dict at ``~/.assistant/comms/config.json``, or None when
    absent / unreadable / not a JSON object. Read per-call from $HOME so a
    tmp-home test sees its own config."""
    home = Path(os.environ.get("HOME", str(Path.home())))
    try:
        raw = json.loads(
            (home / ".assistant" / "comms" / "config.json").read_text())
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, dict) else None


def _cfg_agent(raw: dict | None, *keys: str) -> str | None:
    """Walk ``keys`` into ``raw`` (each level must be a dict), returning the
    leaf value lower-cased iff it names a known agent, else None."""
    node: object = raw
    for k in keys:
        if not isinstance(node, dict):
            return None
        node = node.get(k)
    v = node.strip().lower() if isinstance(node, str) else ""
    return v if v in AGENTS else None


def _config_agent() -> str | None:
    """The dispatch-agent choice persisted at INSTALL time in
    ``~/.assistant/comms/config.json`` (``{"dispatch": {"agent": "claude"|
    "droid"}}``) — how the operator picks Droid or Claude without editing an env
    var. None when absent / unreadable / not a known agent."""
    return _cfg_agent(_read_comms_config(), "dispatch", "agent")


def dispatch_agent(env: dict | None = None) -> str:
    """Which agent the fleet SPAWNS for a dispatch — a POLICY choice, not host
    detection. Precedence:
      1. the ASSISTANT_DISPATCH_AGENT env override (one-off / testing);
      2. the INSTALL-TIME choice persisted in comms/config.json (dispatch.agent);
      3. the global ``llm.provider`` knob (the ONE-KNOB rule: setting
         ``llm.provider: droid`` makes spawns follow headless calls);
      4. the coexistence default ``claude`` — the always-present agent, so a
         droid-less box never spawns a dead workspace by default.
    Passing ``env`` explicitly selects PURE env policy (no config read) — the
    shape the unit tests pin; production calls with no arg, so the operator's
    install-time Droid/Claude choice takes effect. Live behavior stays claude
    until the operator picks droid (at install or via the env)."""
    if env is not None:
        v = (env.get("ASSISTANT_DISPATCH_AGENT") or "").strip().lower()
        return v if v in AGENTS else CLAUDE
    v = (os.environ.get("ASSISTANT_DISPATCH_AGENT") or "").strip().lower()
    if v in AGENTS:
        return v
    raw = _read_comms_config()
    return (_cfg_agent(raw, "dispatch", "agent")
            or _cfg_agent(raw, "llm", "provider")
            or CLAUDE)


def warm_agent(env: dict | None = None) -> str:
    """Which agent to spawn for the comms WARM session. Precedence:
      1. the ASSISTANT_COMMS_AGENT override (from ``env`` when given, else the
         process environment);
      2. ``llm.features.comms.provider`` in comms/config.json (comms-only pin);
      3. the global ``llm.provider`` knob (comms follows the one-knob default);
      4. the coexistence default ``claude``.
    Config is always consulted (unlike ``dispatch_agent``'s pure-env shape); the
    ``env`` arg only redirects where the ASSISTANT_COMMS_AGENT override is read
    from, so a test can inject one without touching the process environment."""
    src = os.environ if env is None else env
    v = (src.get("ASSISTANT_COMMS_AGENT") or "").strip().lower()
    if v in AGENTS:
        return v
    raw = _read_comms_config()
    return (_cfg_agent(raw, "llm", "features", "comms", "provider")
            or _cfg_agent(raw, "llm", "provider")
            or CLAUDE)


def agent_available(agent: str) -> bool:
    """Best-effort pre-flight of the opt-in `droid` binary. Claude launches via
    the ~/.zprofile `claude` alias (not an on-PATH executable) so it is assumed
    present; only droid is checked. A True lets dispatch spawn droid; a False
    makes the caller fall back to claude so a droid-less box keeps dispatching
    instead of spawning a dead workspace.

    IMPORTANT PATH caveat (M8 review): the pulse runs under launchd's PINNED,
    minimal PATH — NOT the login shell that sources ~/.zprofile and actually
    launches the agent. So a bare `shutil.which("droid")` false-negatives a droid
    installed to ~/.local/bin (Factory's default) or a Homebrew path, which the
    launcher WOULD find. We therefore also probe the common install locations. A
    residual false-negative is not catastrophic: it only falls back to claude,
    and the never-ready path now STAMPS (parks) rather than storms, so a wrongly-
    spawned droid can't loop either way.

    Two launch preconditions, both required (else fall back to claude):
      (1) an EXECUTABLE binary — `.exists()` is not enough; a truncated download
          or a lost exec bit passes existence but fails at spawn (permission
          denied → never-ready → park). Require `os.access(..., X_OK)`.
      (2) the `--settings` file `launch_command(DROID)` bakes in must exist and
          parse as JSON — a droid spawned with a missing/broken settings path
          errors, or boots WITHOUT the configured model/autonomy."""
    if agent != DROID:
        return True
    home = Path(os.environ.get("HOME", str(Path.home())))
    binary_ok = shutil.which(DROID) is not None
    if not binary_ok:
        for cand in (home / ".local" / "bin" / DROID,
                     Path("/opt/homebrew/bin") / DROID,
                     Path("/usr/local/bin") / DROID):
            if cand.is_file() and os.access(cand, os.X_OK):
                binary_ok = True
                break
    if not binary_ok:
        return False
    settings = home / ".assistant" / "droid-glm-settings.json"
    try:
        parsed = json.loads(settings.read_text())
    except (OSError, ValueError):
        return False
    return isinstance(parsed, dict)
