#!/bin/zsh
# Start Claude Code (auto mode) in a new unfocused cmux workspace and have it read one staged prompt file.
# usage: cmux-claude-spawn.sh [--env FFP_CHANNEL=<v>|FFP_EXP_<ID>=<v>]... <cwd> <title> <prompt-file>
#   <prompt-file> must be a file under ~/.assistant/spawn-prompts/; the session receives "Read <file> and follow it."
# stdout: "<workspace-ref> <surface-uuid> <transcript>"
# exit: 0 submitted and seen in a new transcript; 1 not ready, refused by cmux, or never seen; 2 bad arguments;
#       3 the claude alias isn't auto mode.
set -u
PROMPTS="$HOME/.assistant/spawn-prompts"
ENVS=()
while [[ "${1:-}" == --env ]]; do
  [[ "${2:-}" =~ '^(FFP_CHANNEL|FFP_EXP_[A-Z0-9_]+)=[a-z0-9]+$' ]] || { echo "bad --env value: ${2:-}" >&2; exit 2; }
  ENVS+=(--env "$2"); shift 2
done
[[ $# -eq 3 ]] || { echo "usage: ${0:t} [--env FFP_CHANNEL=<v>|FFP_EXP_<ID>=<v>]... <cwd> <title> <prompt-file>" >&2; exit 2; }
CWD="${1:A}"; TITLE="$2"; PF="${3:A}"
[[ -d "$CWD" ]] || { echo "no such directory: $CWD" >&2; exit 2; }
[[ "$PF" == "${PROMPTS:A}"/* && -f "$PF" && "$PF" =~ '^[A-Za-z0-9._/-]+$' ]] \
  || { echo "prompt file must be a plain-named file under $PROMPTS: $3" >&2; exit 2; }

ALIAS=$(zsh -lic 'alias claude' 2>/dev/null)
[[ "$ALIAS" == *"--permission-mode auto"* && "$ALIAS" != *--dangerously-skip-permissions* ]] \
  || { echo "refusing: the claude alias isn't auto mode: $ALIAS" >&2; exit 3; }

MSG="Read $PF and follow it."
SD="$HOME/.claude/projects/${CWD//[^A-Za-z0-9]/-}"
BEFORE=$(find "$SD" -maxdepth 1 -name '*.jsonl' 2>/dev/null | sort)

OUT=$(CMUX_QUIET=1 cmux new-workspace --cwd "$CWD" --name "$TITLE" --focus false "${ENVS[@]}" --command "zsh -lic claude")
WS=$(printf '%s' "$OUT" | grep -oE 'workspace:[0-9]+' | head -1)
[[ -n "$WS" ]] && SF=$(CMUX_QUIET=1 cmux list-pane-surfaces --workspace "$WS" --id-format uuids | grep -oE '[0-9A-Fa-f]{8}-[0-9A-Fa-f-]{27}' | head -1)
[[ -n "$WS" && -n "${SF:-}" ]] || { echo "spawn failed: $OUT" >&2; exit 1; }

READY=0
for i in {1..90}; do
  SCREEN=$(CMUX_QUIET=1 cmux read-screen --surface "$SF" --lines 60 2>/dev/null)
  [[ "$SCREEN" == *"auto mode on"* ]] && { READY=1; break; }
  [[ -z "${SCREEN//[[:space:]]/}" ]] && (( i % 5 == 3 )) && CMUX_QUIET=1 cmux send-key --surface "$SF" enter >/dev/null 2>&1
  sleep 1
done
(( READY )) || { echo "$WS $SF not-ready"; echo "session never showed auto mode; nothing was typed" >&2; exit 1; }

CMUX_QUIET=1 cmux paste --surface "$SF" --submit -- "$MSG" >/dev/null || { echo "$WS $SF paste-refused"; exit 1; }

for i in {1..60}; do
  for f in ${(f)"$(find "$SD" -maxdepth 1 -name '*.jsonl' 2>/dev/null | sort | comm -13 <(print -r -- "$BEFORE") -)"}; do
    grep -F -- "$MSG" "$f" 2>/dev/null | grep -qF '"type":"user"' && { echo "$WS $SF $f"; exit 0; }
  done
  sleep 2
done
echo "$WS $SF transcript-not-found"
exit 1
