#!/usr/bin/env python3
"""strategist.py — the Strategist's LLM subprocess caller + CLI (Keel M6).

The pure logic lives in src/assistant/strategist.py (throttle, ceiling,
auto-pause, strict-JSON validation, template fallback, WHAT-not-WHETHER). This
file is the ONE place that actually spawns an LLM — the Observer/triage
subprocess pattern verbatim (`claude --print --output-format json`, the model
writes its structured output to a file, stdout is the CLI result envelope kept
for metering, the whole run is archived under ~/.assistant/strategist-runs/).

It exposes the two INJECTED callables the pure module needs:

  • draft_for_planner(goal, step_class, tt, td, now)  — pulse.run_planner_step
    injects this as goals.plan_pass(strategist_draft=…). It wraps
    strategist.upgrade_step_text with llm_draft=call_strategist_draft, so ALL
    the gating (active/throttle/ceiling/pause) + validation + template fallback
    happen in the pure module; this file only spawns + meters the subprocess.

  • pre_research(pulse_idx, now) — wraps strategist.pre_research_pass with
    llm_context=call_strategist_context for the nightly decision-context
    pre-research on idle capacity.

Every LLM call is metered into ~/.assistant/cost-ledger.jsonl as
caller="strategist" (tokens_in/out, est_usd, wall_ms — match bin/metering.py).
A failed subprocess or an unparseable result envelope books a ZERO-cost
status="failed" row (no phantom spend), exactly like the triage caller.

TESTABILITY: this module's `run` is the ONLY subprocess touchpoint; every test
mocks `run` (as test_triage_llm_caller mocks pulse.run) — NO live LLM, NO
network. Pure stdlib. NEVER closes a workspace; no launchctl from code.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BIN = REPO / "bin"
SRC = REPO / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
from assistant import model_tiers  # noqa: E402

STRATEGIST_DRAFT_PROMPT = REPO / "prompts/strategist-draft-prompt.md"


def _home() -> Path:
    return Path(os.environ.get("HOME", str(Path.home())))


def strategist_runs_dir() -> Path:
    return _home() / ".assistant" / "strategist-runs"


# The Strategist drafts staged-step TEXT from inline JSON → BALANCED tier.
# STRATEGIST_MODEL / OBSERVER_MODEL still win if set (back-compat), else resolve.
DEFAULT_MODEL = (os.environ.get("STRATEGIST_MODEL")
                 or os.environ.get("OBSERVER_MODEL")
                 or model_tiers.model_for("balanced"))
DEFAULT_CLAUDE_BIN = os.environ.get("CLAUDE_BIN", str(Path.home() / ".local/bin/claude"))
# The drafter reads inline goal/decision JSON only (no transcripts), so it gets
# the same short leash the triage batch does — never the Observer's.
STRATEGIST_TIMEOUT_SEC = int(os.environ.get("STRATEGIST_TIMEOUT_SEC", "240"))

log = logging.getLogger("strategist-cli")


def utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ─── bedrock env + subprocess (the ONLY LLM touchpoint; tests mock run) ──────

def _bedrock_env() -> dict:
    """Read the handful of Bedrock/AWS vars from ~/.zprofile that launchd does
    NOT source (same rationale as pulse.load_bedrock_env). Cheap, best-effort."""
    extracted: dict[str, str] = {}
    zprofile = _home() / ".zprofile"
    if not zprofile.exists():
        return extracted
    keys = ("CLAUDE_CODE_USE_BEDROCK", "AWS_REGION", "AWS_BEARER_TOKEN_BEDROCK",
            "AWS_PROFILE", "ANTHROPIC_API_KEY")
    pat = re.compile(r'^\s*export\s+([A-Z_][A-Z0-9_]*)\s*=\s*(.+?)\s*$')
    try:
        for line in zprofile.read_text().splitlines():
            m = pat.match(line)
            if not m or m.group(1) not in keys:
                continue
            v = m.group(2).strip()
            if (v.startswith('"') and v.endswith('"')) or \
               (v.startswith("'") and v.endswith("'")):
                v = v[1:-1]
            extracted[m.group(1)] = v
    except OSError:
        pass
    return extracted


def run(cmd: list[str], *, input_text: str | None = None,
        timeout: int = 30, merge_bedrock: bool = False) -> tuple[int, str, str]:
    """Run a subprocess; return (rc, stdout, stderr). Never raises. The child
    runs in its own session and a timeout kills the whole process GROUP before
    the final reap (a hung `claude` can leave grandchildren holding the pipes),
    mirroring pulse.run so a stalled Strategist call can never stall a pulse."""
    env = dict(os.environ)
    if merge_bedrock:
        env.update(_bedrock_env())
    try:
        proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, env=env,
            start_new_session=True)
    except OSError as e:
        return 127, "", f"spawn failed: {e}"
    try:
        out, err = proc.communicate(input=input_text, timeout=timeout)
        return proc.returncode, out or "", err or ""
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (OSError, ProcessLookupError):
            proc.kill()
        try:
            out, err = proc.communicate(timeout=10)
        except Exception:  # noqa: BLE001
            out, err = "", ""
        return 124, out or "", (err or "") + f"\ntimeout after {timeout}s"


# ─── output parsers (tolerant, like read_lane_suggestions) ───────────────────

def _strip_fences(text: str) -> str:
    lines = [ln for ln in (text or "").splitlines() if not ln.strip().startswith("```")]
    return "\n".join(lines).strip()


def read_draft(path: Path) -> dict | None:
    """Parse the strict-JSON draft the model wrote. Returns a dict or None
    (missing/empty/unparseable → None → the pure module uses the template)."""
    try:
        text = path.read_text()
    except (OSError, FileNotFoundError):
        return None
    text = _strip_fences(text)
    if not text:
        return None
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def read_context_file(path: Path) -> str | None:
    try:
        text = path.read_text()
    except (OSError, FileNotFoundError):
        return None
    text = _strip_fences(text)
    return text or None


# ─── metering (never load-bearing; mirrors call_triage_batch exactly) ────────

def _meter(out: str, rc: int, prompt_len: int, wall_ms: int) -> dict:
    """Append one cost-ledger row (caller='strategist').

    On FAILURE (rc!=0 or an unparseable envelope) we still book an ESTIMATED,
    NON-ZERO cost (C-F-2/C-O-3), not a $0 row: a 240s timeout (rc=124) or an
    aborted call was almost certainly billed server-side, and the daily ceiling
    is the failure-spend backstop — if failures booked $0 the ceiling would be
    evadable by repeatedly failing, and repeated timeouts would never ratchet
    it. metering.observer_usage already returns the real CLI numbers when the
    envelope parses (even on rc!=0) and a chars/4 estimate over the prompt
    otherwise, so the row reflects the best available spend signal. The row is
    still stamped status='failed' so failures stay visible; it just no longer
    hides their cost. Returns the usage dict."""
    usage: dict = {}
    try:
        if str(BIN) not in sys.path:
            sys.path.insert(0, str(BIN))
        import metering  # noqa: PLC0415
        failed = rc != 0 or metering.parse_cli_result(out) is None
        # observer_usage: CLI numbers when the envelope parses, chars/4 estimate
        # (from the prompt we know we sent) otherwise — never a phantom $0.
        usage = metering.observer_usage(out, prompt_len, DEFAULT_MODEL)
        metering.append_cost_row(
            caller="strategist", model=DEFAULT_MODEL, usage=usage,
            wall_ms=wall_ms, status="failed" if failed else "ok")
    except Exception as e:  # noqa: BLE001 — metering must never break the pulse
        log.warning("strategist metering capture failed (ignored): %s", e)
    return usage


def _spawn(prompt: str, run_dir: Path, out_name: str) -> tuple[int, str, str, int]:
    """Spawn ONE Strategist subprocess. The model writes `out_name` into
    run_dir; stdout is the CLI JSON envelope (metering only). Archives
    prompt/stdout/stderr/meta like the Observer/triage runs."""
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "prompt.md").write_text(prompt)
    cmd = [
        DEFAULT_CLAUDE_BIN,
        "--model", DEFAULT_MODEL,
        "--dangerously-skip-permissions",
        "--print",
        "--output-format", "json",
        "--add-dir", str(run_dir),
    ]
    t0 = time.time()
    rc, out, err = run(cmd, input_text=prompt, timeout=STRATEGIST_TIMEOUT_SEC,
                       merge_bedrock=True)
    wall_ms = int((time.time() - t0) * 1000)
    usage = _meter(out, rc, len(prompt), wall_ms)
    (run_dir / "stdout.txt").write_text(out or "")
    (run_dir / "stderr.txt").write_text(err or "")
    (run_dir / "meta.json").write_text(json.dumps({
        "rc": rc, "wall_ms": wall_ms, "model": DEFAULT_MODEL,
        "out_name": out_name, "usage": usage, "ts": utc_iso(),
    }, indent=2))
    if rc != 0:
        log.warning("strategist subprocess rc=%d: %s", rc, (err or "").strip()[-300:])
    return rc, out, err, wall_ms


# ─── the injected LLM callables ──────────────────────────────────────────────

def call_strategist_draft(goal: dict, step_class: str,
                          template_title: str, template_detail: str,
                          pulse_idx: int) -> dict | None:
    """Spawn the Strategist to draft a staged step's TEXT. Returns the parsed
    strict-JSON dict (validated downstream by strategist.validate_draft) or None
    (missing prompt / failed subprocess / unparseable → the pure module uses
    the template). Suggestion-only by construction: the return is fed to
    validate_draft, which rejects any out-of-playbook step_class and returns
    TEXT ONLY — this dict can never become an action class."""
    if not STRATEGIST_DRAFT_PROMPT.exists():
        log.error("strategist draft prompt missing: %s", STRATEGIST_DRAFT_PROMPT)
        return None
    gid = goal.get("id")
    run_dir = strategist_runs_dir() / f"{pulse_idx:04d}" / f"draft-{gid}-{step_class}"
    draft_path = run_dir / "draft.json"
    context = {
        "goal": {k: goal.get(k) for k in
                 ("id", "title", "outcome", "links", "playbook", "horizon")},
        "step_class": step_class,
        "template": {"title": template_title, "detail": template_detail},
    }
    prompt = (
        STRATEGIST_DRAFT_PROMPT.read_text()
        + "\n\n---\n\n## RUNTIME CONTEXT\n\n"
        + "**Output destination — write your ONE JSON object to this file, "
          "not stdout.** Use the Write tool to write to:\n\n"
        + f"    {draft_path}\n\n"
        + f"The step_class is FIXED at `{step_class}` and Python-owned — echo it "
          "exactly or omit it. You are drafting TEXT ONLY; the staged action "
          "class stays fixed regardless of what you echo, and an echoed class "
          "OUTSIDE this goal's playbook rejects your draft (falls back to the "
          "template).\n\n"
        + "Goal + template context:\n\n```json\n"
        + json.dumps(context, indent=2) + "\n```\n"
    )
    _spawn(prompt, run_dir, "draft.json")
    return read_draft(draft_path)


def call_strategist_context(decision: dict, pulse_idx: int) -> str | None:
    """Spawn the Strategist to pre-research one OPEN decision into a markdown
    brief-context. Returns the markdown or None. Draft-only — the file is
    surfaced inline in the brief; it never becomes an action."""
    if not STRATEGIST_DRAFT_PROMPT.exists():
        log.error("strategist draft prompt missing: %s", STRATEGIST_DRAFT_PROMPT)
        return None
    dec_id = decision.get("id")
    run_dir = strategist_runs_dir() / f"{pulse_idx:04d}" / f"context-{dec_id}"
    ctx_path = run_dir / "context.md"
    slim = {k: decision.get(k) for k in
            ("id", "title", "snippet", "source", "kind", "lane", "refs",
             "recommended", "goal_refs")}
    prompt = (
        STRATEGIST_DRAFT_PROMPT.read_text()
        + "\n\n---\n\n## RUNTIME CONTEXT — decision pre-research (draft-only)\n\n"
        + "Write a SHORT markdown context (what this decision is, the relevant "
          "background, and a suggested reversible next move) to this file — "
          "NOT stdout. This is surfaced inline in the morning brief; it never "
          "acts, sends, or dispatches.\n\n"
        + f"    {ctx_path}\n\n"
        + "Decision:\n\n```json\n" + json.dumps(slim, indent=2) + "\n```\n"
    )
    _spawn(prompt, run_dir, "context.md")
    return read_context_file(ctx_path)


# ─── wrappers the pulse injects ──────────────────────────────────────────────

def _load_strategist():
    if str(SRC) not in sys.path:
        sys.path.insert(0, str(SRC))
    from assistant import strategist  # noqa: PLC0415
    return strategist


def draft_for_planner(goal: dict, step_class: str,
                      template_title: str, template_detail: str,
                      now: float, pulse_idx: int = 0) -> tuple[str, str]:
    """The callable pulse injects as goals.plan_pass(strategist_draft=…). All
    gating + validation + template fallback happen in the pure module; this
    only provides the spawning llm_draft. Returns (title, detail) — TEXT ONLY."""
    strategist = _load_strategist()
    return strategist.upgrade_step_text(
        goal, step_class, template_title, template_detail,
        llm_draft=lambda g, sc, tt, td: call_strategist_draft(
            g, sc, tt, td, pulse_idx),
        now=now, log=log)


def pre_research(pulse_idx: int = 0, now: float | None = None) -> dict:
    """Run the nightly decision-context pre-research pass, injecting the
    context subprocess caller. Idle-capacity / throttle / ceiling / auto-pause
    gates all live in the pure module."""
    now = now if now is not None else time.time()
    strategist = _load_strategist()
    return strategist.pre_research_pass(
        now, llm_context=lambda dec: call_strategist_context(dec, pulse_idx),
        log=log)


# ─── CLI ─────────────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--pre-research", action="store_true",
                    help="run the decision-context pre-research pass now")
    ap.add_argument("--now", type=float, default=None,
                    help="run as-of this epoch (tests/replay)")
    ap.add_argument("--pulse-idx", type=int, default=0)
    ap.add_argument("--print", dest="print_json", action="store_true",
                    help="dump the Strategist governance status as JSON")
    args = ap.parse_args(argv)

    strategist = _load_strategist()
    now = args.now if args.now is not None else time.time()

    if args.print_json:
        ok, reason = strategist.active(now)
        status = {
            "enabled": strategist.enabled(),
            "active": ok,
            "blocking_reason": reason,
            "day_spend_usd": strategist.day_spend_usd(now),
            "daily_cost_ceiling_usd": strategist.daily_cost_ceiling(),
            "over_ceiling": strategist.over_ceiling(now),
            "auto_pause_reason": strategist.auto_pause_reason(now),
        }
        print(json.dumps(status, indent=2))
        return 0

    if args.pre_research:
        summary = pre_research(pulse_idx=args.pulse_idx, now=now)
        print(json.dumps(summary, ensure_ascii=False))
        return 0

    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
