#!/usr/bin/env python3
"""memory_repo_sync — fire-and-forget push to the cross-machine memory repo.

Shared by assistant-curator.py (after a lesson is written) and mem0-add.py
(after a memory is added). Both call `sync_to_memory_repo()`, which launches
`~/dev/mukul-memory/scripts/sync-push.sh` as a DETACHED background process and
returns immediately — the caller never blocks on git, and any failure lands in
the audit log, never in the caller's output.

No-ops (returns silently) when:
  - ~/.assistant/memory-repo-config.json is absent (system not installed here),
  - MEMORY_SYNC_IN_PROGRESS=1 (we're inside a sync-pull import — avoids the
    pull→import→add→push→… loop),
  - sync.push_on_* is disabled in config,
  - the push script is missing.

This module raises nothing: a sync failure must never break a lesson/memory add.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

CONFIG = Path.home() / ".assistant" / "memory-repo-config.json"
AUDIT_LOG = Path.home() / ".assistant" / "assistant-audit.log"
DEFAULT_REPO = Path.home() / "dev" / "mukul-memory"


def _expand(p: str) -> Path:
    return Path(os.path.expanduser(p))


def _load_config() -> dict | None:
    try:
        return json.loads(CONFIG.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def sync_to_memory_repo(reason: str = "") -> None:
    """Push local memory state to the repo in the background. Never raises."""
    try:
        if os.environ.get("MEMORY_SYNC_IN_PROGRESS") == "1":
            return  # inside a sync-pull; don't push back what we just pulled
        cfg = _load_config()
        if cfg is None:
            return  # memory repo not installed on this machine
        # Respect the per-trigger toggles; default-on if absent.
        sync_cfg = cfg.get("sync", {})
        toggle = {
            "lesson": "push_on_lesson_confirm",
            "memory": "push_on_memory_add",
        }.get(reason)
        if toggle is not None and not sync_cfg.get(toggle, True):
            return

        repo = _expand(cfg.get("memory_repo", {}).get("local_path",
                                                       str(DEFAULT_REPO)))
        script = repo / "scripts" / "sync-push.sh"
        if not script.exists():
            return

        # Detach completely: own session, no stdin, output to the audit log.
        # The child sets MEMORY_SYNC_IN_PROGRESS so any tool it shells out to
        # won't recursively fire another push.
        env = dict(os.environ, MEMORY_SYNC_IN_PROGRESS="1")
        with open(AUDIT_LOG, "a", encoding="utf-8") as log:
            log.write(f"[memory-sync] push triggered ({reason or 'manual'})\n")
            subprocess.Popen(
                ["bash", str(script)],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                start_new_session=True,
                cwd=str(repo),
                env=env,
            )
    except Exception:  # noqa: BLE001 — a sync failure must never break the caller
        try:
            with open(AUDIT_LOG, "a", encoding="utf-8") as log:
                log.write("[memory-sync] push hook errored (suppressed)\n")
        except OSError:
            pass
