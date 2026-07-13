#!/usr/bin/env python3
"""Observer eval suite — verifies the Observer agent emits correct verdicts.

Each fixture under fixtures/<case>/ contains:

  ctx.json            — the JSON build-ws-context.py would emit
  transcript.jsonl    — the workspace's JSONL Observer reads
  fake-gh-bin/gh      — shim that intercepts `gh pr view` (optional)
  expected.json       — the verdict we expect: {"verdict": "..."}
  notes.md            — what this case proves (human reading)

Runs every fixture by default. Pass a fixture name as argv to run one.

Usage:
  ./run.py                       # all fixtures
  ./run.py ws97-trap             # one fixture
  EVAL_MODEL=us.anthropic.claude-sonnet-4-6[1m] ./run.py
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parent
FIXTURES_DIR = EVAL_DIR / "fixtures"
REPO = EVAL_DIR.parents[1]
OBSERVER_PROMPT = REPO / "prompts/observer-batch-prompt.md"
BIN = REPO / "bin"
if str(BIN) not in sys.path:
    sys.path.insert(0, str(BIN))
import llm_runner

CLAUDE_BIN = os.environ.get("CLAUDE_BIN", str(Path.home() / ".local/bin/claude"))
DROID_BIN = os.environ.get("DROID_BIN", str(Path.home() / ".local/bin/droid"))
CLAUDE_MODEL = os.environ.get(
    "EVAL_CLAUDE_MODEL", "us.anthropic.claude-sonnet-4-6[1m]")
DROID_MODEL = os.environ.get("EVAL_DROID_MODEL", "glm-5.2")
TIMEOUT_SEC = int(os.environ.get("EVAL_TIMEOUT_SEC", "180"))
REPORT_PATH = EVAL_DIR / "last-run.json"
OBSERVER_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "ws_ref": {"type": "string", "minLength": 1},
        "verdict": {
            "type": "string",
            "enum": [
                "ready_for_merge", "ready_for_cleanup", "stranded",
                "needs_user", "active", "no_action",
            ],
        },
        "summary": {"type": "string", "minLength": 1},
        "next": {"type": "string", "minLength": 1},
        "title": {"type": "string", "minLength": 1},
        "detail": {"type": "string", "minLength": 1},
        "nudge_text": {"type": "string", "minLength": 1},
    },
    "required": ["ws_ref", "verdict", "summary", "next"],
    "additionalProperties": False,
}


def log(msg: str) -> None:
    print(f"[observer-eval] {msg}", flush=True)


def qualification_input_hashes() -> dict[str, str]:
    paths = [
        OBSERVER_PROMPT,
        Path(__file__),
        BIN / "llm_runner.py",
        BIN / "pulse.py",
        *sorted(path for path in FIXTURES_DIR.rglob("*") if path.is_file()),
    ]
    return {
        str(path.relative_to(REPO)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in paths
    }


def build_eval_prompt(fixture_dir: Path, ctx: dict) -> str:
    """Wrap the production prompt with one fixture and its runtime paths."""
    base = OBSERVER_PROMPT.read_text()
    ctx_json = json.dumps([ctx], indent=2)
    return (
        base
        + "\n\n---\n\n## EVAL HARNESS\n\n"
        + "You are judging this batch of 1 workspace.\n\n"
        + "Workspace ctx to judge:\n\n"
        + "```json\n" + ctx_json + "\n```\n"
        + "\nReturn one JSON object as the final response. It must include "
          "non-empty `ws_ref`, `verdict`, `summary`, and `next` fields. A "
          "`needs_user` verdict additionally requires non-empty `title` and "
          "`detail`; a `stranded` verdict additionally requires non-empty "
          "`nudge_text`. Verify the schema before responding. Do not write "
          "output files or add prose.\n"
    )


def extract_verdict(text: str) -> dict | None:
    """Return the first verdict object from a model final response."""
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("```"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and "verdict" in obj:
            return obj
    try:
        obj = json.loads((text or "").strip())
        if isinstance(obj, dict) and "verdict" in obj:
            return obj
    except Exception:
        pass
    return None


def stage_transcript(source: Path, destination: Path,
                     max_bytes: int = 131072) -> None:
    data = source.read_bytes()
    if len(data) > max_bytes:
        head_size = min(24576, max_bytes // 4)
        head = data[:head_size]
        head_end = head.rfind(b"\n")
        head = head[:head_end + 1] if head_end >= 0 else b""
        tail = data[-(max_bytes - len(head)):]
        tail_start = tail.find(b"\n")
        tail = tail[tail_start + 1:] if tail_start >= 0 else tail
        data = head + tail
    destination.write_bytes(data)


def pr_snapshot(cwd: Path, env: dict) -> dict | None:
    fields = (
        "state,baseRefName,statusCheckRollup,reviewDecision,mergeable,"
        "mergeStateStatus,files,title,body,number,url"
    )
    try:
        proc = subprocess.run(
            ["gh", "pr", "view", "--json", fields],
            cwd=cwd, env=env, capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    try:
        value = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def run_one(fixture_dir: Path, provider: str) -> tuple[bool, str, dict]:
    """Return pass/fail, message, and a serializable scored result."""
    ctx_path = fixture_dir / "ctx.json"
    transcript_path = fixture_dir / "transcript.jsonl"
    expected_path = fixture_dir / "expected.json"

    for p in (ctx_path, transcript_path, expected_path):
        if not p.exists():
            message = f"missing fixture file: {p.name}"
            return False, message, {"provider": provider, "error": message}

    try:
        expected = json.loads(expected_path.read_text())
    except Exception as e:
        message = f"expected.json invalid: {e}"
        return False, message, {"provider": provider, "error": message}

    # Patch the ctx so transcript_path points at the fixture's local file
    # AND tag with a synthetic ws_ref so the batch prompt can match the
    # output line back. Production ctxs always have a ws_ref; fixtures that
    # don't get a deterministic placeholder.
    staged = tempfile.TemporaryDirectory(prefix=f"observer-{provider}-")
    run_dir = Path(staged.name)
    ctx = json.loads(ctx_path.read_text())
    workspace = run_dir / "workspace"
    workspace.mkdir()
    ctx["cwd"] = str(workspace)
    if ctx.get("transcript_path") is None:
        ctx["transcript_excerpt"] = ""
    else:
        staged_transcript = run_dir / "transcript.jsonl"
        stage_transcript(transcript_path, staged_transcript)
        ctx["transcript_path"] = str(staged_transcript)
        ctx["transcript_excerpt"] = staged_transcript.read_text(errors="replace")
    ctx.setdefault("ws_ref", f"workspace:eval-{fixture_dir.name}")

    env = dict(os.environ)
    fake_gh = fixture_dir / "fake-gh-bin"
    if fake_gh.exists():
        env["PATH"] = f"{fake_gh}:{env.get('PATH', '')}"
    ctx["pr_data"] = pr_snapshot(workspace, env)
    (run_dir / "ctx.json").write_text(json.dumps(ctx, indent=2))
    prompt = build_eval_prompt(fixture_dir, ctx)

    model = DROID_MODEL if provider == "droid" else CLAUDE_MODEL

    def run_cmd(cmd, *, input_text=None, timeout=None, env=None,
                merge_bedrock=False):
        proc = subprocess.run(
            cmd, env=env or run_env, input=input_text,
            capture_output=True, text=True, timeout=timeout,
            cwd=str(run_dir),
        )
        return proc.returncode, proc.stdout, proc.stderr

    run_env = env
    t0 = time.time()
    try:
        result = llm_runner.invoke(
            provider=provider, prompt=prompt, model=model,
            run_dir=run_dir, timeout=TIMEOUT_SEC, run=run_cmd,
            claude_bin=CLAUDE_BIN, droid_bin=DROID_BIN,
            reasoning_effort="high", disable_tools=True,
            json_schema=OBSERVER_RESPONSE_SCHEMA,
            extra_dirs=[run_dir],
            tag=f"assistant-observer-eval-{fixture_dir.name}",
        )
    except subprocess.TimeoutExpired:
        message = f"timeout after {TIMEOUT_SEC}s"
        return False, message, {"provider": provider, "model": model,
                                "error": message}
    elapsed = time.time() - t0
    details = {
        "provider": provider,
        "model": model,
        "elapsed_sec": round(elapsed, 3),
        "rc": result.rc,
        "usable": result.usable,
        "session_id": result.session_id,
        "tokens_in": result.tokens_in,
        "tokens_out": result.tokens_out,
    }

    def fail(message: str):
        details["error"] = message
        details["passed"] = False
        return False, message, details

    if not result.usable:
        message = (
            f"{provider} rc={result.rc}\nstderr: {result.stderr[-400:]}")
        details["stdout_tail"] = result.stdout[-1000:]
        return fail(message)

    verdict = extract_verdict(result.result_text)
    details["verdict"] = verdict
    if verdict is None:
        message = f"no verdict in final response: {result.result_text[-300:]!r}"
        return fail(message)
    if verdict.get("ws_ref") != ctx["ws_ref"]:
        return fail(
            f"ws_ref={verdict.get('ws_ref')!r} expected {ctx['ws_ref']!r}")
    for key in ("summary", "next"):
        value = verdict.get(key)
        if isinstance(value, str) and len(value) > 240:
            clipped = value[:237].rsplit(" ", 1)[0].rstrip(" ,;:-")
            verdict[key] = clipped + "..."

    # Compare verdict kind exactly. For verdicts with payload (stranded /
    # needs_user), accept any non-empty value for the keyed field — content
    # is judged loosely (we care that the right verdict shape comes out).
    exp_kind = expected.get("verdict")
    allowed = {exp_kind, *expected.get("allowed_verdicts", [])}
    forbidden = set(expected.get("forbidden_verdicts", []))
    got_kind = verdict.get("verdict")

    # Loud-fail on dangerous-direction failures — these are the bugs we
    # built this suite to catch. A verdict in `forbidden_verdicts` means
    # the system did something destructive.
    if got_kind in forbidden:
        details["dangerous"] = True
        return fail(
            f"DANGEROUS: verdict={got_kind!r} is FORBIDDEN for this fixture "
            f"(full: {verdict})")

    if got_kind not in allowed:
        return fail(
            f"verdict={got_kind!r} expected one of {sorted(allowed)!r}  "
            f"(full: {verdict})")

    if got_kind == "stranded":
        if not verdict.get("nudge_text"):
            return fail("stranded verdict missing nudge_text")
    if got_kind == "needs_user":
        if not verdict.get("title") or not verdict.get("detail"):
            return fail("needs_user verdict missing title or detail")

    # Every verdict must carry summary + next for the dashboard's Workspaces tab.
    summary = verdict.get("summary", "")
    if not isinstance(summary, str) or not summary.strip():
        return fail(
            f"verdict missing required 'summary' field (got {summary!r})")
    if len(summary) > 240:
        return fail(
            f"summary too long ({len(summary)} chars > 240)")

    next_step = verdict.get("next", "")
    if not isinstance(next_step, str) or not next_step.strip():
        return fail(
            f"verdict missing required 'next' field (got {next_step!r})")
    if len(next_step) > 240:
        return fail(
            f"next too long ({len(next_step)} chars > 240)")

    details["passed"] = True
    return True, (
        f"verdict={got_kind!r} summary={summary[:50]!r}... "
        f"next={next_step[:50]!r}... elapsed={elapsed:.1f}s"), details


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("fixture", nargs="?", default=None, help="Run one fixture by name (default: all)")
    ap.add_argument("--list", action="store_true", help="List fixtures and exit")
    ap.add_argument("--provider", choices=("claude", "droid"),
                    default=os.environ.get("EVAL_PROVIDER", "droid"))
    ap.add_argument("--shadow", action="store_true",
                    help="Score Claude and Droid on identical fixtures")
    args = ap.parse_args()

    if not OBSERVER_PROMPT.exists():
        log(f"observer prompt missing at {OBSERVER_PROMPT}")
        return 2

    fixtures = sorted([d for d in FIXTURES_DIR.iterdir() if d.is_dir()])
    if args.list:
        for f in fixtures:
            print(f.name)
        return 0
    if args.fixture:
        fixtures = [f for f in fixtures if f.name == args.fixture]
        if not fixtures:
            log(f"no fixture named {args.fixture}")
            return 2

    input_hashes = qualification_input_hashes()
    providers = ["claude", "droid"] if args.shadow else [args.provider]
    passed = 0
    failed = 0
    failures = []
    results = []
    for provider in providers:
        for f in fixtures:
            log(f"running {provider}/{f.name} ...")
            ok, msg, details = run_one(f, provider)
            details["fixture"] = f.name
            results.append(details)
            status = "PASS" if ok else "FAIL"
            log(f"  {status}: {msg}")
            if ok:
                passed += 1
            else:
                failed += 1
                failures.append((f"{provider}/{f.name}", msg))

    log(f"\n=== {passed}/{passed+failed} passed ===")
    by_key = {(r["provider"], r["fixture"]): r for r in results}
    provider_scores = {
        provider: sum(
            1 for row in results
            if row["provider"] == provider and row.get("passed"))
        for provider in providers
    }
    agreements = 0
    if args.shadow:
        agreements = sum(
            1 for f in fixtures
            if (by_key.get(("claude", f.name), {}).get("verdict") or {}).get(
                "verdict")
            == (by_key.get(("droid", f.name), {}).get("verdict") or {}).get(
                "verdict"))
    final_input_hashes = qualification_input_hashes()
    inputs_stable = final_input_hashes == input_hashes
    report = {
        "generated_at_epoch": int(time.time()),
        "git_revision": subprocess.run(
            ["git", "-C", str(REPO), "rev-parse", "HEAD"],
            capture_output=True, text=True).stdout.strip(),
        "prompt_sha256": input_hashes[
            str(OBSERVER_PROMPT.relative_to(REPO))],
        "input_hashes": input_hashes,
        "final_input_hashes": final_input_hashes,
        "inputs_stable": inputs_stable,
        "qualification_inputs_sha256": hashlib.sha256(
            json.dumps(input_hashes, sort_keys=True).encode()).hexdigest(),
        "mode": "shadow" if args.shadow else "single",
        "providers": providers,
        "fixtures": len(fixtures),
        "passed": passed,
        "failed": failed,
        "provider_scores": provider_scores,
        "verdict_agreements": agreements if args.shadow else None,
        "qualification_passed": (
            inputs_stable
            and provider_scores.get("droid", 0) == len(fixtures)
            and not any(
                row.get("dangerous") for row in results
                if row["provider"] == "droid")
        ),
        "parity_passed": (
            args.shadow
            and inputs_stable
            and provider_scores.get("droid", 0) == len(fixtures)
            and provider_scores["droid"] >= provider_scores.get("claude", 0)
        ),
        "results": results,
    }
    REPORT_PATH.write_text(json.dumps(report, indent=2) + "\n")
    reports = EVAL_DIR / "reports"
    reports.mkdir(exist_ok=True)
    immutable = reports / (
        f"{report['generated_at_epoch']}-{report['git_revision'][:12]}-"
        f"{report['mode']}.json")
    immutable.write_text(json.dumps(report, indent=2) + "\n")
    log(f"report: {REPORT_PATH}")
    if failures:
        for name, msg in failures:
            print(f"  FAIL {name}: {msg}")
    if not inputs_stable:
        print("  FAIL qualification inputs changed during evaluation")
    if args.shadow:
        return 0 if report["parity_passed"] else 1
    if failures or not inputs_stable:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
