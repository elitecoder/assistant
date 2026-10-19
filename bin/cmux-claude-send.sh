#!/bin/zsh
# Have a running Claude Code session read one staged prompt file, and confirm it reached the transcript.
# usage: cmux-claude-send.sh <surface-uuid> <transcript> <prompt-file>
#   <prompt-file> must be a file under ~/.assistant/spawn-prompts/; the session receives "Read <file> and follow it."
# stdout: "delivered" or "not-delivered"
# exit: 0 delivered; 1 refused by cmux (draft or dialog open) or not seen within 120s; 2 bad arguments.
set -u
PROMPTS="$HOME/.assistant/spawn-prompts"
[[ $# -eq 3 ]] || { echo "usage: ${0:t} <surface-uuid> <transcript> <prompt-file>" >&2; exit 2; }
SF="$1"; T="$2"; PF="${3:A}"
[[ "$SF" =~ '^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$' ]] \
  || { echo "surface must be a UUID: $SF" >&2; exit 2; }
[[ "${SF:u}" != "${${CMUX_SURFACE_ID:-}:u}" ]] || { echo "refusing to type into this session's own surface" >&2; exit 2; }
[[ -f "$T" ]] || { echo "no such transcript: $T" >&2; exit 2; }
[[ "$PF" == "${PROMPTS:A}"/* && -f "$PF" && "$PF" =~ '^[A-Za-z0-9._/-]+$' ]] \
  || { echo "prompt file must be a plain-named file under $PROMPTS: $3" >&2; exit 2; }

MSG="Read $PF and follow it."
count() { grep -F -- "$MSG" "$T" | grep -cF '"type":"user"'; }
BEFORE=$(count)
CMUX_QUIET=1 cmux paste --surface "$SF" --submit -- "$MSG" >/dev/null || { echo not-delivered; echo "cmux refused the paste (draft or dialog open?)" >&2; exit 1; }
for i in {1..60}; do
  (( $(count) > BEFORE )) && { echo delivered; exit 0; }
  sleep 2
done
echo not-delivered
exit 1
