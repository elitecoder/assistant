#!/usr/bin/env python3
"""merge-pr-dispatch.py — execute a merge-pr action with mandatory safety
gates and submission verification.

The Assistant prompt cannot be trusted to faithfully execute a multi-step
"check files, then check CI, then send slash command, then verify it
submitted, then log outcome" sequence on every pulse. Sonnet under load
shortcuts. This script is the unbypassable mechanical layer.

The DECISION (should this PR even be a merge-pr candidate?) stays in the
observer Agent's prompt. But once the observer proposes merge-pr and the
dispatcher decides to act, the dispatcher invokes THIS script — and the
script enforces:

  Step 0 — safety gate:
    - gh pr view --json files,title,body
    - Either (a) every changed file is a test path, OR
             (b) refactor intent in title/body (refactor/rename/extract/
                 move prefix; or "no behavior change" / "byte-identical" /
                 "pure rename" body language)
    - If neither, exit 1 with reason; the dispatcher MUST emit an
      awaiting card and skip the dispatch.

  Step 1 — CI router:
    - Read statusCheckRollup + reviewDecision.
    - If any required check is FAILURE/CANCELLED/PENDING/IN_PROGRESS
      (SKIPPED/NEUTRAL count as pass), dispatch /monitor-ffp-ci.
    - Otherwise dispatch /merge-when-ready.

  Step 2 — actually submit:
    - cmux send <ws_ref> "/<skill> <PR>"  (NOT send-text — send presses Enter)
    - sleep 2
    - Read transcript tail via bin/transcript-tail.py --ws <ws_ref>
    - Confirm last_user.text matches the literal slash command.
    - If not, exit 2 with the observed last_user.text.

  Output: JSON to stdout describing what happened, for the dispatcher to
          log to its action ledger.

Exit codes:
  0  — submitted and verified
  1  — refused (safety gate failed)
  2  — sent but not verified (caller should retry once or surface card)
  3  — usage error / IO failure

Refactor PR rule (b) cannot be checked from this script alone — the
"full local G3 + unit suite green" signal lives in the workspace's claude
transcript. The script accepts an explicit --refactor-attested flag from
the dispatcher (the dispatcher reads the transcript and asserts). If the
dispatcher's claim is wrong, the safety gate still bounds the damage to
"refactor PRs the dispatcher mistakenly attested" — feature/bugfix PRs
without the flag are unconditionally refused.

Usage:
  merge-pr-dispatch.py --ws workspace:N --pr 10349 [--refactor-attested]
"""
import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time

CMUX = "/Applications/cmux.app/Contents/Resources/bin/cmux"
REPO = "Adobe-Firefly/firefly-platform"
TRANSCRIPT_TAIL = os.path.expanduser("~/dev/assistant/bin/transcript-tail.py")

TEST_PATH_RE = re.compile(
    r"^("
    r"e2e/"
    r"|src/.*?/__tests__/"
    r"|src/.*\.(test|spec)\.(ts|tsx|js|jsx)$"
    r"|fixtures/"
    r"|page-objects/"
    r")"
)
REFACTOR_PREFIX_RE = re.compile(r"\b(refactor|rename|extract|move)\(", re.IGNORECASE)
REFACTOR_BODY_PHRASES = [
    "no behavior change", "byte-identical", "pure rename",
    "same observable behavior", "lift up", "push down",
    "extract helper", "split function",
]


def gh_pr_view(pr, fields):
    out = subprocess.check_output(
        ["gh", "pr", "view", str(pr), "--repo", REPO, "--json", fields],
        text=True, timeout=15,
    )
    return json.loads(out)


def step0_safety_gate(pr, refactor_attested):
    """Returns (ok, reason, evidence_dict)."""
    pr_data = gh_pr_view(pr, "files,title,body,state,mergedAt")
    files = [f.get("path", "") for f in pr_data.get("files", [])]
    title = pr_data.get("title", "") or ""
    body = pr_data.get("body", "") or ""

    if pr_data.get("state") == "MERGED":
        return False, "already_merged", {"state": "MERGED", "merged_at": pr_data.get("mergedAt")}

    # Rule (a): test-only
    non_test = [p for p in files if p and not TEST_PATH_RE.match(p)]
    if not non_test and files:
        return True, "test_only", {"files": files, "rule": "(a) test-only"}

    # Rule (b): refactor — only honor if dispatcher attested it (transcript proof)
    if refactor_attested:
        refactor_signal = bool(REFACTOR_PREFIX_RE.search(title))
        body_lower = body.lower()
        body_signal = next((p for p in REFACTOR_BODY_PHRASES if p in body_lower), None)
        if refactor_signal or body_signal:
            return True, "refactor_attested", {
                "files_count": len(files), "title_signal": refactor_signal,
                "body_signal": body_signal, "rule": "(b) refactor + dispatcher attested G3 green",
            }
        return False, "refactor_attested_but_no_signal", {
            "files_count": len(files), "title": title[:100],
            "non_test_files_first_3": non_test[:3],
        }

    return False, "not_auto_mergeable", {
        "files_count": len(files), "non_test_files_first_3": non_test[:3],
        "title": title[:100], "rule_a_failed": "production code present",
        "rule_b_failed": "no --refactor-attested flag passed",
    }


def step1_ci_route(pr):
    """Returns (skill, reason, evidence_dict).

    skill: 'merge-when-ready' or 'monitor-ffp-ci'
    """
    d = gh_pr_view(pr, "statusCheckRollup,reviewDecision")
    rollup = d.get("statusCheckRollup", []) or []
    failing = []
    pending = []
    for check in rollup:
        # CheckRun has 'conclusion' + 'status'; StatusContext has 'state'
        conc = check.get("conclusion") or check.get("state") or ""
        name = check.get("name") or check.get("context") or "?"
        if conc in ("SUCCESS", "SKIPPED", "NEUTRAL"):
            continue
        if conc in ("FAILURE", "CANCELLED", "ERROR", "TIMED_OUT", "STARTUP_FAILURE"):
            failing.append({"name": name, "state": conc})
        elif conc in ("PENDING", "IN_PROGRESS", "QUEUED", "WAITING", "REQUESTED"):
            pending.append({"name": name, "state": conc})
        else:
            pending.append({"name": name, "state": conc or "UNKNOWN"})

    if failing or pending:
        return "monitor-ffp-ci", "ci_not_all_green", {
            "failing": failing, "pending": pending,
            "review_decision": d.get("reviewDecision"),
        }
    return "merge-when-ready", "ci_all_green", {
        "rollup_count": len(rollup),
        "review_decision": d.get("reviewDecision"),
    }


def step2_send_and_verify(ws_ref, slash_command):
    """Returns (ok, observed_last_user_text)."""
    # Use `cmux send` (auto-Enter), NOT `cmux send-text` (no submit).
    r = subprocess.run(
        [CMUX, "send", "--workspace", ws_ref, slash_command],
        capture_output=True, text=True, timeout=10,
    )
    if r.returncode != 0:
        return False, f"cmux_send_exit_{r.returncode}: {r.stderr[:200]}"

    # Wait for the agent to ingest the keystrokes + flush to JSONL.
    time.sleep(2.5)

    # Read the transcript tail and confirm last_user.text matches.
    tail = subprocess.run(
        ["python3", TRANSCRIPT_TAIL, "--ws", ws_ref, "--bytes", "8000"],
        capture_output=True, text=True, timeout=10,
    )
    if tail.returncode != 0:
        return False, f"transcript_tail_exit_{tail.returncode}: {tail.stderr[:200]}"
    try:
        td = json.loads(tail.stdout)
    except json.JSONDecodeError:
        return False, f"transcript_tail_bad_json: {tail.stdout[:200]}"

    last_user = (td.get("last_user") or {}).get("text", "") or ""
    last_user = last_user.strip()
    # Slash commands are wrapped in <command-message>...<command-name>...</command-name>...
    # so the literal echo will appear inside that wrapper, not as the bare /cmd.
    # Match either the bare command OR the wrapper that quotes it.
    bare_match = last_user == slash_command.strip()
    wrapper_match = (
        f"<command-name>{slash_command.split()[0]}</command-name>" in last_user
        and (len(slash_command.split()) == 1
             or " ".join(slash_command.split()[1:]) in last_user)
    )
    if bare_match or wrapper_match:
        return True, last_user[:200]
    return False, last_user[:200]


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--ws", required=True, help="workspace:N")
    p.add_argument("--pr", type=int, required=True)
    p.add_argument("--refactor-attested", action="store_true",
                   help="Dispatcher claims the workspace transcript shows full local G3 + unit suite green")
    args = p.parse_args()

    out = {"ws_ref": args.ws, "pr": args.pr, "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}

    # Step 0 — safety gate
    ok, reason, evidence = step0_safety_gate(args.pr, args.refactor_attested)
    out["step0"] = {"ok": ok, "reason": reason, "evidence": evidence}
    if not ok:
        out["outcome"] = "refused"
        out["awaiting_card"] = {
            "key": f"assistant:merge-pr-refused:{args.pr}:{reason}",
            "tier": "T2",
            "title": f"PR #{args.pr} needs human reviewer — not auto-mergeable",
            "detail": f"merge-pr-dispatch refused: {reason}. {json.dumps(evidence)[:300]}",
        }
        print(json.dumps(out, indent=2))
        sys.exit(1)

    # Step 1 — CI router
    skill, ci_reason, ci_evidence = step1_ci_route(args.pr)
    out["step1"] = {"skill": skill, "reason": ci_reason, "evidence": ci_evidence}

    # Step 2 — send + verify
    slash = f"/{skill} {args.pr}"
    out["step2"] = {"slash_command": slash, "ws": args.ws}
    submitted, observed = step2_send_and_verify(args.ws, slash)
    out["step2"]["submitted"] = submitted
    out["step2"]["observed_last_user_text"] = observed

    if not submitted:
        out["outcome"] = "send_unverified"
        out["awaiting_card"] = {
            "key": f"assistant:merge-pr-stuck:{args.pr}:send-unverified",
            "tier": "T2",
            "title": f"PR #{args.pr}: {slash} did not submit into {args.ws}",
            "detail": f"cmux send returned 0 but the JSONL transcript's last_user.text does not match. Observed: {observed[:200]}",
        }
        print(json.dumps(out, indent=2))
        sys.exit(2)

    out["outcome"] = "submitted"
    print(json.dumps(out, indent=2))
    sys.exit(0)


if __name__ == "__main__":
    main()
