#!/usr/bin/env python3
"""state-write.py — atomically write ~/.claude/cache/assistant-state.json
AND emit a per-pulse trace at ~/.assistant/pulse-trace/<ts>.md.

The trace is the document a human can read tomorrow without grep'ing
three transcripts to figure out what happened. It pulls from:
  - the state JSON itself (actions_taken, awaiting_input, _meta)
  - ~/.assistant/observer-summaries/*.json (per-ws verdicts at time of write)
  - ~/.assistant/sends.jsonl (sends made during this pulse)
  - ~/.assistant/actions-ledger.jsonl (ledger writes during this pulse)

Mechanical only. Reads JSON state from stdin and writes via tmpfile + rename.

Usage:
  cat state.json | state-write.py
"""
import json
import os
import sys
import time
from datetime import datetime, timezone

STATE_PATH = os.path.expanduser("~/.claude/cache/assistant-state.json")
TRACE_DIR = os.path.expanduser("~/.assistant/pulse-trace")
SUMMARIES_DIR = os.path.expanduser("~/.assistant/observer-summaries")
SENDS_LOG = os.path.expanduser("~/.assistant/sends.jsonl")
LEDGER_PATH = os.path.expanduser("~/.assistant/actions-ledger.jsonl")
TRACE_RETENTION = 72 * 3600  # 72h


def utc_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_jsonl_tail(path, since_epoch):
    """Read JSONL records with `epoch` >= since_epoch (or `ts` parsed)."""
    out = []
    if not os.path.exists(path):
        return out
    try:
        with open(path) as f:
            for line in f:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                ts_epoch = d.get("epoch")
                if ts_epoch is None and d.get("ts"):
                    try:
                        ts_epoch = int(datetime.fromisoformat(d["ts"].replace("Z", "+00:00")).timestamp())
                    except Exception:
                        ts_epoch = None
                if ts_epoch is None or ts_epoch >= since_epoch:
                    out.append(d)
    except Exception:
        pass
    return out


def load_observer_summaries():
    """Read every per-ws observer summary so the trace can show classifications."""
    out = {}
    if not os.path.isdir(SUMMARIES_DIR):
        return out
    for name in os.listdir(SUMMARIES_DIR):
        if not name.startswith("workspace_") or not name.endswith(".json"):
            continue
        path = os.path.join(SUMMARIES_DIR, name)
        try:
            d = json.load(open(path))
            out[d.get("ws_ref", name)] = {
                "classification": d.get("classification"),
                "summary": (d.get("summary_for_next_pulse") or d.get("summary") or "")[:200],
                "last_updated_ts": d.get("last_updated_ts"),
                "proposed_actions": d.get("proposed_actions", []),
                "title": d.get("title"),
            }
        except Exception:
            pass
    return out


def write_trace(state, pulse_started_epoch):
    """Write a markdown trace for this pulse."""
    os.makedirs(TRACE_DIR, exist_ok=True)
    pulse_idx = (state.get("_meta") or {}).get("pulse_idx", "?")
    generated_at = (state.get("_meta") or {}).get("generated_at") or utc_iso()
    actions_taken = state.get("actions_taken", []) or []
    awaiting = state.get("awaiting_input", []) or []
    summaries = load_observer_summaries()
    sends = read_jsonl_tail(SENDS_LOG, pulse_started_epoch)
    ledger = read_jsonl_tail(LEDGER_PATH, pulse_started_epoch)

    # Filter ledger to this pulse_idx if available
    if isinstance(pulse_idx, int):
        ledger = [e for e in ledger if e.get("pulse_idx") == pulse_idx]

    out = []
    out.append(f"# Pulse {pulse_idx} — {generated_at}\n")

    # ---- Observers ----
    out.append("## Observers\n")
    if summaries:
        for ws_ref in sorted(summaries.keys()):
            s = summaries[ws_ref]
            cls = s.get("classification") or "?"
            title = s.get("title") or ""
            actions = s.get("proposed_actions") or []
            kinds = ",".join(a.get("kind", "?") for a in actions) if actions else "-"
            out.append(f"- **{ws_ref}** ({title[:50]}) → `{cls}` · proposed: `{kinds}`")
            if s.get("summary"):
                out.append(f"    > {s['summary']}")
        out.append("")
    else:
        out.append("(no observer summaries on disk)\n")

    # ---- Assistant decisions (actions_taken) ----
    out.append("## Assistant decisions (actions_taken)\n")
    if actions_taken:
        for a in actions_taken:
            target = a.get("target", {})
            tgt = target.get("ws") or target.get("ws_ref") or target.get("td") or "?"
            payload = a.get("payload", {})
            payload_str = json.dumps(payload)[:120] if payload else ""
            verified = "✓" if a.get("verified") else "✗"
            out.append(f"- {verified} `{a.get('kind', '?')}` → {tgt} · key=`{a.get('key', '?')}`")
            if payload_str:
                out.append(f"    payload: {payload_str}")
            if a.get("evidence"):
                out.append(f"    evidence: {(a['evidence'] or '')[:200]}")
            if a.get("verification_note"):
                out.append(f"    note: {a['verification_note'][:200]}")
        out.append("")
    else:
        out.append("(no actions taken this pulse)\n")

    # ---- Sends (the load-bearing log) ----
    out.append("## Sends (cmux-send.py)\n")
    if sends:
        for s in sends:
            ws_ref = s.get("target_ws_ref", "?")
            ws_title = (s.get("target_ws_title") or "")[:40]
            tty = s.get("target_tty") or "?"
            text = (s.get("text") or "").replace("\n", " ")[:80]
            delta = s.get("transcript_size_delta")
            outcome = s.get("outcome", "?")
            ingest_marker = "INGEST" if (delta or 0) > 0 else "NO_INGEST" if delta == 0 else "?"
            out.append(
                f"- {s.get('ts')} `{s.get('caller', '?')}` → {ws_ref} ({ws_title}) tty={tty} · "
                f"text={text!r} · outcome={outcome} · delta={delta} **{ingest_marker}**"
            )
        out.append("")
    else:
        out.append("(no sends this pulse)\n")

    # ---- Ledger writes ----
    out.append("## Ledger writes\n")
    if ledger:
        for e in ledger:
            via = e.get("verified_via", "-")
            via_flag = " ⚠ SCREEN_READ" if via == "screen_read" else ""
            out.append(
                f"- `{e.get('outcome', '?')}` `{e.get('kind', '?')}` · ws={e.get('ws_ref') or '-'} "
                f"td={e.get('td') or '-'} · key=`{e.get('key', '?')}` · verified_via=`{via}`{via_flag}"
            )
            if e.get("evidence"):
                out.append(f"    evidence: {(e['evidence'] or '')[:200]}")
        out.append("")
    else:
        out.append("(no ledger writes this pulse)\n")

    # ---- Awaiting input cards ----
    out.append("## Awaiting cards\n")
    if awaiting:
        for c in awaiting:
            out.append(f"- T{c.get('tier', '?')} · `{c.get('key', '?')}` — {(c.get('title') or '')[:80]}")
        out.append("")
    else:
        out.append("(no awaiting cards)\n")

    # ---- Cross-checks (places where reality and the Assistant's claims diverged) ----
    out.append("## Cross-checks\n")
    flags = []
    # 1. Sends with NO_INGEST (delta=0)
    no_ingest = [s for s in sends if s.get("transcript_size_delta") == 0]
    for s in no_ingest:
        flags.append(
            f"- ⚠ Send to {s.get('target_ws_ref')} `{(s.get('text') or '')[:40]!r}` "
            f"had transcript_delta=0 (cmux returned OK but claude did not ingest)"
        )
    # 2. Ledger entries with screen_read evidence (forbidden)
    screen_reads = [e for e in ledger if e.get("verified_via") == "screen_read"]
    for e in screen_reads:
        flags.append(
            f"- ⚠ Ledger entry `{e.get('key')}` used verified_via=screen_read — REJECTED CLASS"
        )
    # 3. Ledger entries with outcome=verified but no verified_via
    bad_verifies = [e for e in ledger if e.get("outcome") == "verified" and not e.get("verified_via")]
    for e in bad_verifies:
        flags.append(
            f"- ⚠ Ledger entry `{e.get('key')}` is outcome=verified but missing verified_via"
        )
    if flags:
        out.extend(flags)
        out.append("")
    else:
        out.append("(no anomalies)\n")

    body = "\n".join(out)

    # Filename: pulse-<idx>-<ts>.md sorts both by idx and by time
    ts_safe = generated_at.replace(":", "").replace("-", "")
    fname = f"pulse-{pulse_idx:0>4}-{ts_safe}.md" if isinstance(pulse_idx, int) else f"pulse-{ts_safe}.md"
    path = os.path.join(TRACE_DIR, fname)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(body)
    os.replace(tmp, path)


def cleanup_old_traces():
    if not os.path.isdir(TRACE_DIR):
        return
    cutoff = time.time() - TRACE_RETENTION
    for name in os.listdir(TRACE_DIR):
        path = os.path.join(TRACE_DIR, name)
        try:
            if os.path.getmtime(path) < cutoff:
                os.unlink(path)
        except Exception:
            pass


def main():
    state = json.load(sys.stdin)
    # Capture pulse start time BEFORE writing — used to filter sends/ledger
    # to records belonging to this pulse. We approximate "pulse start" as
    # 5 minutes before the state's generated_at; ledger entries with ts
    # newer than that are this-pulse activity.
    generated_at = (state.get("_meta") or {}).get("generated_at") or utc_iso()
    try:
        gen_epoch = int(datetime.fromisoformat(generated_at.replace("Z", "+00:00")).timestamp())
    except Exception:
        gen_epoch = int(time.time())
    pulse_started_epoch = gen_epoch - 600  # be generous; state-write happens at end-of-pulse

    # Atomic state write
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_PATH)

    # Best-effort trace; don't fail the state-write if trace fails.
    try:
        write_trace(state, pulse_started_epoch)
        cleanup_old_traces()
    except Exception as e:
        print(f"[state-write] trace dump failed (non-fatal): {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
