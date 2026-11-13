"""agent_session — the ONE place that starts or reads a coding agent.

Every Assistant launch of a coding agent goes through this module: interactive
cmux sessions (``session_command``), resumes (``resume_argv``), and headless
one-shot calls (``headless_argv`` + ``parse_headless``, run by
``llm_runner.invoke``). Supported agents: Claude Code (``claude``), GitHub
Copilot CLI (``copilot``), and Factory Droid (``droid``). Which one runs is the
user's choice, read by ``dispatch_agent`` / ``warm_agent`` and
``llm_runner.load_route_config`` from ``~/.assistant/comms/config.json``
(``llm.provider`` is the one knob). ``tests/test_agent_launch_seam.py`` fails
when any other Python, shell, JavaScript, launchd, workflow, or skill file
builds an agent command line.

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
import subprocess
import tempfile
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


def _home(home: str | Path | None) -> Path:
    """`home`, else $HOME read now (so a caller's HOME change is seen)."""
    if home is not None:
        return Path(home)
    return Path(os.environ.get("HOME") or str(Path.home()))


def copilot_home(home: str | Path | None = None) -> Path:
    """Copilot's config and state dir: $COPILOT_HOME, else <home>/.copilot."""
    env = (os.environ.get("COPILOT_HOME") or "").strip()
    if env:
        return Path(env).expanduser()
    return _home(home) / ".copilot"


def transcript_root(agent: str, home: str | Path | None = None) -> Path:
    """Root dir under which `agent` writes its session transcripts."""
    if agent == COPILOT:
        return copilot_home(home) / "session-state"
    base = _home(home)
    return base / _ROOT_SUBDIR.get(agent, _ROOT_SUBDIR[CLAUDE])


def transcript_roots(agents: tuple[str, ...] = SLUG_AGENTS) -> list[tuple[str, Path]]:
    """[(agent, root)] for each requested agent whose root exists on disk."""
    return [(a, transcript_root(a)) for a in agents if transcript_root(a).is_dir()]


def copilot_session_cwd(session_dir: str | Path) -> str | None:
    """The `cwd:` line of a Copilot <session-id>/workspace.yaml, or None."""
    return copilot_session_field(session_dir, "cwd")


def copilot_session_field(session_dir: str | Path, field: str) -> str | None:
    """One top-level `field:` value of a Copilot <session-id>/workspace.yaml
    (quotes removed), or None."""
    try:
        text = (Path(session_dir) / "workspace.yaml").read_text()
    except OSError:
        return None
    m = re.search(rf"^{re.escape(field)}:\s*(.+?)\s*$", text, re.M)
    if not m:
        return None
    value = m.group(1)
    if value in ("|", "|-", ">", ">-"):
        block = re.match(r"((?:[ \t]+.*\n?)+)", text[m.end() + 1:])
        lines = [line.strip() for line in (block.group(1) if block else "").splitlines()]
        return (" " if value.startswith(">") else "\n").join(lines).strip() or None
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        value = value[1:-1]
    return value


def copilot_transcripts(cwd: str | Path, home: str | Path | None = None,
                        since: float | None = None) -> list[Path]:
    """Copilot events.jsonl transcripts whose session cwd is `cwd`. `since`
    (epoch seconds) skips sessions last written before it without reading
    their workspace.yaml."""
    root = transcript_root(COPILOT, home=home)
    want = os.path.realpath(str(cwd))
    found = []
    try:
        dirs = list(root.iterdir())
    except OSError:
        return []
    for d in dirs:
        events = d / "events.jsonl"
        if since is not None:
            try:
                if events.stat().st_mtime < since:
                    continue
            except OSError:
                continue
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
                        home: str | Path | None = None,
                        since: float | None = None) -> list[Path]:
    """Every transcript `agent` has written for sessions started in `cwd`
    (for Copilot, only those written since `since` when given)."""
    if agent == COPILOT:
        return copilot_transcripts(cwd, home=home, since=since)
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


def message_role(obj: object) -> str | None:
    """record_role, or the raw message.role of any record carrying one
    (Claude's notices such as "Tool loaded." ride on a non-turn record)."""
    msg = obj.get("message") if isinstance(obj, dict) else None
    if isinstance(msg, dict) and msg.get("role"):
        return msg.get("role")
    return record_role(obj)


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


def record_text(obj: object) -> str:
    """The text of a transcript turn of any agent's schema: a string content
    as is, or its text blocks joined by newlines ('' for none)."""
    content = record_content(obj)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content
                         if isinstance(b, dict) and b.get("type") == "text")
    return ""


# ── binaries ──────────────────────────────────────────────────────────────────

_BIN_ENV = {CLAUDE: "CLAUDE_BIN", COPILOT: "COPILOT_BIN", DROID: "DROID_BIN"}


def _bin_candidates(agent: str, home: Path) -> list[Path]:
    cands = [home / ".local" / "bin" / agent,
             Path("/opt/homebrew/bin") / agent,
             Path("/usr/local/bin") / agent]
    if agent == COPILOT:
        nvm = home / ".nvm" / "versions" / "node"
        try:
            versions = sorted(nvm.iterdir(), reverse=True, key=lambda v: [
                int(n) if n.isdigit() else -1
                for n in re.findall(r"\d+|[^\d.]+", v.name.lstrip("v"))])
        except OSError:
            versions = []
        cands += [v / "bin" / COPILOT for v in versions]
    return cands


def _is_node_script(path: str) -> bool:
    try:
        with open(path, "rb") as fh:
            first = fh.readline(200)
    except OSError:
        return False
    return first.startswith(b"#!") and b"node" in first


def _copilot_native(path: str) -> str:
    """The standalone Copilot binary next to the npm `copilot` loader script,
    which needs `node` on PATH (launchd jobs don't have it). `path` itself
    when it isn't the loader or no standalone build sits beside it."""
    if not _is_node_script(path):
        return path
    pkg = Path(os.path.realpath(path)).parent
    for native in sorted(pkg.glob("node_modules/@github/copilot-*/copilot")):
        if _executable(str(native)):
            return str(native)
    return path


def binary(agent: str, env: dict | None = None) -> str:
    """Absolute path of `agent`'s CLI. Order: the agent's *_BIN env var, PATH,
    the common install dirs (launchd's PATH is minimal), else the bare name.
    For Copilot, the npm loader script resolves to its standalone build."""
    src = os.environ if env is None else env
    override = (src.get(_BIN_ENV.get(agent, "")) or "").strip()
    if override:
        return str(Path(override).expanduser())
    home = Path(src.get("HOME") or str(Path.home()))
    if agent == CLAUDE:
        return str(home / ".local" / "bin" / CLAUDE)
    if agent == DROID and _executable(str(home / ".local" / "bin" / DROID)):
        return str(home / ".local" / "bin" / DROID)
    found = shutil.which(agent)
    if not found:
        found = next((str(c) for c in _bin_candidates(agent, home)
                      if _executable(str(c))), None)
    if not found:
        return str(home / ".local" / "bin" / DROID) if agent == DROID else agent
    return _copilot_native(found) if agent == COPILOT else found


def runs(path: str, timeout: float = 20.0) -> bool:
    """True when `path --version` exits 0."""
    try:
        return subprocess.run([path, "--version"], capture_output=True,
                              timeout=timeout).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def bin_env_var(agent: str) -> str:
    """The env var that overrides `agent`'s binary path (CLAUDE_BIN, ...)."""
    return _BIN_ENV[agent]


def context_tokens(record: object) -> int | None:
    """Live context size from a Claude or Droid transcript turn's usage block
    (input plus cache tokens), or None when the turn has none."""
    msg = record.get("message") if isinstance(record, dict) else None
    usage = msg.get("usage") if isinstance(msg, dict) else None
    if not isinstance(usage, dict):
        return None
    return (int(usage.get("input_tokens") or 0)
            + int(usage.get("cache_creation_input_tokens") or 0)
            + int(usage.get("cache_read_input_tokens") or 0))


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


def warm_session_agent() -> str:
    """The agent the Slack session runs: warm_agent(), except a picked Copilot
    that can't start runs as claude. Droid isn't checked here, as before
    Copilot: its pane resolves `droid` through the login shell."""
    agent = warm_agent()
    if agent == COPILOT and not agent_available(COPILOT):
        return CLAUDE
    return agent


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
    `claude` alias, so it is assumed present. Copilot needs a binary that
    answers `--version`. Droid needs an executable binary plus the --settings
    JSON that session_command(DROID) bakes in. A False makes callers fall back
    to claude."""
    if agent == CLAUDE:
        return True
    home = Path(os.environ.get("HOME", str(Path.home())))
    if agent == COPILOT:
        found = binary(COPILOT)
        return _executable(found) and runs(found)
    if shutil.which(agent) is None and not _executable(binary(agent)):
        return False
    settings = home / ".assistant" / "droid-glm-settings.json"
    try:
        parsed = json.loads(settings.read_text())
    except (OSError, ValueError):
        return False
    return isinstance(parsed, dict)


def available_or_claude(agent: str) -> str:
    """`agent` when it can launch, else claude."""
    return agent if agent_available(agent) else CLAUDE


def copilot_chosen() -> bool:
    """True when the user picked Copilot for dispatch, comms, or any feature."""
    if COPILOT in (dispatch_agent(), warm_agent()):
        return True
    raw = _read_comms_config()
    features = _cfg(raw, "llm", "features")
    names = list(features) if isinstance(features, dict) else []
    return any(_cfg_agent(raw, "llm", "features", f, "provider") == COPILOT
               for f in names)


CMUX_CLI = "/Applications/cmux.app/Contents/Resources/bin/cmux"


def copilot_hooks_file(home: str | Path | None = None) -> Path:
    """Where Assistant keeps cmux's Copilot hooks: Copilot's hooks folder."""
    return copilot_home(home) / "hooks" / "cmux.json"


def copilot_hooks_installed(home: str | Path | None = None) -> bool:
    """True when cmux's Copilot hooks are in Copilot's hooks folder or its
    config.json."""
    for path in (copilot_hooks_file(home), copilot_home(home) / "config.json"):
        try:
            if "hooks enqueue copilot" in path.read_text():
                return True
        except OSError:
            continue
    return False


def install_copilot_hooks(cmux: str | None = None,
                          home: str | Path | None = None) -> str | None:
    """Write cmux's Copilot hooks to Copilot's hooks folder. None on success,
    else why not.

    `cmux hooks copilot install` edits config.json, which Copilot 1.0.109
    manages itself (it starts with // comments, so cmux refuses it). cmux
    writes into a scratch Copilot home instead, and its hooks are rewritten
    in the hooks-folder format: {"version": 1, "hooks": {event: [{"type":
    "command", "bash": ..., "timeoutSec": ...}]}}."""
    cmux = cmux or os.environ.get("CMUX_BIN") or CMUX_CLI
    with tempfile.TemporaryDirectory() as scratch:
        (Path(scratch) / "config.json").write_text("{}")
        try:
            done = subprocess.run(
                [cmux, "hooks", "copilot", "install", "--yes"],
                capture_output=True, text=True, timeout=30,
                env={**os.environ, "COPILOT_HOME": scratch})
        except (OSError, subprocess.SubprocessError) as exc:
            return str(exc)
        if done.returncode != 0:
            return (done.stderr or done.stdout).strip()[:300] or f"exit {done.returncode}"
        try:
            generated = json.loads((Path(scratch) / "config.json").read_text())
        except (OSError, ValueError) as exc:
            return f"cmux wrote unreadable hooks: {exc}"
    events = generated.get("hooks") if isinstance(generated, dict) else None
    if not isinstance(events, dict) or not events:
        return "cmux wrote no Copilot hooks"
    hooks = {}
    for event, blocks in events.items():
        hooks[event] = [
            {"type": "command", "bash": h["command"],
             "timeoutSec": max(1, int(h.get("timeout", 5000)) // 1000)}
            for block in blocks for h in block.get("hooks", [])
            if isinstance(h, dict) and h.get("command")]
    target = copilot_hooks_file(home)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"version": 1, "hooks": hooks}, indent=2) + "\n")
    return None


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
    ``copilot --allow-all`` plus the configured model, and only the add_dirs
    that exist (Copilot refuses to start on a missing one). droid carries its
    settings file, --auto high, and the lessons file."""
    base = _home(home)
    if agent == DROID:
        return [DROID, *_droid_flags(base)]
    if agent == COPILOT:
        argv = [*_interactive_exe(COPILOT), "--allow-all", "--no-ask-user"]
        chosen = copilot_model() if model is None else model
        if chosen:
            argv += ["--model", chosen]
        for d in add_dirs:
            if Path(d).is_dir():
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


def copilot_instructions_dir(home: str | Path | None = None) -> Path | None:
    """A folder Copilot reads as custom instructions, holding a link to
    ~/.claude/CLAUDE.md, so Copilot sessions get the user's rules (Copilot
    reads a CLAUDE.md only in the repo root or cwd). None without a CLAUDE.md.
    Copilot loads a linked `.github/instructions/*.instructions.md` from a
    COPILOT_CUSTOM_INSTRUCTIONS_DIRS folder; a top-level AGENTS.md there only
    counts as a nested source."""
    base = _home(home)
    rules = base / ".claude" / "CLAUDE.md"
    if not rules.is_file():
        return None
    folder = base / ".assistant" / "copilot-instructions"
    link = folder / ".github" / "instructions" / "lessons.instructions.md"
    if not (link.is_symlink() and os.readlink(link) == str(rules)):
        link.parent.mkdir(parents=True, exist_ok=True)
        if link.is_symlink() or link.exists():
            link.unlink()
        link.symlink_to(rules)
    return folder


def _copilot_env(home: str | Path | None) -> str:
    folder = copilot_instructions_dir(home)
    return f"COPILOT_CUSTOM_INSTRUCTIONS_DIRS={shlex.quote(str(folder))} " if folder else ""


_COPILOT_EXE_RE = re.compile(r"(^|&&\s*)((?:command\s+)?'?(?:[^'\s]*/)?copilot'?(?=\s|$))")


def with_copilot_rules(command: str | None, home: str | Path | None = None) -> str | None:
    """`command` with the rules folder set in front of its `copilot` launch.
    cmux saves a Copilot resume command without COPILOT_CUSTOM_INSTRUCTIONS_DIRS;
    anything else (Claude, Droid, an already-set command) comes back as is."""
    if not command or not _COPILOT_EXE_RE.search(command):
        return command
    env = _copilot_env(home)
    if not env:
        return command
    return _COPILOT_EXE_RE.sub(lambda m: m.group(1) + env + m.group(2), command, count=1)


def session_command(agent: str, *, home: str | Path | None = None,
                    model: str | None = None, add_dirs: tuple | list = (),
                    env_prefix: str = "") -> str:
    """The cmux ``--command`` string for an interactive `agent` session.
    Copilot gets the user's rules through COPILOT_CUSTOM_INSTRUCTIONS_DIRS."""
    argv = session_argv(agent, home=home, model=model, add_dirs=add_dirs)
    if agent == COPILOT:
        env_prefix += _copilot_env(home)
    return f"{env_prefix}{shlex.join(argv)}"


def launch_command(agent: str, home: str | Path | None = None,
                   unattended: bool = False) -> str:
    """The cmux ``--command`` for a dispatched `agent` session. `unattended`
    claude runs the alias in a login shell with permission prompts bypassed,
    so a background spawn never stalls on the first prompt."""
    if agent == CLAUDE and unattended:
        return "zsh -lic 'claude --dangerously-skip-permissions'"
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
    base = _home(home)
    preserved = list(preserved)
    if agent == DROID:
        drop = ("--settings", "--auto", "--append-system-prompt-file")
        kept, i = [], 0
        while i < len(preserved):
            a = preserved[i]
            if a in drop:
                i += 2
                continue
            if a.startswith(tuple(f"{flag}=" for flag in drop)):
                i += 1
                continue
            kept.append(a)
            i += 1
        return [executable or DROID, "--resume", session_id, *kept,
                *_droid_flags(base)]
    if agent == COPILOT:
        exe = [executable] if executable else _interactive_exe(COPILOT)
        return [*exe, "--resume", session_id, *preserved]
    return [executable or CLAUDE, "--resume", session_id, *preserved]


def resume_command(agent: str, session_id: str, cwd: str | Path,
                   preserved: list[str] | tuple = (),
                   executable: str | None = None,
                   home: str | Path | None = None) -> str:
    """Shell command that cds into `cwd` and resumes the session."""
    argv = resume_argv(agent, session_id, preserved, executable, home)
    env = _copilot_env(home) if agent == COPILOT else ""
    return f"cd {shlex.quote(str(cwd))} && {env}{shlex.join(argv)}"


# Readiness markers seen on the live boot screen via cmux `surface.read_text`.
#   Claude:  the boot banner, or the bypass-permissions status bar.
#   Copilot: the bottom status bar ("← open sidebar · Interactive · Allow All ·
#            / commands · ? help"), hidden while the folder-trust dialog is up.
#            A narrow pane cuts its right end, so its left end matches too.
#   Droid:   the help hint / autonomy status bar / model badge from its banner.
_READY_RE = {
    CLAUDE: re.compile(r"Claude Code v|⏵⏵ bypass permissions on"),
    COPILOT: re.compile(r"/ commands · \? help|← open sidebar · "),
    DROID: re.compile(
        r"\? for help|allow all commands|Skills \(\d+\)|"
        r"(?:Opus 4\.\d|GLM-?5\.2)"),
}


_COPILOT_BAR_RE = re.compile(
    r"^\s*← open sidebar · |· / commands · \? help(?: ·|\s*$)"
    r"|^\s*(?:Interactive|Plan|Autopilot) · (?:Allow All|Ask) · "
    r"|^\s*[○◉◎●] \w+.*ctrl\+c interrupt"
    r"|^\s*[○◉◎●] Retrying model request")
_COPILOT_BANNER_RE = re.compile(r"^│\s+\S+\s+Copilot v\d+\.\d+\.\d+ uses AI\.", re.M)


def shows_copilot(screen: str) -> bool:
    """True when `screen` is a Copilot pane: its status bar (idle, typing, or
    working) is one of the last three non-blank lines, or its boxed boot
    banner and its trust dialog are on screen (the dialog hides the bar). Text a Claude pane prints about Copilot doesn't
    match: it isn't the bottom line or inside the banner box."""
    bottom = [line for line in screen.splitlines() if line.strip()][-3:]
    return (any(_COPILOT_BAR_RE.search(line) for line in bottom)
            or (bool(_COPILOT_BANNER_RE.search(screen))
                and _TRUST_MARKER[COPILOT] in screen))


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


# What to type at the trust prompt. Copilot's "1" trusts the folder for one
# session only, so a later resume would stop at the prompt again; "2" is
# "Yes, and remember this folder", like Claude's lasting "1".
_TRUST_ANSWER = {CLAUDE: "1", COPILOT: "2"}


def trust_answer(agent: str) -> str:
    """The key that answers `agent`'s trust prompt with a lasting yes."""
    return _TRUST_ANSWER.get(agent, "1")


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


_TRANSCRIPT_MARKERS = (("/.factory/sessions", DROID),
                       ("/.copilot/session-state/", COPILOT),
                       ("/.claude/", CLAUDE))


def agent_from_transcript(path: object) -> str | None:
    """Agent whose transcript `path` is: under that agent's transcript root
    (Copilot's follows $COPILOT_HOME), or a path naming its default root."""
    text = str(path or "").replace("\\", "/")
    if not text:
        return None
    for agent in (DROID, COPILOT, CLAUDE):
        if text.startswith(str(transcript_root(agent)).rstrip("/") + "/"):
            return agent
    return next((agent for marker, agent in _TRANSCRIPT_MARKERS if marker in text), None)


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
               "--no-auto-update", "--no-color", "-C", str(run_dir)]
        if model:
            cmd += ["--model", model]
        if reasoning_effort:
            cmd += ["--reasoning-effort", {"off": "low"}.get(reasoning_effort, reasoning_effort)]
        if disable_tools:
            cmd += [f"--available-tools={COPILOT_NO_TOOLS}",
                    "--disable-builtin-mcps", "--no-custom-instructions"]
        else:
            cmd += ["--allow-all", "--add-dir", str(run_dir)]
            for directory in extra_dirs or []:
                if Path(directory).is_dir():
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


def refused_reasoning_effort(provider: str, rc: int, stdout: str, stderr: str) -> bool:
    """True when Copilot failed because its model takes no --reasoning-effort
    (`auto`, or a model without that setting)."""
    return (provider == COPILOT and rc != 0
            and "reasoning effort" in f"{stdout}\n{stderr}".lower())


def without_reasoning_effort(cmd: list[str]) -> list[str]:
    """`cmd` minus its `--reasoning-effort <level>` pair."""
    if "--reasoning-effort" not in cmd:
        return cmd
    i = cmd.index("--reasoning-effort")
    return cmd[:i] + cmd[i + 2:]


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
    if result is None or not any("." in str(e.get("type", "")) for e in events):
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


def headless_errors(provider: str, stdout: str) -> str:
    """Copilot's session.error messages from a headless call's stdout, one per
    line ('' for other agents or a clean run)."""
    if provider != COPILOT:
        return ""
    out = []
    for e in _copilot_events(stdout):
        if e.get("type") == "session.error":
            data = e.get("data")
            msg = data.get("message") if isinstance(data, dict) else None
            out.append(str(msg or json.dumps(data)))
    return "\n".join(out)


def copilot_context_fraction(transcript: str | Path,
                             tail_bytes: int = 2_000_000) -> float | None:
    """How full a Copilot session's context is: the last session.usage_record's
    inputTokens over its maxPromptTokens, or None when none is recorded."""
    try:
        with open(transcript, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - tail_bytes))
            lines = fh.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        if '"session.usage_record"' not in line:
            continue
        try:
            usage = json.loads(line)["data"]["usage"]
            return int(usage["inputTokens"]) / int(usage["maxPromptTokens"])
        except (ValueError, KeyError, TypeError, ZeroDivisionError):
            continue
    return None


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
                     home: str | Path | None = None,
                     since: float | None = None) -> bool:
    """True when a user turn in any of `agent`'s transcripts for `cwd`
    contains `needle`. `since` (epoch seconds) skips transcripts last written
    before it."""
    for path in session_transcripts(agent, cwd, home=home, since=since):
        try:
            if since is not None and path.stat().st_mtime < since:
                continue
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
            ("agent", "print --agent, or the user's pick; exit 1 when that "
                      "agent isn't installed"),
            ("launch-command", "print the cmux --command for a session"),
            ("is-ready", "exit 0 when the screen text on stdin shows a ready REPL"),
            ("trust-marker", "print the folder-trust prompt text, if any"),
            ("trust-answer", "print the key that answers the trust prompt"),
            ("submitted", "exit 0 when a user turn contains --needle"),
            ("copilot-chosen", "exit 0 when the user picked Copilot anywhere"),
            ("install-copilot-hooks", "write cmux's Copilot hooks to Copilot's "
                                      "hooks folder")):
        cmd = sub.add_parser(name, help=help_text)
        cmd.add_argument("--agent", choices=AGENTS)
        if name == "submitted":
            cmd.add_argument("--cwd", required=True)
            cmd.add_argument("--needle", required=True)
            cmd.add_argument("--since", type=float,
                             help="skip transcripts last written before this "
                                  "epoch time")
        if name == "launch-command":
            cmd.add_argument("--unattended", action="store_true",
                             help="claude: bypass permission prompts")
    args = ap.parse_args(argv)
    if args.cmd == "copilot-chosen":
        return 0 if copilot_chosen() else 1
    if args.cmd == "install-copilot-hooks":
        failed = install_copilot_hooks()
        if failed:
            print(f"cmux Copilot hooks not installed: {failed}", file=sys.stderr)
            return 1
        print(f"cmux Copilot hooks installed at {copilot_hooks_file()}")
        return 0
    if args.cmd == "agent":
        agent = args.agent or dispatch_agent()
        if not agent_available(agent):
            print(f"{agent} isn't installed or doesn't start", file=sys.stderr)
            return 1
        print(agent)
        return 0
    agent = args.agent or available_or_claude(dispatch_agent())
    if args.cmd == "launch-command":
        print(launch_command(agent, unattended=args.unattended))
    elif args.cmd == "is-ready":
        return 0 if ready_re(agent).search(sys.stdin.read()) else 1
    elif args.cmd == "trust-marker":
        print(trust_marker(agent) or "")
    elif args.cmd == "trust-answer":
        print(trust_answer(agent))
    else:
        return 0 if prompt_submitted(agent, args.cwd, args.needle,
                                     since=args.since) else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
