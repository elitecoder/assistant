#!/bin/bash
# Launchd pre-flight wrapper for the Assistant pulse.
#
# Compile-checks bin/pulse.py before running it. If pulse.py won't parse (a
# self-update that slipped broken code past its own gate, or any local
# corruption), this logs the reason to the launchd err file and exits 0 instead
# of running a doomed pulse.
#
# The LaunchAgent is StartInterval=300 + RunAtLoad with no KeepAlive, so it
# fires every 5 minutes regardless of exit code. The 2026 outage wasn't launchd
# giving up — it was pulse.py raising SyntaxError on every tick, doing no work
# for months, with the only trace in an err log nobody watches. Exiting 0 on a
# failed compile keeps that log to one legible line per tick (no repeated crash
# traceback, no brief crash-respawn throttle). If the broken file is pulse.py
# ITSELF, self-update (which runs inside pulse.py) can't heal it and recovery is
# manual — but the wrapper still turns a silent crash into a clear, logged retry.
#
# The plist passes the arch-resolved python3 as $1 (install.sh's __PYTHON__
# substitution); the repo is derived from this script's own location.
set -u

if [ "$#" -gt 0 ]; then
    PYTHON="$1"
    shift
else
    PYTHON="python3"
fi

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PULSE="$REPO_DIR/bin/pulse.py"
ERR_LOG="${HOME}/.assistant/logs/assistant-pulse.launchd.err"

if ! compile_out="$("$PYTHON" -m py_compile "$PULSE" 2>&1)"; then
    mkdir -p "$(dirname "$ERR_LOG")"
    {
        printf '[%s] pulse.py pre-flight py_compile FAILED — skipping this run\n' \
            "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
        printf '%s\n' "$compile_out"
    } >> "$ERR_LOG"
    exit 0
fi

exec "$PYTHON" "$PULSE" "$@"
