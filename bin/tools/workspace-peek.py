#!/usr/bin/env python3
"""workspace-peek — live terminal screen for one workspace.

cmux has no `read-screen --workspace` verb. The sanctioned path (mirrored from
pulse.py / comms_session.py) is two RPCs:
  1. `cmux list-pane-surfaces --workspace <ws>` → resolve a `surface:N` ref.
  2. `cmux rpc surface.read_text {"surface_id": <surface>, "lines": N}` →
     the rendered screen text.

Returns JSON to stdout:
  {"ws_ref": "workspace:45", "surface_ref": "surface:65",
   "screen_text": "...last N lines..."}

On any failure (cmux down, no such workspace, no terminal surface) the screen
is "" and an "error" key explains why — the dispatcher still gets valid JSON.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from typing import Any

CMUX = os.environ.get("CMUX_BIN",
                      "/Applications/cmux.app/Contents/Resources/bin/cmux")
# Match the read window pulse.py uses — wide enough to capture a full TUI
# screen, not just the bottom band.
DEFAULT_LINES = 40


def _run(cmd: list[str], timeout: int = 15) -> tuple[int, str, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 124, "", f"timeout after {timeout}s"
    except Exception as e:  # noqa: BLE001
        return 1, "", str(e)


def _cmux_rpc(method: str, params: dict[str, Any], timeout: int = 15) -> dict | None:
    rc, out, _ = _run([CMUX, "rpc", method, json.dumps(params)], timeout=timeout)
    if rc != 0:
        return None
    try:
        return json.loads(out)
    except (json.JSONDecodeError, ValueError):
        return None


def resolve_surface(ws_ref: str) -> str | None:
    """First terminal surface ref for the workspace, or None."""
    rc, out, _ = _run([CMUX, "list-pane-surfaces", "--workspace", ws_ref])
    if rc != 0:
        return None
    m = re.search(r"surface:\d+", out)
    return m.group(0) if m else None


def peek(ws_ref: str, lines: int = DEFAULT_LINES) -> dict[str, Any]:
    surface_ref = resolve_surface(ws_ref)
    if not surface_ref:
        return {"ws_ref": ws_ref, "surface_ref": None, "screen_text": "",
                "error": f"no terminal surface for {ws_ref} (cmux down or no such workspace)"}
    d = _cmux_rpc("surface.read_text",
                  {"surface_id": surface_ref, "lines": lines})
    if not d:
        return {"ws_ref": ws_ref, "surface_ref": surface_ref, "screen_text": "",
                "error": "surface.read_text RPC failed"}
    text = d.get("text", "") or ""
    # Trim to the requested tail in case cmux returns more than asked.
    tail = "\n".join(text.splitlines()[-lines:])
    return {"ws_ref": ws_ref, "surface_ref": surface_ref, "screen_text": tail}


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Live terminal screen for a specific workspace. Returns "
                    "JSON {ws_ref, surface_ref, screen_text}.")
    ap.add_argument("--ws", required=True, help="workspace ref e.g. workspace:45")
    ap.add_argument("--lines", type=int, default=DEFAULT_LINES,
                    help=f"how many trailing lines to return (default {DEFAULT_LINES})")
    args = ap.parse_args()
    print(json.dumps(peek(args.ws, args.lines)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
