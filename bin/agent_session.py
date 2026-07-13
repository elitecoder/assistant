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

import os
import re
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


def dispatch_agent(env: dict | None = None) -> str:
    """Which agent the fleet SPAWNS for a dispatch. This is a POLICY choice
    (not host detection): defaults to claude for coexistence, so live behavior
    is unchanged until the operator flips ASSISTANT_DISPATCH_AGENT=droid."""
    e = os.environ if env is None else env
    v = (e.get("ASSISTANT_DISPATCH_AGENT") or DROID).strip().lower()
    return v if v in AGENTS else DROID
