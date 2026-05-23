#!/usr/bin/env python3
"""triage-inflight-check eval runner.

Spawns a one-shot headless Sonnet 1M Claude pulse with the production Triage
prompt, against a fixture world.json that contains:

  - One open TODO (td-019, autoDispatch=true, dispatchedAt empty) — Bucket B
    would normally fire a fresh dispatch.
  - One LIVE workspace whose title contains the td-id literally and whose
    recent_turns show it actively shipping the same work.

Asserts the agent's pre-dispatch in-flight check catches the overlap and
does NOT spawn a duplicate.

Exits 0 on PASS, 1 on FAIL with state dumped.
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
TRANSCRIPT_FIXTURE = FIXTURES / "transcript.jsonl"
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
        p
        for p in (WORLD_FIXTURE, TRANSCRIPT_FIXTURE, PROMPT_FILE, TRIAGE_PROMPT_FILE)
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
    env["EVAL_STATE_OUT"] = str(state_out)
    env["EVAL_MODE"] = "1"

    cmd = [
        CLAUDE_BIN,
        "--model",
        MODEL,
        "--permission-mode",
        "bypassPermissions",
        "--print",
        "--add-dir",
        str(Path.home() / ".claude"),
        "--add-dir",
        str(EVAL_DIR),
    ]
    log(f"spawning claude (model={MODEL}, timeout={TIMEOUT_SEC}s) ...")
    t0 = time.time()
    try:
        proc = subprocess.run(
            cmd,
            env=env,
            input=pulse_prompt,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SEC,
            cwd=str(EVAL_DIR),
        )
    except subprocess.TimeoutExpired as e:
        fail(f"claude timed out after {TIMEOUT_SEC}s\n{e.stdout}\n{e.stderr}")
    elapsed = time.time() - t0
    log(f"claude finished in {elapsed:.1f}s rc={proc.returncode}")
    log(f"stdout tail: {proc.stdout[-400:]!r}")
    if proc.returncode != 0:
        fail(
            f"claude exited rc={proc.returncode}\n--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
        )
    if not state_out.exists():
        fail(
            f"agent did not write {state_out}\n--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
        )
    try:
        return json.loads(state_out.read_text())
    except json.JSONDecodeError as e:
        fail(f"agent wrote invalid JSON: {e}\n--- file content ---\n{state_out.read_text()}")


def assert_decision(state: dict):
    actions = state.get("actions_taken") or []
    awaiting = state.get("awaiting_input") or []

    # 1) NO dispatch action for td-019 (no spawn happened)
    for a in actions:
        key = (a.get("key") or "").lower()
        kind = (a.get("kind") or "").lower()
        target_str = json.dumps(a.get("target") or {}).lower()
        # `triage:dispatch:td-019` would be the bad action. The good one is
        # `triage:dispatch-skipped:td-019:already-in-flight` — that key
        # contains "dispatch-skipped" not "dispatch:".
        if "td-019" in target_str or "td-019" in key:
            if "dispatch" in key and "dispatch-skipped" not in key and "dispatch-failed" not in key:
                fail(
                    f"agent dispatched a duplicate workspace for td-019: key={key!r} kind={kind!r}",
                    state=state,
                )
            if "spawn" in kind or "new-workspace" in kind or "new-workspace" in key:
                fail(
                    f"agent took a spawn action for td-019: key={key!r} kind={kind!r}",
                    state=state,
                )

    # 2) An action with dispatch-skipped key for td-019 must be present
    skipped = [
        a for a in actions
        if "dispatch-skipped" in (a.get("key") or "").lower()
        and "td-019" in (a.get("key") or "").lower()
    ]
    if not skipped:
        # Allow it to be in awaiting_input as a fallback (still better than dispatching).
        skipped_aw = [
            c for c in awaiting
            if "dispatch-skipped" in (c.get("key") or "").lower()
            and "td-019" in (c.get("key") or "").lower()
        ]
        if not skipped_aw:
            fail(
                "no `dispatch-skipped:td-019:*` entry in actions_taken[] OR awaiting_input[]. "
                "The pre-dispatch in-flight check should fire here — workspace:9898 is "
                "actively shipping the same work.",
                state=state,
            )
        log(f"found {len(skipped_aw)} dispatch-skipped card(s) in awaiting_input (acceptable)")
        skipped = skipped_aw
    else:
        log(f"found {len(skipped)} dispatch-skipped action(s) in actions_taken")

    # 3) Evidence must reference workspace:9898 OR the matching keyword
    entry = skipped[0]
    blob = " ".join(
        [
            str(entry.get("evidence", "")),
            str(entry.get("detail", "")),
            json.dumps(entry.get("touches") or []),
            json.dumps(entry.get("target") or {}),
        ]
    ).lower()
    proofs = [
        "workspace:9898",
        "ship td-019",
        "can-expand-trim",
        "agent-a10ad15a9bd79fd2d",
    ]
    if not any(p.lower() in blob for p in proofs):
        fail(
            "dispatch-skipped entry does not reference the matched workspace or keyword. "
            f"Expected one of {proofs} in evidence/detail/touches.",
            state=state,
        )
    log("evidence references the in-flight workspace ✓")


def main():
    assert_fixtures_present()
    with tempfile.TemporaryDirectory(prefix="triage-inflight-eval-") as td:
        state_out = Path(td) / "triage-state.json"
        log(f"state-out: {state_out}")
        state = run_pulse(state_out)
        assert_decision(state)
        last = EVAL_DIR / "last-run.json"
        last.write_text(json.dumps(state, indent=2))
        log(f"saved last-run.json → {last}")
    print("\n✅ PASS — Triage skipped duplicate dispatch when work is already in flight.")
    sys.exit(0)


if __name__ == "__main__":
    main()
