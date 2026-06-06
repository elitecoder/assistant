#!/usr/bin/env python3
"""mcp-agent-tools — MCP server for Assistant-spawned agents only.

Scoped to Observer/pulse agent sessions via --mcp-config in the spawn command.
Never registered in global settings.json — keeps tool surface clean for coding sessions.

Current tools (memory gate):
  propose_memory   — park a memory/lesson for Mukul's approval (never writes directly)
  confirm_proposal — apply a pending proposal (after Mukul says yes)
  search_memory    — semantic search before proposing (dedup gate)

Future tools (same gate pattern, add as needed):
  propose_todo     — park a TODO mutation for approval
  propose_slack    — draft a Slack message for Mukul to send
  propose_jira     — draft a Jira update for approval

Protocol: JSON-RPC 2.0 over stdio (MCP stdio transport).
No external dependencies — stdlib + the existing bin/tools/ CLIs.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BIN = REPO / "bin"
TOOLS = BIN / "tools"
PROPOSE = TOOLS / "propose-lesson.py"
MEM0_SEARCH = TOOLS / "mem0-search.py"


# ── JSON-RPC helpers ────────────────────────────────────────────────────────

def _send(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def _ok(req_id, result) -> None:
    _send({"jsonrpc": "2.0", "id": req_id, "result": result})


def _err(req_id, code: int, message: str) -> None:
    _send({"jsonrpc": "2.0", "id": req_id,
           "error": {"code": code, "message": message}})


# ── Tool definitions ────────────────────────────────────────────────────────

TOOLS_LIST = [
    {
        "name": "propose_memory",
        "description": (
            "Propose a new memory or lesson for Mukul's approval. "
            "This is the ONLY way to add a memory — it parks the proposal for human review "
            "and does NOT write to the memory store directly. "
            "Always call search_memory first to avoid duplicates."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "trigger": {
                    "type": "string",
                    "description": "When should this rule apply? (e.g. 'About to add a memory')"
                },
                "rule": {
                    "type": "string",
                    "description": "The rule or memory content to record."
                },
                "target": {
                    "type": "string",
                    "enum": ["assistant", "claude"],
                    "description": "Where the lesson lives: 'assistant' = Observer prompt, 'claude' = CLAUDE.md"
                },
                "scope": {
                    "type": "string",
                    "description": "Scope tag (e.g. 'global', 'general', 'comms'). Optional.",
                    "default": "general"
                },
            },
            "required": ["trigger", "rule", "target"],
        },
    },
    {
        "name": "confirm_proposal",
        "description": (
            "Apply a pending memory/lesson proposal that Mukul has approved. "
            "Requires the proposal_id returned by propose_memory."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "proposal_id": {
                    "type": "string",
                    "description": "The proposal ID returned by propose_memory."
                },
            },
            "required": ["proposal_id"],
        },
    },
    {
        "name": "search_memory",
        "description": (
            "Semantic search over existing memories and lessons. "
            "Always call this before propose_memory to check for duplicates."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Natural-language search query."
                },
                "category": {
                    "type": "string",
                    "enum": ["lesson", "working_style", "project", "work_history", "decision"],
                    "description": "Optional category filter."
                },
            },
            "required": ["query"],
        },
    },
]


# ── Tool handlers ───────────────────────────────────────────────────────────

def handle_propose_memory(args: dict) -> dict:
    trigger = args.get("trigger", "").strip()
    rule = args.get("rule", "").strip()
    target = args.get("target", "assistant").strip()
    scope = args.get("scope", "general").strip()

    if not trigger or not rule:
        return {"error": "trigger and rule are required"}

    cmd = [
        sys.executable, str(PROPOSE),
        "--trigger", trigger,
        "--rule", rule,
        "--target", target,
        "--scope", scope,
        "--source", "agent",
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        return {"error": r.stderr.strip() or "propose-lesson failed"}
    try:
        return json.loads(r.stdout.strip())
    except json.JSONDecodeError:
        return {"raw": r.stdout.strip()}


def handle_confirm_proposal(args: dict) -> dict:
    proposal_id = args.get("proposal_id", "").strip()
    if not proposal_id:
        return {"error": "proposal_id is required"}

    cmd = [sys.executable, str(PROPOSE), "--confirm", proposal_id]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        return {"error": r.stderr.strip() or "confirm failed"}
    try:
        return json.loads(r.stdout.strip())
    except json.JSONDecodeError:
        return {"raw": r.stdout.strip()}


def handle_search_memory(args: dict) -> dict:
    query = args.get("query", "").strip()
    if not query:
        return {"error": "query is required"}

    cmd = [sys.executable, str(MEM0_SEARCH), "--query", query, "--user-id", "mukul"]
    category = args.get("category", "")
    if category:
        cmd += ["--category", category]

    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        return {"error": r.stderr.strip() or "search failed"}
    try:
        return json.loads(r.stdout.strip())
    except json.JSONDecodeError:
        return {"raw": r.stdout.strip()}


HANDLERS = {
    "propose_memory": handle_propose_memory,
    "confirm_proposal": handle_confirm_proposal,
    "search_memory": handle_search_memory,
}


# ── MCP protocol dispatch ───────────────────────────────────────────────────

def handle_request(req: dict) -> None:
    req_id = req.get("id")
    method = req.get("method", "")
    params = req.get("params") or {}

    # Initialization handshake
    if method == "initialize":
        _ok(req_id, {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "assistant-memory", "version": "1.0.0"},
        })
        return

    if method == "notifications/initialized":
        return  # no response for notifications

    if method == "tools/list":
        _ok(req_id, {"tools": TOOLS_LIST})
        return

    if method == "tools/call":
        tool_name = params.get("name", "")
        tool_args = params.get("arguments") or {}
        handler = HANDLERS.get(tool_name)
        if handler is None:
            _err(req_id, -32601, f"Unknown tool: {tool_name}")
            return
        try:
            result = handler(tool_args)
        except Exception as exc:  # noqa: BLE001
            _err(req_id, -32603, str(exc))
            return
        _ok(req_id, {
            "content": [{"type": "text", "text": json.dumps(result, indent=2)}]
        })
        return

    if method == "ping":
        _ok(req_id, {})
        return

    _err(req_id, -32601, f"Method not found: {method}")


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            _send({"jsonrpc": "2.0", "id": None,
                   "error": {"code": -32700, "message": "Parse error"}})
            continue
        handle_request(req)


if __name__ == "__main__":
    main()
