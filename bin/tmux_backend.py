#!/usr/bin/env python3
"""tmux_backend — tmux answering the commands Assistant sends to cmux.

Assistant keeps its agent sessions in cmux. In Pro mode it can keep them in tmux instead
(src/assistant/session_backend.py decides), and then every script that would run `cmux <command>` runs this file
with the same arguments. It answers from tmux in the shapes cmux uses, so no caller needs a second code
path: the commands below are the whole of what Assistant asks of a session tool.

    ping                                   tree [--workspace W] [--json]
    list-windows --json                    list-workspaces | workspace list [--json] [--window W]
    list-panes --workspace W               list-pane-surfaces --workspace W [--pane P]
    read-screen --workspace W [--surface S] [--lines N] [--scrollback]
    new-workspace --cwd D --name T --command C
    close-workspace --workspace W          select-workspace --workspace W
    send --workspace W TEXT                send-key --workspace W KEY
    sessions list --json [--session ID]    surface resume show --json --surface S --workspace W
    rpc workspace.list | surface.read_text | surface.send_text | surface.send_key  JSON
    identify [--workspace W]               focus-window --window W
    events                                 (agent turn-end and needs-input events, one JSON object a line)

How tmux's objects stand in for cmux's:

    cmux window     is a tmux session    window:N     is session $(N-1)
    cmux workspace  is a tmux window     workspace:N  is window  @(N-1)
    cmux pane       is a tmux pane       pane:N       is pane    %(N-1)
    cmux surface    is the pane's terminal             surface:N is pane %(N-1)

Refs count from 1, as cmux's do; tmux counts from 0, and a workspace numbered 0 reads as "no workspace" in
places. tmux has no UUIDs, so each object's is made from the tmux server (its socket, process id and start
time) and the object's tmux id: the same object always gets the same UUID, and a restarted server's objects
get new ones, as cmux's do.

Which agent session runs in a pane is asked of the agent, never guessed from a folder or a file's age:
Claude Code's own `claude agents --json` names the process and session id of each live session, and a
session belongs to the pane whose terminal that process is on. Copilot and Droid do not offer that, so their
sessions run and can be typed into here but are not identified yet.

Every command talks to the user's default tmux server, whatever tmux the caller itself may be running in, so
the background jobs and a script run by hand see the same sessions.

This file is run through its `#!` line, which on a Mac without Homebrew's Python first on PATH is the
system's Python 3.9. It must keep working there.
"""
from __future__ import annotations

import base64
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_session  # noqa: E402

TIMEOUT_SEC = 10
# Where tmux is installed when the caller's PATH is launchd's or systemd's short one.
TMUX_DIRS = ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/home/linuxbrew/.linuxbrew/bin")
# A new session's size until someone attaches to it. tmux's own default is 80x24, which wraps an agent's
# status line; readers of the screen look for that line whole.
NEW_SESSION_SIZE = ("200", "50")
EVENTS_POLL_SEC = 2.0

_SEP = "\x1f"
_FIELDS = ("socket_path", "pid", "start_time", "session_id", "session_name", "window_id", "window_index",
           "window_name", "window_active", "automatic-rename", "pane_id", "pane_index", "pane_active",
           "pane_tty", "pane_current_path", "pane_title")
_FORMAT = _SEP.join("#{%s}" % name for name in _FIELDS)
_NAMESPACE = uuid.UUID("6f1d3c52-5a0e-4c53-9d0b-0a5b1f3d7c11")
_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$")
# cmux's key names, and what tmux calls each. A name tmux doesn't know is typed as text, so a key that
# isn't listed here is refused instead of passed on.
_KEYS = {"enter": "Enter", "return": "Enter", "escape": "Escape", "esc": "Escape", "tab": "Tab",
         "shift+tab": "BTab", "backspace": "BSpace", "delete": "DC", "space": "Space", "up": "Up",
         "down": "Down", "left": "Left", "right": "Right", "home": "Home", "end": "End",
         "pageup": "PageUp", "pagedown": "PageDown"}
_VALUE_FLAGS = ("--workspace", "--surface", "--window", "--pane", "--lines", "--cwd", "--name", "--command",
                "--focus", "--id-format", "--session", "--category", "--description", "--after", "--cursor-file",
                "--limit")
_PLAIN_FLAGS = ("--json", "--all", "--scrollback", "--force", "--reconnect", "--no-heartbeat", "--no-ack")
# The most text handed to tmux in one command; tmux refuses a command longer than about 16 KB.
_CHUNK = 2000
_NO_WORKSPACE = ("invalid_params", "Missing or invalid workspace_id")
_NO_SURFACE = ("not_found", "Surface not found for the given surface_id")


class Failure(Exception):
    """A command that can't be answered: printed as cmux prints its own, `Error: <code>: <message>`."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")


# ── tmux ──────────────────────────────────────────────────────────────────────

def tmux_bin() -> str:
    """tmux's path: $ASSISTANT_TMUX_BIN, else PATH, else where package managers put it."""
    override = (os.environ.get("ASSISTANT_TMUX_BIN") or "").strip()
    if override:
        return override
    found = shutil.which("tmux")
    if found:
        return found
    for folder in TMUX_DIRS:
        if os.access(os.path.join(folder, "tmux"), os.X_OK):
            return os.path.join(folder, "tmux")
    return "tmux"


def _tmux(*args: str) -> tuple:
    """(exit status, stdout, stderr) of one tmux command on the default server."""
    env = {name: value for name, value in os.environ.items() if name not in ("TMUX", "TMUX_PANE")}
    try:
        done = subprocess.run([tmux_bin(), *args], capture_output=True, text=True, timeout=TIMEOUT_SEC, env=env)
    except subprocess.TimeoutExpired:
        raise Failure("timeout", f"tmux didn't answer in {TIMEOUT_SEC}s") from None
    except OSError as exc:
        raise Failure("unavailable", f"tmux can't be run ({exc})") from None
    return done.returncode, done.stdout, done.stderr


def _must(*args: str) -> str:
    rc, out, err = _tmux(*args)
    if rc != 0:
        raise Failure("tmux_error", err.strip() or f"tmux {args[0]} exit {rc}")
    return out


def _rows() -> list:
    """One dict per pane of every session. No server running is no panes, not a failure."""
    rc, out, err = _tmux("list-panes", "-a", "-F", _FORMAT)
    if rc != 0:
        # No socket, or a socket nothing listens on: no server, so no sessions. Anything else (a socket that
        # can't be opened, a server that answered with an error) is not knowing, which is not the same thing.
        if "no server running" in err or ("error connecting" in err and (
                "No such file or directory" in err or "Connection refused" in err)):
            return []
        raise Failure("unavailable", err.strip() or f"tmux list-panes exit {rc}")
    rows = []
    for line in out.splitlines():
        values = line.split(_SEP)
        if len(values) == len(_FIELDS):
            rows.append(dict(zip(_FIELDS, values)))
    return rows


def _ref(kind: str, tmux_id: str) -> str:
    return f"{kind}:{int(tmux_id[1:]) + 1}"


def _uid(row: dict, name: str) -> str:
    server = "\n".join((row["socket_path"], row["pid"], row["start_time"]))
    return str(uuid.uuid5(_NAMESPACE, f"{server}\n{name}")).upper()


def _title(window_rows: list) -> str:
    """A workspace's title: the window's name when someone gave it one, else what the program in its
    active pane set as the terminal title (an agent's summary of its work), else the window's name."""
    first = window_rows[0]
    if first["automatic-rename"] == "0":
        return first["window_name"]
    active = next((row for row in window_rows if row["pane_active"] == "1"), first)
    title = active["pane_title"].strip()
    host = socket.gethostname()
    return title if title and title not in (host, host.split(".")[0]) else first["window_name"]


def tree() -> dict:
    """Every tmux session, window and pane, as cmux's `tree --all --json` document."""
    sessions: dict = {}
    for row in _rows():
        sessions.setdefault(row["session_id"], {}).setdefault(row["window_id"], []).append(row)
    windows = []
    for index, session_id in enumerate(sorted(sessions, key=lambda value: int(value[1:]))):
        by_window = sessions[session_id]
        ordered = sorted(by_window.values(), key=lambda rows: int(rows[0]["window_index"]))
        workspaces = [_workspace_doc(rows, position) for position, rows in enumerate(ordered)]
        first = ordered[0][0]
        selected = next((ws for ws in workspaces if ws["selected"]), workspaces[0])
        windows.append({
            "id": _uid(first, session_id), "ref": _ref("window", session_id), "index": index,
            "title": first["session_name"], "selected_workspace_id": selected["id"],
            "selected_workspace_ref": selected["ref"], "workspace_count": len(workspaces),
            "workspaces": workspaces,
        })
    return {"windows": windows}


def _workspace_doc(rows: list, index: int) -> dict:
    rows = sorted(rows, key=lambda row: int(row["pane_index"]))
    first = rows[0]
    active = next((row for row in rows if row["pane_active"] == "1"), first)
    panes = []
    for position, row in enumerate(rows):
        pane_id = row["pane_id"]
        focused = row is active
        surface = {
            "id": _uid(row, pane_id), "ref": _ref("surface", pane_id), "index": position, "index_in_pane": 0,
            "pane_id": _uid(row, "pane" + pane_id), "pane_ref": _ref("pane", pane_id),
            "title": row["pane_title"], "tty": row["pane_tty"].replace("/dev/", "", 1), "type": "terminal",
            "url": None, "selected": True, "selected_in_pane": True, "focused": focused, "active": focused,
            "here": False, "tmux_pane": pane_id, "socket": row["socket_path"],
        }
        panes.append({
            "id": surface["pane_id"], "ref": surface["pane_ref"], "index": position, "focused": focused,
            "active": focused, "surface_count": 1, "surface_ids": [surface["id"]],
            "surface_refs": [surface["ref"]], "selected_surface_id": surface["id"],
            "selected_surface_ref": surface["ref"], "surfaces": [surface],
        })
    selected = first["window_active"] == "1"
    return {
        "id": _uid(first, first["window_id"]), "ref": _ref("workspace", first["window_id"]), "index": index,
        "title": _title(rows), "selected": selected, "active": selected, "pinned": False, "description": None,
        "current_directory": active["pane_current_path"], "tmux_window": first["window_id"],
        "tmux_session": first["session_name"], "panes": panes,
    }


def _workspaces(doc: dict) -> list:
    return [ws for window in doc["windows"] for ws in window["workspaces"]]


def _surfaces(ws: dict) -> list:
    return [surface for pane in ws["panes"] for surface in pane["surfaces"]]


def _matches(item: dict, target) -> bool:
    target = str(target or "")
    return target == item["ref"] or (bool(_UUID_RE.match(target)) and target.upper() == item["id"])


def find_workspace(doc: dict, target) -> dict:
    for ws in _workspaces(doc):
        if _matches(ws, target):
            return ws
    raise Failure(*_NO_WORKSPACE)


def find_surface(doc: dict, target, workspace=None) -> tuple:
    """(workspace, surface) for a surface ref or UUID; with no `target`, the workspace's focused pane.
    Naming neither is refused: there is no "current" pane to fall back on, and typing must never guess one."""
    if not target and not workspace:
        raise Failure(*_NO_SURFACE)
    scope = [find_workspace(doc, workspace)] if workspace else _workspaces(doc)
    for ws in scope:
        for surface in _surfaces(ws):
            if _matches(surface, target) if target else surface["focused"]:
                return ws, surface
    raise Failure(*_NO_SURFACE)


# ── reading and typing ────────────────────────────────────────────────────────

def read_text(surface: dict, lines=None, scrollback: bool = False) -> str:
    """The pane's screen as text, without the blank rows below the last line written. `lines` keeps the
    last that many, reaching into the scrollback; `scrollback` alone reads all of it."""
    try:
        count = int(lines) if lines not in (None, "", False) else 0
    except (TypeError, ValueError):
        raise Failure("invalid_params", "lines must be a number") from None
    args = ["capture-pane", "-p", "-t", surface["tmux_pane"]]
    if count > 0:
        args += ["-S", f"-{count}"]
    elif scrollback:
        args += ["-S", "-"]
    text = _must(*args).rstrip("\n")
    return "\n".join(text.splitlines()[-count:]) if count > 0 else text


def send_text(surface: dict, text: str) -> None:
    for start in range(0, len(text), _CHUNK):
        _must("send-keys", "-t", surface["tmux_pane"], "-l", "--", text[start:start + _CHUNK])


def send_key(surface: dict, key: str) -> None:
    name = str(key or "").strip().lower()
    tmux_key = _KEYS.get(name)
    if tmux_key is None:
        held = re.match(r"^(ctrl|control)[+-]([a-z])$", name)
        if not held:
            raise Failure("invalid_params", f"Unknown key {key!r}")
        tmux_key = f"C-{held.group(2)}"
    _must("send-keys", "-t", surface["tmux_pane"], tmux_key)


def _where(ws: dict, surface: dict) -> dict:
    return {"surface_id": surface["id"], "surface_ref": surface["ref"],
            "workspace_id": ws["id"], "workspace_ref": ws["ref"]}


# ── workspaces ────────────────────────────────────────────────────────────────

def _session_name(title: str) -> str:
    """A tmux session name for a new workspace: the title in characters tmux and a shell both take
    plainly, made unique among the live sessions."""
    base = re.sub(r"[^A-Za-z0-9_-]+", "-", title).strip("-")[:40] or "assistant"
    rc, out, _ = _tmux("list-sessions", "-F", "#{session_name}")
    taken = set(out.splitlines()) if rc == 0 else set()
    name, count = base, 1
    while name in taken:
        count += 1
        name = f"{base}-{count}"
    return name


def _plain(text: str) -> str:
    """`text` as tmux takes it literally: tmux expands `#{…}` in a window's name and in a folder."""
    return text.replace("#", "##")


def _show(target: str, variable: str) -> str:
    return _must("display-message", "-p", "-t", target, "#{%s}" % variable).rstrip("\n")


def _settle_name(window_id: str, title: str) -> None:
    """Make the window's name exactly `title` on a tmux that doesn't expand names. Such a tmux kept the doubled
    #s it was given. The plain title is handed over only once a command has shown it expands nothing."""
    doubled = _plain(title)
    if _show(window_id, "window_name") == doubled:
        _must("rename-window", "-t", window_id, "--", doubled)
        if _show(window_id, "window_name") == doubled:
            _must("rename-window", "-t", window_id, "--", title)


def _check_folder(pane_id: str, cwd: str, sleep=time.sleep) -> None:
    """Refuse a session that didn't start in `cwd`. A tmux that reads the folder's # another way starts the
    session in the home folder and says nothing; an agent must never be started in the wrong folder."""
    found = ""
    for _ in range(40):
        found = _show(pane_id, "pane_current_path")
        if os.path.realpath(found) == os.path.realpath(cwd):
            return
        sleep(0.05)
    raise Failure("tmux_error", f"the new session started in {found}, not in {cwd}")


def new_workspace(cwd, title, command) -> str:
    """Start a detached session with one window named `title` in `cwd`, type `command` into its shell,
    and return the new workspace's ref. Nothing the user is looking at changes. A session that can't be
    finished is ended, so a caller that tries again doesn't leave one behind each time."""
    cwd = cwd or os.path.expanduser("~")
    if not os.path.isdir(cwd):
        raise Failure("invalid_params", f"cwd is not a folder: {cwd}")
    title = title or "Terminal"
    width, height = NEW_SESSION_SIZE
    made = _must("new-session", "-d", "-s", _session_name(title), "-n", _plain(title), "-c", _plain(cwd),
                 "-x", width, "-y", height, "-P", "-F", "#{window_id} #{pane_id}").split()
    window_id, pane_id = made[0], made[1]
    try:
        if "#" in title:
            _settle_name(window_id, title)
        if "#" in cwd:
            _check_folder(pane_id, cwd)
        if command:
            send_text({"tmux_pane": pane_id}, command)
            _must("send-keys", "-t", pane_id, "Enter")
    except Failure:
        _tmux("kill-window", "-t", window_id)
        raise
    return _ref("workspace", window_id)


def select_workspace(ws: dict) -> None:
    """Show the workspace in the tmux client used most recently. With no client, nothing is changed."""
    rc, out, _ = _tmux("list-clients", "-F", "#{client_activity} #{client_name}")
    clients = sorted(line.split(" ", 1) for line in out.splitlines() if " " in line) if rc == 0 else []
    if not clients:
        raise Failure("no_client", "no terminal is attached to tmux; run: tmux attach -t "
                      + shlex.quote(ws["tmux_session"]))
    _must("select-window", "-t", ws["tmux_window"])
    _must("switch-client", "-c", clients[-1][1], "-t", ws["tmux_window"])


# ── agent sessions ────────────────────────────────────────────────────────────

def _claude_rows() -> list:
    """Claude Code's own list of its live interactive sessions: [{pid, sessionId, cwd, startedAt, status}].
    No Claude Code on this machine is no sessions. A Claude Code that doesn't answer is a Failure: "couldn't
    look" must never read as "nothing is running"."""
    claude = shutil.which(agent_session.binary(agent_session.CLAUDE))
    if not claude:
        return []
    try:
        done = subprocess.run([claude, "agents", "--json"], capture_output=True, text=True, timeout=TIMEOUT_SEC)
        rows = json.loads(done.stdout) if done.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, ValueError):
        rows = None
    if not isinstance(rows, list):
        raise Failure("unavailable", "`claude agents --json` didn't answer, so the sessions can't be listed "
                                     "(it needs Claude Code 2.1.139 or later)")
    return [row for row in rows
            if isinstance(row, dict) and type(row.get("pid")) is int and row.get("kind") == "interactive"
            and isinstance(row.get("sessionId"), str) and row["sessionId"]]


def _terminals(pids: list) -> dict:
    """{pid: its terminal} for the processes that are the foreground job of a terminal."""
    if not pids:
        return {}
    try:
        done = subprocess.run(["ps", "-o", "pid=,pgid=,tpgid=,tty=", "-p", ",".join(map(str, pids))],
                              capture_output=True, text=True, timeout=TIMEOUT_SEC)
    except (OSError, subprocess.SubprocessError) as exc:
        raise Failure("unavailable", f"ps can't be run ({exc}), so the sessions can't be listed") from None
    found = {}
    for line in done.stdout.splitlines():
        fields = line.split()
        if len(fields) == 4 and fields[1] == fields[2]:
            found[int(fields[0])] = fields[3]
    return found


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat().replace("+00:00", "Z")


def sessions(doc: dict) -> list:
    """The live agent session of each pane, as cmux's `sessions list` rows. A pane with two candidate
    sessions gets none: which one it shows can't be told."""
    by_tty = {surface["tty"]: (ws, surface) for ws in _workspaces(doc) for surface in _surfaces(ws)}
    rows = _claude_rows()
    terminals = _terminals([row["pid"] for row in rows])
    placed: dict = {}
    for row in rows:
        place = by_tty.get(terminals.get(row["pid"]))
        if place:
            placed.setdefault(place[1]["id"], []).append((row, place))
    out = []
    for candidates in placed.values():
        if len(candidates) != 1:
            continue
        row, (ws, surface) = candidates[0]
        started = row.get("startedAt")
        started = started / 1000 if type(started) in (int, float) else None
        transcript = agent_session.transcript_for_session(agent_session.CLAUDE, row["sessionId"])
        try:
            on_disk = os.stat(transcript) if transcript else None
        except OSError:
            on_disk = None
        written = on_disk.st_mtime if on_disk else None
        # A resumed session's transcript is older than its process until its first new line.
        updated = max(value for value in (started, written, 0.0) if value is not None) or None
        out.append({
            "session_id": row["sessionId"], "agent": agent_session.CMUX_KIND[agent_session.CLAUDE],
            "agent_display_name": "Claude Code", "agent_lifecycle": "running",
            "workspace_id": ws["id"], "workspace_ref": ws["ref"],
            "surface_id": surface["id"], "surface_ref": surface["ref"],
            "cwd": row.get("cwd"), "launch_working_directory": row.get("cwd"), "pid": row["pid"],
            "transcript_path": transcript, "started_at": _iso(started) if started else None,
            "updated_at": _iso(updated) if updated else None, "updated_at_unix": updated,
            "active_for_surface": True, "active_for_workspace": True, "is_restorable": False,
            "status": row.get("status"), "waiting_for": row.get("waitingFor"),
            "transcript_size": on_disk.st_size if on_disk else 0,
        })
    return out


def events(write, sleep=time.sleep, rounds=None, category="agent") -> None:
    """Write cmux's agent events for tmux sessions, one JSON object a line, until the reader goes away.

    cmux hears of a finished turn or a question from hooks it puts in each agent. Here the agent's own
    session list is read every EVENTS_POLL_SEC and a change is the event. A session that starts waiting on
    its user (a permission dialog, a question) needs input: `agent.hook.Notification`. A session that is
    idle after being busy, or idle with a transcript that grew since the last look (a turn shorter than the
    wait between looks), has finished a turn: `agent.hook.Stop`. A session seen for the first time reports
    nothing. tmux has no events of another `category`: asked for one, this waits and writes nothing."""
    seen: dict = {}
    count = 0
    while rounds is None or count < rounds:
        count += 1
        try:
            live = sessions(tree()) if category == "agent" else None
        except Failure:
            live = None  # tmux or the agent didn't answer: keep what was seen, look again
        now: dict = {}
        for row in live or []:
            key = (row["surface_id"], row["session_id"])
            state = "waiting" if row["waiting_for"] or row["status"] in ("waiting", "blocked") else row["status"]
            now[key] = (state, row["transcript_size"])
            if key not in seen:
                continue
            was, size = seen[key]
            if state == "waiting":
                name = "agent.hook.Notification" if was != "waiting" else None
            else:
                name = "agent.hook.Stop" if state == "idle" and (was == "busy" or now[key][1] > size) else None
            if name:
                write(json.dumps({
                    "type": "event", "name": name, "category": "agent", "workspace_id": row["workspace_id"],
                    "payload": {"session_id": row["session_id"], "cwd": row["cwd"],
                                "workspace_id": row["workspace_id"], "surface_id": row["surface_id"],
                                "_opencode_request_id": f"tmux-{uuid.uuid4()}"},
                }) + "\n")
        if live is not None:
            seen = now
        if rounds is None or count < rounds:
            sleep(EVENTS_POLL_SEC)


def _context(doc: dict, ws: dict, surface: dict) -> dict:
    window = next(w for w in doc["windows"] if ws in w["workspaces"])
    return {"window_id": window["id"], "window_ref": window["ref"], "workspace_id": ws["id"],
            "workspace_ref": ws["ref"], "pane_id": surface["pane_id"], "pane_ref": surface["pane_ref"],
            "surface_id": surface["id"], "surface_ref": surface["ref"], "surface_type": "terminal",
            "is_browser_surface": False}


def identify(doc: dict, workspace=None) -> dict:
    """cmux's `identify`: `caller` is the pane this command runs in ($TMUX_PANE, when that pane is on the
    default server), or the focused pane of `workspace` when one is named; None when neither."""
    caller = None
    if workspace:
        caller = _context(doc, *find_surface(doc, None, workspace))
    else:
        socket_path = (os.environ.get("TMUX") or "").split(",")[0]
        for ws in _workspaces(doc):
            for surface in _surfaces(ws):
                if surface["tmux_pane"] == os.environ.get("TMUX_PANE") and surface["socket"] == socket_path:
                    caller = _context(doc, ws, surface)
    return {"caller": caller}


# ── the command line ──────────────────────────────────────────────────────────

def _parse(argv: list) -> tuple:
    """(positional words, {flag: value}) of a cmux command line. Only the flags Assistant passes are flags:
    anything else, and everything after a bare `--`, is a word, so text to type may start with dashes."""
    words, flags, index = [], {}, 0
    while index < len(argv):
        arg = argv[index]
        if arg == "--":
            words += argv[index + 1:]
            break
        if arg in _VALUE_FLAGS and index + 1 < len(argv):
            flags[arg] = argv[index + 1]
            index += 2
            continue
        if arg in _PLAIN_FLAGS:
            flags[arg] = True
        else:
            words.append(arg)
        index += 1
    return words, flags


def _json(document) -> str:
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"


def _workspace_list(doc: dict, window=None) -> dict:
    windows = [w for w in doc["windows"] if not window or _matches(w, window)]
    if window and not windows:
        raise Failure("invalid_params", "Missing or invalid window_id")
    first = windows[0] if windows else {}
    return {"window_id": first.get("id"), "window_ref": first.get("ref"),
            "workspaces": [{key: ws[key] for key in ("id", "ref", "index", "title", "selected", "pinned",
                                                     "description", "current_directory")}
                           for w in windows for ws in w["workspaces"]]}


def _tree_text(doc: dict) -> str:
    lines = []
    for window in doc["windows"]:
        lines.append(f"window {window['ref']} \"{window['title']}\"")
        for ws in window["workspaces"]:
            lines.append(f"├── workspace {ws['ref']} \"{ws['title']}\"" + (" [selected]" if ws["selected"] else ""))
            for pane in ws["panes"]:
                lines.append(f"│   └── pane {pane['ref']}" + (" [focused]" if pane["focused"] else ""))
                for surface in pane["surfaces"]:
                    lines.append(f"│       └── surface {surface['ref']} [terminal] \"{surface['title']}\" "
                                 f"tty={surface['tty']}")
    return "\n".join(lines) + "\n"


def _rpc(method: str, params: dict) -> dict:
    if method == "workspace.list":
        return _workspace_list(tree())
    if method not in ("surface.read_text", "surface.send_text", "surface.send_key"):
        raise Failure("method_not_found", "Unknown method")
    ws, surface = find_surface(tree(), params.get("surface_id"), params.get("workspace_id"))
    answer = _where(ws, surface)
    if isinstance(params.get("surface_id"), str) and params["surface_id"].startswith("surface:"):
        answer["surface_ref"] = params["surface_id"]
    if method == "surface.read_text":
        text = read_text(surface, params.get("lines"), bool(params.get("scrollback")))
        answer.update(text=text, base64=base64.b64encode(text.encode()).decode())
    elif method == "surface.send_text":
        send_text(surface, str(params.get("text") or ""))
    else:
        send_key(surface, params.get("key"))
    return answer


def run(argv: list, out=None) -> int:
    out = out or sys.stdout
    words, flags = _parse(argv)
    command = words[0] if words else ""
    rest = words[1:]
    as_json = bool(flags.get("--json"))
    workspace = flags.get("--workspace")

    if command == "ping":
        _must("-V")
        out.write("PONG\n")
    elif command == "tree":
        doc = tree()
        if workspace:
            ws = find_workspace(doc, workspace)
            doc = {"windows": [dict(window, workspaces=[ws], workspace_count=1)
                               for window in doc["windows"] if ws in window["workspaces"]]}
        out.write(_json(doc) if as_json else _tree_text(doc))
    elif command == "identify":
        out.write(_json(identify(tree(), workspace)))
    elif command == "focus-window":
        _workspace_list(tree(), flags.get("--window") or "(none)")  # a tmux session has no window to raise
        out.write("OK\n")
    elif command == "list-windows":
        out.write(_json([{"id": w["id"], "ref": w["ref"], "index": w["index"], "key": w["index"] == 0,
                          "selected_workspace_id": w["selected_workspace_id"],
                          "workspace_count": w["workspace_count"]} for w in tree()["windows"]]))
    elif command == "list-workspaces" or (command, rest[:1]) == ("workspace", ["list"]):
        listing = _workspace_list(tree(), flags.get("--window"))
        out.write(_json(listing) if as_json else "".join(
            f"{'*' if ws['selected'] else ' '} {ws['ref']}  {ws['title']}"
            f"{'  [selected]' if ws['selected'] else ''}\n" for ws in listing["workspaces"]))
    elif command == "list-panes":
        out.write("".join(f"{'*' if pane['focused'] else ' '} {pane['ref']}  [1 surface]"
                          f"{'  [focused]' if pane['focused'] else ''}\n"
                          for pane in find_workspace(tree(), workspace)["panes"]))
    elif command == "list-pane-surfaces":
        ws = find_workspace(tree(), workspace)
        wanted = flags.get("--pane")
        panes = [pane for pane in ws["panes"] if (_matches(pane, wanted) if wanted else pane["focused"])]
        out.write("".join(f"* {surface['ref']} {surface['id']}  {surface['title']}  [selected]\n"
                          for pane in panes for surface in pane["surfaces"]))
    elif command == "read-screen":
        _, surface = find_surface(tree(), flags.get("--surface"), workspace)
        out.write(read_text(surface, flags.get("--lines"), bool(flags.get("--scrollback"))) + "\n")
    elif command == "new-workspace":
        out.write(f"OK {new_workspace(flags.get('--cwd'), flags.get('--name'), flags.get('--command'))}\n")
    elif command == "close-workspace":
        _must("kill-window", "-t", find_workspace(tree(), workspace)["tmux_window"])
        out.write("OK\n")
    elif command == "select-workspace":
        select_workspace(find_workspace(tree(), workspace))
        out.write("OK\n")
    elif command in ("send", "send-key") and rest:
        _, surface = find_surface(tree(), flags.get("--surface"), workspace)
        send_text(surface, " ".join(rest)) if command == "send" else send_key(surface, rest[0])
        out.write("OK\n")
    elif (command, rest[:1]) == ("sessions", ["list"]):
        rows = [row for row in sessions(tree())
                if not flags.get("--session") or row["session_id"] == flags["--session"]]
        out.write(_json({"sessions": rows, "total_matches": len(rows), "limit": None,
                         "state_dir": None, "stores": []}))
    elif (command, rest[:2]) == ("surface", ["resume", "show"]):
        doc = tree()
        ws, surface = find_surface(doc, flags.get("--surface"), workspace)
        answer = {"surface_ref": surface["ref"], "workspace_ref": ws["ref"], "pane_ref": surface["pane_ref"]}
        for row in sessions(doc):
            if row["surface_id"] == surface["id"]:
                answer["resume_binding"] = {
                    "kind": row["agent"], "name": row["agent_display_name"], "checkpoint_id": row["session_id"],
                    "cwd": row["cwd"], "source": "tmux", "updated_at": row["updated_at_unix"]}
        out.write(_json(answer))
    elif command == "rpc" and rest:
        try:
            params = json.loads(rest[1]) if len(rest) > 1 else {}
        except ValueError:
            params = None
        if not isinstance(params, dict):
            raise Failure("invalid_params", "params must be a JSON object")
        out.write(_json(_rpc(rest[0], params)))
    elif command == "events":
        def write(line: str) -> None:
            out.write(line)
            out.flush()
        events(write, category=flags.get("--category", "agent"))
    else:
        sys.stderr.write(f"Error: Unknown command: {command or '(none)'} (sessions are kept in tmux)\n")
        return 2
    return 0


def main(argv=None) -> int:
    try:
        return run(sys.argv[1:] if argv is None else argv)
    except Failure as failure:
        sys.stderr.write(f"Error: {failure}\n")
        return 1
    except (BrokenPipeError, KeyboardInterrupt):
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
