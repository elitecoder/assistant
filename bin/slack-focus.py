#!/usr/bin/env python3
"""slack-focus.py — refresh the Slack focus board behind the dashboard's Slack tab.

  python3 bin/slack-focus.py              pull Slack, triage, judge, re-render
  python3 bin/slack-focus.py --triage     re-triage and judge the last pull (no Slack calls)
  python3 bin/slack-focus.py --no-judge   skip the model; show the raw mechanical rows

Read-only toward Slack, GitHub, and Jira. The pure logic lives in
src/assistant/slack_focus/; this file is the one place that spawns the model
(the narrator's subprocess pattern: tools off, metered as caller="slack_focus").
The dashboard's Refresh button starts this through bin/todo-server.py, which
sources ~/.zprofile first so $SLACK_USER_TOKEN and PATH resolve under launchd.
Each step is recorded in ~/.assistant/slack-focus/state.json for the button.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BIN = REPO / "bin"
SRC = REPO / "src"
sys.path[:0] = [str(SRC), str(BIN)]

import comms_lib  # noqa: E402
import llm_runner  # noqa: E402
import metering  # noqa: E402
from assistant import model_tiers  # noqa: E402
from assistant.slack_focus import client as slack_client  # noqa: E402
from assistant.slack_focus import collect, judge, settings, store, triage  # noqa: E402

RENDER_SCRIPT = BIN / "render-assistant-page.py"
JUDGE_TIMEOUT_SEC = int(os.environ.get("SLACK_FOCUS_JUDGE_TIMEOUT_SEC", "420"))
REFRESH_TIMEOUT_SEC = 1200
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", str(Path.home() / ".local/bin/claude"))


def _home() -> Path:
    return Path(os.environ.get("HOME", str(Path.home())))


def run(cmd: list[str], *, input_text: str | None = None, timeout: int = 30,
        merge_bedrock: bool = False) -> tuple[int, str, str]:
    """Run a subprocess in its own session; a timeout kills the whole group."""
    env = dict(os.environ)
    if merge_bedrock:
        env.update(comms_lib.load_bedrock_env(_home()))
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, env=env, start_new_session=True)
    except OSError as e:
        return 127, "", f"spawn failed: {e}"
    try:
        out, err = proc.communicate(input=input_text, timeout=timeout)
        return proc.returncode, out or "", err or ""
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (OSError, ProcessLookupError):
            proc.kill()
        try:
            out, err = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            out, err = "", ""
        return 124, out or "", (err or "") + f"\ntimeout after {timeout}s"


def call_model(prompt: str) -> str:
    """The judge's one model call. Tools are off, so Slack text can't steer it
    into acting. Returns "" on any failure, which keeps the raw rows."""
    route = llm_runner.load_route_config(llm_runner.config_path(home=_home()), "slack_focus")
    provider = llm_runner.select_provider(route, [])
    model = route.droid_model if provider == "droid" else model_tiers.model_for("balanced")
    run_dir = store.base_dir(_home()) / "judge-run"
    run_dir.mkdir(parents=True, exist_ok=True)
    result = llm_runner.invoke(
        provider=provider, prompt=prompt, model=model, run_dir=run_dir, timeout=JUDGE_TIMEOUT_SEC, run=run,
        claude_bin=CLAUDE_BIN, droid_bin=route.droid_bin, reasoning_effort=route.droid_reasoning_effort,
        disable_tools=True, tag="assistant-slack-focus")
    try:
        usage = metering.observer_usage(result.stdout, len(prompt), model)
        metering.append_cost_row(caller="slack_focus", model=model, usage=usage, wall_ms=result.wall_ms,
                                 status="ok" if result.usable else "failed", provider=provider)
    except Exception as e:  # noqa: BLE001 — metering never blocks the board
        print(f"metering failed (ignored): {e}", file=sys.stderr)
    if not result.usable:
        print(f"judge model rc={result.rc}: {result.stderr.strip()[-300:]}", file=sys.stderr)
    return result.result_text if result.usable else ""


def rerender() -> None:
    subprocess.run([sys.executable, str(RENDER_SCRIPT)], timeout=60, check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def phase(home: Path, text: str) -> None:
    print(f"PHASE: {text}", flush=True)
    store.write_state(home, phase=text)


def refresh(home: Path, *, pull: bool, use_judge: bool, token: str,
            transport=slack_client.http_transport, model=call_model,
            resolver: triage.Resolver | None = None) -> dict:
    """Run the pipeline and return the board it wrote."""
    root = store.base_dir(home)
    cfg = settings.load(home)
    if not cfg.configured:
        raise RuntimeError("Add a slack_focus block to ~/.assistant/comms/config.json first.")
    if pull:
        phase(home, "Pulling Slack")
        client = slack_client.Client(token, transport)
        threads_doc, mine = collect.collect(
            client, cfg, progress=lambda i, n: store.write_state(home, phase=f"Pulling Slack ({i} of {n} threads)"))
        store.write_json(root / "threads.json", threads_doc)
        store.write_json(root / "mine.json", mine)
    threads_doc = store.read_json(root / "threads.json")
    mine = store.read_json(root / "mine.json", [])
    if not isinstance(threads_doc, dict):
        raise RuntimeError("There's no Slack pull to triage yet. Run a full refresh first.")
    resolver = resolver or triage.Resolver(cfg)
    phase(home, "Sorting threads")
    focus = triage.triage(threads_doc, mine, cfg, resolver)
    if use_judge:
        phase(home, "Asking the model which threads need you")
        threads = {t["id"]: t for t in threads_doc["threads"]}
        evidence = judge.done_evidence(cfg, resolver, focus["window_days"], datetime.now())
        focus, cache = judge.judge(focus, threads, evidence, store.read_json(root / "judge-cache.json"), model)
        store.write_json(root / "judge-cache.json", cache)
    store.write_json(root / "focus.json", focus)
    return focus


def _too_slow(signum, frame):
    raise TimeoutError(f"The refresh took longer than {REFRESH_TIMEOUT_SEC // 60} minutes.")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Refresh the Slack focus board.")
    ap.add_argument("--triage", action="store_true", help="re-triage the last pull without calling Slack")
    ap.add_argument("--no-judge", action="store_true", help="skip the model step")
    args = ap.parse_args(argv)
    home = _home()
    root = store.base_dir(home)
    with store.locked(root / "refresh.lock", blocking=False) as got:
        if not got:
            print("A refresh is already running.", file=sys.stderr)
            return 0
        store.write_state(home, running=True, pid=os.getpid(), started=store.now_iso(), finished=None,
                          ok=None, error=None, phase="Starting")
        started = time.time()
        error = None
        signal.signal(signal.SIGALRM, _too_slow)
        signal.alarm(REFRESH_TIMEOUT_SEC)
        try:
            focus = refresh(home, pull=not args.triage, use_judge=not args.no_judge,
                            token=os.environ.get("SLACK_USER_TOKEN", ""))
            print(json.dumps(focus["counts"]))
        except Exception as e:  # noqa: BLE001 — every run must record how it ended
            error = str(e)[:300] or type(e).__name__
            print(f"refresh failed: {error}", file=sys.stderr)
        finally:
            signal.alarm(0)
        store.write_state(home, running=False, finished=store.now_iso(), ok=error is None, error=error,
                          phase=None, seconds=round(time.time() - started))
        rerender()
    return 0 if error is None else 1


if __name__ == "__main__":
    sys.exit(main())
