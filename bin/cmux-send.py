#!/usr/bin/env python3
"""cmux-send.py — single sanctioned path for sending text to a cmux workspace.

Every text-and-Enter send the Assistant performs MUST go through this script.
The script:

  1. Resolves the *terminal* surface UUID for the target workspace via
     `cmux tree --id-format both` — surface refs (surface:N) are window-scoped
     and `cmux send --surface <ref>` silently misroutes when the pane isn't
     focused. Surface UUIDs route unambiguously.
  2. Sends via `surface.send_text` + `surface.send_key` RPCs (the unambiguous
     form). NEVER `cmux send --workspace <ws>` (picks "active" surface — for
     workspaces with a browser-preview pane on top of the claude PTY, that's
     the browser).
  3. Logs every call to ~/.assistant/sends.jsonl, structured, no
     interpretation. Includes target ws/title/surface/tty, the literal text,
     the RPC return values, and — load-bearing — the post-send transcript
     byte delta. Size unchanged after the send means cmux returned OK but
     claude PID never ingested the keystrokes (routing bug). Size grew means
     the target session actually saw it.

The post-send size delta is the proof field. Today's incident
(2026-05-26 01:36): the Assistant believed cleanup landed at ws:90 because
cmux returned OK, but a screen-read confused itself about a different
workspace's content. With this delta logged, "cmux returned OK + 0 bytes
appended to transcript" is unambiguous evidence the send didn't actually
reach claude.

Usage:
  cmux-send.py --ws workspace:N --text "/merge-when-ready 10362" [--enter]
                [--caller "merge-pr-dispatch.py"] [--no-log]

Exit codes:
  0  — text and Enter both delivered successfully (no claim about ingest)
  1  — could not resolve a terminal surface for the workspace
  2  — RPC error from cmux
  3  — usage error

Output: JSON with the same record written to sends.jsonl, on stdout.
"""
import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

CMUX = os.environ.get("CMUX_BIN", "/Applications/cmux.app/Contents/Resources/bin/cmux")
WORLD = os.path.expanduser("~/.claude/cache/world.json")
SENDS_LOG = os.path.expanduser("~/.assistant/sends.jsonl")


def utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def resolve_terminal_surface(ws_ref):
    """Return (surface_id, surface_ref, tty, ws_title) for the workspace's
    terminal surface, or (None, None, None, None) if none exists."""
    try:
        r = subprocess.run(
            [CMUX, "--id-format", "both", "tree", "--workspace", ws_ref, "--json"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0:
            return None, None, None, None
        d = json.loads(r.stdout)
    except Exception:
        return None, None, None, None
    for win in d.get("windows", []):
        for ws in win.get("workspaces", []):
            if ws.get("ref") != ws_ref:
                continue
            ws_title = ws.get("title", "")
            for pane in ws.get("panes", []):
                for s in pane.get("surfaces", []):
                    if s.get("type") == "terminal" and s.get("id"):
                        return s["id"], s.get("ref"), s.get("tty"), ws_title
    return None, None, None, None


def find_transcript_for_ws(ws_ref):
    """Read world.json to find this workspace's claude transcript path.
    Returns None if not found (workspace has no live claude session)."""
    try:
        w = json.load(open(WORLD))
    except Exception:
        return None
    for s in w.get("live_sessions", []):
        if s.get("ws_ref") == ws_ref:
            return s.get("transcript_path")
    return None


def file_size(path):
    if not path:
        return None
    try:
        return os.path.getsize(path)
    except Exception:
        return None


def append_log(record):
    os.makedirs(os.path.dirname(SENDS_LOG), exist_ok=True)
    with open(SENDS_LOG, "a") as f:
        f.write(json.dumps(record) + "\n")


def rpc(method, payload):
    """Run cmux rpc <method> <json> and return (returncode, parsed_json_or_text)."""
    try:
        r = subprocess.run(
            [CMUX, "rpc", method, json.dumps(payload)],
            capture_output=True, text=True, timeout=10,
        )
    except subprocess.TimeoutExpired:
        return 124, {"error": "timeout"}
    rc = r.returncode
    body = r.stdout.strip() or r.stderr.strip()
    try:
        body = json.loads(body)
    except Exception:
        pass
    return rc, body


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--ws", required=True, help="workspace:N")
    p.add_argument("--text", required=True, help="literal text to send")
    p.add_argument("--enter", action="store_true",
                   help="press Enter after sending text (default: do not press Enter)")
    p.add_argument("--caller", default=os.path.basename(sys.argv[0]),
                   help="who is calling — recorded in the log")
    p.add_argument("--no-log", action="store_true",
                   help="do not append to sends.jsonl (for debug runs)")
    p.add_argument("--post-send-wait", type=float, default=2.5,
                   help="seconds to wait before re-stat'ing the transcript "
                        "for the size-delta proof (default 2.5)")
    args = p.parse_args()

    ts = utc_iso()
    started_at = time.time()

    surface_id, surface_ref, tty, ws_title = resolve_terminal_surface(args.ws)
    transcript_path = find_transcript_for_ws(args.ws)
    size_before = file_size(transcript_path)

    record = {
        "ts": ts,
        "caller": args.caller,
        "caller_pid": os.getppid(),
        "target_ws_ref": args.ws,
        "target_ws_title": ws_title,
        "target_surface_id": surface_id,
        "target_surface_ref": surface_ref,
        "target_tty": tty,
        "text": args.text,
        "send_enter": bool(args.enter),
        "transcript_path": transcript_path,
        "transcript_size_before": size_before,
    }

    if not surface_id:
        record["outcome"] = "no_terminal_surface"
        record["transcript_size_after"] = size_before
        record["transcript_size_delta"] = 0
        if not args.no_log:
            append_log(record)
        print(json.dumps(record, indent=2))
        sys.exit(1)

    # Step 1: send_text
    rc1, body1 = rpc("surface.send_text", {"surface_id": surface_id, "text": args.text})
    record["rpc_send_text"] = {"rc": rc1, "body": body1}
    if rc1 != 0:
        record["outcome"] = "rpc_send_text_failed"
        size_after = file_size(transcript_path)
        record["transcript_size_after"] = size_after
        record["transcript_size_delta"] = (size_after or 0) - (size_before or 0)
        if not args.no_log:
            append_log(record)
        print(json.dumps(record, indent=2))
        sys.exit(2)

    # Step 2 (optional): send_key Enter
    if args.enter:
        rc2, body2 = rpc("surface.send_key", {"surface_id": surface_id, "key": "enter"})
        record["rpc_send_key"] = {"rc": rc2, "body": body2}
        if rc2 != 0:
            record["outcome"] = "rpc_send_key_failed"
            size_after = file_size(transcript_path)
            record["transcript_size_after"] = size_after
            record["transcript_size_delta"] = (size_after or 0) - (size_before or 0)
            if not args.no_log:
                append_log(record)
            print(json.dumps(record, indent=2))
            sys.exit(2)

    # Wait for claude to ingest + flush before stat'ing
    time.sleep(args.post_send_wait)
    size_after = file_size(transcript_path)
    record["transcript_size_after"] = size_after
    record["transcript_size_delta"] = (size_after or 0) - (size_before or 0)
    record["wall_ms"] = int((time.time() - started_at) * 1000)
    record["outcome"] = "sent"

    if not args.no_log:
        append_log(record)
    print(json.dumps(record, indent=2))
    sys.exit(0)


if __name__ == "__main__":
    main()
