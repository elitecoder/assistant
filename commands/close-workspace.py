#!/usr/bin/env python3
"""Close the cmux workspace this process runs in, and nothing else.

`/close-workspace` runs this. It finds the caller's workspace from process
lineage: the login process cmux started for a terminal must be an ancestor of
this process. It never uses the selected workspace, `cmux current-workspace`,
`cmux identify`, a sidebar index, or a `[N]` name suffix.

`CMUX_WORKSPACE_ID` is only a cross-check. It's inherited, so it can be stale
or missing. When it disagrees with the lineage, or the lineage matches no
workspace or more than one, this script closes nothing and says why. It also
refuses when a process between it and the terminal runs in a third session,
such as a background daemon that only started from that terminal.

Exit codes: 0 closed (or `--dry-run` found the target), 1 refused, 2 cmux
reported closing a different workspace than the one asked for.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

CMUX = os.environ.get("CMUX_BIN") or "/Applications/cmux.app/Contents/Resources/bin/cmux"
CMUX_ENV = {**os.environ, "CMUX_QUIET": "1"}
CMUX_STARTED = "explicit-root-pid"


class Refusal(Exception):
    pass


def ancestors(pid: int) -> list[int]:
    out = subprocess.run(["ps", "-axo", "pid=,ppid="], capture_output=True, text=True, check=True).stdout
    parent = {}
    for line in out.splitlines():
        child, ppid = line.split()
        parent[int(child)] = int(ppid)
    chain = []
    while pid > 1 and pid not in chain:
        chain.append(pid)
        pid = parent.get(pid, 0)
    return chain


def cmux(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([CMUX, *args], capture_output=True, text=True, env=CMUX_ENV, timeout=20)


def lineage_workspaces(chain: list[int]) -> list[tuple[dict, int]]:
    """Every (workspace, login pid) whose terminal login process is in `chain`.

    A terminal's login process is the one the cmux app started for it, so its
    parent is a cmux app process. cmux also lists other processes under a
    terminal: by the CMUX_* variables in their environment (the inherited values
    this script can't trust) and by controlling terminal (which still covers a
    process that left the terminal's process tree). Neither counts here."""
    res = cmux("--json", "--id-format", "both", "top", "--all", "--processes")
    if res.returncode != 0:
        raise Refusal(f"cmux couldn't list its terminals: {res.stderr.strip() or res.stdout.strip()}")
    try:
        windows = json.loads(res.stdout)["windows"]
    except (ValueError, KeyError) as exc:
        raise Refusal(f"cmux's terminal list wasn't readable ({exc})") from None
    mine = set(chain)
    found = {}
    for window in windows:
        app = set(window.get("app_process_pids") or [])
        for ws in window.get("workspaces", []):
            for pane in ws.get("panes", []):
                for surface in pane.get("surfaces", []):
                    logins = {p.get("pid") for p in surface.get("processes", [])
                              if p.get("attribution_reason") == CMUX_STARTED and p.get("ppid") in app}
                    for login in logins & mine:
                        found[ws["id"]] = (ws, login)
    return list(found.values())


def resolve() -> dict:
    chain = ancestors(os.getpid())
    matches = lineage_workspaces(chain)
    if not matches:
        raise Refusal("this session isn't running in a cmux terminal it can verify "
                      "(for example, it runs under tmux or as a background job)")
    if len(matches) > 1:
        refs = ", ".join(ws["ref"] for ws, _ in matches)
        raise Refusal(f"this session matches more than one workspace: {refs}")
    ws, login = matches[0]
    sessions = {os.getsid(0), os.getsid(login)}
    if any(os.getsid(pid) not in sessions for pid in chain[:chain.index(login)]):
        raise Refusal(f"this session runs under a background process that started in "
                      f"{ws['ref']} but may be shown in another workspace")
    claimed = os.environ.get("CMUX_WORKSPACE_ID", "")
    if claimed and claimed.upper() != ws["id"].upper():
        raise Refusal(f"CMUX_WORKSPACE_ID is {claimed}, but this session runs in "
                      f"{ws['ref']} ({ws['id']})")
    if ws.get("pinned"):
        raise Refusal(f"{ws['ref']} ({ws.get('title', '')}) is pinned. Unpin it, then run "
                      "/close-workspace again")
    return ws


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true",
                        help="print the workspace this would close, and close nothing")
    args = parser.parse_args(argv)
    try:
        ws = resolve()
    except (Refusal, OSError, subprocess.SubprocessError) as why:
        print(f"Didn't close any workspace: {why}. Close it yourself if you still want it gone.")
        return 1
    label = f"{ws['ref']} ({ws['id']}) \"{ws.get('title', '')}\""
    if args.dry_run:
        print(f"Would close {label}.")
        return 0
    close = ["close-workspace", "--workspace", ws["id"]]
    try:
        res = cmux(*close)
        if res.returncode != 0 and "confirmation_required" in res.stderr:
            res = cmux(*close, "--force")
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"cmux didn't answer when asked to close {label} ({exc}). Check whether it's still open.")
        return 1
    if res.returncode != 0:
        print(f"cmux didn't close {label}: {res.stderr.strip() or res.stdout.strip()}")
        return 1
    if ws["ref"] not in res.stdout.split():
        print(f"Warning: asked cmux to close {label}, but it answered: {res.stdout.strip()}")
        return 2
    print(f"Closed {label}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
