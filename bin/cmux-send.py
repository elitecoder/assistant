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
  3. Records the intent in the effects ledger (~/.assistant/effects) before the first keystroke and the result after.
     No record, no typing: when the intent can't be written, or the same text has been typed into the same
     session three times in half an hour, nothing is sent and the script exits 4. The record is made here, not in
     each caller, so nothing can type through this script unrecorded. It holds the session and where the text came
     from (--source), never the text.
  4. Logs every call to ~/.assistant/sends.jsonl, structured, no
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
                [--source merge] [--auth-kind tap|standing] [--auth-ref cli:cmux-send] [--task td-N]

  Run by hand it counts as a person's tap (the defaults). A script that runs it says what allowed the typing.

Exit codes:
  0  — text and Enter both delivered successfully (no claim about ingest)
  1  — could not resolve a terminal surface for the workspace
  2  — RPC error from cmux: it refused the text or the Enter
  5  — cmux didn't answer in time (outcome rpc_send_text_timeout or rpc_send_key_timeout): the text MAY have been
       typed, so a caller must not take this for "not sent"
  3  — usage error
  4  — refused: the intent couldn't be recorded, or this is a loop, or it was already typed (nothing was sent)

Output: JSON with the same record written to sends.jsonl, on stdout.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
try:
    from assistant import session_gate  # noqa: E402 - the default mode types through it; stdlib only
except Exception:  # noqa: BLE001 - nothing here may stop the default mode from starting
    session_gate = None

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


def _pro() -> bool:
    """Pro mode. Asked once, before the options are read. Anything that goes wrong asking is the default mode."""
    try:
        return bool(session_gate and session_gate.active())
    except Exception:  # noqa: BLE001
        return False


def legacy_main(args):
    """What this script did before typing was recorded, exactly: the default mode runs this, loading nothing from
    `assistant` but the pass-through keys in `assistant.session_gate`. Its results and exit statuses are 0, 1 and 2
    only: a time-out is a refusal here, as it always was."""
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

    answers = {}

    def transport(method, payload):
        rc, body = rpc(method, payload)
        answers[method.rsplit("_", 1)[-1]] = (rc, body)
        return ({} if body is None else body) if rc == 0 else None

    keys = session_gate.Keys(transport, surface_id)

    # Step 1: send_text
    keys.text(args.text)
    rc1, body1 = answers["text"]
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
        keys.key("enter")
        rc2, body2 = answers["key"]
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


def build_parser(pro: bool):
    """The options. The default mode has the ones this script always had (so its errors, its abbreviations and its
    `--help` are what they always were); Pro mode adds what the record of a typing needs."""
    p = argparse.ArgumentParser()
    p.add_argument("--ws", required=True, help="workspace:N")
    p.add_argument("--text", required=True, help="literal text to send")
    p.add_argument("--enter", action="store_true",
                   help="press Enter after sending text (default: do not press Enter)")
    p.add_argument("--caller", default=os.path.basename(sys.argv[0]),
                   help="who is calling — recorded in the log")
    if pro:
        p.add_argument("--source", default="cli", help="where the text comes from, a fixed name (recorded)")
        p.add_argument("--auth-kind", default="tap", choices=("tap", "standing", "owner_flag"),
                       help="what allowed the typing: a person's tap, a standing setting, or a task's flag")
        p.add_argument("--auth-ref", default="cli:cmux-send", help="a pointer to what allowed it (recorded)")
        p.add_argument("--task", default="", help="the task this is for, td-N (recorded)")
    p.add_argument("--no-log", action="store_true",
                   help="do not append to sends.jsonl (for debug runs)")
    p.add_argument("--post-send-wait", type=float, default=2.5,
                   help="seconds to wait before re-stat'ing the transcript "
                        "for the size-delta proof (default 2.5)")
    return p


def main():
    pro = _pro()  # asked once, before the options are read: the default mode is what this script always was
    args = build_parser(pro).parse_args()
    if not pro:
        legacy_main(args)
    try:
        from assistant import session_effects  # noqa: PLC0415 - Pro mode only
    except Exception as exc:  # noqa: BLE001 - loud: without its record nothing is typed
        print(json.dumps({"outcome": "refused", "refused": {
            "code": "unrecorded", "reason": f"the record of what is typed can't be loaded ({type(exc).__name__}: {exc})"}},
            indent=2))
        print(f"cmux-send: the record of what is typed can't be loaded, so nothing was typed ({exc})", file=sys.stderr)
        sys.exit(4)

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

    # The intent goes to disk before the first keystroke; without it nothing is typed. A typing refused before
    # anything was sent leaves no row in sends.jsonl: the pulse reads that file as "the last send never landed", and
    # would then skip this nudge for good.
    def refuse(refusal):
        record["outcome"] = "refused"
        record["refused"] = {"code": refusal.code, "reason": str(refusal)}
        record["transcript_size_after"] = size_before
        record["transcript_size_delta"] = 0
        print(json.dumps(record, indent=2))
        print(f"cmux-send: {refusal}", file=sys.stderr)
        sys.exit(4)

    component = re.sub(r"[^A-Za-z0-9_:./-]", "-", os.path.basename(args.caller))[:64] or "cmux-send"
    try:
        effect_id = session_effects.begin(
            args.ws, args.source, kind=args.auth_kind, ref=args.auth_ref, task=args.task, component=component)
    except session_effects.Refused as refusal:
        refuse(refusal)

    # What cmux answered to each keystroke call, for the log. A timeout is "can't tell": the call may have landed.
    # Any exit status 0 is an answer, as it always was; a body that isn't JSON (or is `null`) is not a refusal.
    answers = {}

    def transport(method, payload):
        rc, body = rpc(method, payload)
        answers[method.rsplit("_", 1)[-1]] = (rc, body)  # "text" or "key"
        if rc == 124:
            raise TimeoutError(method)
        return ({} if body is None else body) if rc == 0 else None

    try:
        keys = session_effects.Keys(effect_id, transport, surface_id, args.ws)
    except session_effects.Refused as refusal:
        session_effects.finish(effect_id, "failed", error="not recorded")
        refuse(refusal)

    def end(outcome, code):
        """Close the effect, say how it ended, and exit."""
        record["outcome"] = outcome
        size_after = file_size(transcript_path)
        record["transcript_size_after"] = size_after
        record["transcript_size_delta"] = (size_after or 0) - (size_before or 0)
        # "verified" is only said when there was a transcript to watch: it grew, or it didn't.
        if code == 0 and size_before is not None:
            keys.confirmed = record["transcript_size_delta"] > 0
        keys.finish()
        if code == 0:
            record["wall_ms"] = int((time.time() - started_at) * 1000)
        if not args.no_log:
            append_log(record)
        print(json.dumps(record, indent=2))
        sys.exit(code)

    try:
        # Step 1: send_text
        ok = keys.text(args.text)
        record["rpc_send_text"] = dict(zip(("rc", "body"), answers.get("text", (None, None))))
        if not ok:
            end("rpc_send_text_timeout" if keys.raised else "rpc_send_text_failed", 5 if keys.raised else 2)

        # Step 2 (optional): send_key Enter
        if args.enter:
            ok = keys.key("enter")
            record["rpc_send_key"] = dict(zip(("rc", "body"), answers.get("key", (None, None))))
            if not ok:
                end("rpc_send_key_timeout" if keys.raised else "rpc_send_key_failed", 5 if keys.raised else 2)

        # Wait for claude to ingest + flush before stat'ing
        time.sleep(args.post_send_wait)
        end("sent", 0)
    finally:
        keys.finish()  # whatever happened above, the intent gets its result: failed if nothing went out, else unknown


if __name__ == "__main__":
    main()
