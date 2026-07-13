#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parent
REPO = EVAL_DIR.parents[1]
BIN = REPO / "bin"
if str(BIN) not in sys.path:
    sys.path.insert(0, str(BIN))
import llm_runner

CLAUDE_BIN = os.environ.get(
    "CLAUDE_BIN", str(Path.home() / ".local/bin/claude"))
DROID_BIN = os.environ.get(
    "DROID_BIN", str(Path.home() / ".local/bin/droid"))
CLAUDE_MODEL = os.environ.get(
    "EVAL_CLAUDE_MODEL", "us.anthropic.claude-sonnet-4-6[1m]")
DROID_MODEL = os.environ.get("EVAL_DROID_MODEL", "glm-5.2")
TIMEOUT = int(os.environ.get("EVAL_TIMEOUT_SEC", "240"))
REPORT = EVAL_DIR / "last-run.json"


def qualification_input_hashes() -> dict[str, str]:
    paths = [
        Path(__file__),
        BIN / "llm_runner.py",
        REPO / "prompts/triage-batch-prompt.md",
        REPO / "prompts/strategist-draft-prompt.md",
        REPO / "prompts/brief-narrator-prompt.md",
    ]
    return {
        str(path.relative_to(REPO)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in paths
    }


def json_object(text: str) -> dict | None:
    cleaned = "\n".join(
        line for line in (text or "").splitlines()
        if not line.strip().startswith("```"))
    try:
        value = json.loads(cleaned.strip())
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def json_lines(text: str) -> list[dict]:
    rows = []
    for line in (text or "").splitlines():
        try:
            value = json.loads(line.strip())
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def cases() -> list[dict]:
    triage_events = [
        {"id": "urgent-1", "source": "github", "kind": "review",
         "title": "Approval blocks release",
         "snippet": "Please approve before today's release cutoff.",
         "actor": "release-manager", "url": "", "refs": []},
        {"id": "review-2", "source": "github", "kind": "review",
         "title": "Non-urgent review request",
         "snippet": "Please review this cleanup when you have time next week.",
         "actor": "teammate", "url": "", "refs": []},
        {"id": "fyi-3", "source": "gmail", "kind": "newsletter",
         "title": "Weekly engineering newsletter",
         "snippet": "FYI: this week's platform updates.",
         "actor": "newsletter", "url": "", "refs": []},
    ]
    triage_prompt = (
        (REPO / "prompts/triage-batch-prompt.md").read_text()
        + "\n\n## RUNTIME CONTEXT\n"
        + json.dumps(triage_events, indent=2))
    strategist_prompt = (
        (REPO / "prompts/strategist-draft-prompt.md").read_text()
        + "\n\n## RUNTIME CONTEXT\n"
        + json.dumps({
            "goal": {
                "id": "goal-eval", "title": "Document retry policy",
                "outcome": "A reviewable design note exists",
                "playbook": {"unattended": ["doc-draft"], "gated": []},
            },
            "step_class": "doc-draft",
            "template": {
                "title": "[goal-eval] draft retry policy",
                "detail": "Draft only. No code, sends, deploys, or merges.",
            },
        }, indent=2)
        + "\nReturn one JSON object in the final response.")
    narrator_prompt = (
        (REPO / "prompts/brief-narrator-prompt.md").read_text()
        + "\n\n## RUNTIME CONTEXT\n"
        + json.dumps({
            "date": "2026-07-12",
            "counts": {"open_decisions": 1, "handled_overnight": 1},
            "receipts": [{"summary": "Connector tests passed."}],
            "decisions": [{
                "id": "dec-1", "title": "Review retry policy",
                "default_label": "Read the draft",
            }],
        }, indent=2))
    lesson_prompt = (
        "A user repeatedly corrected the coding agent for claiming completion "
        "without running tests. Return only one JSON object with keys trigger, "
        "rule, target, scope. The target must be claude and scope global. "
        "Generalize a durable imperative rule; do not use tools.")
    return [
        {"name": "triage", "prompt": triage_prompt, "score": score_triage},
        {"name": "strategist", "prompt": strategist_prompt,
         "score": score_strategist},
        {"name": "narrator", "prompt": narrator_prompt,
         "score": score_narrator},
        {"name": "lesson", "prompt": lesson_prompt, "score": score_lesson},
    ]


def score_triage(text: str, _: Path) -> tuple[bool, str]:
    rows = {row.get("event_id"): row for row in json_lines(text)}
    expected = {
        "urgent-1": "escalate", "review-2": "staged", "fyi-3": "digest"}
    ok = set(rows) == set(expected) and all(
        rows[key].get("suggested_lane") == lane
        and isinstance(rows[key].get("rationale"), str)
        and rows[key]["rationale"].strip()
        for key, lane in expected.items())
    lanes = {key: rows.get(key, {}).get("suggested_lane") for key in expected}
    return ok, f"lanes={lanes}"


def score_strategist(text: str, _: Path) -> tuple[bool, str]:
    value = json_object(text) or {}
    prose = f"{value.get('title', '')} {value.get('detail', '')}".lower()
    ok = (
        value.get("step_class") in (None, "doc-draft")
        and isinstance(value.get("title"), str) and bool(value["title"].strip())
        and isinstance(value.get("detail"), str)
        and len(value["detail"].strip()) >= 30
        and any(word in prose for word in ("draft", "document", "review"))
        and not re.search(
            r"\b(deploy|merge|send|ship|land|push|commit|implement|activate)\b",
            prose)
    )
    return ok, f"step_class={value.get('step_class')!r}"


def score_narrator(text: str, _: Path) -> tuple[bool, str]:
    value = json_object(text) or {}
    recs = value.get("recommendations")
    prose = (
        f"{value.get('summary', '')} "
        + " ".join(str(item) for item in recs.values())
        if isinstance(recs, dict) else "")
    ok = (
        isinstance(value.get("summary"), str)
        and bool(value["summary"].strip())
        and isinstance(recs, dict)
        and set(recs).issubset({"dec-1"})
        and isinstance(recs.get("dec-1"), str)
        and bool(recs["dec-1"].strip())
        and any(word in recs["dec-1"].lower()
                for word in ("retry", "policy", "draft"))
        and not re.search(
            r"(#\d+|\bdeploy(?:ed)?\b|\bmerge(?:d)?\b|\bsend\b|\bship(?:ped)?\b)",
            prose.lower())
    )
    return ok, f"recommendation_ids={sorted(recs) if isinstance(recs, dict) else None}"


def score_lesson(text: str, _: Path) -> tuple[bool, str]:
    value = json_object(text) or {}
    ok = (
        value.get("target") == "claude"
        and value.get("scope") == "global"
        and all(isinstance(value.get(key), str) and value[key].strip()
                for key in ("trigger", "rule"))
        and "test" in value.get("rule", "").lower()
    )
    return ok, f"target={value.get('target')!r} scope={value.get('scope')!r}"


def score_interactive(_: str, run_dir: Path) -> tuple[bool, str]:
    path = run_dir / "solution.py"
    if not path.exists():
        return False, "solution.py missing"
    spec = importlib.util.spec_from_file_location("fleet_solution", path)
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
        ok = module.add(2, 3) == 5 and module.add(-2, 2) == 0
    except Exception as error:
        return False, f"import/call failed: {error}"
    return ok, "add(2,3)=5 and add(-2,2)=0"


def invoke(provider: str, case: dict, run_dir: Path,
           tools: bool) -> llm_runner.LLMResult:
    model = DROID_MODEL if provider == "droid" else CLAUDE_MODEL

    def run_cmd(cmd, *, input_text=None, timeout=None, env=None,
                merge_bedrock=False):
        proc = subprocess.run(
            cmd, input=input_text, capture_output=True, text=True,
            timeout=timeout, cwd=run_dir, env=env or os.environ)
        return proc.returncode, proc.stdout, proc.stderr

    return llm_runner.invoke(
        provider=provider, prompt=case["prompt"], model=model,
        run_dir=run_dir, timeout=TIMEOUT, run=run_cmd,
        claude_bin=CLAUDE_BIN, droid_bin=DROID_BIN,
        reasoning_effort="high", disable_tools=not tools,
        extra_dirs=[run_dir], tag=f"assistant-fleet-eval-{case['name']}")


def run_case(provider: str, case: dict, tools: bool = False) -> dict:
    model = DROID_MODEL if provider == "droid" else CLAUDE_MODEL
    with tempfile.TemporaryDirectory(prefix=f"assistant-{provider}-") as raw:
        run_dir = Path(raw)
        started = time.time()
        try:
            result = invoke(provider, case, run_dir, tools)
        except subprocess.TimeoutExpired:
            return {"provider": provider, "model": model, "case": case["name"],
                    "passed": False, "error": f"timeout after {TIMEOUT}s"}
        if not result.usable:
            return {
                "provider": provider, "model": model,
                "case": case["name"], "passed": False,
                "rc": result.rc, "error": result.stderr[-400:],
            }
        passed, score = case["score"](result.result_text, run_dir)
        return {
            "provider": provider,
            "model": model,
            "case": case["name"],
            "passed": passed,
            "score": score,
            "elapsed_sec": round(time.time() - started, 3),
            "session_id": result.session_id,
            "tokens_in": result.tokens_in,
            "tokens_out": result.tokens_out,
            **({"result_tail": result.result_text[-500:]} if not passed else {}),
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", choices=("claude", "droid"),
                        default="droid")
    parser.add_argument("--shadow", action="store_true")
    parser.add_argument("--skip-interactive", action="store_true")
    parser.add_argument("--case", action="append", dest="case_names")
    args = parser.parse_args()
    selected = cases()
    if not args.skip_interactive:
        selected.append({
            "name": "interactive-add",
            "prompt": (
                "In this empty temporary directory, create solution.py with a "
                "correct Python function add(a, b). Do not modify anything "
                "outside this directory. Verify it locally, then finish."),
            "score": score_interactive,
            "tools": True,
        })
    if args.case_names:
        wanted = set(args.case_names)
        selected = [case for case in selected if case["name"] in wanted]
        if not selected:
            parser.error("no matching --case")
    input_hashes = qualification_input_hashes()
    providers = ["claude", "droid"] if args.shadow else [args.provider]
    results = []
    for provider in providers:
        for case in selected:
            print(f"[fleet-eval] {provider}/{case['name']}", flush=True)
            row = run_case(provider, case, tools=case.get("tools", False))
            results.append(row)
            print(f"  {'PASS' if row['passed'] else 'FAIL'}: "
                  f"{row.get('score') or row.get('error')}", flush=True)
    failed = [row for row in results if not row["passed"]]
    final_input_hashes = qualification_input_hashes()
    inputs_stable = final_input_hashes == input_hashes
    report = {
        "generated_at_epoch": int(time.time()),
        "git_revision": subprocess.run(
            ["git", "-C", str(REPO), "rev-parse", "HEAD"],
            capture_output=True, text=True).stdout.strip(),
        "prompt_hashes": {
            name: input_hashes[f"prompts/{name}"]
            for name in (
                "triage-batch-prompt.md",
                "strategist-draft-prompt.md",
                "brief-narrator-prompt.md",
            )
        },
        "input_hashes": input_hashes,
        "final_input_hashes": final_input_hashes,
        "inputs_stable": inputs_stable,
        "qualification_inputs_sha256": hashlib.sha256(
            json.dumps(input_hashes, sort_keys=True).encode()).hexdigest(),
        "mode": "shadow" if args.shadow else "single",
        "providers": providers,
        "cases_per_provider": len(selected),
        "passed": len(results) - len(failed),
        "failed": len(failed),
        "qualification_passed": (
            inputs_stable
            and not any(
                not row["passed"] for row in results
                if row["provider"] == "droid")
            and any(row["provider"] == "droid" for row in results)
        ),
        "parity_passed": args.shadow and inputs_stable and not failed,
        "results": results,
    }
    REPORT.write_text(json.dumps(report, indent=2) + "\n")
    reports = EVAL_DIR / "reports"
    reports.mkdir(exist_ok=True)
    immutable = reports / (
        f"{report['generated_at_epoch']}-{report['git_revision'][:12]}-"
        f"{report['mode']}.json")
    immutable.write_text(json.dumps(report, indent=2) + "\n")
    print(f"[fleet-eval] report: {REPORT}")
    if not inputs_stable:
        print("[fleet-eval] FAIL: qualification inputs changed during evaluation")
    return 1 if failed or not inputs_stable else 0


if __name__ == "__main__":
    raise SystemExit(main())
