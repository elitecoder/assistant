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
MERGE_LEDGER_PATH = ASSISTANT_DIR / "assistant-merged-prs.jsonl"
OBSERVER_RUNS_DIR = ASSISTANT_DIR / "observer-runs"
# Observer-runs are the LLM transcript archive. We never delete them — disk
# is cheap, the audit trail is not. If this ever grows unmanageable, prune
# by hand or export to cold storage. The pulse loop does NOT touch it.
OBSERVER_BATCH_PROMPT = REPO / "prompts/observer-batch-prompt.md"
SPAWN_SKILL = HOME / ".claude/skills/spawn-claude-workspace/SKILL.md"

# TODO dispatch: where the source list lives, where staged prompts go, and the
# cmux CLI. The staged-prompt dir + 7-day sweep mirror the spawn-claude-workspace
# skill, ported here so the orchestrator owns dispatch end-to-end (no LLM).
TODO_PATH = HOME / ".claude/assistant-todo.json"
# Single source of truth for dispatch classification/routing. Appended to every
# dispatched TODO's prompt; the spawned Claude is the classifier that acts on it.
DISPATCH_CLASSIFICATION_PROMPT = REPO / "prompts/dispatch-classification.md"
SPAWN_PROMPT_DIR = HOME / ".claude/spawn-prompts"
CMUX_BIN = os.environ.get("CMUX_BIN", "/Applications/cmux.app/Contents/Resources/bin/cmux")
# Model + launch flags are NOT set here — the dispatched workspace runs `claude`,
# which expands the ~/.zprofile alias (model, --dangerously-skip-permissions,
# --add-dir). That alias is the single source of truth; see step 2 in dispatch_todo.
# cwd for dispatched work. ~/dev keeps the spawn inside Mukul's permission roots;
# FFP work re-homes itself into a fresh firefly-platform worktree via archffp.
DISPATCH_CWD = Path(os.environ.get("DISPATCH_CWD", str(HOME / "dev")))

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


def load_bedrock_env() -> dict:
    """Read AWS / Bedrock auth vars from ~/.zprofile and merge into our env.

    launchd does NOT source ~/.zprofile, so when pulse.py runs as a
    LaunchAgent the spawned `claude --print` subprocess sees no
    CLAUDE_CODE_USE_BEDROCK / AWS_BEARER_TOKEN_BEDROCK / AWS_REGION and
    fails with a 403 from AWS STS. Parse the zprofile directly for the
    handful of vars Bedrock needs. Cheap; runs once at process startup.
    """
    extracted: dict[str, str] = {}
    zprofile = HOME / ".zprofile"
    if not zprofile.exists():
        return extracted
    keys = ("CLAUDE_CODE_USE_BEDROCK", "AWS_REGION", "AWS_BEARER_TOKEN_BEDROCK",
            "AWS_PROFILE", "ANTHROPIC_API_KEY")
    pat = re.compile(r'^\s*export\s+([A-Z_][A-Z0-9_]*)\s*=\s*(.+?)\s*$')
    for line in zprofile.read_text().splitlines():
        m = pat.match(line)
        if not m:
            continue
        k, v = m.group(1), m.group(2).strip()
        if k not in keys:
            continue
        # Strip surrounding quotes if present.
        if (v.startswith('"') and v.endswith('"')) or \
           (v.startswith("'") and v.endswith("'")):
            v = v[1:-1]
        extracted[k] = v
    return extracted


# Cache once at module import — re-reading on every Observer call is cheap
# but pointless.
_BEDROCK_ENV = load_bedrock_env()


def run(cmd: list[str], *, input_text: str | None = None,
        timeout: int = 30, env: dict | None = None,
        merge_bedrock: bool = False) -> tuple[int, str, str]:
    """Run a subprocess; return (rc, stdout, stderr). Never raises.

    If merge_bedrock=True, layer the cached zprofile-extracted Bedrock vars
    onto the subprocess env. Used for `claude --print` which authenticates
    against AWS Bedrock via these vars (launchd does not source zprofile)."""
    if merge_bedrock:
        merged = dict(env if env is not None else os.environ)
        for k, v in _BEDROCK_ENV.items():
            # Only inject if not already set, so an explicit override (e.g.
            # OBSERVER_MODEL=anthropic-direct + ANTHROPIC_API_KEY in plist)
            # still wins.
            merged.setdefault(k, v)
        env = merged
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


def record_assistant_merge(ws_ref: str, pr_refs: list[str]) -> None:
    """Mark that the Assistant pulse just dispatched /merge-when-ready for
    this ws_ref. Used by the /cleanup gate: a workspace is only eligible
    for auto-/cleanup if the Assistant queued its merge."""
    MERGE_LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(MERGE_LEDGER_PATH, "a") as f:
        f.write(json.dumps({
            "ws_ref": ws_ref,
            "pr_refs": list(pr_refs or []),
            "ts": utc_ts(),
        }) + "\n")


def assistant_merged_workspace(ws_ref: str) -> bool:
    """True iff the Assistant pulse has dispatched /merge-when-ready for
    this ws_ref at any point. Read-only; never raises on a missing or
    corrupt ledger."""
    if not MERGE_LEDGER_PATH.exists():
        return False
    try:
        with open(MERGE_LEDGER_PATH) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if rec.get("ws_ref") == ws_ref:
                    return True
    except Exception:
        return False
    return False


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
    rc, out, err = run(cmd, input_text=prompt, timeout=OBSERVER_TIMEOUT_SEC,
                       merge_bedrock=True)
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

    # /cleanup gate: only send /cleanup to workspaces where the Assistant
    # itself queued the merge. Every other "looks done" signal — recap +
    # idle, audit complete, no-PR-needed declaration — gets downgraded
    # to an awaiting card so the user can decide. Recap-based heuristics
    # have misfired too many times (eval mid-flight, feature awaiting
    # review, work-done-awaiting-approval) and the cost of the wrong
    # /cleanup (destroyed mid-flight session) is much higher than the
    # cost of an extra awaiting card.
    if kind == "ready_for_cleanup" and not assistant_merged_workspace(ws_ref):
        title = f"{ws_ref} looks done — confirm /cleanup"
        detail = (
            f"Observer says: {(verdict.get('summary') or '').strip()[:600]}\n\n"
            "/cleanup will NOT auto-fire because the Assistant did not "
            "merge a PR for this workspace. Run /cleanup yourself if the "
            "work is truly done, or close the awaiting card to dismiss."
        )
        awaiting.append({
            "key": f"{ws_ref}:cleanup-needs-confirm",
            "tier": "T2",
            "title": title[:120],
            "detail": detail[:1200],
            "ws_ref": ws_ref,
        })
        return {**base, "kind": "emit-card", "evidence": "downgraded ready_for_cleanup → needs_user (no Assistant-merge record)"}

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

    # On a successful /merge-when-ready dispatch, mark this workspace as
    # eligible for future auto-/cleanup. This is the *only* path that
    # creates a merge-ledger entry — by construction, /cleanup can only
    # fire on workspaces the Assistant itself queued for merge.
    if kind == "ready_for_merge":
        record_assistant_merge(ws_ref, ws.get("pr_refs") or [])

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


def _load_todo_item(todo_id: str) -> dict | None:
    """Return the items[] entry for todo_id, or None."""
    try:
        data = json.loads(TODO_PATH.read_text())
    except Exception:
        return None
    for it in data.get("items", []):
        if it.get("id") == todo_id:
            return it
    return None


def _mark_todo_dispatched(todo_id: str, ws_ref: str) -> bool:
    """Stamp dispatchedAt + dispatchedWs on the TODO so it leaves bucket_b.

    Atomic read-modify-write of assistant-todo.json. Without this the TODO
    stays in bucket_b and every pulse re-spawns a duplicate workspace.
    Matches the field names todo-server.py's dispatch_now() clears.
    """
    try:
        data = json.loads(TODO_PATH.read_text())
    except Exception as e:
        log.warning("dispatch %s: cannot read todo file to stamp: %s", todo_id, e)
        return False
    target = next((i for i in data.get("items", []) if i.get("id") == todo_id), None)
    if target is None:
        return False
    target["dispatchedAt"] = utc_iso()
    target["dispatchedWs"] = ws_ref
    target["status"] = "in-progress"
    target["statusReason"] = f"dispatched to {ws_ref}"
    target["statusUpdatedAt"] = utc_iso()
    tmp = TODO_PATH.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
        os.replace(tmp, TODO_PATH)
    except Exception as e:
        log.warning("dispatch %s: cannot write todo stamp: %s", todo_id, e)
        return False
    return True


def _build_dispatch_prompt(item: dict) -> str:
    """Self-contained prompt for a dispatched TODO.

    Two halves: the work description (this TODO's fields) followed by the
    classification + routing rules from prompts/dispatch-classification.md.
    That file is the SINGLE source of truth for how dispatched work is
    classified (FFP Squirrel → /architect-ffp:archffp, else direct) — the
    spawned Claude is the classifier and acts on it. The rules are NOT
    duplicated in the operating guide (which no automated component reads);
    edit the template to change dispatch behavior.
    """
    tid = item.get("id", "")
    title = item.get("title", "")
    detail = (item.get("detail") or "").strip()
    url = item.get("url") or ""
    tags = ", ".join(item.get("tags") or [])
    lines = [
        f"You are picking up TODO {tid} from Mukul's Assistant dispatch queue.",
        "",
        "# Title",
        title,
        "",
    ]
    if detail:
        lines += ["# Detail", detail, ""]
    if url:
        lines += ["# Reference", url, ""]
    if tags:
        lines += ["# Tags", tags, ""]
    work = "\n".join(lines)
    try:
        rules = DISPATCH_CLASSIFICATION_PROMPT.read_text().strip()
    except Exception as e:
        # Fail loud in the prompt rather than silently dropping routing rules:
        # a worker with no classification guidance could ship FFP work raw.
        log.error("dispatch %s: cannot read %s: %s", tid,
                  DISPATCH_CLASSIFICATION_PROMPT, e)
        rules = (
            "# How to proceed\n"
            "Classification rules file is missing. If this work touches FFP "
            "Squirrel (firefly-platform timeline editor), you MUST route it "
            "via `/architect-ffp:archffp` and NOT touch git/test/PR directly. "
            "Otherwise implement directly, validate end-to-end, open a PR. "
            f"Reference {tid} in your branch / PR."
        )
    return f"{work}\n{rules}\n"


def _cmux_rpc(method: str, params: dict, timeout: int = 15) -> dict | None:
    """Call `cmux rpc <method> <json>` and return parsed JSON, or None."""
    rc, out, _ = run([CMUX_BIN, "rpc", method, json.dumps(params)], timeout=timeout)
    if rc != 0:
        return None
    try:
        return json.loads(out)
    except Exception:
        return None


def _surface_read_text(surface_ref: str, lines: int = 40) -> str:
    d = _cmux_rpc("surface.read_text", {"surface_id": surface_ref, "lines": lines})
    if not d or d.get("surface_ref") != surface_ref:
        return ""
    return d.get("text", "") or ""


def dispatch_todo(todo_id: str) -> bool:
    """Spawn a background cmux workspace for one TODO and deliver its prompt.

    Ports the spawn-claude-workspace skill into Python so the orchestrator owns
    dispatch end-to-end (no LLM in the loop). Steps mirror the skill:
      1. Stage the full prompt on disk (never stream the body through cmux —
         it drops middle chunks above ~3-4 KB).
      2. Create an UNFOCUSED workspace with the claude launch baked into
         --command (--focus false is mandatory; never take Mukul's foreground).
      3. Answer the first-launch trust prompt if it appears.
      4. Wait for the `Claude Code v` banner (readiness).
      5. Send a short `Read <prompt-file>` instruction + Enter.
      6. Confirm submission via the session transcript (a new *.jsonl with a
         user line carrying the prompt-file path).
      7. Stamp dispatchedAt/dispatchedWs on the TODO so it leaves bucket_b.

    Returns True only when submission is confirmed AND the TODO was stamped.
    """
    item = _load_todo_item(todo_id)
    if item is None:
        log.warning("dispatch %s: not found in todo file", todo_id)
        return False

    # cmux must be up.
    rc, _, _ = run([CMUX_BIN, "ping"], timeout=10)
    if rc != 0:
        log.warning("dispatch %s: cmux not running — skipping", todo_id)
        return False

    # 1. Stage the prompt on disk. 7-day sweep, then a per-todo stamped file.
    SPAWN_PROMPT_DIR.mkdir(parents=True, exist_ok=True)
    try:
        cutoff = time.time() - 7 * 86400
        for p in SPAWN_PROMPT_DIR.glob("prompt-*.md"):
            if p.stat().st_mtime < cutoff:
                p.unlink()
    except Exception:
        pass
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    prompt_file = SPAWN_PROMPT_DIR / f"prompt-dispatch-{todo_id}-{stamp}.md"
    prompt_file.write_text(_build_dispatch_prompt(item))

    # 2. Create the workspace with claude baked into --command (atomic launch).
    #    Just invoke `claude` — cmux runs --command in an interactive login
    #    shell, so the `claude` alias in ~/.zprofile expands (verified: it
    #    resolves to the model + --dangerously-skip-permissions + --add-dir
    #    flags). That alias is the single source of truth for model/flags; do
    #    NOT re-specify --model or --add-dir here or it will drift from it.
    claude_cmd = "claude"
    cwd = str(DISPATCH_CWD)
    title = f"{todo_id}: {item.get('title','')}"[:40]
    rc, out, err = run(
        [CMUX_BIN, "new-workspace", "--cwd", cwd, "--name", title,
         "--focus", "false", "--command", claude_cmd],
        timeout=30,
    )
    if rc != 0:
        log.warning("dispatch %s: new-workspace failed rc=%d: %s", todo_id, rc, err.strip())
        return False
    m = re.search(r"workspace:\d+", out)
    if not m:
        log.warning("dispatch %s: no workspace ref in: %s", todo_id, out.strip())
        return False
    ws_ref = m.group(0)

    rc, out, _ = run([CMUX_BIN, "list-pane-surfaces", "--workspace", ws_ref], timeout=15)
    sm = re.search(r"surface:\d+", out)
    if not sm:
        log.warning("dispatch %s: no surface for %s", todo_id, ws_ref)
        return False
    surface_ref = sm.group(0)

    # 3. Snapshot transcripts before, so a new one confirms submission.
    cwd_real = os.path.realpath(cwd)
    project_dir = HOME / ".claude/projects" / cwd_real.replace("/", "-")
    project_dir.mkdir(parents=True, exist_ok=True)
    before = {p.name for p in project_dir.glob("*.jsonl")}

    # 4. Trust prompt (first launch in a never-used cwd). --dangerously-skip
    #    does NOT bypass it; the transcript never appears until it's answered.
    time.sleep(2)
    if "1. Yes, I trust this folder" in _surface_read_text(surface_ref):
        _cmux_rpc("surface.send_text", {"surface_id": surface_ref, "text": "1"})
        _cmux_rpc("surface.send_key", {"surface_id": surface_ref, "key": "enter"})

    # 5. Wait for readiness banner (the only pre-submission screen marker).
    ready = False
    for _ in range(30):
        if re.search(r"Claude Code v", _surface_read_text(surface_ref)):
            ready = True
            break
        time.sleep(1)
    if not ready:
        log.warning("dispatch %s: claude never ready in %s/%s", todo_id, ws_ref, surface_ref)
        return False

    # 6. Deliver by reference. Strip the trailing newline (send_text streams
    #    keystrokes; a trailing \n auto-submits mid-paste), then Enter explicitly.
    instruction = f"Read {prompt_file} in full and execute every instruction in it."
    _cmux_rpc("surface.send_text", {"surface_id": surface_ref, "text": instruction.rstrip("\n")})
    time.sleep(1)
    _cmux_rpc("surface.send_key", {"surface_id": surface_ref, "key": "enter"})

    # 7. Confirm submission via the transcript (authoritative, no screen-scraping).
    sig = str(prompt_file)[:60]
    submitted = False
    for _ in range(30):
        new = {p.name for p in project_dir.glob("*.jsonl")} - before
        for name in new:
            try:
                for line in (project_dir / name).read_text().splitlines():
                    try:
                        d = json.loads(line)
                    except Exception:
                        continue
                    msg = d.get("message") if isinstance(d.get("message"), dict) else None
                    if d.get("type") == "user" and msg and msg.get("role") == "user":
                        c = msg.get("content", "")
                        if isinstance(c, list):
                            c = " ".join(
                                str(x.get("text", "") if isinstance(x, dict) else x)
                                for x in c
                            )
                        if sig in str(c):
                            submitted = True
                            break
            except Exception:
                continue
            if submitted:
                break
        if submitted:
            break
        time.sleep(1)

    if not submitted:
        log.warning("dispatch %s: spawned %s but submission unconfirmed (prompt staged at %s)",
                    todo_id, ws_ref, prompt_file)
        return False

    if not _mark_todo_dispatched(todo_id, ws_ref):
        log.warning("dispatch %s: submitted to %s but TODO stamp failed — "
                    "will re-dispatch next pulse", todo_id, ws_ref)
        return False

    log.info("dispatch %s → %s (surface %s, prompt %s)",
             todo_id, ws_ref, surface_ref, prompt_file)
    return True


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

    # 5. TODO dispatch — spawn an unfocused cmux workspace per bucket_b TODO,
    #    capped at MAX_DISPATCH_PER_PULSE and gated on the active/total caps.
    #    Skipped entirely in dry-run (dispatch_todo creates real workspaces).
    todos = pick_open_todos() if not dry_run else {"bucket_b": []}
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
