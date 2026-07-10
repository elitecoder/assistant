#!/usr/bin/env python3
"""pulse.py — mechanical Assistant orchestrator. Replaces the LLM-driven loop.

Run by the com.assistant.assistant-pulse LaunchAgent every 2 min. Single
process, no LLM in the orchestration layer. The only LLM call is the
per-workspace Observer subprocess (claude --print + observer prompt).

Pipeline (in order):

  1. Drain ~/.assistant/inbox/ through the typed event spine
     (src/assistant/eventspine.py): every drop becomes a deduplicated
     WorldEvent in ~/.assistant/events.jsonl; malformed drops are
     quarantined, never fatal.
  1.5. Triage new WorldEvents (src/assistant/triage.py, Keel M2): the
     deterministic policy engine lanes each event (auto/staged/escalate/
     digest/drop); policy hits act mechanically; unmatched events get
     fail-safe escalate decisions plus ONE suggestion-only triage LLM call
     per pulse max (its lane vocabulary structurally lacks `auto`). Open
     escalate decisions mirror to awaiting cards keyed `dec-<id>`.
  2. Run bin/purge-stale-awaiting.py (mechanical card cleanup; `dec-*`
     cards derive from decision-queue state).
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

import hashlib
import json
import logging
import os
import re
import shlex
import shutil
import signal
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
# Triage LLM (Keel M2): ONE suggestion-only batched call per pulse max, same
# subprocess + file-side-effect + archived-runs pattern as the Observer. Runs
# are kept for the same audit-trail reason observer-runs are.
TRIAGE_BATCH_PROMPT = REPO / "prompts/triage-batch-prompt.md"
TRIAGE_RUNS_DIR = ASSISTANT_DIR / "triage-runs"
AGENT_TOOLS_MCP_CONFIG = REPO / "config/agent-tools-mcp.json"
SPAWN_SKILL = HOME / ".claude/skills/spawn-claude-workspace/SKILL.md"

# Work-receipt gate: pulse.py consults pre-cleanup-check.py before sending
# /cleanup, and surfaces an awaiting card when the gate blocks.
PRE_CLEANUP_CHECK = BIN / "pre-cleanup-check.py"

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
# The triage batch reads inline event JSON only (no transcripts), so it gets a
# much shorter leash than the Observer.
TRIAGE_TIMEOUT_SEC = int(os.environ.get("TRIAGE_TIMEOUT_SEC", "240"))

# Cap dispatched TODO spawns. We don't hammer the user's machine on a single pulse.
MAX_DISPATCH_PER_PULSE = 2

# Lesson extraction cadence. Pattern detection over the ledger is cheap, but a
# surviving candidate triggers an LLM draft call, so we throttle: run it once
# every N pulses (~hourly at the current interval) rather than every pulse.
LESSON_EXTRACT_EVERY = int(os.environ.get("LESSON_EXTRACT_EVERY", "12"))
# Transcript mining is heavier (reads up to 500 session files). Run it ~daily
# rather than every ledger pass — every 144th pulse (12× the ledger cadence).
# Every other extractor run passes --ledger-only.
LESSON_TRANSCRIPT_EVERY = int(os.environ.get("LESSON_TRANSCRIPT_EVERY", "144"))
# Hard bound on the extractor subprocess so a hung LLM draft can never stall the
# orchestrator. It runs AFTER state-write + heartbeat, so even a timeout here
# leaves the dashboard and the heartbeat fresh. The transcript pass gets a
# longer leash since it reads many files before any LLM draft.
LESSON_EXTRACT_TIMEOUT_SEC = int(os.environ.get("LESSON_EXTRACT_TIMEOUT_SEC", "300"))
LESSON_TRANSCRIPT_TIMEOUT_SEC = int(os.environ.get("LESSON_TRANSCRIPT_TIMEOUT_SEC", "600"))

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

    The child starts in its OWN session (start_new_session=True) and a
    timeout kills the whole process GROUP before the final reap: a timed-out
    `claude` subprocess can leave grandchildren holding the inherited stdout/
    stderr pipes, and without the group kill the post-kill communicate()
    blocks until every pipe-holder exits — a hung Observer/triage call would
    stall the pulse far past its timeout.

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
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE if input_text is not None else None,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=env, start_new_session=True,
        )
    except Exception as e:
        return 1, "", str(e)
    try:
        out, err = proc.communicate(input=input_text, timeout=timeout)
        return proc.returncode, out, err
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            proc.kill()
        try:
            proc.communicate(timeout=5)
        except Exception:  # noqa: BLE001 — reap is best-effort after SIGKILL
            pass
        return 124, "", f"timeout after {timeout}s"
    except Exception as e:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            proc.kill()
        return 1, "", str(e)


def append_ledger(entry: dict) -> None:
    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LEDGER_PATH, "a") as f:
        f.write(json.dumps(entry) + "\n")


def run_merge_pr_dispatch(ws_ref: str, pr, refactor_attested: bool,
                          e2e_attested: bool) -> dict:
    """Invoke bin/merge-pr-dispatch.py and return its parsed JSON result.

    The script owns the safety gate, Squirrel-E2E gate, freeze retarget, CI
    router, and send-verify. On any failure to run it, return an awaiting
    card so the workspace surfaces rather than silently stalling."""
    cmd = ["python3", str(BIN / "merge-pr-dispatch.py"),
           "--ws", ws_ref, "--pr", str(pr)]
    if refactor_attested:
        cmd.append("--refactor-attested")
    if e2e_attested:
        cmd.append("--e2e-attested")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except Exception as e:
        return {"outcome": "dispatch_error",
                "awaiting_card": {
                    "key": f"assistant:merge-pr-dispatch-error:{pr}",
                    "tier": "T2",
                    "title": f"PR #{pr}: merge-pr-dispatch.py failed to run",
                    "detail": f"{type(e).__name__}: {e}",
                }}
    try:
        return json.loads(proc.stdout)
    except Exception:
        return {"outcome": "dispatch_unparsable",
                "awaiting_card": {
                    "key": f"assistant:merge-pr-dispatch-unparsable:{pr}",
                    "tier": "T2",
                    "title": f"PR #{pr}: merge-pr-dispatch.py output unparsable",
                    "detail": f"rc={proc.returncode} stdout={proc.stdout[:200]} stderr={proc.stderr[:200]}",
                }}


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

def self_update_pulse(pulse_idx: int) -> None:
    """Step 0: pull + re-install the running system from git (throttled).

    Imported lazily so a broken/absent self_update module can never stop the
    pulse from doing its real job. Every outcome that did real work (a pull, a
    skip-because-dirty, a failure) lands on the actions-ledger so it shows on
    the dashboard. A plain throttle (None) or a clean already-up-to-date
    result is silent — no ledger spam every pulse."""
    try:
        sys.path.insert(0, str(BIN))
        import self_update  # noqa: PLC0415
        result = self_update.maybe_update(REPO, log=log)
    except Exception as e:  # noqa: BLE001 — self-update must never break the pulse
        log.exception("self-update raised (ignored): %s", e)
        return

    if result is None:
        return  # throttled — nothing to report

    reason = result.get("skipped_reason")
    changed = result.get("changed")
    # Silent path: attempted, nothing to do, no problem.
    if not changed and reason is None and not result.get("error"):
        return

    if changed:
        files = result.get("files_changed", [])
        installed = result.get("installed")
        outcome = "verified"
        evidence = (f"pulled {result.get('from_sha')}..{result.get('to_sha')} "
                    f"({len(files)} file(s)); install={'ran' if installed else 'not-needed'}")
        if result.get("stashed"):
            # An aged-out dirty tree was auto-stashed before the pull. Make the
            # recovery path loud so the operator knows their work is parked.
            evidence += "; auto-stashed dirty tree (recover: git stash pop)"
        if result.get("install_rc") not in (None, 0):
            outcome = "failed"
            evidence += f" install_rc={result.get('install_rc')}"
        if result.get("self_plist_reload_deferred"):
            evidence += "; pulse-plist reload deferred"
        key = f"self-update-{result.get('to_sha', pulse_idx)}"
    elif reason in ("dirty", "ahead"):
        # Operator has local state we won't clobber — surface it, don't fail.
        outcome = "verified"
        if reason == "ahead":
            n = result.get("ahead")
            evidence = (f"skipped self-update: repo is ahead ({n} commit(s) "
                        "ahead — unpushed work) — pull deferred until clean")
        else:
            age_h = (result.get("dirty_age_sec") or 0) / 3600.0
            evidence = (f"skipped self-update: working tree dirty for {age_h:.1f}h "
                        "— pull deferred (auto-stash at 24h)")
        key = f"self-update-skip-{reason}-p{pulse_idx}"
    elif reason == "stash-failed":
        # Tried to auto-stash an aged-out dirty tree; git refused. The
        # operator's work is untouched, but the update is blocked.
        outcome = "failed"
        evidence = f"self-update auto-stash failed: {result.get('error', '')}"[:300]
        key = f"self-update-stash-failed-p{pulse_idx}"
    else:
        outcome = "failed"
        evidence = f"self-update {reason or 'error'}: {result.get('error', '')}"[:300]
        key = f"self-update-fail-p{pulse_idx}"

    append_ledger({
        "ts": utc_iso(),
        "epoch": utc_ts(),
        "pulse_idx": pulse_idx,
        "key": key,
        "kind": "self-update",
        "ws_ref": "(launchd)",
        "outcome": outcome,
        "evidence": evidence,
    })
    log.info("self-update: %s", evidence)


def drain_inbox(pulse_idx: int = 0) -> int:
    """Step 1: typed event-spine drain (Keel M1).

    The old body unlinked pulse-*.json UNREAD and ignored the cmux-watcher
    signal drops entirely — produced signal was never consumed. Now every
    inbox file (and any orphaned ~/.claude/cmux-crash-events/ drop) becomes a
    deduplicated WorldEvent row in ~/.assistant/events.jsonl via
    src/assistant/eventspine.py: parse → archive raw → dedup → append →
    unlink, behind a pid-checked single-consumer lock. Malformed files are
    quarantined + ledgered, never fatal. Imported lazily and fully fenced so
    a broken spine can never stop the pulse from its real job (a failure
    leaves the inbox intact for the next pulse — nothing is lost).

    Returns the number of inbox files disposed of (consumed + duplicate +
    quarantined), the same "how much did we drain" meaning the old count had.
    """
    try:
        src_dir = str(REPO / "src")
        if src_dir not in sys.path:
            sys.path.insert(0, src_dir)
        from assistant import eventspine  # noqa: PLC0415
        result = eventspine.drain_typed_inbox(pulse_idx=pulse_idx, log=log)
    except Exception as e:  # noqa: BLE001 — the spine must never break the pulse
        log.exception("eventspine drain failed (inbox left intact): %s", e)
        return 0
    if result.get("locked"):
        return 0
    if result.get("events_appended") or result.get("inbox_quarantined"):
        log.info("eventspine: %s", json.dumps(result))
    return (result.get("inbox_consumed", 0)
            + result.get("inbox_duplicates", 0)
            + result.get("inbox_quarantined", 0))


def read_lane_suggestions(path: Path) -> dict[str, dict]:
    """Read the JSONL the triage LLM wrote. Each line needs `event_id` and
    `suggested_lane`; anything else is silently dropped (an event with no
    usable suggestion keeps its fail-safe escalate default)."""
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
        ev = obj.get("event_id")
        if not ev or not obj.get("suggested_lane"):
            continue
        out[ev] = {"suggested_lane": obj.get("suggested_lane"),
                   "rationale": obj.get("rationale") or ""}
    return out


def call_triage_batch(events: list[dict], pulse_idx: int) -> dict[str, dict]:
    """Spawn the ONE suggestion-only triage LLM subprocess for this pulse.

    Observer pattern throughout: the model writes its structured output to
    <run_dir>/lanes.jsonl (stdout is the CLI's JSON result envelope, kept for
    metering only); prompt/events/stdout/stderr/meta are archived under
    ~/.assistant/triage-runs/<pulse>/; usage is captured via metering and
    appended to the cost ledger as caller="triage".

    Suggestion-only by construction: the caller validates every suggested
    lane against policy.TRIAGE_LANE_MAP (which structurally lacks `auto` and
    `drop`), and a suggestion only ever annotates an already-open decision.
    Returns {event_id: {suggested_lane, rationale}}.
    """
    if not events:
        return {}
    if not TRIAGE_BATCH_PROMPT.exists():
        log.error("triage batch prompt missing: %s", TRIAGE_BATCH_PROMPT)
        return {}

    run_dir = TRIAGE_RUNS_DIR / f"{pulse_idx:04d}"
    run_dir.mkdir(parents=True, exist_ok=True)
    lanes_path = run_dir / "lanes.jsonl"
    events_json = json.dumps(events, indent=2)
    prompt = (
        TRIAGE_BATCH_PROMPT.read_text()
        + "\n\n---\n\n## RUNTIME CONTEXT\n\n"
        + f"You are triaging this batch of {len(events)} event(s).\n\n"
        + "**Output destination — write your JSONL suggestions to a file, "
          "not stdout.** Use the Write tool (or `printf > <path>`) to write "
          f"to:\n\n    {lanes_path}\n\n"
          "One JSON object per line: {\"event_id\": ..., \"suggested_lane\": "
          "\"escalate|staged|digest\", \"rationale\": ...}. Anything on "
          "stdout is diagnostic only and never parsed for lanes.\n\n"
        + "Events to triage:\n\n"
        + "```json\n" + events_json + "\n```\n"
    )
    (run_dir / "prompt.md").write_text(prompt)
    (run_dir / "events.json").write_text(events_json)

    cmd = [
        DEFAULT_CLAUDE_BIN,
        "--model", DEFAULT_OBSERVER_MODEL,
        "--dangerously-skip-permissions",
        "--print",
        "--output-format", "json",
        "--add-dir", str(run_dir),  # so the model can write lanes.jsonl
    ]
    t0 = time.time()
    rc, out, err = run(cmd, input_text=prompt, timeout=TRIAGE_TIMEOUT_SEC,
                       merge_bedrock=True)
    wall_ms = int((time.time() - t0) * 1000)

    # Metering + cost ledger — never load-bearing. A failed subprocess
    # (rc!=0) or an unparseable result envelope must NOT book phantom spend:
    # it gets a zero-cost status:"failed" row instead, so failures stay
    # visible in the ledger without inventing dollars.
    usage: dict = {}
    try:
        sys.path.insert(0, str(BIN))
        import metering  # noqa: PLC0415
        failed = rc != 0 or metering.parse_cli_result(out) is None
        if failed:
            metering.append_cost_row(
                caller="triage", model=DEFAULT_OBSERVER_MODEL,
                usage={"tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0,
                       "source": "none"},
                wall_ms=wall_ms, status="failed")
        else:
            usage = metering.observer_usage(out, len(prompt),
                                            DEFAULT_OBSERVER_MODEL)
            metering.append_cost_row(caller="triage",
                                     model=DEFAULT_OBSERVER_MODEL,
                                     usage=usage, wall_ms=wall_ms)
    except Exception as e:  # noqa: BLE001 — metering must never break the pulse
        log.warning("triage metering capture failed (ignored): %s", e)

    (run_dir / "stdout.txt").write_text(out or "")
    (run_dir / "stderr.txt").write_text(err or "")
    (run_dir / "meta.json").write_text(json.dumps({
        "rc": rc,
        "wall_ms": wall_ms,
        "model": DEFAULT_OBSERVER_MODEL,
        "n_events": len(events),
        "event_ids": [e.get("id") for e in events],
        "usage": usage,
        "ts": utc_iso(),
    }, indent=2))

    if rc != 0:
        log.warning("triage batch (size=%d) rc=%d: %s",
                    len(events), rc, err.strip()[-300:])
    return read_lane_suggestions(lanes_path)


def run_triage_step(pulse_idx: int) -> dict:
    """Step 1.5: lane new WorldEvents through the deterministic policy engine
    (src/assistant/triage.py). Imported lazily and fully fenced, same contract
    as drain_inbox — a broken triage layer can never stop the pulse (events
    stay un-disposition'd and are retried next pulse). Returns the triage
    summary; its `cards` list seeds this pulse's awaiting_input."""
    try:
        src_dir = str(REPO / "src")
        if src_dir not in sys.path:
            sys.path.insert(0, src_dir)
        from assistant import triage  # noqa: PLC0415
        summary = triage.triage_new_events(
            pulse_idx=pulse_idx, log=log,
            llm_batch=lambda events: call_triage_batch(events, pulse_idx))
    except Exception as e:  # noqa: BLE001 — triage must never break the pulse
        log.exception("triage step failed (events retried next pulse): %s", e)
        return {"cards": []}
    if summary.get("events_processed") or summary.get("expired") \
            or summary.get("proposals"):
        log.info("triage: %s", json.dumps(
            {k: v for k, v in summary.items() if k != "cards"}))
    return summary


# ─── Observer no-change skip (Keel M2) ───────────────────────────────────────
#
# A workspace whose observable state hasn't changed since its last verdict
# gets that verdict carried forward and is left OUT of the Observer batch —
# an unchanged fleet costs zero Observer calls. The hash covers exactly the
# signals build-ws-context gathers: the transcript tail, the live screen, and
# the git/cwd state. The raw last_turn_age_sec is EXCLUDED (it grows every
# pulse without any state change and would defeat the skip) — but its BAND
# relative to the Observer prompt's 1800s idle threshold IS included: the
# prompt's stranded/ready_for_cleanup/needs_user rules all pivot on
# idle ≤ 1800s vs > 1800s (observer-batch-prompt.md threshold cheat-sheet),
# so crossing that line is an observable state change even when no byte of
# transcript/screen moved. Without the band, a hung-but-idle ws hashes
# identical forever, its `active` verdict carries forever, and the recovery
# nudge never fires.

# How much transcript tail feeds the hash. Matches the transcript_signals
# window in build-ws-context: any appended turn lands in the tail, so any
# transcript growth changes the digest.
OBS_HASH_TAIL_BYTES = 65536

# The Observer prompt's ONLY time threshold: idle ≤ 1800s reads active,
# idle > 1800s unlocks stranded/ready_for_cleanup/needs_user. Mirror exactly.
OBS_IDLE_THRESHOLD_SEC = 1800

# Hard cap on consecutive carried-forward verdicts: after this many skips
# the ws is force-observed regardless of hash. Structural defense against
# ANY hash blind spot (present or future) — no workspace can go unobserved
# longer than MAX_CONSECUTIVE_CARRIES pulses.
MAX_CONSECUTIVE_CARRIES = 6


def idle_age_band(age) -> str:
    """Which side of the Observer prompt's 1800s idle threshold `age` is on.
    The band (never the raw age) feeds obs_input_hash: growth within a band
    can't defeat the skip; crossing the threshold changes the hash and forces
    a re-observe, because the prompt's verdict rules flip at that line."""
    if not isinstance(age, (int, float)):
        return "age-unknown"
    return "le1800" if age <= OBS_IDLE_THRESHOLD_SEC else "gt1800"


def _transcript_tail_digest(path: str | None) -> str:
    """Digest of the transcript's size + last OBS_HASH_TAIL_BYTES. Size is
    included so pathological same-size rewrites still differ. Unreadable or
    missing → a constant marker (state 'no transcript' is itself hashable)."""
    if not path:
        return "no-transcript"
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - OBS_HASH_TAIL_BYTES))
            return f"{size}:{hashlib.sha256(f.read()).hexdigest()}"
    except OSError:
        return "unreadable-transcript"


def obs_input_hash(ctx: dict) -> str:
    """Stable digest of one workspace's Observer-visible state. Equal hashes
    ⇒ the Observer would see byte-identical inputs ⇒ its prior verdict is
    still the verdict; carry it forward instead of spending a call."""
    payload = json.dumps([
        ctx.get("ws_ref"),
        ctx.get("title"),
        ctx.get("cwd"),
        ctx.get("transcript_path"),
        ctx.get("session_id8"),
        _transcript_tail_digest(ctx.get("transcript_path")),
        ctx.get("screen_text") or "",
        bool(ctx.get("screen_shows_error")),
        ctx.get("agent_status"),
        bool(ctx.get("cwd_dirty")),
        bool(ctx.get("cwd_unpushed")),
        idle_age_band(ctx.get("last_turn_age_sec")),
    ], sort_keys=False)
    return hashlib.sha256(payload.encode("utf-8", "replace")).hexdigest()


def load_prior_summary(ws_ref: str) -> dict | None:
    p = SUMMARIES_DIR / f"{ws_ref.replace(':', '_')}.json"
    try:
        d = json.loads(p.read_text())
        return d if isinstance(d, dict) else None
    except Exception:
        return None


# Meta fields save-ws-summary.py adds around the verdict; stripped before a
# carried-forward verdict is re-saved (save adds them back fresh).
_SUMMARY_META_KEYS = {"ws_ref", "title", "cwd", "pr_refs", "last_updated_ts",
                      "state_hash", "state_unchanged_since_ts"}


def carry_forward_verdict(prior: dict) -> dict:
    """The prior summary reduced back to a bare verdict dict, ready for
    save_summary (which refreshes last_updated_ts so the LRU rotates and
    preserves state_unchanged_since_ts because the verdict fields are
    byte-identical)."""
    return {k: v for k, v in prior.items() if k not in _SUMMARY_META_KEYS}


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


def call_observer_batch(ctxs: list[dict], pulse_idx: int,
                        batch_idx: int) -> tuple[dict[str, dict], dict]:
    """Spawn ONE Observer subprocess to judge a batch of workspaces.

    The Observer writes its structured output to <run_dir>/verdicts.jsonl.
    pulse.py reads that file. We do NOT parse stdout for verdicts — with
    `--output-format json`, stdout is the CLI's single result envelope
    (captured to <run_dir>/stdout.txt), which carries the REAL token usage
    and total_cost_usd for metering.

    Returns ({ws_ref: verdict-dict}, usage-dict). Missing ws_refs in the
    result mean the Observer didn't emit a line for them; the caller treats
    that as a skipped action. usage-dict is metering.observer_usage() shape
    (falls back to a chars/4 estimate when the envelope is unparsable).
    """
    if not ctxs:
        return {}, {}
    if not OBSERVER_BATCH_PROMPT.exists():
        log.error("observer batch prompt missing: %s", OBSERVER_BATCH_PROMPT)
        return {}, {}

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
        # Single JSON result envelope on stdout — carries real token usage +
        # total_cost_usd for metering. Verdicts still come from verdicts.jsonl.
        "--output-format", "json",
        "--add-dir", str(REPO / "prompts"),
        "--add-dir", str(HOME / ".claude/projects"),
        "--add-dir", str(run_dir),  # so the model can write verdicts.jsonl
    ]
    if AGENT_TOOLS_MCP_CONFIG.exists():
        cmd += ["--mcp-config", str(AGENT_TOOLS_MCP_CONFIG), "--strict-mcp-config"]
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

    # Token/cost capture: real numbers from the CLI result envelope when it
    # parses, chars/4 estimate otherwise. Never load-bearing — a broken
    # metering module degrades to an empty usage dict, not a failed batch.
    usage: dict = {}
    try:
        sys.path.insert(0, str(BIN))
        import metering  # noqa: PLC0415
        usage = metering.observer_usage(out, len(prompt), DEFAULT_OBSERVER_MODEL)
    except Exception as e:  # noqa: BLE001 — metering must never break the pulse
        log.warning("metering usage capture failed (ignored): %s", e)

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
        "usage": usage,
        "ts": utc_iso(),
    }, indent=2))

    if rc != 0:
        log.warning("observer batch (size=%d) rc=%d: %s",
                    len(ctxs), rc, err.strip()[-300:])

    return read_verdicts_file(verdicts_path), usage


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


# ─── work-receipt gate ────────────────────────────────────────────────────

def pre_cleanup_check(ws_ref: str) -> dict:
    """Run pre-cleanup-check.py for ws_ref; return its gate dict.

    The gate tool always exits 0 (result is in the JSON), and fails safe to
    block on its own internal errors. We mirror that here: any failure to run
    or parse the tool returns a block, so a broken gate can never let an
    un-receipted /cleanup through."""
    rc, out, err = run([sys.executable, str(PRE_CLEANUP_CHECK), "--ws", ws_ref])
    if rc != 0:
        log.warning("pre-cleanup-check %s rc=%d: %s", ws_ref, rc, err.strip()[-200:])
        return {"gate": "block", "reason": "gate failed to run",
                "evidence": err.strip()[-200:], "ws_ref": ws_ref}
    try:
        return json.loads(out)
    except Exception as e:
        log.warning("pre-cleanup-check %s bad json: %s", ws_ref, e)
        return {"gate": "block", "reason": "gate bad output",
                "evidence": (out or "")[:200], "ws_ref": ws_ref}


# ─── verdict execution ──────────────────────────────────────────────────────

def execute_verdict(ws: dict, verdict: dict, awaiting: list[dict],
                    carry: bool = False) -> dict:
    """Execute the action implied by the verdict. Returns an action_taken
    dict for the state file.

    carry=True (no-change skip re-emitting a carried verdict): ONLY the
    card-emitting paths run — cards are rebuilt every pulse and derive from
    persisting state, so they must re-emit with the exact same keys/dedup as
    a fresh verdict. Every acting path (merge dispatch, /cleanup send, nudge
    send) is refused: those actions already ran when the verdict was first
    earned, and the whole point of the carry is that nothing changed since."""
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

    # Work-receipt gate: even a merge-eligible /cleanup must NOT fire until a
    # work receipt exists for this workspace (the audit trail of what shipped).
    # No receipt → downgrade to an awaiting card so the user can confirm the
    # close (or write the receipt) before the session is torn down. A receipt
    # destroyed alongside the workspace is unrecoverable.
    if kind == "ready_for_cleanup":
        gate = pre_cleanup_check(ws_ref)
        if gate.get("gate") == "block":
            title = f"{ws_ref} ready to close — confirm /cleanup"
            detail = (
                f"Observer says: {(verdict.get('summary') or '').strip()[:600]}\n\n"
                f"No work receipt on file ({gate.get('reason', 'no receipt')}). "
                "/cleanup is held until you confirm. Run /cleanup yourself to "
                "close, or close this card to dismiss."
            )
            awaiting.append({
                "key": f"{ws_ref}:cleanup-no-receipt",
                "tier": "T2",
                "title": title[:120],
                "detail": detail[:1200],
                "ws_ref": ws_ref,
            })
            return {**base, "kind": "emit-card",
                    "evidence": f"blocked /cleanup — {gate.get('reason', 'no receipt')}"}
        base["receipt_path"] = gate.get("receipt_path")

    # ready_for_merge routes through merge-pr-dispatch.py — the unbypassable
    # mechanical layer (safety gate → Squirrel-E2E gate → freeze retarget →
    # CI router → send + verify). INCIDENTS.md mandates this; a direct
    # /merge-when-ready send would skip all of it.
    if kind == "ready_for_merge":
        if carry:
            # Carried verdicts never act — the dispatch ran when the verdict
            # was earned; a carry re-running it would re-submit the merge.
            return {**base, "kind": "skipped",
                    "evidence": "carried verdict; merge dispatch not re-run"}
        pr_refs = ws.get("pr_refs") or []
        if not pr_refs:
            return {**base, "kind": "skipped", "outcome": "failed",
                    "evidence": "ready_for_merge but no pr_refs on ws"}
        result = run_merge_pr_dispatch(
            ws_ref, pr_refs[0],
            refactor_attested=bool(verdict.get("refactor_attested")),
            e2e_attested=bool(verdict.get("e2e_attested")),
        )
        base["evidence"] = f"merge-pr-dispatch pr=#{pr_refs[0]} outcome={result.get('outcome')}"
        base["dispatch"] = result
        card = result.get("awaiting_card")
        if card:
            awaiting.append({**card, "ws_ref": ws_ref})
            return {**base, "kind": "emit-card"}
        if result.get("outcome") == "submitted":
            record_assistant_merge(ws_ref, pr_refs)
            return {**base, "kind": "merge-dispatched"}
        base["outcome"] = "failed"
        return base

    # Sends. Apply NO_INGEST_GUARD: if the same text was sent last and got
    # delta=0, skip this pulse rather than loop forever.
    if carry:
        # Carried verdicts never send — /cleanup and nudges fired when the
        # verdict was earned; unchanged state means the send already landed
        # (or NO_INGEST_GUARD is already holding it).
        return {**base, "kind": "skipped",
                "evidence": "carried verdict; send not re-run"}
    if kind == "ready_for_cleanup":
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


# Default read window must capture the WHOLE TUI screen, not just the bottom.
# In cmux fullscreen (`/tui`) mode a freshly-spawned claude renders the
# `Claude Code v…` banner and the trust prompt pinned to the TOP of a tall
# (~70-line) screen, with the input box pinned to the bottom and dozens of
# blank rows between. A 40-line bottom window misses both markers, so the
# readiness poll times out and the prompt is never sent — the workspace spawns
# but sits idle with no transcript (td-101, 2026-05-30). 200 lines covers the
# top of any realistic terminal height while staying cheap.
def _surface_read_text(surface_ref: str, lines: int = 200) -> str:
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
    rc, _pout, _perr = run([CMUX_BIN, "ping"], timeout=10)
    if rc != 0:
        log.warning("dispatch %s: cmux not running — skipping (ping rc=%s bin=%s stderr=%r)",
                    todo_id, rc, CMUX_BIN, (_perr or "")[:300])
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
    #    Recursive glob: newer Claude Code writes the main transcript inside a
    #    per-session subdir (<project>/<session-id>/…), not a flat <id>.jsonl.
    #    A non-recursive glob missed those and the confirmation in step 7 always
    #    came back negative — which used to re-spawn the TODO every pulse
    #    (td-128: 7 duplicate workspaces). Match both layouts by relative path.
    cwd_real = os.path.realpath(cwd)
    project_dir = HOME / ".claude/projects" / cwd_real.replace("/", "-")
    project_dir.mkdir(parents=True, exist_ok=True)
    before = {str(p.relative_to(project_dir)) for p in project_dir.rglob("*.jsonl")}

    # 4. Trust prompt (first launch in a never-used cwd). --dangerously-skip
    #    does NOT bypass it; the transcript never appears until it's answered.
    time.sleep(2)
    if "1. Yes, I trust this folder" in _surface_read_text(surface_ref):
        _cmux_rpc("surface.send_text", {"surface_id": surface_ref, "text": "1"})
        _cmux_rpc("surface.send_key", {"surface_id": surface_ref, "key": "enter"})

    # 5. Wait for readiness. Match EITHER pre-submission marker so the gate is
    #    independent of `/tui` mode and terminal height:
    #      - "Claude Code v…" — the boot banner. Top-pinned, so in fullscreen it
    #        can sit above a short read window (see _surface_read_text note).
    #      - "⏵⏵ bypass permissions on" — the bottom status bar. Present in both
    #        default-inline and fullscreen /tui, and in both idle and working
    #        states, so it's always inside a bottom-anchored read window even
    #        when the banner has scrolled off the top.
    #    Either marker means claude is up and accepting input.
    ready = False
    for _ in range(30):
        screen = _surface_read_text(surface_ref)
        if re.search(r"Claude Code v|⏵⏵ bypass permissions on", screen):
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

    # 6.5. STAMP NOW — a real workspace exists and the prompt has been sent.
    #    Stamping (dispatchedAt + dispatchedWs + status=in-progress) moves the
    #    TODO out of bucket_b, so the next pulse cannot re-spawn it. This is the
    #    load-bearing idempotency guard: the stamp must NOT depend on the
    #    transcript-confirmation below. Previously confirmation gated the stamp,
    #    so any false negative (e.g. a Claude Code transcript-layout change)
    #    left the TODO in bucket_b and every pulse spawned another duplicate
    #    workspace (td-128: 7 dupes). If the workspace later dies without the
    #    session flipping status to done, the picker routes it to bucket_a
    #    (re-classify) — which is NOT auto-spawned — never back to bucket_b.
    if not _mark_todo_dispatched(todo_id, ws_ref):
        log.warning("dispatch %s: spawned %s + sent prompt but TODO stamp failed — "
                    "will re-dispatch next pulse (prompt staged at %s)",
                    todo_id, ws_ref, prompt_file)
        return False

    # 7. Confirm submission via the transcript (authoritative, no screen-scraping).
    #    Advisory ONLY — the stamp above already guarantees no re-spawn. A miss
    #    here is logged as a warning so a genuinely-stuck spawn is still visible,
    #    but it never reverses the stamp.
    sig = str(prompt_file)[:60]
    submitted = False
    for _ in range(30):
        new = {str(p.relative_to(project_dir)) for p in project_dir.rglob("*.jsonl")} - before
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
        log.warning("dispatch %s: stamped to %s but submission unconfirmed within window "
                    "(prompt staged at %s) — not re-dispatching; check the workspace",
                    todo_id, ws_ref, prompt_file)
    else:
        log.info("dispatch %s → %s (surface %s, prompt %s)",
                 todo_id, ws_ref, surface_ref, prompt_file)
    return True


def run_lesson_extractor(pulse_idx: int) -> None:
    """Step 8 (throttled): mine recurring patterns and draft lesson proposals.
    The fast ledger pass runs every LESSON_EXTRACT_EVERY pulses; the heavier
    transcript pass only every LESSON_TRANSCRIPT_EVERY pulses (~daily), all
    other runs pass --ledger-only. Runs as a bounded subprocess so a slow/hung
    LLM draft can never stall the pulse. Invoked AFTER state-write + heartbeat,
    so a timeout here leaves the dashboard and heartbeat fresh. Failures are
    logged and swallowed — extraction is a nice-to-have, never load-bearing."""
    extractor = BIN / "lesson-extractor.py"
    if not extractor.exists():
        return
    transcript_pass = (LESSON_TRANSCRIPT_EVERY > 0
                       and pulse_idx % LESSON_TRANSCRIPT_EVERY == 0)
    cmd = [sys.executable, str(extractor)]
    if not transcript_pass:
        cmd.append("--ledger-only")
    timeout = (LESSON_TRANSCRIPT_TIMEOUT_SEC if transcript_pass
               else LESSON_EXTRACT_TIMEOUT_SEC)
    rc, out, err = run(cmd, timeout=timeout, merge_bedrock=True)
    if rc != 0:
        log.warning("lesson-extractor rc=%d: %s", rc, (err or "").strip()[-200:])
        return
    try:
        summary = json.loads(out)
        if summary.get("n_proposed"):
            log.info("lesson-extractor: proposed %d lesson(s) from %d candidate(s) "
                     "(transcript_pass=%s, transcript_candidates=%d)",
                     summary.get("n_proposed"), summary.get("n_candidates"),
                     transcript_pass, summary.get("n_transcript_candidates", 0))
    except Exception:  # noqa: BLE001 — extractor output is diagnostic only
        pass


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

    # 0. Self-update: keep the running system current with its git remote
    #    (throttled hourly). bin/ + prompts/ are symlinked, so a pull alone
    #    makes code + Observer-prompt changes live on the very next pulse;
    #    skills/plists/hooks go through install.sh --apply. Refuses to touch a
    #    dirty or ahead repo — it surfaces, never discards. Logged to the
    #    actions-ledger so it shows on the dashboard.
    if not dry_run:
        self_update_pulse(pulse_idx)

    # 1. Drain inbox (typed event spine — see drain_inbox docstring).
    n_drained = 0 if dry_run else drain_inbox(pulse_idx)

    # 1.5. Triage new WorldEvents through the deterministic policy engine
    #      (Keel M2 — see run_triage_step docstring). Policy hits act
    #      mechanically; unmatched events get fail-safe escalate decisions
    #      plus at most ONE suggestion-only LLM call. Open escalate decisions
    #      mirror to awaiting cards (keyed `dec-<id>`), seeded below.
    triage_summary = {"cards": []} if dry_run else run_triage_step(pulse_idx)

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
    # Cards are rebuilt every pulse; open escalate decisions re-mirror here
    # each time (and vanish the pulse after they leave `open` — card
    # existence derives from queue state).
    awaiting_input: list[dict] = list(triage_summary.get("cards") or [])
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

    # Metering: snapshot the previous verdict per ws from observer-summaries
    # BEFORE phase 3's save_summary overwrites them (the summary on disk IS
    # last pulse's verdict — no extra state file needed). Never load-bearing:
    # a broken metering module degrades to an un-metered pulse, nothing else.
    prev_verdicts: dict[str, str] | None = {}
    observer_usages: list[dict] = []
    observer_duration_s = 0.0
    new_verdicts: dict[str, str] = {}
    synthesized_refs: set[str] = set()
    obs_hashes: dict[str, str] = {}
    carried: dict[str, dict] = {}  # ws_ref → prior verdict carried forward
    try:
        sys.path.insert(0, str(BIN))
        import metering  # noqa: PLC0415
    except Exception as e:  # noqa: BLE001 — metering must never break the pulse
        metering = None
        log.warning("metering module unavailable (ignored): %s", e)
    if metering is not None:
        try:
            prev_verdicts = metering.load_prev_verdicts(list(ctxs_by_ref.keys()))
        except Exception as e:  # noqa: BLE001 — metering must never break the pulse
            # Degrade ONLY the comparison: verdict_changes becomes null in the
            # record but cost/usage for this pulse is still written.
            prev_verdicts = None
            log.warning("metering prev-verdict snapshot failed (ignored): %s", e)

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
        # Phase 1.5 (Keel M2): Observer no-change skip. Hash each workspace's
        # Observer-visible state (transcript tail + screen + git signals +
        # idle-age band — everything build-ws-context gathered, minus the
        # always-growing raw age counter). A hash equal to the one stored
        # with the last verdict means the Observer would re-read
        # threshold-equivalent inputs: carry that verdict forward and leave
        # the ws out of the batch — UNLESS the verdict has already been
        # carried MAX_CONSECUTIVE_CARRIES times, in which case the ws is
        # force-observed (the cap is the defense against any hash blind
        # spot). Fenced per-ws — a hash failure just means that ws gets
        # observed normally.
        for ws_ref, ctx in ctxs_by_ref.items():
            try:
                obs_hashes[ws_ref] = obs_input_hash(ctx)
            except Exception as e:  # noqa: BLE001 — skip is an optimization only
                log.warning("obs-input hash failed for %s (observing): %s",
                            ws_ref, e)
                continue
            prior = load_prior_summary(ws_ref)
            if not (prior and prior.get("verdict")
                    and prior.get("obs_input_hash") == obs_hashes[ws_ref]):
                continue
            try:
                prior_carries = int(prior.get("carry_count") or 0)
            except (TypeError, ValueError):
                prior_carries = 0
            if prior_carries >= MAX_CONSECUTIVE_CARRIES:
                log.info("carry cap hit for %s (%d consecutive skips) — "
                         "force-observing", ws_ref, prior_carries)
                continue
            v = carry_forward_verdict(prior)
            v["carry_count"] = prior_carries + 1
            carried[ws_ref] = v
        if carried:
            log.info("no-change skip: %d/%d ws carried forward (%s)",
                     len(carried), len(ctxs_by_ref),
                     ", ".join(sorted(carried))[:200])

        # Phase 2: parallel batched Observer calls (skipped ws excluded — an
        # unchanged fleet spawns ZERO Observer subprocesses this pulse).
        ctxs_to_observe = [ctx for ws_ref, ctx in ctxs_by_ref.items()
                           if ws_ref not in carried]
        batches = chunk(ctxs_to_observe, WS_BATCH_SIZE) if ctxs_to_observe else []
        log.info("observing %d ws in %d batch(es) of <=%d in parallel",
                 len(ctxs_to_observe), len(batches), WS_BATCH_SIZE)

        verdicts_by_ref: dict[str, dict] = {}
        t_obs = time.time()
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
                        verdicts, usage = fut.result()
                        verdicts_by_ref.update(verdicts)
                        if usage:
                            observer_usages.append(usage)
                    except Exception as e:
                        log.exception("observer batch crashed: %s", e)
        observer_duration_s = time.time() - t_obs

        # Phase 3: save + execute per ws. Drop the synthetic `ws_ref` field
        # from the verdict before save (save-ws-summary writes it back from
        # its --ws-ref flag; we don't want it in the json blob twice).
        for ws_ref, ctx in ctxs_by_ref.items():
            ws = ws_by_ref[ws_ref]
            if ws_ref in carried:
                # No-change skip: re-save the prior verdict (refreshes
                # last_updated_ts so the LRU rotates; state_unchanged_since_ts
                # is preserved because the state-hash fields are byte-
                # identical) and re-emit any human-facing CARD the verdict
                # implies — awaiting cards are rebuilt from scratch every
                # pulse, so a carried needs_user/ready_for_cleanup that
                # didn't re-emit would vanish while the state persists.
                # NO action fires: execute_verdict(carry=True) runs only the
                # card-emitting paths — merge/cleanup/nudge sends already ran
                # when the verdict was first earned and must not repeat.
                v = carried[ws_ref]
                save_summary(ws, v)
                new_verdicts[ws_ref] = v.get("verdict") or "active"
                card_action = execute_verdict(ws, v, awaiting_input,
                                              carry=True)
                evidence = (f"state unchanged since last verdict "
                            f"({v.get('verdict')}); Observer call skipped "
                            f"(carry {v.get('carry_count')}/"
                            f"{MAX_CONSECUTIVE_CARRIES})")
                if card_action.get("kind") == "emit-card":
                    evidence += "; card re-emitted"
                actions_taken.append({
                    "key": f"{ws_ref}-no-change-skip",
                    "kind": "skipped-no-change", "ws_ref": ws_ref,
                    "outcome": "verified",
                    "evidence": evidence,
                })
                continue
            verdict = verdicts_by_ref.get(ws_ref)
            if not verdict:
                # Old-prompt behavior: an Observer failure (timeout / no verdict
                # for this ws) defaults to `active` — a benign no-op send-wise.
                # Save a synthesized summary so the dashboard's Workspaces tab
                # stays fresh instead of going stale on the prior verdict.
                synth = {
                    "verdict": "active",
                    "summary": "Observer returned no verdict this pulse "
                               "(timeout or batch error); defaulted to active.",
                    "next": "Assistant will re-observe next pulse.",
                }
                save_summary(ws, synth)
                new_verdicts[ws_ref] = "active"
                # A synthesized verdict is a failure artifact, not a real
                # judgment — it must never count as a verdict change (it would
                # inflate the rate once now and again on recovery next pulse).
                synthesized_refs.add(ws_ref)
                actions_taken.append({
                    "key": f"{ws_ref}-observer-failed",
                    "kind": "noop", "ws_ref": ws_ref, "outcome": "verified",
                    "evidence": "observer returned no verdict; defaulted to active",
                })
                continue
            v_for_save = {k: v for k, v in verdict.items() if k != "ws_ref"}
            # Stamp the input hash a REAL verdict was earned against, so the
            # next pulse can skip an unchanged workspace. Synthesized fallback
            # verdicts (above) deliberately carry no hash — an Observer
            # failure must be retried, never carried forward.
            if ws_ref in obs_hashes:
                v_for_save["obs_input_hash"] = obs_hashes[ws_ref]
            save_summary(ws, v_for_save)
            # Effective verdict for metering — mirrors what was just saved
            # (a verdict-less line defaults to active, same as the synth path).
            new_verdicts[ws_ref] = v_for_save.get("verdict") or "active"
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
        # Old-prompt behavior: surface the cap hit as an awaiting card so Mukul
        # knows dispatchable work is being held back, not just a trace line.
        held = ", ".join(t["id"] for t in bucket_b[:5])
        more = f" (+{len(bucket_b) - 5} more)" if len(bucket_b) > 5 else ""
        awaiting_input.append({
            "key": "dispatch-cap-hit",
            "tier": "T3",
            "title": f"Dispatch capped — {len(bucket_b)} TODO(s) waiting",
            "detail": (f"active={n_active}/{ACTIVE_WS_CAP}, "
                       f"total={n_total}/{TOTAL_WS_CAP}. Holding: {held}{more}. "
                       f"Reclaim a workspace or raise the cap to dispatch."),
        })
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

    # 7.5. Metering: one record per pulse to ~/.assistant/metrics.jsonl so
    #      cost/cadence regressions show on the dashboard the next morning.
    #      Runs AFTER state-write + heartbeat and swallows every failure —
    #      observability must never break the pulse.
    if metering is not None:
        try:
            # Skipped (carried-forward) workspaces cost no Observer call and
            # are excluded from batch_size, so the dashboard's skip rate now
            # reflects reality: an all-unchanged pulse records
            # observer_called=false.
            n_observed = len(ctxs_by_ref) - len(carried)
            observer_called = n_observed > 0
            if prev_verdicts is None:
                # Snapshot failed earlier — comparison degraded, cost still real.
                verdict_changes = None
            else:
                # Synthesized fallback verdicts are failure artifacts, not
                # judgments — exclude them from the change count.
                real_verdicts = {ws: v for ws, v in new_verdicts.items()
                                 if ws not in synthesized_refs}
                verdict_changes = metering.count_verdict_changes(prev_verdicts, real_verdicts)
            metering.append_metric(metering.build_pulse_record(
                epoch=utc_ts(),
                pulse_idx=pulse_idx,
                observer_called=observer_called,
                batch_size=n_observed,
                model=DEFAULT_OBSERVER_MODEL if observer_called else None,
                duration_s=observer_duration_s,
                usage=metering.sum_usage(observer_usages),
                new_verdicts=new_verdicts,
                verdict_changes=verdict_changes,
                synthesized=len(synthesized_refs),
                skipped=len(carried),
                actions=actions_taken,
            ))
        except Exception as e:  # noqa: BLE001 — metering must never break the pulse
            log.warning("metering append failed (ignored): %s", e)

    # 8. Lesson extraction (throttled, ~hourly). Lightweight pattern detection;
    #    only spends an LLM call when a candidate survives dedup. Runs last and
    #    bounded so it can never stall the dashboard or the heartbeat above.
    if pulse_idx % LESSON_EXTRACT_EVERY == 0:
        run_lesson_extractor(pulse_idx)

    log.info("=== pulse %d done in %.1fs ===", pulse_idx, time.time() - t0)
    return 0


def write_heartbeat(pulse_idx: int, drained: int) -> None:
    """The mechanical pulse has no cmux workspace of its own — it runs as a
    LaunchAgent. We still write heartbeat.json so the dashboard's pulse-health
    banner (render-assistant-page.py) can show the pulse is alive and fresh."""
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
