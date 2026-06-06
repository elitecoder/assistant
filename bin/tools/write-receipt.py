#!/usr/bin/env python3
"""write-receipt — record a work receipt before a workspace closes.

A receipt is the audit trail Mukul has no other way to reconstruct once a
cmux workspace is torn down: what the work was, whether a PR shipped, whether
CI was green, whether a reviewer signed off, and a one-line quality verdict.

Two sinks:
  1. ~/.assistant/receipts/<ws_ref_slug>-<utc_ts>.json   — the canonical
     per-workspace receipt the pre-cleanup gate and the Fleet DONE column read.
  2. ~/dev/generated-docs/work-receipts.jsonl            — an append-only
     searchable log (one line per receipt, all workspaces, forever).

quality_score is computed from the CI + reviewer signals, mirroring the Fleet
DONE-column badge rules:
    high   = CI green AND reviewer approved        (green dot)
    medium = CI green OR  reviewer approved         (yellow dot — partial)
    low    = CI red OR outcome abandoned, else none (red dot)

The receipt file write is atomic (tmp + rename). The log append is a single
append-mode write, matching pulse.py's append_ledger convention.

Stdout on success: the receipt dict as JSON. Exit 0.
Stdout on error:   {"error": "..."}. Exit 1.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HOME = Path.home()
RECEIPTS_DIR = HOME / ".assistant" / "receipts"
LOG_PATH = HOME / "dev" / "generated-docs" / "work-receipts.jsonl"
OBSIDIAN_WRITE = Path(__file__).resolve().parent / "obsidian-write.py"

# FFP is the only repo whose PRs we link; the receipt's pr_number is an FFP PR.
PR_URL_TEMPLATE = "https://git.corp.adobe.com/Adobe-Firefly/firefly-platform/pull/{pr}"


def utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def slug(ws_ref: str) -> str:
    """Filesystem-safe ws_ref: workspace:43 -> workspace-43."""
    return ws_ref.replace(":", "-")


def parse_tristate(value: str | None) -> bool | None:
    """true/false/unknown (case-insensitive) -> True/False/None."""
    if value is None:
        return None
    v = value.strip().lower()
    if v == "true":
        return True
    if v == "false":
        return False
    return None


def compute_quality_score(ci_status: str, reviewer_approved: bool | None,
                          outcome: str) -> str:
    """high / medium / low — see module docstring for the badge mapping."""
    if ci_status == "red" or outcome == "abandoned":
        return "low"
    if ci_status == "green" and reviewer_approved is True:
        return "high"
    if ci_status == "green" or reviewer_approved is True:
        return "medium"
    return "low"


def cmux_title(ws_ref: str) -> str | None:
    """Best-effort workspace title via `cmux list-workspaces --json`. Returns
    None on any failure (cmux down, bad JSON, no match) so the caller can fall
    back to the ws_ref as the project name."""
    try:
        out = subprocess.run(
            ["cmux", "list-workspaces", "--json"],
            capture_output=True, text=True, timeout=8,
        )
        if out.returncode != 0 or not out.stdout.strip():
            return None
        data = json.loads(out.stdout)
    except Exception:
        return None
    for w in data.get("workspaces", []) or []:
        if w.get("ref") == ws_ref:
            return (w.get("title") or "").strip() or None
    return None


def mirror_receipt_to_vault(receipt: dict[str, Any]) -> str | None:
    """Best-effort: mirror a work receipt into the Obsidian vault as a
    work_history note under Work Log/<YYYY-MM>. Returns the note path, or None
    on any failure — a vault hiccup must never break a receipt write."""
    if not OBSIDIAN_WRITE.exists():
        return None
    project = receipt.get("project") or receipt.get("ws_ref") or "work"
    outcome = receipt.get("outcome") or "shipped"
    date = (receipt.get("ts") or utc_iso())[:10]
    month = date[:7]  # YYYY-MM
    title = f"{project} — {outcome} {date}"
    tag = "shipped" if outcome == "shipped" else "abandoned"

    lines = [f"- **Outcome:** {outcome}",
             f"- **CI:** {receipt.get('ci_status', 'unknown')}",
             f"- **Reviewer approved:** {receipt.get('reviewer_approved')}",
             f"- **Quality:** {receipt.get('quality_score', 'unknown')}"]
    if receipt.get("test_count") is not None:
        lines.append(f"- **Tests:** {receipt['test_count']}")
    if receipt.get("pr_url"):
        lines.append(f"- **PR:** [{receipt.get('pr_number')}]({receipt['pr_url']})")
    if receipt.get("summary"):
        lines.append(f"\n{receipt['summary']}")
    body = "\n".join(lines)

    fm = {"project": project, "outcome": outcome,
          "quality": receipt.get("quality_score", "unknown")}
    if receipt.get("pr_number") is not None:
        fm["pr"] = receipt["pr_number"]
    cmd = [sys.executable, str(OBSIDIAN_WRITE),
           "--title", title,
           "--category", "work_history",
           "--folder", f"Work Log/{month}",
           "--body", body,
           "--tags", tag, "work-log",
           "--frontmatter", json.dumps(fm),
           "--date", date]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        if p.returncode == 0 and p.stdout.strip():
            return json.loads(p.stdout).get("path")
    except Exception:  # noqa: BLE001 — never raise into the receipt path
        return None
    return None


def write_receipt(*, ws_ref: str, project: str | None, pr: int | None,
                  ci_status: str, reviewer_approved: bool | None,
                  test_count: int | None, summary: str,
                  outcome: str) -> dict[str, Any]:
    """Build, persist, and log one receipt. Returns the receipt dict.

    Raises OSError on a filesystem failure — main() converts it to the JSON
    error envelope + exit 1."""
    if not project:
        project = cmux_title(ws_ref) or ws_ref

    pr_url = PR_URL_TEMPLATE.format(pr=pr) if pr is not None else None
    receipt = {
        "ts": utc_iso(),
        "ws_ref": ws_ref,
        "project": project,
        "pr_number": pr,
        "pr_url": pr_url,
        "ci_status": ci_status,
        "reviewer_approved": reviewer_approved,
        "test_count": test_count,
        "summary": summary,
        "outcome": outcome,
        "quality_score": compute_quality_score(ci_status, reviewer_approved, outcome),
    }

    # 1. Canonical per-workspace receipt — atomic tmp + rename.
    RECEIPTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = RECEIPTS_DIR / f"{slug(ws_ref)}-{int(time.time())}.json"
    tmp = out_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(receipt, indent=2) + "\n")
    os.replace(tmp, out_path)

    # 2. Append-only searchable log — single append write (append_ledger style).
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_PATH, "a") as f:
        f.write(json.dumps(receipt) + "\n")

    receipt["_receipt_path"] = str(out_path)

    # 3. Mirror into the Obsidian vault (best-effort, never fatal).
    vault_path = mirror_receipt_to_vault(receipt)
    if vault_path:
        receipt["_obsidian_note"] = vault_path
    return receipt


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Write a work receipt for a workspace before cleanup.")
    ap.add_argument("--ws", required=True, help="workspace ref e.g. workspace:43")
    ap.add_argument("--project", default=None,
                    help="project name (default: cmux title, else ws_ref)")
    ap.add_argument("--pr", type=int, default=None, help="PR number, if any")
    ap.add_argument("--ci-status", dest="ci_status", default="unknown",
                    choices=["green", "red", "unknown"])
    ap.add_argument("--reviewer-approved", dest="reviewer_approved",
                    default="unknown", choices=["true", "false", "unknown"])
    ap.add_argument("--test-count", dest="test_count", type=int, default=None)
    ap.add_argument("--summary", default="", help="1-2 sentence evidence summary")
    ap.add_argument("--outcome", default="shipped",
                    choices=["shipped", "closed-no-pr", "abandoned"])
    args = ap.parse_args(argv)

    try:
        receipt = write_receipt(
            ws_ref=args.ws,
            project=args.project,
            pr=args.pr,
            ci_status=args.ci_status,
            reviewer_approved=parse_tristate(args.reviewer_approved),
            test_count=args.test_count,
            summary=args.summary,
            outcome=args.outcome,
        )
    except Exception as e:  # noqa: BLE001 — surface as JSON, never traceback
        print(json.dumps({"error": str(e), "ws_ref": args.ws}))
        return 1

    print(json.dumps(receipt))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
