#!/bin/zsh
# Start Claude Code in a new unfocused cmux workspace and submit one message.
# usage: cmux-claude-spawn.sh [--env KEY=VALUE]... <cwd> <title> <message>
# stdout: "<workspace> <surface> <transcript>"; exit 1 when the session or its transcript never appears.
set -u
EXPORTS=()
while [[ "${1:-}" == --env ]]; do
  [[ "${2:-}" =~ '^[A-Za-z_][A-Za-z0-9_]*=[A-Za-z0-9._:/@%+-]*$' ]] || { echo "bad --env value: ${2:-}" >&2; exit 2; }
  EXPORTS+=("$2"); shift 2
done
[[ $# -eq 3 ]] || { echo "usage: ${0:t} [--env KEY=VALUE]... <cwd> <title> <message>" >&2; exit 2; }
CWD="${1:A}"; TITLE="$2"; MSG="$3"
[[ -d "$CWD" ]] || { echo "no such directory: $CWD" >&2; exit 2; }
SD="$HOME/.claude/projects/${CWD//[^A-Za-z0-9]/-}"
NEEDLE=$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1].splitlines()[0])[1:-1])' "$MSG")
BEFORE=$(find "$SD" -maxdepth 1 -name '*.jsonl' 2>/dev/null | sort)

LAUNCH="claude"
(( ${#EXPORTS} )) && LAUNCH="export ${EXPORTS[*]}; claude"
OUT=$(CMUX_QUIET=1 cmux new-workspace --cwd "$CWD" --name "$TITLE" --focus false --command "zsh -lic '$LAUNCH'")
WS=$(printf '%s' "$OUT" | grep -oE 'workspace:[0-9]+' | head -1)
[[ -n "$WS" ]] && SF=$(cmux list-pane-surfaces --workspace "$WS" | grep -oE 'surface:[0-9]+' | head -1)
[[ -n "$WS" && -n "${SF:-}" ]] || { echo "spawn failed: $OUT" >&2; exit 1; }

rpc() { cmux rpc "$1" "$(python3 -c 'import json,sys; d={"surface_id":sys.argv[1]}; d.update(a.split("=",1) for a in sys.argv[2:]); print(json.dumps(d))' "$SF" "${@:2}")"; }
screen() { rpc surface.read_text lines=200 | python3 -c 'import json,sys; print(json.load(sys.stdin).get("text",""))'; }

for i in {1..60}; do
  screen | grep -Eiq 'auto mode|\? for shortcuts|bypass permissions' && break
  (( i == 3 )) && rpc surface.send_key key=enter >/dev/null
  sleep 1
done
rpc surface.send_text "text=$MSG" >/dev/null
sleep 1
rpc surface.send_key key=enter >/dev/null

for i in {1..60}; do
  for f in ${(f)"$(find "$SD" -maxdepth 1 -name '*.jsonl' 2>/dev/null | sort | comm -13 <(print -r -- "$BEFORE") -)"}; do
    grep -qF -- "$NEEDLE" "$f" && { echo "$WS $SF $f"; exit 0; }
  done
  sleep 2
done
echo "$WS $SF transcript-not-found"
exit 1
