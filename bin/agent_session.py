"""agent_session — the ONE place that starts or reads a coding agent.

Every Assistant launch of a coding agent goes through this module: interactive
cmux sessions (``session_command``), resumes (``resume_argv``), and headless
one-shot calls (``headless_argv`` + ``parse_headless``, run by
``llm_runner.invoke``). Supported agents: Claude Code (``claude``), GitHub
Copilot CLI (``copilot``), and Factory Droid (``droid``). Which one runs is the
user's choice, read by ``dispatch_agent`` / ``warm_agent`` and
``llm_runner.load_route_config`` from ``~/.assistant/comms/config.json``
(``llm.provider`` is the one knob). ``tests/test_agent_launch_seam.py`` fails
when any other file builds an agent command line.

The rest of this docstring covers transcripts.

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
import shlex
import shutil
from pathlib import Path

HOME = Path(os.environ.get("HOME", str(Path.home())))

CLAUDE = "claude"
COPILOT = "copilot"
DROID = "droid"
AGENTS = (CLAUDE, COPILOT, DROID)

# Transcript roots. The per-cwd subdir slug is shared; only the root differs.
CLAUDE_PROJECTS = HOME / ".claude" / "projects"
DROID_SESSIONS = HOME / ".factory" / "sessions"
# Copilot keeps one <session-id>/events.jsonl per session, with the session's
# cwd in <session-id>/workspace.yaml — not a per-cwd slug directory.
COPILOT_SESSIONS = HOME / ".copilot" / "session-state"


_ROOT_SUBDIR = {
    CLAUDE: ".claude/projects",
    COPILOT: ".copilot/session-state",
    DROID: ".factory/sessions",
}
# Agents whose transcripts live under <root>/<cwd-slug>/<id>.jsonl.
SLUG_AGENTS = (CLAUDE, DROID)


def transcript_root(agent: str, home: str | Path | None = None) -> Path:
    """Root dir under which `agent` writes its session transcripts."""
    base = HOME if home is None else Path(home)
    return base / _ROOT_SUBDIR.get(agent, _ROOT_SUBDIR[CLAUDE])


def transcript_roots(agents: tuple[str, ...] = SLUG_AGENTS) -> list[tuple[str, Path]]:
    """[(agent, root)] for each requested agent whose root exists on disk."""
    return [(a, transcript_root(a)) for a in agents if transcript_root(a).is_dir()]


def copilot_session_cwd(session_dir: str | Path) -> str | None:
    """The `cwd:` line of a Copilot <session-id>/workspace.yaml, or None."""
    try:
        text = (Path(session_dir) / "workspace.yaml").read_text()
    except OSError:
        return None
    m = re.search(r"^cwd:\s*(.+?)\s*$", text, re.M)
    if not m:
        return None
    value = m.group(1)
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        value = value[1:-1]
    return value


def copilot_transcripts(cwd: str | Path, home: str | Path | None = None) -> list[Path]:
    """Copilot events.jsonl transcripts whose session cwd is `cwd`."""
    root = transcript_root(COPILOT, home=home)
    want = os.path.realpath(str(cwd))
    found = []
    try:
        dirs = list(root.iterdir())
    except OSError:
        return []
    for d in dirs:
        events = d / "events.jsonl"
        sess_cwd = copilot_session_cwd(d)
        if sess_cwd and os.path.realpath(sess_cwd) == want and events.is_file():
            found.append(events)
    return found


def transcript_for_session(agent: str, session_id: str,
                           home: str | Path | None = None) -> str | None:
    """Newest transcript path of `agent` session `session_id`, or None."""
    if agent not in AGENTS or not session_id:
        return None
    root = transcript_root(agent, home=home)
    if agent == COPILOT:
        events = root / session_id / "events.jsonl"
        return str(events) if events.is_file() else None
    if not root.is_dir():
        return None
    matches = list(root.glob(f"*/{session_id}.jsonl"))
    if not matches:
        return None
    return str(max(matches, key=lambda p: p.stat().st_mtime))


def session_transcripts(agent: str, cwd: str | Path,
                        home: str | Path | None = None) -> list[Path]:
    """Every transcript `agent` has written for sessions started in `cwd`."""
    if agent == COPILOT:
        return copilot_transcripts(cwd, home=home)
    d = confirm_dir(agent, cwd, home=home)
    return sorted(d.rglob("*.jsonl")) if d.is_dir() else []


def project_slug(cwd: str | Path) -> str:
    """The <cwd> → directory-name slug both agents use (real path, `/` → `-`)."""
    return os.path.realpath(str(cwd)).replace("/", "-")


def claude_project_slug(cwd: str | Path) -> str:
    """The project dir name Claude Code uses for `cwd`: every non-alphanumeric
    character of the real path becomes `-`, so `/x/.worktrees/a_b` is
    `-x--worktrees-a-b`. Same as project_slug for plain paths."""
    return re.sub(r"[^A-Za-z0-9-]", "-", project_slug(cwd))


def confirm_dir(agent: str, cwd: str | Path,
                home: str | Path | None = None) -> Path:
    """Per-cwd transcript dir a freshly-spawned `agent` session writes into.
    Copilot has no per-cwd dir; use session_transcripts for it."""
    if agent == COPILOT:
        return transcript_root(agent, home=home)
    slug = project_slug(cwd) if agent == DROID else claude_project_slug(cwd)
    return transcript_root(agent, home=home) / slug


def record_role(obj: object) -> str | None:
    """Normalized turn role ('user' | 'assistant') for a transcript record of
    ANY agent's schema, or None for non-turn records (session_start, summaries,
    tool-only rows). Pair with record_content for the turn's content."""
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
    if t in ("user.message", "assistant.message"):  # Copilot events.jsonl
        return t.split(".", 1)[0]
    return None


def record_content(obj: object):
    """The turn content (string or list of {type,text} blocks) of a transcript
    record of any agent's schema, or None."""
    if not isinstance(obj, dict):
        return None
    if obj.get("type") in ("user.message", "assistant.message"):
        data = obj.get("data")
        return data.get("content") if isinstance(data, dict) else None
    m = obj.get("message")
    return m.get("content") if isinstance(m, dict) else None


# ── binaries ──────────────────────────────────────────────────────────────────

_BIN_ENV = {CLAUDE: "CLAUDE_BIN", COPILOT: "COPILOT_BIN", DROID: "DROID_BIN"}


def _bin_candidates(agent: str, home: Path) -> list[Path]:
    cands = [home / ".local" / "bin" / agent,
             Path("/opt/homebrew/bin") / agent,
             Path("/usr/local/bin") / agent]
    if agent == COPILOT:
        nvm = home / ".nvm" / "versions" / "node"
        try:
            versions = sorted(nvm.iterdir(), reverse=True)
        except OSError:
            versions = []
        cands += [v / "bin" / COPILOT for v in versions]
    return cands


def binary(agent: str, env: dict | None = None) -> str:
    """Absolute path of `agent`'s CLI. Order: the agent's *_BIN env var, PATH,
    the common install dirs (launchd's PATH is minimal), else the bare name."""
    src = os.environ if env is None else env
    override = (src.get(_BIN_ENV.get(agent, "")) or "").strip()
    if override:
        return str(Path(override).expanduser())
    home = Path(src.get("HOME") or str(Path.home()))
    if agent == CLAUDE:
        return str(home / ".local" / "bin" / CLAUDE)
    on_path = shutil.which(agent)
    if on_path:
        return on_path
    for cand in _bin_candidates(agent, home):
        if cand.is_file() and os.access(cand, os.X_OK):
            return str(cand)
    return agent


def _executable(path: str) -> bool:
    return os.path.isfile(path) and os.access(path, os.X_OK)


# ── which agent the user picked ───────────────────────────────────────────────

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


def _cfg(raw: dict | None, *keys: str):
    node: object = raw
    for k in keys:
        if not isinstance(node, dict):
            return None
        node = node.get(k)
    return node


def _cfg_agent(raw: dict | None, *keys: str) -> str | None:
    """Walk ``keys`` into ``raw`` (each level must be a dict), returning the
    leaf value lower-cased iff it names a known agent, else None."""
    node = _cfg(raw, *keys)
    v = node.strip().lower() if isinstance(node, str) else ""
    return v if v in AGENTS else None


def dispatch_agent(env: dict | None = None) -> str:
    """Which agent the fleet SPAWNS for a dispatch. Precedence:
      1. the ASSISTANT_DISPATCH_AGENT env override;
      2. ``dispatch.agent`` in comms/config.json;
      3. the global ``llm.provider`` knob;
      4. ``claude``.
    Passing ``env`` explicitly selects pure env policy (no config read)."""
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
      1. the ASSISTANT_COMMS_AGENT override (from ``env`` when given);
      2. ``llm.features.comms.provider`` in comms/config.json;
      3. the global ``llm.provider`` knob;
      4. ``claude``."""
    src = os.environ if env is None else env
    v = (src.get("ASSISTANT_COMMS_AGENT") or "").strip().lower()
    if v in AGENTS:
        return v
    raw = _read_comms_config()
    return (_cfg_agent(raw, "llm", "features", "comms", "provider")
            or _cfg_agent(raw, "llm", "provider")
            or CLAUDE)


def copilot_model(feature: str | None = None, env: dict | None = None) -> str:
    """The Copilot model the user configured ('' = Copilot's own default).
    Order: <FEATURE>_COPILOT_MODEL, COPILOT_MODEL, llm.features.<f>.copilot.model,
    llm.copilot.model."""
    src = os.environ if env is None else env
    if feature:
        v = src.get(f"{feature.upper()}_COPILOT_MODEL")
        if v is not None:
            return v.strip()
    v = src.get("COPILOT_MODEL")
    if v is not None:
        return v.strip()
    raw = _read_comms_config()
    for keys in ((("llm", "features", feature, "copilot", "model") if feature else None),
                 ("llm", "copilot", "model")):
        if keys:
            node = _cfg(raw, *keys)
            if isinstance(node, str):
                return node.strip()
    return ""


def agent_available(agent: str) -> bool:
    """True when `agent` can be launched. Claude launches via the ~/.zprofile
    `claude` alias, so it is assumed present. Copilot needs an executable
    binary. Droid needs an executable binary plus the --settings JSON that
    session_command(DROID) bakes in. A False makes callers fall back to claude."""
    if agent == CLAUDE:
        return True
    home = Path(os.environ.get("HOME", str(Path.home())))
    if shutil.which(agent) is None and not _executable(binary(agent)):
        return False
    if agent == COPILOT:
        return True
    settings = home / ".assistant" / "droid-glm-settings.json"
    try:
        parsed = json.loads(settings.read_text())
    except (OSError, ValueError):
        return False
    return isinstance(parsed, dict)


def available_or_claude(agent: str) -> str:
    """`agent` when it can launch, else claude."""
    return agent if agent_available(agent) else CLAUDE


# ── interactive sessions ──────────────────────────────────────────────────────

def _droid_flags(home: Path) -> list[str]:
    settings = home / ".assistant" / "droid-glm-settings.json"
    lessons = home / ".claude" / "CLAUDE.md"
    flags = ["--settings", str(settings), "--auto", "high"]
    if lessons.is_file():
        flags += ["--append-system-prompt-file", str(lessons)]
    return flags


def _interactive_exe(agent: str) -> list[str]:
    """How a pane's login shell starts `agent`: its *_BIN override, else the
    name resolved through PATH (`command` skips the alias), so cmux's PATH shim
    and session hooks wrap it."""
    override = (os.environ.get(_BIN_ENV[agent]) or "").strip()
    if override:
        return [str(Path(override).expanduser())]
    return ["command", agent]


def session_argv(agent: str, *, home: str | Path | None = None,
                 model: str | None = None,
                 add_dirs: tuple | list = ()) -> list[str]:
    """argv for an interactive `agent` REPL with permissions bypassed.

    claude with no model/add_dirs is the bare ``claude`` alias (~/.zprofile owns
    its flags); with either, the explicit binary plus flags. copilot is
    ``copilot --allow-all`` plus the configured model. droid carries its
    settings file, --auto high, and the lessons file."""
    base = HOME if home is None else Path(home)
    if agent == DROID:
        return [DROID, *_droid_flags(base)]
    if agent == COPILOT:
        argv = [*_interactive_exe(COPILOT), "--allow-all"]
        chosen = copilot_model() if model is None else model
        if chosen:
            argv += ["--model", chosen]
        for d in add_dirs:
            argv += ["--add-dir", str(d)]
        return argv
    if model is None and not add_dirs:
        return [CLAUDE]
    argv = _interactive_exe(CLAUDE)
    if model:
        argv += ["--model", model]
    argv.append("--dangerously-skip-permissions")
    for d in add_dirs:
        argv += ["--add-dir", str(d)]
    return argv


def session_command(agent: str, *, home: str | Path | None = None,
                    model: str | None = None, add_dirs: tuple | list = (),
                    env_prefix: str = "") -> str:
    """The cmux ``--command`` string for an interactive `agent` session."""
    argv = session_argv(agent, home=home, model=model, add_dirs=add_dirs)
    return f"{env_prefix}{shlex.join(argv)}"


def launch_command(agent: str, home: str | Path | None = None) -> str:
    """The cmux ``--command`` for a dispatched `agent` session."""
    return session_command(agent, home=home)


def binding_is_current(agent: str, command: str | None) -> bool:
    """True when a stored resume command still carries `agent`'s launch flags
    (Droid bindings from before the GLM migration lack them)."""
    if agent != DROID:
        return True
    command = command or ""
    return ("droid-glm-settings.json" in command
            and "--append-system-prompt-file" in command
            and "--auto high" in command)


def resume_argv(agent: str, session_id: str, preserved: list[str] | tuple = (),
                executable: str | None = None,
                home: str | Path | None = None) -> list[str]:
    """argv that resumes `agent` session `session_id`. `preserved` carries the
    original launch flags (session flags already stripped)."""
    base = HOME if home is None else Path(home)
    preserved = list(preserved)
    if agent == DROID:
        drop = {"--settings": 1, "--auto": 1, "--append-system-prompt-file": 1}
        kept, i = [], 0
        while i < len(preserved):
            a = preserved[i]
            if a in drop:
                i += 1 + drop[a]
                continue
            kept.append(a)
            i += 1
        return [executable or DROID, "--resume", session_id, *kept,
                *_droid_flags(base)]
    if agent == COPILOT:
        if "--allow-all" not in preserved and "--yolo" not in preserved:
            preserved.append("--allow-all")
        exe = [executable] if executable else _interactive_exe(COPILOT)
        return [*exe, "--resume", session_id, *preserved]
    return [executable or CLAUDE, "--resume", session_id, *preserved]


def resume_command(agent: str, session_id: str, cwd: str | Path,
                   preserved: list[str] | tuple = (),
                   executable: str | None = None,
                   home: str | Path | None = None) -> str:
    """Shell command that cds into `cwd` and resumes the session."""
    argv = resume_argv(agent, session_id, preserved, executable, home)
    return f"cd {shlex.quote(str(cwd))} && {shlex.join(argv)}"


# Readiness markers seen on the live boot screen via cmux `surface.read_text`.
#   Claude:  the boot banner, or the bypass-permissions status bar.
#   Copilot: the bottom status bar ("· / commands · ? help"), which is hidden
#            while the folder-trust dialog is up.
#   Droid:   the help hint / autonomy status bar / model badge from its banner.
_READY_RE = {
    CLAUDE: re.compile(r"Claude Code v|⏵⏵ bypass permissions on"),
    COPILOT: re.compile(r"/ commands · \? help"),
    DROID: re.compile(
        r"\? for help|allow all commands|Skills \(\d+\)|"
        r"(?:Opus 4\.\d|GLM-?5\.2)"),
}


def ready_re(agent: str) -> "re.Pattern[str]":
    """Compiled regex whose match on the boot screen means the REPL is ready."""
    return _READY_RE.get(agent, _READY_RE[CLAUDE])


# First-launch folder-trust prompt to auto-answer with "1" + Enter. Droid's
# trust UX is not pinned, so it returns None (no auto-answer).
_TRUST_MARKER = {
    CLAUDE: "1. Yes, I trust this folder",
    COPILOT: "Do you trust the files in this folder?",
    DROID: None,
}


def trust_marker(agent: str) -> str | None:
    """Screen substring of the first-launch trust prompt to auto-answer, or
    None when this agent has no known auto-answerable trust gate."""
    return _TRUST_MARKER.get(agent)


# cmux's agent kind for each agent (`cmux hooks <kind>`, resume bindings).
CMUX_KIND = {CLAUDE: "claude", COPILOT: "copilot", DROID: "factory"}
_KIND_TO_AGENT = {"claude": CLAUDE, "copilot": COPILOT,
                  "factory": DROID, "droid": DROID}


def agent_from_kind(value: object) -> str | None:
    """Agent name for a cmux kind / provider / agent string, or None."""
    return _KIND_TO_AGENT.get(str(value or "").strip().lower())


def agent_from_executable(path: object) -> str | None:
    """Agent name for a launch executable path (``/x/bin/copilot`` → copilot)."""
    return _KIND_TO_AGENT.get(os.path.basename(str(path or "")).lower())


# ── headless one-shot calls ───────────────────────────────────────────────────

# A tool name no agent defines: `--available-tools=<this>` leaves Copilot with
# no tools (an empty value does not).
COPILOT_NO_TOOLS = "__assistant_no_tools__"


def droid_list_tools_argv(binary_path: str, model: str) -> list[str]:
    """argv that lists Droid's tool ids as JSON."""
    return [binary_path, "exec", "--model", model, "--list-tools",
            "--output-format", "json"]


def headless_argv(provider: str, *, binary_path: str, model: str,
                  run_dir: Path, disable_tools: bool = False,
                  json_schema: dict | None = None,
                  extra_dirs: list[Path] | None = None,
                  reasoning_effort: str = "high", tag: str = "assistant",
                  disabled_tools: list[str] | None = None) -> list[str]:
    """argv for a one-shot headless call; the prompt goes on stdin."""
    if provider == DROID:
        cmd = [binary_path, "exec", "--model", model,
               "--reasoning-effort", reasoning_effort,
               "--output-format", "json", "--cwd", str(run_dir), "--tag", tag]
        if disabled_tools:
            cmd += ["--disabled-tools", ",".join(disabled_tools)]
        elif not disable_tools:
            cmd += ["--auto", "high"]
        return cmd
    if provider == COPILOT:
        cmd = [binary_path, "--output-format", "json", "--no-ask-user",
               "--no-auto-update", "--no-color"]
        if model:
            cmd += ["--model", model]
        if disable_tools:
            cmd += [f"--available-tools={COPILOT_NO_TOOLS}",
                    "--disable-builtin-mcps", "--no-custom-instructions"]
        else:
            cmd += ["--allow-all", "--add-dir", str(run_dir)]
            for directory in extra_dirs or []:
                cmd += ["--add-dir", str(directory)]
        return cmd
    cmd = [binary_path, "--model", model, "--print", "--output-format", "json"]
    if json_schema is not None:
        cmd += ["--json-schema", json.dumps(json_schema, separators=(",", ":"))]
    if disable_tools:
        cmd += ["--tools", "", "--disable-slash-commands",
                "--mcp-config", '{"mcpServers":{}}', "--strict-mcp-config"]
    else:
        cmd += ["--dangerously-skip-permissions", "--add-dir", str(run_dir)]
        for directory in extra_dirs or []:
            cmd += ["--add-dir", str(directory)]
    return cmd


def headless_prompt(provider: str, prompt: str,
                    json_schema: dict | None = None) -> str:
    """The stdin text for a headless call. Copilot has no --json-schema flag,
    so the schema rides in the prompt."""
    if provider == COPILOT and json_schema is not None:
        return (prompt + "\n\nReply with ONLY one JSON object, no prose and no "
                "code fence, that matches this JSON Schema:\n"
                + json.dumps(json_schema, separators=(",", ":")) + "\n")
    return prompt


def _claude_usage(stdout: str) -> dict | None:
    try:
        doc = json.loads((stdout or "").strip())
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(doc, dict) or not isinstance(doc.get("usage"), dict):
        return None
    usage = doc["usage"]

    def _integer(key: str) -> int:
        value = usage.get(key)
        return int(value) if isinstance(value, (int, float)) else 0

    cost = doc.get("total_cost_usd")
    return {
        "tokens_in": (_integer("input_tokens")
                      + _integer("cache_creation_input_tokens")
                      + _integer("cache_read_input_tokens")),
        "tokens_out": _integer("output_tokens"),
        "cost_usd": float(cost) if isinstance(cost, (int, float)) else None,
        "session_id": doc.get("session_id"),
        "result_text": doc.get("result") if isinstance(doc.get("result"), str)
        else "",
        "_doc": doc,
    }


def _copilot_events(stdout: str) -> list[dict]:
    events = []
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            events.append(obj)
    return events


def _copilot_usage(stdout: str) -> dict | None:
    events = _copilot_events(stdout)
    result = next((e for e in reversed(events) if e.get("type") == "result"), None)
    if result is None:
        return None
    text = ""
    for e in events:
        if e.get("type") == "assistant.message":
            content = (e.get("data") or {}).get("content")
            if isinstance(content, str) and content.strip():
                text = content
    tokens_in = tokens_out = 0
    for e in events:
        if e.get("type") != "session.usage_checkpoint":
            continue
        metrics = (e.get("data") or {}).get("providerModelMetrics") or []
        tokens_in = tokens_out = 0
        for m in metrics if isinstance(metrics, list) else []:
            usage = ((m or {}).get("metrics") or {}).get("usage") or {}
            tokens_in += int(usage.get("inputTokens") or 0)
            tokens_out += int(usage.get("outputTokens") or 0)
    return {
        "tokens_in": tokens_in, "tokens_out": tokens_out, "cost_usd": None,
        "session_id": result.get("sessionId"), "result_text": text,
        "_ok": result.get("exitCode") == 0 and bool(text),
    }


def parse_usage(provider: str, stdout: str) -> dict | None:
    """Token/cost/session/result fields from a headless call's stdout, or None
    when the output is not the agent's JSON envelope."""
    parsed = _copilot_usage(stdout) if provider == COPILOT else _claude_usage(stdout)
    if parsed is None:
        return None
    return {k: v for k, v in parsed.items() if not k.startswith("_")}


def parse_headless(provider: str, stdout: str) -> dict | None:
    """parse_usage, but None unless the envelope reports success."""
    if provider == COPILOT:
        parsed = _copilot_usage(stdout)
        if parsed is None or not parsed["_ok"]:
            return None
        return {k: v for k, v in parsed.items() if not k.startswith("_")}
    parsed = _claude_usage(stdout)
    if parsed is None:
        return None
    doc = parsed["_doc"]
    if doc.get("type") == "result" and (
            doc.get("is_error") is True
            or doc.get("subtype") not in (None, "success")):
        return None
    if provider == DROID and (
            doc.get("type") != "result" or doc.get("subtype") != "success"
            or doc.get("is_error") is not False):
        return None
    return {k: v for k, v in parsed.items() if not k.startswith("_")}


def merges_bedrock(provider: str) -> bool:
    """True when the headless call needs the Bedrock env layered in."""
    return provider == CLAUDE


# ── shell entry point (skills, shell scripts) ─────────────────────────────────

def prompt_submitted(agent: str, cwd: str | Path, needle: str,
                     home: str | Path | None = None) -> bool:
    """True when a user turn in any of `agent`'s transcripts for `cwd`
    contains `needle`."""
    for path in session_transcripts(agent, cwd, home=home):
        try:
            lines = path.read_text(errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            if needle not in line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record_role(rec) != "user":
                continue
            content = record_content(rec)
            if isinstance(content, list):
                content = " ".join(b.get("text", "") for b in content
                                   if isinstance(b, dict))
            if isinstance(content, str) and needle in content:
                return True
    return False


def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys
    ap = argparse.ArgumentParser(
        description="Start or check a coding agent the way Assistant does.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, help_text in (
            ("agent", "print the agent to launch (the user's pick, or --agent)"),
            ("launch-command", "print the cmux --command for a session"),
            ("is-ready", "exit 0 when the screen text on stdin shows a ready REPL"),
            ("trust-marker", "print the folder-trust prompt text, if any"),
            ("submitted", "exit 0 when a user turn contains --needle")):
        cmd = sub.add_parser(name, help=help_text)
        cmd.add_argument("--agent", choices=AGENTS)
        if name == "submitted":
            cmd.add_argument("--cwd", required=True)
            cmd.add_argument("--needle", required=True)
    args = ap.parse_args(argv)
    agent = available_or_claude(getattr(args, "agent", None) or dispatch_agent())
    if args.cmd == "agent":
        print(agent)
    elif args.cmd == "launch-command":
        print(launch_command(agent))
    elif args.cmd == "is-ready":
        return 0 if ready_re(agent).search(sys.stdin.read()) else 1
    elif args.cmd == "trust-marker":
        print(trust_marker(agent) or "")
    else:
        return 0 if prompt_submitted(agent, args.cwd, args.needle) else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
