#!/usr/bin/env python3
"""pre-cleanup-check — the gate pulse.py consults before dispatching /cleanup.

A workspace must have a work receipt on disk before it may be auto-torn-down.
The receipt is the audit trail (what shipped, CI status, reviewer sign-off);
tearing a workspace down without one destroys the only record that the work
was reviewable. So:

  receipt exists for this ws_ref  -> {"gate": "pass", "receipt_path": "...", ...}
  no receipt                      -> {"gate": "block", "reason": "no receipt",
                                      "evidence": "..."}

On a block, pulse.py emits a needs_user card + Telegram ping instead of
cleaning up. On a pass, cleanup proceeds and the receipt_path is attached to
the ledger entry.

Receipt lookup: glob ~/.assistant/receipts/<ws_ref_slug>-*.json, newest by
mtime wins (a workspace can be receipted more than once across its life — the
last word is the truth).

Contract: ALWAYS exit 0. The gate result lives in the JSON on stdout, never in
the exit code — a nonzero exit would be ambiguous with a crash, and pulse.py
must be able to distinguish "blocked" (don't clean up) from "tool broke"
(also don't clean up, but for a different reason). On an internal error we
still emit gate=block (fail safe: never auto-clean on a broken gate).
"""
from __future__ import annotations

import argparse
import glob
import json
import os
from pathlib import Path
from typing import Any

HOME = Path.home()
RECEIPTS_DIR = HOME / ".assistant" / "receipts"


def slug(ws_ref: str) -> str:
    """workspace:43 -> workspace-43 (matches write-receipt.py)."""
    return ws_ref.replace(":", "-")


def latest_receipt(ws_ref: str) -> Path | None:
    """Newest receipt file for this ws_ref by mtime, or None."""
    pattern = str(RECEIPTS_DIR / f"{slug(ws_ref)}-*.json")
    matches = glob.glob(pattern)
    if not matches:
        return None
    return Path(max(matches, key=os.path.getmtime))


def pre_cleanup_check(ws_ref: str) -> dict[str, Any]:
    """Return the gate verdict for ws_ref. Never raises — a broken gate
    fails safe to block."""
    try:
        path = latest_receipt(ws_ref)
    except Exception as e:  # noqa: BLE001 — fail safe to block
        return {
            "gate": "block",
            "reason": "gate error",
            "evidence": f"receipt lookup failed: {e}",
            "ws_ref": ws_ref,
        }

    if path is None:
        return {
            "gate": "block",
            "reason": "no receipt",
            "evidence": (f"No work receipt for {ws_ref} in {RECEIPTS_DIR}. "
                         "Write one with write_receipt before cleanup."),
            "ws_ref": ws_ref,
        }

    # Surface the receipt's own evidence so the caller's ping can quote it.
    evidence = ""
    try:
        data = json.loads(path.read_text())
        evidence = (data.get("summary") or "").strip()
    except Exception:
        pass

    return {
        "gate": "pass",
        "receipt_path": str(path),
        "evidence": evidence,
        "ws_ref": ws_ref,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Gate the /cleanup dispatch on a work receipt existing. "
                    "Returns JSON {gate: pass|block, ...}. Always exits 0.")
    ap.add_argument("--ws", required=True, help="workspace ref e.g. workspace:43")
    args = ap.parse_args(argv)
    print(json.dumps(pre_cleanup_check(args.ws)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
