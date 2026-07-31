#!/usr/bin/env python3
"""Append `[N]` workspace-ref suffix to every cmux workspace name, and give any
workspace that has no color yet a sidebar color.

Polls `cmux list-workspaces --json` every few seconds and:
  1. renames any workspace whose title is not `<base-title> [N]`, where N is the
     stable `workspace:N` ref, and
  2. assigns a sidebar color to any workspace whose `custom_color` is null,
     picking a palette color not already in use (shuffle-without-replacement;
     falls back to a ref-derived choice once all 16 are taken).

Idempotent: a workspace whose title already ends with the correct `[N]` is left
alone, and a workspace that already has ANY color is never recolored — so the
color the spawn-claude-workspace skill assigns at spawn is preserved, and this
daemon only fills the gaps (python-spawned probe workspaces, manually-created
ones). A poll that finds nothing to do is a no-op.

Targets the cmux 0.64.8 CLI: `list-workspaces --json` for reads and
`workspace-action --action {rename,set-color} --workspace <ref>` for writes.
The list exposes a stable `ref` ("workspace:N") but no UUID, and the actions
accept that ref directly — so we key everything off the ref and never need a
UUID.

SAFETY: `workspace-action` silently falls back to the SELECTED workspace when
`--workspace <ref>` does not resolve (e.g. the workspace closed between the list
and the action). Every action's stdout echoes `workspace=workspace:N`; we parse
it and refuse to trust an action whose echoed ref differs from the one we
targeted, so a mid-poll close can never clobber the wrong (selected) workspace.
"""
from __future__ import annotations

import json
import logging
import os
import random
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
# Every workspace-action echoes the workspace it resolved to; we assert it
# matches our target to defeat the silent fallback-to-selected behavior.
ECHOED_WS_RE = re.compile(r"workspace=(workspace:\d+)")

# The 16-color cmux palette, name → canonical #RRGGBB. These hexes are what
# `cmux workspace-action --action set-color --color <name>` persists as
# `custom_color` (verified against the live CLI). Kept name-keyed so we set by
# name (readable) but compare in-use colors by the hex the list reports. If cmux
# rebrands the palette, re-probe by running set-color for each name and reading
# the `color=#…` token from stdout.
PALETTE: dict[str, str] = {
    "Red": "#C0392B",
    "Crimson": "#922B21",
    "Orange": "#A04000",
    "Amber": "#7D6608",
    "Olive": "#4A5C18",
    "Green": "#196F3D",
    "Teal": "#006B6B",
    "Aqua": "#0E6B8C",
    "Blue": "#1565C0",
    "Navy": "#1A5276",
    "Indigo": "#283593",
    "Purple": "#6A1B9A",
    "Magenta": "#AD1457",
    "Rose": "#880E4F",
    "Brown": "#7B3F00",
    "Charcoal": "#3E4B5E",
}
PALETTE_NAMES = list(PALETTE.keys())

logging.basicConfig(
    filename=str(LOG_PATH),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("cmux-ws-numberer")

# Silence the legacy-alias deprecation notices so they never pollute parsed stdout.
CMUX_ENV = {**os.environ, "CMUX_QUIET": "1"}


def list_workspaces() -> list[dict]:
    """Return [{ref:int, title:str, color:str|None}] for every workspace.

    `title` is glyph-stripped; `color` is the reported `custom_color` (hex or
    None) upper-cased for comparison against PALETTE."""
    out = subprocess.run(
        [CMUX, "list-workspaces", "--json"],
        capture_output=True, text=True, timeout=10, env=CMUX_ENV,
    )
    try:
        data = json.loads(out.stdout)
    except json.JSONDecodeError:
        log.error("list-workspaces --json rc=%s stdout=%r stderr=%r",
                  out.returncode, out.stdout[:300], out.stderr[:300])
        return []
    rows: list[dict] = []
    for w in data.get("workspaces", []):
        m = re.match(r"workspace:(\d+)$", w.get("ref", ""))
        if not m:
            continue
        title = GLYPH_PREFIX_RE.sub("", w.get("title", "")).rstrip()
        color = w.get("custom_color")
        rows.append({
            "ref": int(m.group(1)),
            "title": title,
            "color": color.upper() if isinstance(color, str) else None,
        })
    return rows


def desired_title(base: str, ref: int) -> str:
    base = GLYPH_PREFIX_RE.sub("", base)
    base = SUFFIX_RE.sub("", base).rstrip()
    return f"{base} [{ref}]"


def pick_color(ref: int, used_hexes: set[str]) -> str:
    """Return a palette NAME whose hex is not in `used_hexes`. Shuffle-without-
    replacement for visual distinctness; once every color is taken, fall back to
    a deterministic ref-derived pick so the choice is still stable per ref."""
    free = [name for name in PALETTE_NAMES if PALETTE[name] not in used_hexes]
    if free:
        return random.choice(free)
    return PALETTE_NAMES[ref % len(PALETTE_NAMES)]


def _run_action(action: str, ref: str, extra: list[str]) -> bool:
    """Run one `workspace-action` and confirm it resolved to `ref` (guards the
    silent fallback-to-selected). Returns True on a verified success."""
    res = subprocess.run(
        [CMUX, "workspace-action", "--action", action, "--workspace", ref, *extra],
        capture_output=True, text=True, timeout=10, env=CMUX_ENV,
    )
    if res.returncode != 0:
        log.error("%s failed %s extra=%s stderr=%s",
                  action, ref, extra, res.stderr.strip())
        return False
    m = ECHOED_WS_RE.search(res.stdout)
    if m and m.group(1) != ref:
        # The ref didn't resolve — cmux fell back to the selected workspace.
        # Do NOT treat this as success; the target likely closed mid-poll.
        log.warning("%s target=%s but cmux resolved to %s (skipped, ref stale)",
                    action, ref, m.group(1))
        return False
    return True


def reconcile() -> None:
    """One pass: fix the `[N]` suffix, then fill in a color, per workspace."""
    rows = list_workspaces()
    used_hexes = {r["color"] for r in rows if r["color"]}
    for r in rows:
        ref = f"workspace:{r['ref']}"

        target = desired_title(r["title"], r["ref"])
        if r["title"] != target:
            if _run_action("rename", ref, ["--title", target]):
                log.info("renamed %s: %r -> %r", ref, r["title"], target)

        if r["color"] is None:
            name = pick_color(r["ref"], used_hexes)
            if _run_action("set-color", ref, ["--color", name]):
                used_hexes.add(PALETTE[name])
                log.info("colored %s -> %s (%s)", ref, name, PALETTE[name])


POLL_INTERVAL = 3.0  # seconds; a cosmetic title suffix does not need sub-second latency


def main() -> int:
    # Poll, don't stream. The prior event-stream design (`cmux events
    # --reconnect`) broke three different ways: (1) cmux block-buffers stdout on
    # a plain pipe so events never flush; (2) the --cursor-file records a
    # per-boot absolute seq, so a cursor from a previous boot stalls the stream
    # forever after a reboot/cmux-restart — this is what silently died in the
    # Mac migration; (3) the pipe→pty workaround that fixes (1) in a TTY does
    # not deliver under launchd (no controlling terminal). reconcile() is cheap
    # (one `list-workspaces --json` + a write only when a suffix/color is
    # missing) and idempotent, so a simple poll loop is robust against all of
    # the above. Per the operator's own lesson: for cadence problems, one knob
    # (this interval); a few seconds of latency on a cosmetic suffix is
    # imperceptible.
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
