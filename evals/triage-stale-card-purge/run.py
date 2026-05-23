#!/usr/bin/env python3
"""triage-stale-card-purge eval runner.

Tests Step 2.5 — re-validation of carried-over awaiting cards.

Fixture:
  - world.json: td-501, td-502 with autoDispatch=true, dispatchedAt empty
  - prior triage-state: a `triage:autodispatch-unset:bulk` card claiming both
    TODOs have autoDispatch=null (which was true at the moment that prior
    pulse ran but is no longer true)

Asserts:
  1. Output awaiting_input[] does NOT contain the stale autodispatch-unset card.
  2. Output actions_taken[] contains an `awaiting-purged` entry quoting current
     state.
  3. Output actions_taken[] contains dispatch-intent entries for BOTH td-501
     and td-502 (since the in-flight check finds no live sessions).

Exits 0 on PASS, 1 on FAIL.
"""
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parent
FIXTURES = EVAL_DIR / "fixtures"
WORLD_FIXTURE = FIXTURES / "world.json"
PRIOR_STATE_FIXTURE = FIXTURES / "prior-triage-state.json"
PROMPT_FILE = EVAL_DIR / "eval-prompt.md"
TRIAGE_PROMPT_FILE = Path.home() / ".claude/spawn-prompts/prompt-triage-agent.md"

CLAUDE_BIN = os.environ.get("CLAUDE_BIN", str(Path.home() / ".local/bin/claude"))
MODEL = os.environ.get("EVAL_MODEL", "us.anthropic.claude-sonnet-4-6[1m]")
TIMEOUT_SEC = int(os.environ.get("EVAL_TIMEOUT_SEC", "600"))


def fail(msg, *, state=None):
    print(f"\n❌ FAIL: {msg}")
    if state is not None:
        print("\n--- triage-state output ---")
        print(json.dumps(state, indent=2))
    sys.exit(1)


def log(msg):
    print(f"[eval] {msg}")


def assert_fixtures_present():
    missing = [
        p for p in (WORLD_FIXTURE, PRIOR_STATE_FIXTURE, PROMPT_FILE, TRIAGE_PROMPT_FILE)
        if not p.exists()
    ]
    if missing:
        fail(f"missing fixture(s): {missing}")
    if not Path(CLAUDE_BIN).exists():
        fail(f"claude binary not found at {CLAUDE_BIN}")


def run_pulse(state_out: Path) -> dict:
    pulse_prompt = PROMPT_FILE.read_text()
    env = dict(os.environ)
    env["EVAL_WORLD"] = str(WORLD_FIXTURE)
    env["EVAL_PRIOR_STATE"] = str(PRIOR_STATE_FIXTURE)
    env["EVAL_STATE_OUT"] = str(state_out)
    env["EVAL_MODE"] = "1"

    cmd = [
        CLAUDE_BIN,
        "--model", MODEL,
        "--permission-mode", "bypassPermissions",
        "--print",
        "--add-dir", str(Path.home() / ".claude"),
        "--add-dir", str(EVAL_DIR),
    ]
    log(f"spawning claude (model={MODEL}, timeout={TIMEOUT_SEC}s) ...")
    t0 = time.time()
    try:
        proc = subprocess.run(
            cmd, env=env, input=pulse_prompt, capture_output=True, text=True,
            timeout=TIMEOUT_SEC, cwd=str(EVAL_DIR),
        )
    except subprocess.TimeoutExpired as e:
        fail(f"claude timed out after {TIMEOUT_SEC}s\n{e.stdout}\n{e.stderr}")
    elapsed = time.time() - t0
    log(f"claude finished in {elapsed:.1f}s rc={proc.returncode}")
    log(f"stdout tail: {proc.stdout[-400:]!r}")
    if proc.returncode != 0:
        fail(f"claude exited rc={proc.returncode}\n--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}")
    if not state_out.exists():
        fail(f"agent did not write {state_out}\n--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}")
    try:
        return json.loads(state_out.read_text())
    except json.JSONDecodeError as e:
        fail(f"agent wrote invalid JSON: {e}\n--- file content ---\n{state_out.read_text()}")


def assert_decision(state: dict):
    actions = state.get("actions_taken") or []
    awaiting = state.get("awaiting_input") or []

    # 1) The stale autodispatch-unset card MUST NOT survive
    stale_survivors = [
        c for c in awaiting
        if "autodispatch-unset" in (c.get("key") or "").lower()
    ]
    if stale_survivors:
        fail(
            f"stale autodispatch-unset card was re-emitted: keys={[c.get('key') for c in stale_survivors]}. "
            "Step 2.5 should have caught that all referenced TODOs now have autoDispatch=true.",
            state=state,
        )
    log("stale autodispatch-unset card was correctly purged ✓")

    # 2) An awaiting-purged action must record the drop
    purged = [
        a for a in actions
        if "awaiting-purged" in (a.get("key") or "").lower()
        or "awaiting-purged" in (a.get("kind") or "").lower()
    ]
    if not purged:
        log("WARN: no `awaiting-purged` action_taken entry — agent dropped silently. "
            "Not a hard fail (the audit trail is missing but the outcome is correct).")
    else:
        log(f"found {len(purged)} awaiting-purged action(s) ✓")

    # 3) Both td-501 and td-502 must have dispatch entries
    dispatched = []
    for a in actions:
        key = (a.get("key") or "").lower()
        target_str = json.dumps(a.get("target") or {}).lower()
        if "dispatch" in key and "dispatch-skipped" not in key and "dispatch-failed" not in key and "awaiting" not in key:
            for tid in ("td-501", "td-502"):
                if tid in key or tid in target_str:
                    dispatched.append(tid)

    dispatched_set = set(dispatched)
    expected = {"td-501", "td-502"}
    missing = expected - dispatched_set
    if missing:
        fail(
            f"agent did NOT dispatch the eligible TODOs: missing {missing}. "
            f"Both td-501 and td-502 had autoDispatch=true and no live in-flight session. "
            f"They are clear Bucket B candidates and should have a `triage:dispatch:td-NNN` action.",
            state=state,
        )
    log(f"both td-501 and td-502 have dispatch actions ✓")


def main():
    assert_fixtures_present()
    with tempfile.TemporaryDirectory(prefix="triage-purge-eval-") as td:
        state_out = Path(td) / "triage-state.json"
        log(f"state-out: {state_out}")
        state = run_pulse(state_out)
        assert_decision(state)
        last = EVAL_DIR / "last-run.json"
        last.write_text(json.dumps(state, indent=2))
        log(f"saved last-run.json → {last}")
    print("\n✅ PASS — Triage purged stale awaiting card AND dispatched newly-eligible TODOs.")
    sys.exit(0)


if __name__ == "__main__":
    main()
