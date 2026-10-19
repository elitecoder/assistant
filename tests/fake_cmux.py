"""A stand-in `cmux` executable for `cmux sessions list --json`.

`install(directory, records)` writes an executable `cmux` into `directory` that
answers `sessions list` with the given records in cmux 0.64.25's JSON shape and
logs each argv to `cmux-argv.jsonl` beside it. Point `CMUX_BIN` at the returned
path so bin/cmux_sessions.py runs it as a real subprocess.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

SCRIPT = """#!{python}
import json, pathlib, sys
here = pathlib.Path(__file__).resolve().parent
with open(here / "cmux-argv.jsonl", "a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")
if sys.argv[1:3] != ["sessions", "list"]:
    sys.exit(2)
sys.stdout.write((here / "cmux-sessions.out").read_text())
sys.exit(int((here / "cmux-sessions.rc").read_text()))
"""


def record(session_id: str, **fields) -> dict:
    """One `sessions` row as cmux prints it, with fixture defaults. The launch
    directory defaults to the cwd, as it is for most live sessions."""
    fields.setdefault("launch_working_directory", fields.get("cwd", "/work"))
    row = {
        "session_id": session_id,
        "agent": "claude",
        "agent_display_name": "Claude Code",
        "agent_lifecycle": "running",
        "workspace_id": "AAAAAAAA-0000-0000-0000-000000000001",
        "surface_id": "BBBBBBBB-0000-0000-0000-000000000001",
        "cwd": "/work",
        "launch_working_directory": "/work",
        "pid": None,
        "transcript_path": None,
        "started_at": "2026-10-18T15:48:38.207Z",
        "updated_at": "2026-10-19T14:07:50.256Z",
        "updated_at_unix": 1792418870.2557909,
        "active_for_surface": True,
        "active_for_workspace": True,
        "is_restorable": True,
        "store_path": "/fixture/.cmuxterm/claude-hook-sessions.json",
    }
    row.update(fields)
    return row


def write(directory: Path, records=(), *, stdout: str | None = None, returncode: int = 0) -> None:
    """Replace what the fake prints. `stdout` overrides the JSON document."""
    if stdout is None:
        stdout = json.dumps({
            "sessions": list(records), "total_matches": len(records), "limit": None,
            "state_dir": "/fixture/.cmuxterm", "stores": [],
        }, indent=2)
    (directory / "cmux-sessions.out").write_text(stdout)
    (directory / "cmux-sessions.rc").write_text(str(returncode))


def install(directory: Path, records=(), **kwargs) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    cli = directory / "cmux"
    cli.write_text(SCRIPT.format(python=sys.executable))
    cli.chmod(0o755)
    write(directory, records, **kwargs)
    return cli


def calls(directory: Path) -> list[list[str]]:
    log = directory / "cmux-argv.jsonl"
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines()]
