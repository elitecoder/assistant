#!/bin/bash
# Launchd pre-flight wrapper for the Assistant pulse.
#
# Compile-checks bin/pulse.py before running it. If pulse.py won't parse (a
# self-update that slipped broken code past its own gate, or any local
# corruption), this logs the failure and exits 0 so launchd keeps the
# StartInterval schedule alive. A non-zero exit here would make launchd
# throttle and eventually stop retrying — exactly the silent months-long
# outage this guards against. The next self-update (or a manual fix) heals the
# tree and the following run succeeds.
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
