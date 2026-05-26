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
OBSERVER_PROMPT = Path.home() / "dev/assistant/prompts/observer-prompt.md"

CLAUDE_BIN = os.environ.get("CLAUDE_BIN", str(Path.home() / ".local/bin/claude"))
MODEL = os.environ.get("EVAL_MODEL", "us.anthropic.claude-sonnet-4-6[1m]")
TIMEOUT_SEC = int(os.environ.get("EVAL_TIMEOUT_SEC", "180"))


def log(msg: str) -> None:
    print(f"[observer-eval] {msg}", flush=True)


def build_eval_prompt(ctx_path: Path, transcript_path: Path) -> str:
    """Wrap the Observer prompt with eval-specific instructions.

    Real production Observer is invoked via the Agent tool with the workspace
    ctx as input. Here, we tell the LLM to read ctx + emit one JSON line.
    """
    base = OBSERVER_PROMPT.read_text()
    return (
        base
        + "\n\n---\n\n## EVAL HARNESS\n\n"
        f"Your input ctx is at: `{ctx_path}`. Read it.\n\n"
        f"The workspace's transcript JSONL is at: `{transcript_path}`. "
        "The path in ctx.json points there too.\n\n"
        "Emit exactly ONE JSON object on stdout. No commentary, no markdown "
        "fence — just a single line of JSON matching the verdict vocabulary.\n"
    )


def extract_verdict(stdout: str) -> dict | None:
    """Pull the last well-formed JSON object out of stdout."""
    # The model may emit some prose before the final JSON. Walk lines from
    # the bottom and try parsing each. Take first that parses to a dict
    # with a 'verdict' key.
    lines = [l.strip() for l in stdout.splitlines() if l.strip()]
    for line in reversed(lines):
        # Strip code fences if present.
        if line.startswith("```"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and "verdict" in obj:
            return obj
    # Fall back: try parsing the whole stdout (sometimes a multi-line JSON).
    try:
        obj = json.loads(stdout.strip())
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

    # Patch the ctx so transcript_path points at the fixture's local file.
    ctx = json.loads(ctx_path.read_text())
    ctx["transcript_path"] = str(transcript_path.resolve())
    runtime_ctx = fixture_dir / "ctx.runtime.json"
    runtime_ctx.write_text(json.dumps(ctx, indent=2))

    prompt = build_eval_prompt(runtime_ctx, transcript_path)

    env = dict(os.environ)
    fake_gh = fixture_dir / "fake-gh-bin"
    if fake_gh.exists():
        env["PATH"] = f"{fake_gh}:{env.get('PATH', '')}"

    cmd = [
        CLAUDE_BIN,
        "--model", MODEL,
        "--permission-mode", "bypassPermissions",
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

    verdict = extract_verdict(proc.stdout)
    if verdict is None:
        return False, f"no JSON verdict found in stdout (got {len(proc.stdout)} bytes)\n  tail: {proc.stdout[-300:]!r}"

    # Compare verdict kind exactly. For verdicts with payload (stranded /
    # needs_user), accept any non-empty value for the keyed field — content
    # is judged loosely (we care that the right verdict shape comes out).
    exp_kind = expected.get("verdict")
    got_kind = verdict.get("verdict")
    if got_kind != exp_kind:
        return False, f"verdict={got_kind!r} expected {exp_kind!r}  (full: {verdict})"

    if exp_kind == "stranded":
        if not verdict.get("nudge_text"):
            return False, "stranded verdict missing nudge_text"
    if exp_kind == "needs_user":
        if not verdict.get("title") or not verdict.get("detail"):
            return False, "needs_user verdict missing title or detail"

    return True, f"verdict={got_kind!r} elapsed={elapsed:.1f}s"


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
