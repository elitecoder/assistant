#!/usr/bin/env python3
"""Append `[N]` workspace-ref suffix to every cmux workspace name.

Polls `cmux workspace list --json` every few seconds and renames any
workspace whose title is not `<base-title> [N]`, where N is the stable
`workspace:N` ref.

Idempotent: a workspace whose title already ends with the correct `[N]` is
left alone, so a poll that finds nothing to do is a no-op.

Targets the cmux 0.64+ canonical CLI: `workspace list --json` and
`workspace rename <ref> --title`. The list exposes a stable `ref`
("workspace:N") but no UUID, and rename accepts that ref directly — so we key
everything off the ref and never need a UUID.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path

CMUX = "/Applications/cmux.app/Contents/Resources/bin/cmux"
LOG_PATH = Path.home() / ".claude" / "logs" / "cmux-ws-numberer.log"
SUFFIX_RE = re.compile(r"\s*\[(\d+)\]\s*$")
# cmux prepends a transient status glyph (spinner / activity dot) to the
# DISPLAYED title — e.g. "✳ Foo", "⠐ Foo". It is not part of the name we set,
# but the live `title` field carries it. Strip a single leading run of
# non-word, non-"[" glyph chars + spaces so we compare and persist the clean
# base name (otherwise we'd bake the spinner into the title and churn).
GLYPH_PREFIX_RE = re.compile(r"^[^\w\[(]+\s*")

logging.basicConfig(
    filename=str(LOG_PATH),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("cmux-ws-numberer")

# Silence the legacy-alias deprecation notices so they never pollute parsed stdout.
CMUX_ENV = {**os.environ, "CMUX_QUIET": "1"}


def list_workspaces() -> list[dict]:
    """Return [{ref:int, title:str}] for every workspace, glyph-stripped."""
    out = subprocess.run(
        [CMUX, "workspace", "list", "--json"],
        capture_output=True, text=True, timeout=10, env=CMUX_ENV,
    )
    try:
        data = json.loads(out.stdout)
    except json.JSONDecodeError:
        log.error("workspace list --json rc=%s stdout=%r stderr=%r",
                  out.returncode, out.stdout[:300], out.stderr[:300])
        return []
    rows: list[dict] = []
    for w in data.get("workspaces", []):
        m = re.match(r"workspace:(\d+)$", w.get("ref", ""))
        if not m:
            continue
        title = GLYPH_PREFIX_RE.sub("", w.get("title", "")).rstrip()
        rows.append({"ref": int(m.group(1)), "title": title})
    return rows


def desired_title(base: str, ref: int) -> str:
    base = GLYPH_PREFIX_RE.sub("", base)
    base = SUFFIX_RE.sub("", base).rstrip()
    return f"{base} [{ref}]"


def reconcile() -> None:
    """Rename every workspace whose title lacks the correct `[N]` suffix."""
    for r in list_workspaces():
        target = desired_title(r["title"], r["ref"])
        if r["title"] == target:
            continue
        ref = f"workspace:{r['ref']}"
        res = subprocess.run(
            [CMUX, "workspace", "rename", ref, "--title", target],
            capture_output=True, text=True, timeout=10, env=CMUX_ENV,
        )
        if res.returncode == 0:
            log.info("renamed %s: %r -> %r", ref, r["title"], target)
        else:
            log.error("rename failed %s target=%r stderr=%s",
                      ref, target, res.stderr.strip())


POLL_INTERVAL = 3.0  # seconds; a cosmetic title suffix does not need sub-second latency


def main() -> int:
    # Poll, don't stream. The prior event-stream design (`cmux events
    # --reconnect`) broke three different ways: (1) cmux block-buffers stdout on
    # a plain pipe so events never flush; (2) the --cursor-file records a
    # per-boot absolute seq, so a cursor from a previous boot stalls the stream
    # forever after a reboot/cmux-restart — this is what silently died in the
    # Mac migration; (3) the pipe→pty workaround that fixes (1) in a TTY does
    # not deliver under launchd (no controlling terminal). reconcile() is cheap
    # (one `workspace list --json` + a rename only when a suffix is missing) and
    # idempotent, so a simple poll loop is robust against all of the above. Per
    # the operator's own lesson: for cadence problems, one knob (this interval);
    # a few seconds of latency on a cosmetic suffix is imperceptible.
    log.info("=== cmux-ws-numberer pid=%d (poll every %.0fs) ===", os.getpid(), POLL_INTERVAL)
    while True:
        try:
            reconcile()
        except KeyboardInterrupt:
            log.info("interrupted, exiting")
            return 0
        except Exception:
            log.exception("reconcile failed; continuing")
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    sys.exit(main())
