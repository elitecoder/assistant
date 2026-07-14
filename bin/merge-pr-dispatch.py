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

CMUX = os.environ.get("CMUX_BIN", "/Applications/cmux.app/Contents/Resources/bin/cmux")
REPO = "Adobe-Firefly/firefly-platform"
# Resolve sibling scripts relative to THIS file, not a hardcoded ~/dev/assistant
# — so a checkout elsewhere still finds them (portability).
_BIN = os.path.dirname(os.path.abspath(__file__))
TRANSCRIPT_TAIL = os.path.join(_BIN, "transcript-tail.py")
CMUX_SEND = os.path.join(_BIN, "cmux-send.py")

# When main is under code freeze, green PRs do NOT merge to main — they merge
# into this staging branch. A PR that is BLOCKED on main purely because of the
# freeze gets retargeted here and proceeds. See operator memory
# "knowledge_freeze_blocked_prs_merge_to_munk_freeze_queue".
FREEZE_QUEUE_BRANCH = "munk/main-freeze-queue"
MAIN_FREEZE_RULESET_ID = 17577517  # MainFreeze ruleset on firefly-platform

# Any path under a Squirrel surface. A PR touching these is gated on the
# Squirrel E2E suite (pnpm e2e:squirrel), which FFP CI does NOT run — so
# CI-green is NOT validated-green for Squirrel. See operator memory
# "feedback_squirrel_pr_green_means_e2e_ran".
SQUIRREL_PATH_RE = re.compile(r"(^|/)squirrel(/|$)|squirrel", re.IGNORECASE)

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


def main_is_frozen():
    """True iff the MainFreeze ruleset is enforcement=active on the repo."""
    try:
        out = subprocess.check_output(
            ["gh", "api", f"repos/{REPO}/rulesets", "--jq",
             f'.[] | select(.id=={MAIN_FREEZE_RULESET_ID}) | .enforcement'],
            text=True, timeout=15,
        ).strip()
        return out == "active"
    except Exception:
        return False


def step_freeze_retarget(pr):
    """If the PR is BLOCKED on main AND main is frozen, retarget it to the
    freeze queue so it can proceed. Returns (retargeted, reason, evidence).

    Idempotent: a PR already based on the freeze queue is a no-op pass.
    """
    d = gh_pr_view(pr, "baseRefName,mergeStateStatus,state")
    base = d.get("baseRefName", "")
    if d.get("state") == "MERGED":
        return False, "already_merged", {"state": "MERGED"}
    if base == FREEZE_QUEUE_BRANCH:
        return False, "already_on_freeze_queue", {"base": base}
    if base != "main":
        return False, "base_not_main", {"base": base}
    # base == main. Only retarget if blocked AND a freeze is actually active.
    if d.get("mergeStateStatus") != "BLOCKED":
        return False, "not_blocked", {"base": base, "merge_state": d.get("mergeStateStatus")}
    if not main_is_frozen():
        return False, "main_not_frozen", {"base": base, "merge_state": "BLOCKED"}
    subprocess.run(
        ["gh", "pr", "edit", str(pr), "--repo", REPO, "--base", FREEZE_QUEUE_BRANCH],
        capture_output=True, text=True, timeout=20,
    )
    return True, "retargeted_to_freeze_queue", {"from": "main", "to": FREEZE_QUEUE_BRANCH}


def squirrel_e2e_gate(files, e2e_attested):
    """A Squirrel PR may not route to merge on CI-green alone — FFP CI does
    not run pnpm e2e:squirrel. Returns (ok, reason).

    ok=True when: no Squirrel files touched (gate N/A), OR the dispatcher
    attests the workspace transcript shows pnpm e2e:squirrel passed.
    """
    touches_squirrel = any(SQUIRREL_PATH_RE.search(p or "") for p in files)
    if not touches_squirrel:
        return True, "no_squirrel_files"
    if e2e_attested:
        return True, "squirrel_e2e_attested"
    return False, "squirrel_pr_needs_e2e_run"


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
    """Returns (ok, observed_last_user_text).

    Delegates the actual send to bin/cmux-send.py — the single sanctioned
    send path. cmux-send logs every call to ~/.assistant/sends.jsonl with
    the post-send transcript byte delta, which is the proof field that
    distinguishes "cmux returned OK but claude never ingested" from "claude
    actually saw it".
    """
    send = subprocess.run(
        [
            CMUX_SEND,
            "--ws", ws_ref,
            "--text", slash_command,
            "--enter",
            "--caller", "merge-pr-dispatch.py",
        ],
        capture_output=True, text=True, timeout=20,
    )
    try:
        send_record = json.loads(send.stdout)
    except Exception:
        return False, f"cmux_send_bad_output: {send.stdout[:200]}{send.stderr[:200]}"
    outcome = send_record.get("outcome", "")
    if send.returncode != 0:
        return False, f"cmux_send_exit_{send.returncode}_outcome={outcome}: {send_record.get('rpc_send_text',{}).get('body','')[:200]}"

    # Pre-check: did the transcript actually grow? cmux-send already
    # waited 2.5s post-send. If size_delta is 0, the keystrokes did not
    # reach the claude PID — fail fast rather than reading a stale
    # last_user.text and wrongly claiming submitted.
    delta = send_record.get("transcript_size_delta") or 0
    if delta <= 0:
        return False, f"transcript_delta_zero (sent OK but claude did not ingest): surface={send_record.get('target_surface_id','?')[:8]} ws_title={(send_record.get('target_ws_title') or '')[:50]}"

    # Read the transcript tail and confirm last_user.text matches.
    # 200KB budget: a slash-command user message inlines the entire skill
    # body verbatim and can be 30-50KB on its own; smaller budgets read
    # mid-line and silently fail to parse.
    tail = subprocess.run(
        ["python3", TRANSCRIPT_TAIL, "--ws", ws_ref, "--bytes", "200000"],
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
    # Slash commands appear in the transcript in one of three shapes:
    #   1. Bare echo: "/monitor-ffp-ci 10360" (rare — only if the user
    #      types it raw and it doesn't expand into the skill body).
    #   2. Wrapped: "<command-name>/foo</command-name>" + args (some
    #      older claude versions; not seen in current sessions).
    #   3. Inlined skill body: the full skill markdown gets pasted into
    #      the user message, ending with "ARGUMENTS: <pr>". Current
    #      claude (2.1.150) uses this shape.
    parts = slash_command.split()
    cmd, args = parts[0], parts[1:]
    bare_match = last_user == slash_command.strip()
    wrapper_match = (
        f"<command-name>{cmd}</command-name>" in last_user
        and (not args or " ".join(args) in last_user)
    )
    # Skill-body match: the slash skill name (cmd[1:] strips leading /)
    # appears in the body, AND the args appear at the trailing ARGUMENTS
    # line. Belt-and-braces: require both.
    skill_body_match = False
    if cmd.startswith("/") and len(cmd) > 1:
        skill_name = cmd[1:]
        skill_body_match = (
            skill_name in last_user
            and (not args or any(f"ARGUMENTS: {a}" in last_user for a in args))
        )
    if bare_match or wrapper_match or skill_body_match:
        return True, last_user[:200]
    return False, last_user[:200]


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--ws", required=True, help="workspace:N")
    p.add_argument("--pr", type=int, required=True)
    p.add_argument("--refactor-attested", action="store_true",
                   help="Dispatcher claims the workspace transcript shows full local G3 + unit suite green")
    p.add_argument("--e2e-attested", action="store_true",
                   help="Dispatcher claims the workspace transcript shows pnpm e2e:squirrel passed (required for Squirrel PRs)")
    args = p.parse_args()

    if not re.fullmatch(r"workspace:\d+", args.ws):
        print(json.dumps({"error": f"invalid --ws: {args.ws!r}"}), file=sys.stderr)
        sys.exit(3)

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

    # Step 0.5 — Squirrel E2E gate. CI-green is NOT validated-green for a
    # Squirrel PR (FFP CI never runs pnpm e2e:squirrel). Refuse to route to
    # merge unless the dispatcher attests the E2E suite passed.
    files = [f.get("path", "") for f in gh_pr_view(args.pr, "files").get("files", [])]
    e2e_ok, e2e_reason = squirrel_e2e_gate(files, args.e2e_attested)
    out["step0_5"] = {"ok": e2e_ok, "reason": e2e_reason}
    if not e2e_ok:
        out["outcome"] = "refused"
        out["awaiting_card"] = {
            "key": f"assistant:merge-pr-needs-e2e:{args.pr}",
            "tier": "T2",
            "title": f"PR #{args.pr} is Squirrel — needs pnpm e2e:squirrel before merge",
            "detail": "Squirrel PR; FFP CI does not run the E2E suite. Run pnpm e2e:squirrel "
                      "in the workspace and confirm pass; CI-green is not enough.",
        }
        print(json.dumps(out, indent=2))
        sys.exit(1)

    # Step 0.7 — freeze retarget. If main is frozen and this PR is BLOCKED on
    # main solely for that, point it at the freeze queue so it can proceed.
    retargeted, retarget_reason, retarget_ev = step_freeze_retarget(args.pr)
    out["step0_7"] = {"retargeted": retargeted, "reason": retarget_reason, "evidence": retarget_ev}
    if retargeted:
        time.sleep(6)  # let GitHub recompute mergeability against the new base

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
