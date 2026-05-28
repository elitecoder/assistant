#!/usr/bin/env python3
"""pulse.py — mechanical Assistant orchestrator. Replaces the LLM-driven loop.

Run by the com.assistant.assistant-pulse LaunchAgent every 2 min. Single
process, no LLM in the orchestration layer. The only LLM call is the
per-workspace Observer subprocess (claude --print + observer prompt).

Pipeline (in order):

  1. Drain ~/.assistant/inbox/pulse-*.json (delete after reading).
  2. Run bin/purge-stale-awaiting.py (mechanical card cleanup).
  3. Pick batch via bin/pick-ws-batch.py — already filters back-off list.
  4. For each ws in batch:
       a. bin/build-ws-context.py → JSON ctx
       b. spawn Observer subprocess → emit one verdict JSON
       c. bin/save-ws-summary.py (rejects verdicts missing `next`)
       d. Execute via lookup table:
            ready_for_merge   → cmux-send.py /merge-when-ready
            ready_for_cleanup → cmux-send.py /cleanup
            stranded          → cmux-send.py <nudge_text>
            needs_user        → append awaiting_input
            active            → no-op
            no_action         → no-op
       e. Log every action to actions-ledger.jsonl
  5. Dispatch open TODOs via bin/pick-open-todos.py (cap: 5 active, 30 total,
     2 per pulse).
  6. Pipe state JSON to bin/state-write.py — writes assistant-state.json +
     pulse-trace markdown.
  7. Update ~/.assistant/heartbeat.json.

The bugs we built this for don't exist here:
  - Verdict shape is enforced by save-ws-summary.py (rejects missing `next`).
  - Verdict→action is a Python dict, not a prompt example the LLM can drift from.
  - Back-off filter is upstream of every per-ws step (pick-ws-batch).
  - Send loops are bounded: a workspace whose previous send returned delta=0
    AND whose verdict hasn't changed gets skipped this pulse (NO_INGEST_GUARD).
  - Restart is `launchctl kickstart -k`, not "kill PID + backdate heartbeat".
"""
from __future__ import annotations

import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BIN = REPO / "bin"
HOME = Path.home()

ASSISTANT_DIR = HOME / ".assistant"
INBOX_DIR = ASSISTANT_DIR / "inbox"
PULSE_LOG = ASSISTANT_DIR / "assistant-pulse.log"
HEARTBEAT_PATH = ASSISTANT_DIR / "heartbeat.json"
LEDGER_PATH = ASSISTANT_DIR / "actions-ledger.jsonl"
STATE_PATH = HOME / ".claude/cache/assistant-state.json"
SUMMARIES_DIR = ASSISTANT_DIR / "observer-summaries"
SENDS_LOG = ASSISTANT_DIR / "sends.jsonl"
OBSERVER_RUNS_DIR = ASSISTANT_DIR / "observer-runs"
# Observer-runs are the LLM transcript archive. We never delete them — disk
# is cheap, the audit trail is not. If this ever grows unmanageable, prune
# by hand or export to cold storage. The pulse loop does NOT touch it.
OBSERVER_PROMPT = REPO / "prompts/observer-prompt.md"
OBSERVER_BATCH_PROMPT = REPO / "prompts/observer-batch-prompt.md"
SPAWN_SKILL = HOME / ".claude/skills/spawn-claude-workspace/SKILL.md"

DEFAULT_OBSERVER_MODEL = os.environ.get(
    "OBSERVER_MODEL",
    "us.anthropic.claude-sonnet-4-6[1m]",
)
DEFAULT_CLAUDE_BIN = os.environ.get("CLAUDE_BIN", str(HOME / ".local/bin/claude"))

# One Observer subprocess judges WS_BATCH_SIZE workspaces. With 30 ws and
# size=10, we spawn 3 Observers in parallel each pulse. Each call gets longer
# context (10 transcripts to read), so timeout scales accordingly.
WS_BATCH_SIZE = int(os.environ.get("WS_BATCH_SIZE", "10"))
OBSERVER_TIMEOUT_SEC = int(os.environ.get("OBSERVER_TIMEOUT_SEC", "600"))

# Cap dispatched TODO spawns. We don't hammer the user's machine on a single pulse.
MAX_DISPATCH_PER_PULSE = 2

# Cap concurrent active workspaces (matches old prompt's rule).
ACTIVE_WS_CAP = 5
TOTAL_WS_CAP = 30

# Acceptable verdict kinds. Anything else falls through to a logged no-op.
VALID_VERDICTS = {
    "ready_for_merge",
    "ready_for_cleanup",
    "stranded",
    "needs_user",
    "active",
    "no_action",
}

logging.basicConfig(
    filename=str(PULSE_LOG),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s pulse.py %(message)s",
)
log = logging.getLogger("pulse")


# ─── helpers ────────────────────────────────────────────────────────────────

def utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_ts() -> int:
    return int(time.time())


def run(cmd: list[str], *, input_text: str | None = None,
        timeout: int = 30, env: dict | None = None) -> tuple[int, str, str]:
    """Run a subprocess; return (rc, stdout, stderr). Never raises."""
    try:
        proc = subprocess.run(
            cmd, input=input_text, capture_output=True, text=True,
            timeout=timeout, env=env,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        return 124, "", f"timeout after {timeout}s"
    except Exception as e:
        return 1, "", str(e)


def append_ledger(entry: dict) -> None:
    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LEDGER_PATH, "a") as f:
        f.write(json.dumps(entry) + "\n")


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {"_meta": {"pulse_idx": 0}, "actions_taken": [], "awaiting_input": []}
    try:
        return json.loads(STATE_PATH.read_text())
    except Exception:
        return {"_meta": {"pulse_idx": 0}, "actions_taken": [], "awaiting_input": []}


# ─── steps ──────────────────────────────────────────────────────────────────

def drain_inbox() -> int:
    if not INBOX_DIR.exists():
        return 0
    n = 0
    for p in sorted(INBOX_DIR.glob("pulse-*.json")):
        try:
            p.unlink()
            n += 1
        except Exception:
            pass
    return n


def purge_stale_awaiting() -> None:
    rc, _, err = run([sys.executable, str(BIN / "purge-stale-awaiting.py")])
    if rc != 0:
        log.warning("purge-stale-awaiting rc=%d: %s", rc, err.strip())


def pick_ws_batch() -> dict:
    """Returns dict with to_reclassify, reuse_cached, backed_off, total_ws."""
    rc, out, err = run([sys.executable, str(BIN / "pick-ws-batch.py")])
    if rc != 0:
        log.error("pick-ws-batch rc=%d: %s", rc, err.strip())
        return {"to_reclassify": [], "reuse_cached": [], "backed_off": [], "total_ws": 0}
    try:
        return json.loads(out)
    except Exception as e:
        log.error("pick-ws-batch bad json: %s", e)
        return {"to_reclassify": [], "reuse_cached": [], "backed_off": [], "total_ws": 0}


def build_ctx(ws: dict) -> dict | None:
    rc, out, err = run([
        sys.executable, str(BIN / "build-ws-context.py"),
        "--ws-ref", ws["ref"],
        "--title", ws.get("title") or "",
        "--cwd", ws.get("cwd") or "",
    ])
    if rc != 0:
        log.error("build-ws-context %s rc=%d: %s", ws.get("ref"), rc, err.strip())
        return None
    try:
        return json.loads(out)
    except Exception as e:
        log.error("build-ws-context %s bad json: %s", ws.get("ref"), e)
        return None


def call_observer_batch(ctxs: list[dict], pulse_idx: int, batch_idx: int) -> dict[str, dict]:
    """Spawn ONE Observer subprocess to judge a batch of workspaces.

    The Observer writes its structured output to <run_dir>/verdicts.jsonl.
    pulse.py reads that file. We do NOT parse stdout for verdicts — stdout
    is captured to <run_dir>/stdout.txt as the LLM work transcript (tool
    calls, intermediate reasoning, the final result envelope).

    Returns {ws_ref: verdict-dict}. Missing ws_refs in the result mean the
    Observer didn't emit a line for them; the caller treats that as a
    skipped action.
    """
    if not ctxs:
        return {}
    if not OBSERVER_BATCH_PROMPT.exists():
        log.error("observer batch prompt missing: %s", OBSERVER_BATCH_PROMPT)
        return {}

    run_dir = OBSERVER_RUNS_DIR / f"{pulse_idx:04d}" / f"batch-{batch_idx}"
    run_dir.mkdir(parents=True, exist_ok=True)
    verdicts_path = run_dir / "verdicts.jsonl"
    prompt_path = run_dir / "prompt.md"
    ctxs_path = run_dir / "ctxs.json"
    stdout_path = run_dir / "stdout.txt"
    stderr_path = run_dir / "stderr.txt"
    meta_path = run_dir / "meta.json"

    base = OBSERVER_BATCH_PROMPT.read_text()
    ctxs_json = json.dumps(ctxs, indent=2)
    prompt = (
        base
        + "\n\n---\n\n## RUNTIME CONTEXT\n\n"
        + f"You are judging this batch of {len(ctxs)} workspace(s). "
          "Read each transcript at its `transcript_path` "
          "(start with `tail -200 <path>`; read more if the verdict isn't obvious).\n\n"
        + "**Output destination — write your structured verdicts to a file, not stdout.** "
          f"Use the Write tool (or `printf > <path>`) to write your JSONL output to:\n\n"
        + f"    {verdicts_path}\n\n"
          "One JSON object per line, each tagged with `ws_ref` and `verdict`. "
          "The orchestrator reads that file directly. Anything you print to stdout is "
          "treated as work-trail diagnostic only and is never parsed for verdicts. "
          "If you write nothing to the file, every workspace in this batch is treated "
          "as an Observer failure for this pulse.\n\n"
        + "Workspace ctxs to judge:\n\n"
        + "```json\n" + ctxs_json + "\n```\n"
    )

    # Persist input artifacts before spawning so we have them even if the
    # subprocess dies / is killed.
    prompt_path.write_text(prompt)
    ctxs_path.write_text(ctxs_json)

    cmd = [
        DEFAULT_CLAUDE_BIN,
        "--model", DEFAULT_OBSERVER_MODEL,
        "--dangerously-skip-permissions",
        "--print",
        "--add-dir", str(REPO / "prompts"),
        "--add-dir", str(HOME / ".claude/projects"),
        "--add-dir", str(run_dir),  # so the model can write verdicts.jsonl
    ]
    seen_dirs = {str(REPO / "prompts"), str(HOME / ".claude/projects"), str(run_dir)}
    for c in ctxs:
        cwd = c.get("cwd")
        if cwd and Path(cwd).is_dir() and cwd not in seen_dirs:
            cmd += ["--add-dir", cwd]
            seen_dirs.add(cwd)

    t0 = time.time()
    rc, out, err = run(cmd, input_text=prompt, timeout=OBSERVER_TIMEOUT_SEC)
    wall_ms = int((time.time() - t0) * 1000)

    # Always persist stdout/stderr — these are the LLM work transcript even
    # if the run failed.
    stdout_path.write_text(out or "")
    stderr_path.write_text(err or "")
    meta_path.write_text(json.dumps({
        "rc": rc,
        "wall_ms": wall_ms,
        "model": DEFAULT_OBSERVER_MODEL,
        "cmd": cmd,
        "ws_refs": [c.get("ws_ref") for c in ctxs],
        "ts": utc_iso(),
    }, indent=2))

    if rc != 0:
        log.warning("observer batch (size=%d) rc=%d: %s",
                    len(ctxs), rc, err.strip()[-300:])

    return read_verdicts_file(verdicts_path)


def read_verdicts_file(path: Path) -> dict[str, dict]:
    """Read JSONL written by Observer. Each line is a JSON object with
    `ws_ref` and `verdict`. Lines that don't parse, or that lack
    ws_ref/verdict, are silently dropped — the orchestrator treats missing
    ws_refs as Observer failures."""
    if not path.exists():
        return {}
    out: dict[str, dict] = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("```"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        ws = obj.get("ws_ref")
        if not ws or not obj.get("verdict"):
            continue
        out[ws] = obj
    return out


def chunk(items: list, size: int) -> list[list]:
    """Split items into lists of at most `size`."""
    if size <= 0:
        return [items]
    return [items[i:i + size] for i in range(0, len(items), size)]


def save_summary(ws: dict, verdict: dict) -> None:
    """Persist the verdict via save-ws-summary.py. The save script rejects
    verdicts missing `next` — we treat its rejection as a hard failure here
    (rather than re-running with a synthesized next), because that's the
    contract the dashboard's NEXT line depends on."""
    rc, out, err = run([
        sys.executable, str(BIN / "save-ws-summary.py"),
        "--ws-ref", ws["ref"],
        "--title", ws.get("title") or "",
        "--cwd", ws.get("cwd") or "",
        "--json", json.dumps(verdict),
    ])
    if rc != 0:
        log.error("save-ws-summary %s rc=%d: %s", ws["ref"], rc, err.strip())


def cmux_send(ws_ref: str, text: str, *, caller: str = "assistant-pulse") -> dict:
    """Returns the parsed JSON record cmux-send.py wrote (which captures
    rpc results and transcript-byte delta). On failure returns
    {'outcome': 'failed', 'transcript_size_delta': 0}."""
    rc, out, err = run([
        sys.executable, str(BIN / "cmux-send.py"),
        "--ws", ws_ref,
        "--text", text,
        "--enter",
        "--caller", caller,
    ])
    if rc != 0:
        log.warning("cmux-send %s rc=%d: %s", ws_ref, rc, err.strip()[-200:])
        return {"outcome": "failed", "transcript_size_delta": 0}
    try:
        return json.loads(out)
    except Exception:
        return {"outcome": "ok-unparsed", "transcript_size_delta": None}


def previous_send_ingested(ws_ref: str, text: str) -> bool:
    """Look at the last send to ws_ref. If text matched AND delta=0, the
    previous send was a NO_INGEST. Returning False here causes the caller
    to skip re-sending — breaks the cleanup-loop class of bug.

    Tail sends.jsonl from the bottom; first matching ws_ref wins."""
    if not SENDS_LOG.exists():
        return True  # no history → assume ok
    try:
        with open(SENDS_LOG, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 200_000))
            tail = f.read().decode("utf-8", errors="replace")
    except Exception:
        return True
    for line in reversed(tail.splitlines()):
        if not line.strip():
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        if d.get("target_ws_ref") != ws_ref:
            continue
        # Only consider the most recent prior send to this ws (any text).
        if d.get("text") == text:
            return bool(d.get("transcript_size_delta"))
        return True  # previous send was different text — not in a stuck loop
    return True


# ─── verdict execution ──────────────────────────────────────────────────────

def execute_verdict(ws: dict, verdict: dict, awaiting: list[dict]) -> dict:
    """Execute the action implied by the verdict. Returns an action_taken
    dict for the state file."""
    kind = verdict.get("verdict")
    ws_ref = ws["ref"]
    key = f"{ws_ref}-{kind}"
    base = {
        "key": key, "kind": kind, "ws_ref": ws_ref,
        "outcome": "verified", "verified_via": "observer",
        "evidence": (verdict.get("summary") or "")[:240],
    }

    if kind not in VALID_VERDICTS:
        log.warning("unknown verdict %r for %s — skipping", kind, ws_ref)
        return {**base, "kind": "unknown", "outcome": "failed"}

    # No-ops first.
    if kind in ("active", "no_action"):
        return {**base, "kind": "noop"}

    # awaiting card.
    if kind == "needs_user":
        title = (verdict.get("title") or "").strip() or f"{ws_ref} needs attention"
        detail = (verdict.get("detail") or verdict.get("summary") or "").strip()
        awaiting.append({
            "key": f"{ws_ref}:needs_user",
            "tier": "T2",
            "title": title[:120],
            "detail": detail[:1200],
            "ws_ref": ws_ref,
        })
        return {**base, "kind": "emit-card"}

    # Sends. Apply NO_INGEST_GUARD: if the same text was sent last and got
    # delta=0, skip this pulse rather than loop forever.
    if kind == "ready_for_merge":
        text = "/merge-when-ready"
    elif kind == "ready_for_cleanup":
        text = "/cleanup"
    elif kind == "stranded":
        text = (verdict.get("nudge_text") or "").strip()
        if not text:
            log.warning("stranded verdict for %s missing nudge_text", ws_ref)
            return {**base, "kind": "skipped", "outcome": "failed",
                    "evidence": "missing nudge_text"}
    else:
        return {**base, "kind": "noop"}

    if not previous_send_ingested(ws_ref, text):
        log.info("NO_INGEST_GUARD skipped resend %s text=%r", ws_ref, text)
        return {**base, "kind": "skipped",
                "evidence": f"prior send {text!r} returned delta=0; skipping resend"}

    rec = cmux_send(ws_ref, text)
    delta = rec.get("transcript_size_delta")
    base["evidence"] = f"sent {text!r} delta={delta}"
    if rec.get("outcome") not in ("sent", "ok-unparsed"):
        base["outcome"] = "failed"
    return base


# ─── TODO dispatch ──────────────────────────────────────────────────────────

def count_active(ws_meta: list[dict]) -> int:
    """Count workspaces with last_turn_age_sec < 600 OR agent_status=working."""
    n = 0
    for w in ws_meta:
        ctx = w.get("ctx") or {}
        age = ctx.get("last_turn_age_sec")
        if ctx.get("agent_status") == "working":
            n += 1
            continue
        if age is not None and age < 600:
            n += 1
    return n


def pick_open_todos() -> dict:
    rc, out, err = run([sys.executable, str(BIN / "pick-open-todos.py")])
    if rc != 0:
        log.warning("pick-open-todos rc=%d: %s", rc, err.strip())
        return {"bucket_a": [], "bucket_b": [], "bucket_c": [], "totals": {}}
    try:
        return json.loads(out)
    except Exception:
        return {"bucket_a": [], "bucket_b": [], "bucket_c": [], "totals": {}}


def dispatch_todo(todo_id: str) -> bool:
    """Spawn a workspace for one TODO via the spawn skill. Returns True on success.

    The skill is bash inside SKILL.md; we invoke it by piping the spawn-claude-workspace
    skill body verbatim isn't appropriate for a python harness. Instead, we shell out
    to the wrapper script the existing prompt uses. If no wrapper exists, log + skip.
    """
    # Conservative: skip dispatch from the orchestrator if no wrapper exists.
    # The skill is designed to be invoked by an LLM agent that handles all the
    # cmux RPC steps. Re-implementing it here is out of scope for the swap.
    log.info("dispatch_todo %s — orchestrator dispatch not implemented (skipping)",
             todo_id)
    return False


# ─── main ───────────────────────────────────────────────────────────────────

def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dry-run", action="store_true",
                    help="Skip Observer LLM calls, sends, save-summary, and state-write. "
                         "Just exercise inbox drain + batch picking + summary loading and report.")
    ap.add_argument("--once", action="store_true",
                    help="Run one pulse and exit (default — kept for explicitness).")
    args = ap.parse_args()
    dry_run = args.dry_run

    ASSISTANT_DIR.mkdir(parents=True, exist_ok=True)
    INBOX_DIR.mkdir(parents=True, exist_ok=True)
    SUMMARIES_DIR.mkdir(parents=True, exist_ok=True)

    state = load_state()
    pulse_idx = int(state.get("_meta", {}).get("pulse_idx", 0)) + 1
    t0 = time.time()
    log.info("=== pulse %d start (dry_run=%s) ===", pulse_idx, dry_run)

    # 1. Drain inbox.
    n_drained = 0 if dry_run else drain_inbox()

    # 2. Purge stale awaiting cards.
    if not dry_run:
        purge_stale_awaiting()

    # 3. Pick batch.
    batch = pick_ws_batch()
    to_reclassify = batch.get("to_reclassify", [])
    backed_off = batch.get("backed_off", [])

    # 4. Per-workspace flow.
    #    Phase 1: build ctx for every ws (sequential — these are quick).
    #    Phase 2: chunk into batches, fan out Observer calls in parallel.
    #    Phase 3: per ws, save the returned verdict + execute its action.
    actions_taken: list[dict] = []
    awaiting_input: list[dict] = []
    ws_meta: list[dict] = []
    ctxs_by_ref: dict[str, dict] = {}
    ws_by_ref: dict[str, dict] = {}

    for ws in to_reclassify:
        ws_ref = ws["ref"]
        ws_by_ref[ws_ref] = ws
        ctx = build_ctx(ws)
        ws_meta.append({"ref": ws_ref, "ctx": ctx})
        if not ctx:
            actions_taken.append({
                "key": f"{ws_ref}-build-ctx-failed",
                "kind": "skipped", "ws_ref": ws_ref, "outcome": "failed",
                "evidence": "build-ws-context returned no JSON",
            })
            continue
        ctxs_by_ref[ws_ref] = ctx

    if dry_run:
        for ws_ref, ctx in ctxs_by_ref.items():
            actions_taken.append({
                "key": f"{ws_ref}-dry-run",
                "kind": "noop", "ws_ref": ws_ref, "outcome": "verified",
                "evidence": f"dry-run: would observe ws (ctx transcript_path={ctx.get('transcript_path')!r}, "
                            f"agent_status={ctx.get('agent_status')!r}, "
                            f"last_turn_age_sec={ctx.get('last_turn_age_sec')!r})",
            })
    else:
        # Phase 2: parallel batched Observer calls.
        ctxs_to_observe = list(ctxs_by_ref.values())
        batches = chunk(ctxs_to_observe, WS_BATCH_SIZE)
        log.info("observing %d ws in %d batch(es) of <=%d in parallel",
                 len(ctxs_to_observe), len(batches), WS_BATCH_SIZE)

        verdicts_by_ref: dict[str, dict] = {}
        if batches:
            # Spawn one subprocess per batch in parallel. ThreadPoolExecutor is
            # fine here: each subprocess is fully isolated and we're I/O-bound
            # on the LLM call.
            from concurrent.futures import ThreadPoolExecutor, as_completed
            with ThreadPoolExecutor(max_workers=len(batches)) as ex:
                futures = {
                    ex.submit(call_observer_batch, b, pulse_idx, i): b
                    for i, b in enumerate(batches)
                }
                for fut in as_completed(futures):
                    try:
                        verdicts_by_ref.update(fut.result())
                    except Exception as e:
                        log.exception("observer batch crashed: %s", e)

        # Phase 3: save + execute per ws. Drop the synthetic `ws_ref` field
        # from the verdict before save (save-ws-summary writes it back from
        # its --ws-ref flag; we don't want it in the json blob twice).
        for ws_ref, ctx in ctxs_by_ref.items():
            ws = ws_by_ref[ws_ref]
            verdict = verdicts_by_ref.get(ws_ref)
            if not verdict:
                actions_taken.append({
                    "key": f"{ws_ref}-observer-failed",
                    "kind": "skipped", "ws_ref": ws_ref, "outcome": "failed",
                    "evidence": "observer call returned no verdict for this ws",
                })
                continue
            v_for_save = {k: v for k, v in verdict.items() if k != "ws_ref"}
            save_summary(ws, v_for_save)
            action = execute_verdict(ws, v_for_save, awaiting_input)
            actions_taken.append(action)
            append_ledger({
                "ts": utc_iso(),
                "epoch": utc_ts(),
                "pulse_idx": pulse_idx,
                **action,
            })

    # Note backed-off workspaces in the trace.
    for bo in backed_off:
        actions_taken.append({
            "key": f"{bo['ref']}-backed-off",
            "kind": "backed-off", "ws_ref": bo["ref"], "outcome": "verified",
            "evidence": (bo.get("reason") or "")[:200],
        })

    # 5. TODO dispatch (best-effort; orchestrator-side spawn not implemented).
    todos = pick_open_todos()
    bucket_b = todos.get("bucket_b", [])
    n_active = count_active(ws_meta)
    n_total = batch.get("total_ws", 0)
    if bucket_b and (n_active >= ACTIVE_WS_CAP or n_total >= TOTAL_WS_CAP):
        actions_taken.append({
            "key": f"dispatch-cap-hit-{n_active}-active",
            "kind": "skipped", "outcome": "verified",
            "evidence": f"active={n_active}/{ACTIVE_WS_CAP} total={n_total}/{TOTAL_WS_CAP}",
        })
    elif bucket_b:
        priority_order = {"P0": 0, "P1": 1, "P2": 2, "P3": 3, "P4": 4}
        bucket_b.sort(key=lambda t: priority_order.get(t.get("priority", "P4"), 9))
        n_dispatched = 0
        for todo in bucket_b[:MAX_DISPATCH_PER_PULSE]:
            ok = dispatch_todo(todo["id"])
            actions_taken.append({
                "key": f"dispatch-{todo['id']}",
                "kind": "dispatch" if ok else "dispatch-skipped",
                "outcome": "verified" if ok else "deferred",
                "evidence": f"todo={todo['id']} priority={todo.get('priority','?')}",
            })
            if ok:
                n_dispatched += 1

    # 6. Write state + trace.
    if dry_run:
        # In dry-run mode just print a brief summary and skip state-write/heartbeat.
        print(json.dumps({
            "pulse_idx": pulse_idx,
            "to_reclassify": [w["ref"] for w in to_reclassify],
            "backed_off": [b["ref"] for b in backed_off],
            "actions_taken": actions_taken,
            "awaiting_input": awaiting_input,
            "duration_sec": round(time.time() - t0, 2),
        }, indent=2))
        return 0

    state_payload = {
        "_meta": {
            "pulse_idx": pulse_idx,
            "ts": utc_iso(),
            "drained_inbox": n_drained,
            "duration_sec": round(time.time() - t0, 2),
            "n_observed": len(to_reclassify),
            "n_backed_off": len(backed_off),
        },
        "actions_taken": actions_taken,
        "awaiting_input": awaiting_input,
    }
    rc, _, err = run(
        [sys.executable, str(BIN / "state-write.py")],
        input_text=json.dumps(state_payload),
    )
    if rc != 0:
        log.error("state-write rc=%d: %s", rc, err.strip())

    # 7. Heartbeat.
    write_heartbeat(pulse_idx, n_drained)

    log.info("=== pulse %d done in %.1fs ===", pulse_idx, time.time() - t0)
    return 0


def write_heartbeat(pulse_idx: int, drained: int) -> None:
    """The mechanical pulse has no cmux workspace of its own — it runs as a
    LaunchAgent. We still write heartbeat.json so existing watchers (e.g.
    spawn-assistant.sh's staleness check) see the pulse is alive."""
    payload = {
        "ws_ref": "(launchd)",
        "surface_ref": None,
        "last_pulse_iso": utc_iso(),
        "last_pulse_ts": utc_ts(),
        "pulses_drained_this_run": drained,
        "status": "running",
        "model": "python-mechanical",
        "pulse_idx": pulse_idx,
    }
    HEARTBEAT_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = HEARTBEAT_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    tmp.replace(HEARTBEAT_PATH)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        log.exception("pulse crashed")
        sys.exit(1)
