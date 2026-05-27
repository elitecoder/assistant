#!/usr/bin/env python3
"""Append `[N]` workspace-ref suffix to every cmux workspace name.

Streams `cmux events --reconnect --category workspace` and on
workspace.created / workspace.renamed renames the workspace to
`<base-title> [N]` where N is the stable `workspace:N` ref.

Idempotent: if the title already ends with the correct `[N]`, no rename is
issued (which prevents an infinite rename->event->rename loop).
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path

CMUX = "/Applications/cmux.app/Contents/Resources/bin/cmux"
LOG_PATH = Path.home() / ".claude" / "logs" / "cmux-ws-numberer.log"
SUFFIX_RE = re.compile(r"\s*\[(\d+)\]\s*$")

logging.basicConfig(
    filename=str(LOG_PATH),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("cmux-ws-numberer")


def list_workspaces() -> list[dict]:
    """Return [{ref_num, uuid, title}] for every workspace."""
    out = subprocess.run(
        [CMUX, "list-workspaces", "--id-format", "both"],
        capture_output=True, text=True, timeout=10,
    )
    rows: list[dict] = []
    for raw in out.stdout.splitlines():
        line = raw.lstrip()
        if line.startswith("*"):
            line = line[1:].lstrip()
        m = re.match(r"workspace:(\d+)\s+([0-9A-Fa-f-]{36})\s+(.*?)\s*$", line)
        if not m:
            continue
        title = m.group(3)
        if title.endswith("[selected]"):
            title = title[: -len("[selected]")].rstrip()
        rows.append({"ref": int(m.group(1)), "uuid": m.group(2), "title": title})
    return rows


def desired_title(base: str, ref: int) -> str:
    base = SUFFIX_RE.sub("", base).rstrip()
    return f"{base} [{ref}]"


def ensure_numbered(uuid: str) -> None:
    rows = list_workspaces()
    row = next((r for r in rows if r["uuid"].lower() == uuid.lower()), None)
    if not row:
        log.warning("uuid %s not found in list-workspaces", uuid)
        return
    target = desired_title(row["title"], row["ref"])
    if row["title"] == target:
        return
    res = subprocess.run(
        [CMUX, "rename-workspace", "--workspace", uuid, target],
        capture_output=True, text=True, timeout=10,
    )
    if res.returncode != 0:
        log.error("rename failed uuid=%s target=%r stderr=%s",
                  uuid, target, res.stderr.strip())
    else:
        log.info("renamed %s: %r -> %r", uuid, row["title"], target)


def backfill() -> None:
    for r in list_workspaces():
        target = desired_title(r["title"], r["ref"])
        if r["title"] != target:
            res = subprocess.run(
                [CMUX, "rename-workspace", "--workspace", r["uuid"], target],
                capture_output=True, text=True, timeout=10,
            )
            if res.returncode == 0:
                log.info("backfill ws:%d %r -> %r", r["ref"], r["title"], target)


def stream() -> None:
    cursor_file = Path.home() / ".claude" / "logs" / "cmux-ws-numberer.cursor"
    cmd = [
        CMUX, "events",
        "--reconnect",
        "--category", "workspace",
        "--no-heartbeat",
        "--no-ack",
        "--cursor-file", str(cursor_file),
    ]
    log.info("starting event stream: %s", " ".join(cmd))
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True, bufsize=1)
    assert proc.stdout
    for line in proc.stdout:
        line = line.strip()
        if not line:
            continue
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            continue
        name = evt.get("name", "")
        if name not in ("workspace.created", "workspace.renamed"):
            continue
        payload = evt.get("payload") or {}
        uuid = payload.get("workspace_id")
        if not uuid and name == "workspace.renamed":
            uuid = (payload.get("params") or {}).get("workspace_id")
        if not uuid:
            uuid = evt.get("workspace_id")
        if not uuid:
            continue
        try:
            ensure_numbered(uuid)
        except Exception:
            log.exception("ensure_numbered failed for %s", uuid)


def main() -> int:
    log.info("=== cmux-ws-numberer pid=%d ===", os.getpid())
    while True:
        try:
            backfill()
            stream()
        except KeyboardInterrupt:
            log.info("interrupted, exiting")
            return 0
        except Exception:
            log.exception("stream crashed; sleeping 5s and retrying")
            time.sleep(5)


if __name__ == "__main__":
    sys.exit(main())
