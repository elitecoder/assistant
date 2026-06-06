#!/usr/bin/env python3
"""tool-dispatch.py — named-tool dispatcher for the Assistant.

The warm comms session (and any other caller) invokes named tools instead of
hand-rolling shell pipelines against ~/.assistant state. The registry lives in
bin/tools-manifest.json; each tool is a standalone script under bin/tools/ that
emits clean JSON to stdout. This dispatcher is the single sanctioned entry
point: it validates the tool name, checks/normalizes args against the manifest,
execs the script, and passes its stdout through verbatim.

Usage:
    tool-dispatch.py <tool> [--arg val ...]
    tool-dispatch.py --list            # list available tools as JSON
    tool-dispatch.py <tool> --help     # delegate to the tool's own --help

Examples:
    tool-dispatch.py fleet_status
    tool-dispatch.py workspace_peek --ws workspace:45
    tool-dispatch.py recent_actions --n 5 --ws workspace:12

Contract:
    - stdout is the tool's stdout, unmodified (callers expect JSON).
    - On any dispatcher-level error (unknown tool, missing/ill-typed required
      arg, subprocess failure) we print {"error": "...", "tool": "..."} to
      stdout and exit 1. We never raise.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
MANIFEST_PATH = REPO / "bin" / "tools-manifest.json"


def _fail(msg: str, tool: str | None = None) -> int:
    """Emit a structured error to stdout and signal exit 1. Never raises."""
    print(json.dumps({"error": msg, "tool": tool}))
    return 1


def load_manifest(path: Path | None = None) -> list[dict[str, Any]]:
    """Read the tool registry. Raises ValueError with a clear message on a
    missing or malformed manifest so the caller can convert it to a JSON
    error — the manifest is a build artifact, not user input.

    `path` defaults to the module-level MANIFEST_PATH resolved at CALL time
    (not import time), so a caller — or a test — that reassigns
    tool_dispatch.MANIFEST_PATH is honored."""
    path = path if path is not None else MANIFEST_PATH
    try:
        text = path.read_text()
    except FileNotFoundError as e:
        raise ValueError(f"manifest not found at {path}") from e
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"manifest is not valid JSON: {e}") from e
    if not isinstance(data, list):
        raise ValueError("manifest must be a JSON array of tool definitions")
    return data


def find_tool(manifest: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    for tool in manifest:
        if tool.get("name") == name:
            return tool
    return None


def _coerce(value: str, typ: str) -> Any:
    """Coerce a raw CLI string to the manifest-declared type. Raises
    ValueError on a bad int so the dispatcher can report it."""
    if typ == "int":
        try:
            return int(value)
        except (TypeError, ValueError) as e:
            raise ValueError(f"expected int, got {value!r}") from e
    # Everything else passes through as a string.
    return value


def parse_tool_args(spec_args: list[dict[str, Any]],
                    argv: list[str]) -> dict[str, Any]:
    """Validate and normalize `--name value` argv against a tool's arg spec.

    Returns {arg_name: coerced_value} for every arg the caller actually
    supplied (defaults are NOT injected here — each tool owns its own
    defaults so it stays runnable standalone). Raises ValueError on an
    unknown flag, a missing value, a bad type, an invalid choice, or a
    missing required arg.
    """
    by_name = {a["name"]: a for a in spec_args}
    supplied: dict[str, Any] = {}

    i = 0
    while i < len(argv):
        token = argv[i]
        if not token.startswith("--"):
            raise ValueError(f"unexpected positional argument {token!r}")
        key = token[2:]
        if key not in by_name:
            raise ValueError(f"unknown argument --{key}")
        if i + 1 >= len(argv):
            raise ValueError(f"argument --{key} expects a value")
        raw = argv[i + 1]
        spec = by_name[key]
        val = _coerce(raw, spec.get("type", "string"))
        choices = spec.get("choices")
        if choices and val not in choices:
            raise ValueError(
                f"argument --{key} must be one of {choices}, got {raw!r}")
        supplied[key] = val
        i += 2

    missing = [a["name"] for a in spec_args
               if a.get("required") and a["name"] not in supplied]
    if missing:
        raise ValueError(f"missing required argument(s): {', '.join(missing)}")
    return supplied


def _to_cli(name: str, value: Any) -> list[str]:
    """Render one normalized arg back into `--name value` for the subprocess."""
    return [f"--{name}", str(value)]


def dispatch(tool_name: str, argv: list[str]) -> int:
    """Validate + execute one tool. Returns the process exit code."""
    try:
        manifest = load_manifest()
    except ValueError as e:
        return _fail(str(e), tool_name)

    tool = find_tool(manifest, tool_name)
    if tool is None:
        names = sorted(t.get("name", "") for t in manifest)
        return _fail(f"unknown tool {tool_name!r}; available: {names}", tool_name)

    # `--help` short-circuits validation: delegate straight to the tool so its
    # own argparse description prints.
    if "--help" in argv or "-h" in argv:
        forward_help = True
        normalized: dict[str, Any] = {}
    else:
        forward_help = False
        try:
            normalized = parse_tool_args(tool.get("args", []), argv)
        except ValueError as e:
            return _fail(str(e), tool_name)

    script = REPO / tool["script"]
    if not script.exists():
        return _fail(f"tool script missing: {tool['script']}", tool_name)

    cmd = [sys.executable, str(script)]
    if forward_help:
        cmd.append("--help")
    else:
        for name, value in normalized.items():
            cmd += _to_cli(name, value)

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        return _fail(f"tool {tool_name!r} timed out after 60s", tool_name)
    except Exception as e:  # noqa: BLE001 — dispatcher must never raise
        return _fail(f"tool {tool_name!r} failed to launch: {e}", tool_name)

    # Pass stdout through verbatim — callers expect the tool's JSON.
    if proc.stdout:
        sys.stdout.write(proc.stdout)
    if proc.returncode != 0:
        # The tool failed. Surface its stderr in the structured error so the
        # caller sees why, but keep the contract (JSON on stdout, exit 1).
        if proc.stdout.strip():
            # The tool already emitted JSON (likely its own error envelope);
            # don't double-wrap — just propagate the nonzero code.
            return 1
        err = (proc.stderr or "").strip()[-500:]
        return _fail(f"tool {tool_name!r} exited {proc.returncode}: {err}", tool_name)
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        return _fail("usage: tool-dispatch.py <tool> [--arg val ...] | --list")

    if argv[0] in ("--list", "-l"):
        try:
            manifest = load_manifest()
        except ValueError as e:
            return _fail(str(e))
        print(json.dumps(
            [{"name": t.get("name"), "description": t.get("description"),
              "args": t.get("args", [])} for t in manifest],
            indent=2))
        return 0

    return dispatch(argv[0], argv[1:])


if __name__ == "__main__":
    sys.exit(main())
