"""Daemon-tier gate — install.sh loads CORE by default and FEATURE daemons only
when opted in. Drives install.sh in DRY-RUN (no mutation, no launchctl) and
asserts on its planned actions. Locks in the opt-in design: a fresh install
must not silently start memory-sync / crash-resume / Slack daemons.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

CORE = [
    "com.assistant.assistant-pulse",
    "com.assistant.world-scanner",
    "com.assistant.session-context-watcher",
    "com.assistant.assistant-page",
    "com.assistant.assistant-todo-server",
]
FEATURE = [
    "com.assistant.memory-sync-pull",
    "com.assistant.workspace-watcher",
    "com.assistant.assistant-comms",
    "com.assistant.slack-reactor",
]


def _dryrun(*flags: str) -> str:
    """Run install.sh (dry-run — no --apply) against a sandbox HOME PRE-SEEDED
    with placeholder plists, so the phase-3 tier decision hits the UPDATE path
    and phase-5 emits a deterministic 'would reload <label>' for every daemon it
    chooses to load. (A fresh empty HOME would print 'NEW' and, in dry-run, the
    file is never actually written, so phase-5 can't report a reload — an
    artifact of dry-run, not the tier logic.) Never touches the real machine;
    runs no launchctl."""
    import tempfile, shutil
    home = tempfile.mkdtemp(prefix="tier-home-", dir="/tmp")
    try:
        la = Path(home) / "Library" / "LaunchAgents"
        la.mkdir(parents=True)
        (Path(home) / ".claude").mkdir(parents=True)
        (Path(home) / ".assistant").mkdir(parents=True)
        # Seed each committed plist as a differing stub so the installer sees an
        # existing-but-changed target (→ UPDATE → CHANGED_LABELS → would reload).
        for p in (REPO / "launchagents").glob("com.assistant.*.plist"):
            (la / p.name).write_text("<!-- stale stub to force UPDATE -->\n")
        # Pin feature answers to "no" via the state file. This isolates the tier
        # decision from whatever daemons happen to be loaded in the REAL
        # launchctl user domain (the sandbox HOME can't sandbox launchctl, so
        # the daemon-adoption backfill would otherwise flip a running feature to
        # "load" and make the test machine-dependent). An explicit --with flag in
        # `flags` still wins over this "no" (that's the precedence we assert).
        (Path(home) / ".assistant" / "feature-opt-in").write_text(
            "memory=no\ncrash-resume=no\n")
        env = {
            "HOME": home,
            "PATH": "/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin:"
                    "/Applications/cmux.app/Contents/Resources/bin",
            "ASSISTANT_SELF_UPDATE": "1",
        }
        r = subprocess.run(["bash", str(REPO / "install.sh"), *flags],
                           capture_output=True, text=True, env=env,
                           stdin=subprocess.DEVNULL, timeout=90)
        return r.stdout
    finally:
        shutil.rmtree(home, ignore_errors=True)


def _loads(out: str, label: str) -> bool:
    # "chosen to load" = a planned reload OR the self-update skip-reload (the
    # pulse plist is deliberately skip-reloaded under ASSISTANT_SELF_UPDATE=1
    # because it can't bootout its own running process — still a CORE load).
    return (f"would reload {label}" in out) or (f"skip reload of {label}" in out)


def test_bare_install_loads_core_not_features():
    out = _dryrun()  # dry-run, no flags
    for label in CORE:
        assert _loads(out, label), f"CORE {label} should load by default\n{out[-1500:]}"
    for label in FEATURE:
        assert not _loads(out, label), f"FEATURE {label} must NOT load by default"
    assert "memory-sync-pull.plist — not enabled" in out
    assert "workspace-watcher.plist — not enabled" in out


def test_with_memory_loads_memory_only():
    out = _dryrun("--with-memory")
    assert "would reload com.assistant.memory-sync-pull" in out
    assert "memory-sync-pull.plist — enabled" in out
    assert "would reload com.assistant.workspace-watcher" not in out


def test_with_crash_resume_loads_watcher_only():
    out = _dryrun("--with-crash-resume")
    assert "would reload com.assistant.workspace-watcher" in out
    assert "would reload com.assistant.memory-sync-pull" not in out


def test_with_all_loads_both_feature_timers():
    out = _dryrun("--with-all")
    assert "would reload com.assistant.memory-sync-pull" in out
    assert "would reload com.assistant.workspace-watcher" in out


def test_token_gated_daemons_never_auto_load_even_with_all():
    # comms + slack-reactor are token-gated: copied, hand-loaded after setup —
    # never auto-loaded, even under --with-all.
    out = _dryrun("--with-all")
    assert "would reload com.assistant.assistant-comms" not in out
    assert "would reload com.assistant.slack-reactor" not in out
    assert "assistant-comms.plist — token-gated" in out
