#!/usr/bin/env python3
"""narrate-brief.py — the brief narrator's LLM subprocess caller + CLI (Keel M7).

The pure logic lives in src/assistant/narrator.py (grounding, strict validation,
deterministic template floor, epoch-tied sidecar). This file is the ONE place
that actually spawns an LLM — the Strategist/Observer/triage subprocess pattern
verbatim (`claude --print --output-format json`, the model writes its ONE JSON
object to a file, stdout is the CLI result envelope kept for metering, the whole
run is archived under ~/.assistant/narrator-runs/).

It exposes the injected callable the pure module needs:

  • generate(now, pulse_idx) — reads the latest (or as-of `now`) brief file,
    wraps narrator.build_narrative with llm_narrate=call_narrate (so ALL the
    gating + validation + template fallback happen in the pure module), and
    writes the narrative sidecar beside the brief. The brief file itself is
    NEVER touched — it stays a pure derivation.

Every LLM call is metered into ~/.assistant/cost-ledger.jsonl as
caller="narrator" (match bin/metering.py); a failed subprocess books an
ESTIMATED status="failed" row, never a phantom $0 (same as the triage/strategist
callers, so repeated failures still ratchet the daily ceiling).

TESTABILITY: `run` is the ONLY subprocess touchpoint; every test mocks it — NO
live LLM, NO network. Pure stdlib. NEVER closes a workspace; no launchctl.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BIN = REPO / "bin"
SRC = REPO / "src"

NARRATOR_PROMPT = REPO / "prompts/brief-narrator-prompt.md"


def _home() -> Path:
    return Path(os.environ.get("HOME", str(Path.home())))


def narrator_runs_dir() -> Path:
    return _home() / ".assistant" / "narrator-runs"


DEFAULT_MODEL = os.environ.get(
    "NARRATOR_MODEL", os.environ.get(
        "OBSERVER_MODEL", "us.anthropic.claude-sonnet-4-6[1m]"))
DEFAULT_CLAUDE_BIN = os.environ.get(
    "CLAUDE_BIN", str(Path.home() / ".local/bin/claude"))
# The narrator reads inline brief facts only (no transcripts, no repo) — the
# short triage/strategist leash, never the Observer's.
NARRATOR_TIMEOUT_SEC = int(os.environ.get("NARRATOR_TIMEOUT_SEC", "180"))

log = logging.getLogger("narrator-cli")


def utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ─── bedrock env + subprocess (the ONLY LLM touchpoint; tests mock run) ──────

def _bedrock_env() -> dict:
    """The handful of Bedrock/AWS vars from ~/.zprofile launchd doesn't source
    (verbatim from bin/strategist.py / pulse.load_bedrock_env)."""
    extracted: dict[str, str] = {}
    zprofile = _home() / ".zprofile"
    if not zprofile.exists():
        return extracted
    keys = ("CLAUDE_CODE_USE_BEDROCK", "AWS_REGION", "AWS_BEARER_TOKEN_BEDROCK",
            "AWS_PROFILE", "ANTHROPIC_API_KEY")
    pat = re.compile(r'^\s*export\s+([A-Z_][A-Z0-9_]*)\s*=\s*(.+?)\s*$')
    try:
        for line in zprofile.read_text().splitlines():
            m = pat.match(line)
            if not m or m.group(1) not in keys:
                continue
            v = m.group(2).strip()
            if (v.startswith('"') and v.endswith('"')) or \
               (v.startswith("'") and v.endswith("'")):
                v = v[1:-1]
            extracted[m.group(1)] = v
    except OSError:
        pass
    return extracted


def run(cmd: list[str], *, input_text: str | None = None,
        timeout: int = 30, merge_bedrock: bool = False) -> tuple[int, str, str]:
    """Run a subprocess; return (rc, stdout, stderr). Never raises. The child
    runs in its own session and a timeout kills the whole process GROUP before
    the reap (verbatim from bin/strategist.run), so a hung `claude` can never
    stall the pulse."""
    env = dict(os.environ)
    if merge_bedrock:
        env.update(_bedrock_env())
    try:
        proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, env=env,
            start_new_session=True)
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
        except Exception:  # noqa: BLE001
            out, err = "", ""
        return 124, out or "", (err or "") + f"\ntimeout after {timeout}s"


# ─── output parser (tolerant, like read_draft) ───────────────────────────────

def _strip_fences(text: str) -> str:
    lines = [ln for ln in (text or "").splitlines()
             if not ln.strip().startswith("```")]
    return "\n".join(lines).strip()


def read_json_obj(path: Path) -> dict | None:
    """Parse the ONE JSON object the model wrote. Missing / empty / unparseable
    / not-an-object → None (→ the pure module uses the template floor)."""
    try:
        text = path.read_text()
    except (OSError, FileNotFoundError):
        return None
    text = _strip_fences(text)
    if not text:
        return None
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


# ─── metering (never load-bearing; mirrors bin/strategist._meter) ────────────

def _meter(out: str, rc: int, prompt_len: int, wall_ms: int) -> dict:
    """Append one cost-ledger row (caller='narrator'). On failure still books an
    ESTIMATED, non-zero cost so the daily ceiling can't be evaded by failing."""
    usage: dict = {}
    try:
        if str(BIN) not in sys.path:
            sys.path.insert(0, str(BIN))
        import metering  # noqa: PLC0415
        failed = rc != 0 or metering.parse_cli_result(out) is None
        usage = metering.observer_usage(out, prompt_len, DEFAULT_MODEL)
        metering.append_cost_row(
            caller="narrator", model=DEFAULT_MODEL, usage=usage,
            wall_ms=wall_ms, status="failed" if failed else "ok")
    except Exception as e:  # noqa: BLE001 — metering must never break the pulse
        log.warning("narrator metering capture failed (ignored): %s", e)
    return usage


def _spawn(prompt: str, run_dir: Path, out_name: str) -> tuple[int, str, str]:
    """Spawn ONE narrator subprocess. The model writes `out_name` into run_dir;
    stdout is the CLI JSON envelope (metering only). Archives
    prompt/stdout/stderr/meta like the Strategist/Observer runs."""
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "prompt.md").write_text(prompt)
    cmd = [
        DEFAULT_CLAUDE_BIN,
        "--model", DEFAULT_MODEL,
        "--dangerously-skip-permissions",
        "--print",
        "--output-format", "json",
        "--add-dir", str(run_dir),
    ]
    t0 = time.time()
    rc, out, err = run(cmd, input_text=prompt, timeout=NARRATOR_TIMEOUT_SEC,
                       merge_bedrock=True)
    wall_ms = int((time.time() - t0) * 1000)
    usage = _meter(out, rc, len(prompt), wall_ms)
    (run_dir / "stdout.txt").write_text(out or "")
    (run_dir / "stderr.txt").write_text(err or "")
    (run_dir / "meta.json").write_text(json.dumps({
        "rc": rc, "wall_ms": wall_ms, "model": DEFAULT_MODEL,
        "out_name": out_name, "usage": usage, "ts": utc_iso(),
    }, indent=2))
    if rc != 0:
        log.warning("narrator subprocess rc=%d: %s", rc,
                    (err or "").strip()[-300:])
    return rc, out, err


# ─── the injected LLM callable ───────────────────────────────────────────────

def call_narrate(facts: dict, pulse_idx: int = 0) -> dict | None:
    """Spawn the narrator to phrase one brief's facts. Returns the parsed JSON
    dict (validated downstream by narrator.validate_narrative) or None (missing
    prompt / failed subprocess / unparseable → the pure module uses the template
    floor). Suggestion-only by construction: the return is fed to
    validate_narrative, which drops any decision id the brief never surfaced and
    keeps TEXT ONLY."""
    if not NARRATOR_PROMPT.exists():
        log.error("narrator prompt missing: %s", NARRATOR_PROMPT)
        return None
    date = facts.get("date") or "today"
    run_dir = narrator_runs_dir() / f"{pulse_idx:04d}" / f"narrate-{date}"
    out_path = run_dir / "narrative.json"
    prompt = (
        NARRATOR_PROMPT.read_text()
        + "\n\n---\n\n## RUNTIME CONTEXT\n\n"
        + "**Output destination — write your ONE JSON object to this file, "
          "not stdout.** Use the Write tool to write to:\n\n"
        + f"    {out_path}\n\n"
        + "These are the ONLY facts you may phrase. Invent nothing; write a "
          "recommendation ONLY for a decision id listed under `decisions`.\n\n"
        + "```json\n" + json.dumps(facts, indent=2, ensure_ascii=False)
        + "\n```\n"
    )
    _spawn(prompt, run_dir, "narrative.json")
    return read_json_obj(out_path)


# ─── the entrypoint the pulse / CLI drives ───────────────────────────────────

def _load() -> tuple[object, object]:
    if str(SRC) not in sys.path:
        sys.path.insert(0, str(SRC))
    from assistant import brief, narrator  # noqa: PLC0415
    return brief, narrator


def _brief_for(now: float | None):
    """The brief doc to narrate: the on-disk brief for `now`'s local date if it
    exists (what the dashboard actually renders), else the latest written brief.
    Returns (brief_doc, date) or (None, None) when no brief exists yet."""
    brief, _narr = _load()
    now = now if now is not None else time.time()
    date = brief.local_date(now)
    doc = brief._read_json(brief.brief_path(date))
    if not isinstance(doc, dict):
        latest = brief.latest_brief_date()
        doc = brief._read_json(brief.brief_path(latest)) if latest else None
        date = latest
    return (doc if isinstance(doc, dict) else None), date


def generate(now: float | None = None, pulse_idx: int = 0,
             force: bool = False) -> dict:
    """Read the brief, build its narrative (gated + validated + template floor in
    the pure module, spawning the LLM via call_narrate), and write the sidecar.
    Returns a small summary dict. Never raises past its own fence — a broken
    narrator costs a templated voice, never a pulse."""
    brief, narrator = _load()
    now = now if now is not None else time.time()
    doc, date = _brief_for(now)
    if doc is None:
        return {"written": False, "reason": "no-brief"}
    # Once-per-date spend stamp (unless forced by an on-demand CLI rebuild): the
    # stamp is written reserve-style BEFORE the spend so a crash mid-call can't
    # let the narrator re-fire every pulse (fail-closed, like the strategist
    # reservation). The floor already renders, so skipping a day costs only the
    # voice.
    stamp = narrator.narrate_stamp_path(date)
    if not force and stamp.exists():
        return {"written": False, "reason": "already-narrated", "date": date}
    try:
        stamp.parent.mkdir(parents=True, exist_ok=True)
        tmp = stamp.with_name(f"{stamp.name}.{os.getpid()}.tmp")
        tmp.write_text(utc_iso() + "\n")
        os.replace(tmp, stamp)
    except OSError:
        pass
    narr = narrator.build_narrative(
        doc, llm_narrate=lambda facts: call_narrate(facts, pulse_idx),
        now=now, log=log)
    path = narrator.write_narrative(narr)
    return {"written": True, "date": date, "source": narr.get("source"),
            "reason": narr.get("reason"), "path": str(path),
            "n_recs": len(narr.get("recommendations") or {})}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--now", type=float, default=None,
                    help="narrate the brief as-of this epoch (default: now)")
    ap.add_argument("--force", action="store_true",
                    help="re-narrate even if today's stamp exists (on-demand)")
    ap.add_argument("--print", dest="print_json", action="store_true",
                    help="dump the narrative sidecar JSON to stdout")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    summary = generate(now=args.now, force=args.force)
    print(json.dumps(summary, indent=2))
    if args.print_json and summary.get("written"):
        brief, narrator = _load()
        side = narrator.read_narrative(summary["date"])
        print(json.dumps(side, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
