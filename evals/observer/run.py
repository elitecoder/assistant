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
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parent
FIXTURES_DIR = EVAL_DIR / "fixtures"
OBSERVER_PROMPT = Path.home() / "dev/assistant/prompts/observer-batch-prompt.md"

CLAUDE_BIN = os.environ.get("CLAUDE_BIN", str(Path.home() / ".local/bin/claude"))
MODEL = os.environ.get("EVAL_MODEL", "us.anthropic.claude-sonnet-4-6[1m]")
TIMEOUT_SEC = int(os.environ.get("EVAL_TIMEOUT_SEC", "180"))


def log(msg: str) -> None:
    print(f"[observer-eval] {msg}", flush=True)


def build_eval_prompt(fixture_dir: Path, ctx: dict, verdicts_path: Path) -> str:
    """Wrap the batch-Observer prompt with a single-fixture ctx and tell
    the model to write its JSONL output to verdicts_path. Same shape as
    production — pulse.py invokes the Observer the same way."""
    base = OBSERVER_PROMPT.read_text()
    ctx_json = json.dumps([ctx], indent=2)
    return (
        base
        + "\n\n---\n\n## EVAL HARNESS\n\n"
        + "You are judging this batch of 1 workspace.\n\n"
        + f"**Write your JSONL output to**: `{verdicts_path}`\n\n"
        + "One JSON object per line, tagged with `ws_ref` and `verdict`. "
          "Anything you print to stdout is treated as work-trail diagnostic "
          "and is never parsed for verdicts.\n\n"
        + "Workspace ctx to judge:\n\n"
        + "```json\n" + ctx_json + "\n```\n"
    )


def extract_verdict(verdicts_path: Path) -> dict | None:
    """Read JSONL written by Observer. Returns first verdict object found,
    or None if file missing/empty/malformed."""
    if not verdicts_path.exists():
        return None
    for raw in verdicts_path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("```"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and "verdict" in obj:
            return obj
    # Fall back: try parsing whole file as a single multi-line JSON object.
    try:
        obj = json.loads(verdicts_path.read_text().strip())
        if isinstance(obj, dict) and "verdict" in obj:
            return obj
    except Exception:
        pass
    return None


def run_one(fixture_dir: Path) -> tuple[bool, str]:
    """Returns (passed, message)."""
    ctx_path = fixture_dir / "ctx.json"
    transcript_path = fixture_dir / "transcript.jsonl"
    expected_path = fixture_dir / "expected.json"

    for p in (ctx_path, transcript_path, expected_path):
        if not p.exists():
            return False, f"missing fixture file: {p.name}"

    try:
        expected = json.loads(expected_path.read_text())
    except Exception as e:
        return False, f"expected.json invalid: {e}"

    # Patch the ctx so transcript_path points at the fixture's local file
    # AND tag with a synthetic ws_ref so the batch prompt can match the
    # output line back. Production ctxs always have a ws_ref; fixtures that
    # don't get a deterministic placeholder.
    ctx = json.loads(ctx_path.read_text())
    ctx["transcript_path"] = str(transcript_path.resolve())
    ctx.setdefault("ws_ref", f"workspace:eval-{fixture_dir.name}")

    verdicts_path = fixture_dir / "verdicts.jsonl"
    if verdicts_path.exists():
        verdicts_path.unlink()  # fresh slate per run

    prompt = build_eval_prompt(fixture_dir, ctx, verdicts_path)

    env = dict(os.environ)
    fake_gh = fixture_dir / "fake-gh-bin"
    if fake_gh.exists():
        env["PATH"] = f"{fake_gh}:{env.get('PATH', '')}"

    cmd = [
        CLAUDE_BIN,
        "--model", MODEL,
        "--dangerously-skip-permissions",
        "--print",
        "--add-dir", str(fixture_dir),
        "--add-dir", str(EVAL_DIR),
        "--add-dir", str(Path.home() / "dev/assistant/prompts"),
    ]
    t0 = time.time()
    try:
        proc = subprocess.run(
            cmd, env=env, input=prompt,
            capture_output=True, text=True, timeout=TIMEOUT_SEC,
            cwd=str(fixture_dir),
        )
    except subprocess.TimeoutExpired as e:
        return False, f"timeout after {TIMEOUT_SEC}s"
    elapsed = time.time() - t0

    if proc.returncode != 0:
        return False, f"claude rc={proc.returncode}\nstderr: {proc.stderr[-400:]}"

    verdict = extract_verdict(verdicts_path)
    if verdict is None:
        return False, (
            f"no verdict in {verdicts_path.name} "
            f"(stdout {len(proc.stdout)} bytes, stderr {len(proc.stderr)} bytes)\n"
            f"  stdout tail: {proc.stdout[-300:]!r}"
        )

    # Compare verdict kind exactly. For verdicts with payload (stranded /
    # needs_user), accept any non-empty value for the keyed field — content
    # is judged loosely (we care that the right verdict shape comes out).
    exp_kind = expected.get("verdict")
    forbidden = set(expected.get("forbidden_verdicts", []))
    got_kind = verdict.get("verdict")

    # Loud-fail on dangerous-direction failures — these are the bugs we
    # built this suite to catch. A verdict in `forbidden_verdicts` means
    # the system did something destructive.
    if got_kind in forbidden:
        return False, f"DANGEROUS: verdict={got_kind!r} is FORBIDDEN for this fixture  (full: {verdict})"

    if got_kind != exp_kind:
        return False, f"verdict={got_kind!r} expected {exp_kind!r}  (full: {verdict})"

    if exp_kind == "stranded":
        if not verdict.get("nudge_text"):
            return False, "stranded verdict missing nudge_text"
    if exp_kind == "needs_user":
        if not verdict.get("title") or not verdict.get("detail"):
            return False, "needs_user verdict missing title or detail"

    # Every verdict must carry summary + next for the dashboard's Workspaces tab.
    summary = verdict.get("summary", "")
    if not isinstance(summary, str) or not summary.strip():
        return False, f"verdict missing required 'summary' field (got {summary!r})"
    if len(summary) > 300:
        return False, f"summary too long ({len(summary)} chars > 300; should be ~30 words)"

    next_step = verdict.get("next", "")
    if not isinstance(next_step, str) or not next_step.strip():
        return False, f"verdict missing required 'next' field (got {next_step!r})"
    if len(next_step) > 300:
        return False, f"next too long ({len(next_step)} chars > 300; should be ~30 words)"

    return True, f"verdict={got_kind!r} summary={summary[:50]!r}... next={next_step[:50]!r}... elapsed={elapsed:.1f}s"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("fixture", nargs="?", default=None, help="Run one fixture by name (default: all)")
    ap.add_argument("--list", action="store_true", help="List fixtures and exit")
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

    passed = 0
    failed = 0
    failures = []
    for f in fixtures:
        log(f"running {f.name} ...")
        ok, msg = run_one(f)
        status = "PASS" if ok else "FAIL"
        log(f"  {status}: {msg}")
        if ok:
            passed += 1
        else:
            failed += 1
            failures.append((f.name, msg))

    log(f"\n=== {passed}/{passed+failed} passed ===")
    if failures:
        for name, msg in failures:
            print(f"  FAIL {name}: {msg}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
