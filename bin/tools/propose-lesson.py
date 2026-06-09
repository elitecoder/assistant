#!/usr/bin/env python3
"""propose-lesson — record (and optionally confirm) a lesson proposal.

A lesson proposal is a pending rule the user must approve before it lands in a
lesson store. Proposals live in ~/.assistant/comms/proposals.jsonl, one JSON
object per line:

  {"ts": "...", "id": "...", "type": "lesson", "target": "assistant",
   "trigger": "...", "rule": "...", "scope": "...",
   "source": "manual|extractor", "status": "pending"}

Two modes:

  RECORD (default) — append a new pending proposal. Requires --trigger and
  --rule. Returns {"status": "recorded", "proposal_id": "<id>"}.

  CONFIRM (--confirm <id>) — apply a pending proposal: run assistant-curator.py
  to write the lesson into its target store, then atomically flip the
  proposal's status to "confirmed". Returns {"status": "confirmed", ...}.
  Idempotent: a slug that already exists is treated as already-applied.

Writes are atomic: RECORD builds the full dict then appends one line; CONFIRM
rewrites the whole file via a tmp + os.replace so a crash never leaves a torn
proposal.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent.parent
HOME = Path.home()
PROPOSALS_PATH = HOME / ".assistant" / "comms" / "proposals.jsonl"
CURATOR = REPO / "bin" / "assistant-curator.py"
OBSIDIAN_WRITE = REPO / "bin" / "tools" / "obsidian-write.py"

VALID_TARGETS = ("assistant", "claude")


def mirror_lesson_to_vault(entry: dict[str, Any]) -> str | None:
    """Best-effort: mirror a just-confirmed lesson into the Obsidian vault as a
    note under Assistant/Lessons. Returns the note path, or None on any failure
    — a vault hiccup must never break a lesson confirm. Idempotency is handled
    upstream (only first-confirm reaches here) and by obsidian-write's
    never-overwrite suffixing."""
    if not OBSIDIAN_WRITE.exists():
        return None
    trigger = entry.get("trigger", "") or "lesson"
    scope = entry.get("scope") or "general"
    target = entry.get("target") or "assistant"
    confirmed = entry.get("confirmed_at") or entry.get("ts") or ""
    body = (
        f"**Rule:** {entry.get('rule', '')}\n\n"
        f"- **Trigger:** {trigger}\n"
        f"- **Target store:** {target}\n"
        f"- **Scope:** {scope}\n"
        f"- **Confirmed:** {confirmed}\n"
        f"- **Source:** {entry.get('source', 'manual')}\n"
    )
    cmd = [sys.executable, str(OBSIDIAN_WRITE),
           "--title", trigger[:120],
           "--category", "lesson",
           "--body", body,
           "--tags", "lesson", scope, target,
           "--frontmatter", json.dumps({"target": target, "scope": scope,
                                        "source": entry.get("source", "manual")})]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        if p.returncode == 0 and p.stdout.strip():
            return json.loads(p.stdout).get("path")
    except Exception:  # noqa: BLE001 — never raise into the confirm path
        return None
    return None


def utc_iso_us() -> str:
    """ISO-8601 UTC with microseconds — high enough resolution that the
    timestamp doubles as a collision-free proposal id."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _read_proposals(path: Path) -> list[dict[str, Any]]:
    try:
        text = path.read_text()
    except FileNotFoundError:
        return []
    out: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def _ping_proposal(entry: dict[str, Any]) -> None:
    """Best-effort ping when a new proposal is recorded. Respects transport config. Never raises."""
    try:
        sys.path.insert(0, str(REPO / "bin"))
        import comms_lib  # noqa: PLC0415
        comms_cfg = HOME / ".assistant" / "comms" / "config.json"
        if not comms_cfg.exists():
            return
        trigger = entry.get("trigger", "")[:80]
        rule = entry.get("rule", "")[:120]
        source = entry.get("source", "manual")
        pid = entry.get("id", "")
        text = (
            f"New lesson proposal ({source}):\n"
            f"Trigger: {trigger}\n"
            f"Rule: {rule}...\n"
            f"Reply 'confirm {pid}' to apply, or ignore to skip."
        )
        comms_lib.send_notification(text, comms_cfg, REPO / "bin", kind="reply")
    except Exception:  # noqa: BLE001
        pass


def record_proposal(trigger: str, rule: str, target: str, scope: str,
                    source: str, path: Path = PROPOSALS_PATH) -> dict[str, Any]:
    ts = utc_iso_us()
    entry = {
        "ts": ts,
        "id": ts,
        "type": "lesson",
        "target": target,
        "trigger": trigger,
        "rule": rule,
        "scope": scope,
        "source": source,
        "status": "pending",
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    _ping_proposal(entry)
    return {"status": "recorded", "proposal_id": ts}


def _write_all(path: Path, entries: list[dict[str, Any]]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in entries))
    os.replace(tmp, path)


def confirm_proposal(proposal_id: str,
                     path: Path = PROPOSALS_PATH,
                     curator: Path = CURATOR) -> dict[str, Any]:
    """Apply a pending lesson proposal: curator write + status flip."""
    entries = _read_proposals(path)
    target_entry = None
    for e in entries:
        if e.get("id") == proposal_id or e.get("ts") == proposal_id:
            target_entry = e
            break
    if target_entry is None:
        return {"status": "error", "error": f"no proposal with id {proposal_id!r}",
                "proposal_id": proposal_id}
    if target_entry.get("type") != "lesson":
        return {"status": "error", "error": "proposal is not a lesson",
                "proposal_id": proposal_id}
    if target_entry.get("status") == "confirmed":
        return {"status": "confirmed", "proposal_id": proposal_id,
                "note": "already confirmed"}

    cmd = [sys.executable, str(curator), "write",
           "--trigger", target_entry.get("trigger", ""),
           "--rule", target_entry.get("rule", ""),
           "--target", target_entry.get("target", "assistant")]
    scope = target_entry.get("scope")
    if scope:
        cmd += ["--scope", scope]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "error": f"curator launch failed: {e}",
                "proposal_id": proposal_id}

    already = "already exists" in (p.stderr or "")
    if p.returncode != 0 and not already:
        return {"status": "error", "proposal_id": proposal_id,
                "error": (p.stderr or "").strip()[-300:]}

    # Flip status atomically.
    target_entry["status"] = "confirmed"
    target_entry["confirmed_at"] = utc_iso_us()
    _write_all(path, entries)
    result = {"status": "confirmed", "proposal_id": proposal_id,
              "curator_stdout": (p.stdout or "").strip()}
    if already:
        result["note"] = "lesson slug already existed in store; marked confirmed"
    # Mirror into the Obsidian vault (best-effort, never fatal).
    vault_path = mirror_lesson_to_vault(target_entry)
    if vault_path:
        result["obsidian_note"] = vault_path
    return result


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Record a lesson proposal (default) or confirm a pending "
                    "one with --confirm <id>. Returns JSON.")
    ap.add_argument("--trigger", help="one-line trigger (required to record)")
    ap.add_argument("--rule", help="one-paragraph rule (required to record)")
    ap.add_argument("--target", default="assistant", choices=VALID_TARGETS,
                    help="lesson store (default: assistant)")
    ap.add_argument("--scope", default="general",
                    help="sub-domain within the target (default: general)")
    ap.add_argument("--source", default="manual",
                    help="who produced this proposal (manual|extractor)")
    ap.add_argument("--confirm", default=None,
                    help="confirm the pending proposal with this id")
    args = ap.parse_args()

    if args.confirm:
        result = confirm_proposal(args.confirm)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result.get("status") != "error" else 1

    if not args.trigger or not args.rule:
        print(json.dumps({"status": "error",
                          "error": "--trigger and --rule are required to record a proposal"}))
        return 1
    result = record_proposal(args.trigger, args.rule, args.target,
                             args.scope, args.source)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
