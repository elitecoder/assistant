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


# First-launch folder-trust prompt to auto-answer with "1" + Enter. Claude's
# exact line is known; Droid's trust UX is not yet pinned here, so it returns
# None (no auto-answer) until verified — a missing answer only delays, never
# misfires.
_TRUST_MARKER = {
    CLAUDE: "1. Yes, I trust this folder",
    DROID: None,
}


def trust_marker(agent: str) -> str | None:
    """Screen substring of the first-launch trust prompt to auto-answer, or
    None when this agent has no known auto-answerable trust gate."""
    return _TRUST_MARKER.get(agent)


def _config_agent() -> str | None:
    """The dispatch-agent choice persisted at INSTALL time in
    ``~/.assistant/comms/config.json`` (``{"dispatch": {"agent": "claude"|
    "droid"}}``) — how the operator picks Droid or Claude without editing an env
    var. None when absent / unreadable / not a known agent. Read per-call from
    $HOME so a tmp-home test sees its own config."""
    import json  # noqa: PLC0415
    home = Path(os.environ.get("HOME", str(Path.home())))
    try:
        raw = json.loads(
            (home / ".assistant" / "comms" / "config.json").read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    v = (raw.get("dispatch") or {}).get("agent") if isinstance(
        raw.get("dispatch"), dict) else None
    v = v.strip().lower() if isinstance(v, str) else ""
    return v if v in AGENTS else None


def dispatch_agent(env: dict | None = None) -> str:
    """Which agent the fleet SPAWNS for a dispatch — a POLICY choice, not host
    detection. Precedence:
      1. the ASSISTANT_DISPATCH_AGENT env override (one-off / testing);
      2. the INSTALL-TIME choice persisted in comms/config.json (dispatch.agent);
      3. the coexistence default ``claude`` — the always-present agent, so a
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
    return _config_agent() or CLAUDE


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
        json.loads(settings.read_text())
    except (OSError, ValueError):
        return False
    return True
