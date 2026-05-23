#!/usr/bin/env python3
"""assistant-dispatch-validation eval runner.

Locks in the rule that Assistant must NEVER log a dispatch as successful when
the spawned workspace's last_user is the literal prompt-Read text and
last_assistant is null (the stranded-dispatch pattern from the 2026-05-23
ws:28/ws:29 incident).

Asserts:
  1. actions_taken[] contains a dispatch-failed / dispatch-stranded /
     dispatch-rescued / needs-rescue entry referencing td-901.
  2. actions_taken[] does NOT contain a key matching `assistant:dispatch:td-901`
     (the success path).
  3. The failure/rescue entry's evidence quotes the stranded condition.
"""
import json, os, subprocess, sys, tempfile, time
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parent
FIXTURES = EVAL_DIR / "fixtures"
WORLD_FIXTURE = FIXTURES / "world.json"
TRANSCRIPT_FIXTURE = FIXTURES / "stranded-transcript.jsonl"
PROMPT_FILE = EVAL_DIR / "eval-prompt.md"
ASSISTANT_PROMPT_FILE = Path.home() / ".claude/spawn-prompts/prompt-assistant-agent.md"

CLAUDE_BIN = os.environ.get("CLAUDE_BIN", str(Path.home() / ".local/bin/claude"))
MODEL = os.environ.get("EVAL_MODEL", "us.anthropic.claude-sonnet-4-6[1m]")
TIMEOUT_SEC = int(os.environ.get("EVAL_TIMEOUT_SEC", "600"))


def fail(msg, *, state=None):
    print(f"\n❌ FAIL: {msg}")
    if state is not None:
        print("\n--- state ---")
        print(json.dumps(state, indent=2))
    sys.exit(1)


def log(msg):
    print(f"[eval] {msg}")


def assert_fixtures_present():
    missing = [p for p in (WORLD_FIXTURE, TRANSCRIPT_FIXTURE, PROMPT_FILE, ASSISTANT_PROMPT_FILE) if not p.exists()]
    if missing: fail(f"missing: {missing}")
    if not Path(CLAUDE_BIN).exists(): fail(f"claude not at {CLAUDE_BIN}")


def run_pulse(state_out):
    pulse = PROMPT_FILE.read_text()
    env = dict(os.environ)
    env["EVAL_WORLD"] = str(WORLD_FIXTURE)
    env["EVAL_STATE_OUT"] = str(state_out)
    env["EVAL_MODE"] = "1"
    cmd = [
        CLAUDE_BIN, "--model", MODEL, "--permission-mode", "bypassPermissions",
        "--print",
        "--add-dir", str(Path.home() / ".claude"),
        "--add-dir", str(EVAL_DIR),
    ]
    log(f"spawning claude (model={MODEL}) ...")
    t0 = time.time()
    try:
        proc = subprocess.run(cmd, env=env, input=pulse, capture_output=True, text=True,
                              timeout=TIMEOUT_SEC, cwd=str(EVAL_DIR))
    except subprocess.TimeoutExpired as e:
        fail(f"timeout {TIMEOUT_SEC}s\n{e.stdout}\n{e.stderr}")
    log(f"finished in {time.time()-t0:.1f}s rc={proc.returncode}")
    log(f"stdout tail: {proc.stdout[-300:]!r}")
    if proc.returncode != 0:
        fail(f"rc={proc.returncode}\n{proc.stdout}\n{proc.stderr}")
    if not state_out.exists():
        fail(f"no state file at {state_out}\n{proc.stdout}\n{proc.stderr}")
    try:
        return json.loads(state_out.read_text())
    except json.JSONDecodeError as e:
        fail(f"bad JSON: {e}\n{state_out.read_text()}")


FAILURE_KEY_TOKENS = ("dispatch-failed", "dispatch-stranded", "dispatch-rescued", "needs-rescue")


def assert_decision(state):
    actions = state.get("actions_taken") or []

    # 1) MUST NOT log success-path dispatch
    for a in actions:
        key = (a.get("key") or "").lower()
        if "td-901" in key and "dispatch:" in key and not any(t in key for t in FAILURE_KEY_TOKENS):
            fail(f"agent logged a SUCCESS dispatch for td-901: key={key!r}. "
                 "Stranded workspace was treated as healthy. This is the bug we're guarding against.",
                 state=state)
        # also check kind
        kind = (a.get("kind") or "").lower()
        if "td-901" in json.dumps(a.get("target") or {}).lower():
            if kind == "dispatch" or kind == "dispatch-success":
                if not any(t in kind for t in FAILURE_KEY_TOKENS):
                    fail(f"agent logged kind={kind!r} for td-901 (success-path).", state=state)

    # 2) MUST log a failure/rescue entry
    failed_entries = []
    for a in actions:
        key_l = (a.get("key") or "").lower()
        kind_l = (a.get("kind") or "").lower()
        target_str = json.dumps(a.get("target") or {}).lower()
        is_failure = any(t in key_l for t in FAILURE_KEY_TOKENS) or any(t in kind_l for t in FAILURE_KEY_TOKENS)
        if is_failure and "td-901" in (key_l + " " + target_str):
            failed_entries.append(a)

    if not failed_entries:
        # also check awaiting_input as a fallback
        awaiting = state.get("awaiting_input") or []
        aw_failed = []
        for c in awaiting:
            key_l = (c.get("key") or "").lower()
            if any(t in key_l for t in FAILURE_KEY_TOKENS) and "td-901" in key_l:
                aw_failed.append(c)
        if not aw_failed:
            fail("no dispatch-failed/stranded/rescued/needs-rescue entry for td-901 found in actions_taken[] or awaiting_input[]. "
                 "Agent failed to detect the stranded dispatch.", state=state)
        log(f"found {len(aw_failed)} failure-flag card(s) in awaiting_input (acceptable)")
        failed_entries = aw_failed
    else:
        log(f"found {len(failed_entries)} failure/rescue action(s) ✓")

    # 3) Evidence must quote the stranded condition
    entry = failed_entries[0]
    blob = " ".join([
        str(entry.get("evidence", "")),
        str(entry.get("detail", "")),
        json.dumps(entry.get("touches") or []),
        json.dumps(entry.get("target") or {}),
        str(entry.get("title", "")),
    ]).lower()

    proofs = [
        "workspace:9700",
        "last_assistant",
        "last_turn_age",
        "1800",
        "30 min",
        "no assistant",
        "never submitted",
        "stranded",
        "prompt-dispatch-td-901",
    ]
    if not any(p in blob for p in proofs):
        fail("failure entry does not quote the stranded condition. Expected one of "
             f"{proofs} in evidence/detail.", state=state)
    log("evidence references the stranded condition ✓")


def main():
    assert_fixtures_present()
    with tempfile.TemporaryDirectory(prefix="dispatch-validation-eval-") as td:
        state_out = Path(td) / "triage-state.json"
        log(f"state-out: {state_out}")
        state = run_pulse(state_out)
        assert_decision(state)
        last = EVAL_DIR / "last-run.json"
        last.write_text(json.dumps(state, indent=2))
        log(f"saved last-run.json → {last}")
    print("\n✅ PASS — Assistant detected stranded dispatch and refused to log it as success.")
    sys.exit(0)


if __name__ == "__main__":
    main()
