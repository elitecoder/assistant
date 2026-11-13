#!/usr/bin/env python3
"""Replay past Sonnet Observer checks with the plain-text conversation slice.

For each saved Sonnet run under ~/.assistant/observer-runs, rebuilds the
transcript as it was (cut at the last line Sonnet saw), renders the new slice,
and asks each model under test. Scores agreement with Sonnet's verdict and
counts acting verdicts Sonnet didn't give.

  ./replay.py -n 40 --models claude-haiku-4-5:raw claude-haiku-4-5:slice
"""
from __future__ import annotations

import argparse
import collections
import json
import random
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parent
REPO = EVAL_DIR.parents[1]
sys.path[:0] = [str(REPO / "bin"), str(REPO / "src"), str(EVAL_DIR)]
import llm_runner  # noqa: E402
import run as fixture_eval  # noqa: E402
from assistant import transcript_slice  # noqa: E402

CTX_START = "Workspace ctxs to judge:\n\n```json\n"
RUNTIME = "\n\n---\n\n## RUNTIME CONTEXT\n\n"
ACTING = {"ready_for_merge", "ready_for_cleanup", "stranded"}


def sonnet_verdict(run_dir: Path) -> dict | None:
    try:
        envelope = json.loads((run_dir / "stdout.txt").read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(envelope, dict):
        return None
    return fixture_eval.extract_verdict(envelope.get("result") or "")


def saved_case(run_dir: Path) -> dict | None:
    """The saved prompt, its single ctx, and Sonnet's verdict, or None."""
    try:
        meta = json.loads((run_dir / "meta.json").read_text())
        prompt = (run_dir / "prompt.md").read_text()
    except (OSError, ValueError):
        return None
    if meta.get("provider") != "claude" or "sonnet" not in str(meta.get("model")):
        return None
    verdict = sonnet_verdict(run_dir)
    start = prompt.find(CTX_START)
    if verdict is None or start < 0:
        return None
    start += len(CTX_START)
    end = prompt.index("\n```\n", start)
    ctxs = json.loads(prompt[start:end])
    if len(ctxs) != 1 or not ctxs[0].get("transcript_path"):
        return None
    return {"run": str(run_dir), "prompt": prompt, "ctx": ctxs[0],
            "ctx_span": (start, end), "sonnet": verdict["verdict"]}


def raw_excerpt(data: bytes, max_bytes: int = 131072) -> str:
    """The first-and-last JSONL window the Observer read before the plain-text slice."""
    if len(data) > max_bytes:
        head = data[:min(24576, max_bytes // 4)]
        head = head[:head.rfind(b"\n") + 1]
        tail = data[-(max_bytes - len(head)):]
        data = head + tail[tail.find(b"\n") + 1:]
    return data.decode("utf-8", errors="replace")


def transcript_end(ctx: dict) -> int | None:
    """Byte offset where the transcript ended when Sonnet read it: the cut
    whose old-style excerpt equals the saved one, or None when none does."""
    excerpt = ctx.get("transcript_excerpt", "")
    lines = [line for line in excerpt.splitlines() if line.strip()]
    path = Path(ctx["transcript_path"])
    if not lines or not path.exists():
        return None
    needle = lines[-1].encode()
    data = path.read_bytes()
    at = data.find(needle)
    while at >= 0:
        end = at + len(needle) + 1
        if raw_excerpt(data[:end]) == excerpt:
            return end
        at = data.find(needle, at + 1)
    return None


def slice_prompt(case: dict, end: int) -> str:
    ctx = dict(case["ctx"])
    ctx["transcript_excerpt"] = transcript_slice.read_conversation(
        Path(ctx["transcript_path"]), end=end)
    tail = case["prompt"][case["prompt"].index(RUNTIME):]
    start, stop = case["ctx_span"]
    offset = len(case["prompt"]) - len(tail)
    tail = (tail[:start - offset] + json.dumps([ctx], indent=2)
            + tail[stop - offset:])
    return fixture_eval.OBSERVER_PROMPT.read_text() + tail


def ask(model: str, prompt: str, run_dir: Path) -> tuple[str | None, dict]:
    run_dir.mkdir(parents=True, exist_ok=True)

    def run_cmd(cmd, *, input_text=None, timeout=None, env=None, merge_bedrock=False):
        proc = subprocess.run(cmd, input=input_text, capture_output=True, text=True,
                              timeout=timeout, cwd=str(run_dir))
        return proc.returncode, proc.stdout, proc.stderr

    result = llm_runner.invoke(
        provider="claude", prompt=prompt, model=model, run_dir=run_dir,
        timeout=300, run=run_cmd,
        disable_tools=True, json_schema=fixture_eval.OBSERVER_RESPONSE_SCHEMA,
        extra_dirs=[run_dir], tag="assistant-observer-replay")
    verdict = fixture_eval.extract_verdict(result.result_text) if result.usable else None
    return (verdict or {}).get("verdict"), {
        "tokens_in": result.tokens_in, "tokens_out": result.tokens_out,
        "usd": result.cost_usd, "sec": result.wall_ms / 1000}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("-n", type=int, default=40)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--runs", type=Path, default=Path.home() / ".assistant/observer-runs")
    parser.add_argument("--out", type=Path, default=EVAL_DIR / "reports")
    parser.add_argument("--models", nargs="+", default=[
        "claude-haiku-4-5:raw", "claude-haiku-4-5:slice", "claude-sonnet-5-5:slice"])
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()

    dirs = sorted(args.runs.glob("*/batch-*"))
    random.Random(args.seed).shuffle(dirs)
    cases, seen = [], set()
    for run_dir in dirs:
        if len(cases) >= args.n:
            break
        case = saved_case(run_dir)
        if case is None:
            continue
        key = (case["ctx"]["ws_ref"], case["sonnet"])
        if key in seen:
            continue
        end = transcript_end(case["ctx"])
        if end is None:
            continue
        seen.add(key)
        case["slice_prompt"] = slice_prompt(case, end)
        cases.append(case)
    print(f"{len(cases)} cases; Sonnet verdicts {dict(collections.Counter(c['sonnet'] for c in cases))}", flush=True)

    work_dir = args.out / "replay-runs"
    jobs = [(i, spec) for i in range(len(cases)) for spec in args.models]

    def one(job):
        i, spec = job
        model, form = spec.rsplit(":", 1)
        prompt = cases[i]["prompt"] if form == "raw" else cases[i]["slice_prompt"]
        verdict, usage = ask(model, prompt, work_dir / spec.replace(":", "-") / str(i))
        return i, spec, verdict, usage

    with ThreadPoolExecutor(args.workers) as pool:
        results = list(pool.map(one, jobs))

    summary = {}
    rows = []
    for spec in args.models:
        mine = [(i, v, u) for i, s, v, u in results if s == spec]
        agree = sum(v == cases[i]["sonnet"] for i, v, _ in mine)
        extra_acting = sum(v in ACTING and v != cases[i]["sonnet"] for i, v, _ in mine)
        summary[spec] = {
            "agree": agree, "cases": len(mine),
            "no_answer": sum(v is None for _, v, _ in mine),
            "acting_sonnet_did_not_give": extra_acting,
            "usd": round(sum(u["usd"] or 0 for _, _, u in mine), 2),
            "median_tokens_in": sorted(u["tokens_in"] for _, _, u in mine)[len(mine) // 2],
            "sonnet_to_model": {f"{a}->{b}": n for (a, b), n in collections.Counter(
                (cases[i]["sonnet"], v) for i, v, _ in mine).items()},
        }
        print(spec, json.dumps(summary[spec]), flush=True)
    for i, spec, verdict, usage in results:
        rows.append({"run": cases[i]["run"], "sonnet": cases[i]["sonnet"],
                     "spec": spec, "verdict": verdict, **usage})
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "replay-last.json").write_text(json.dumps(
        {"summary": summary, "rows": rows}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
