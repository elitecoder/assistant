#!/usr/bin/env python3
"""Retired: cmux records agent sessions itself (`cmux sessions list`).

This used to append SessionStart/SessionEnd events to ~/.cmux-session-ledger.jsonl.
It stays one release as a no-op so a registration that outlives the pull still
exits cleanly; `install.sh --apply` (install/patch-settings.py) removes those
registrations. Delete this file and its install.sh link in the next release.
"""
import sys

if __name__ == "__main__":
    if not sys.stdin.isatty():
        sys.stdin.read()
