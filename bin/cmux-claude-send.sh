#!/bin/zsh
# Type a message into a running Claude Code session's cmux surface, press Enter, and confirm it reached the transcript.
# usage: cmux-claude-send.sh <surface> <transcript> <message>
# stdout: "delivered" or "not-delivered"; exit 1 when not delivered within 60s.
set -u
[[ $# -eq 3 ]] || { echo "usage: ${0:t} <surface> <transcript> <message>" >&2; exit 2; }
SF="$1"; T="$2"; MSG="$3"
[[ -f "$T" ]] || { echo "no such transcript: $T" >&2; exit 2; }
NEEDLE=$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1].splitlines()[0])[1:-1])' "$MSG")
rpc() { cmux rpc "$1" "$(python3 -c 'import json,sys; d={"surface_id":sys.argv[1]}; d.update(a.split("=",1) for a in sys.argv[2:]); print(json.dumps(d))' "$SF" "${@:2}")"; }
count() { grep -cF -- "$NEEDLE" "$T"; }

BEFORE=$(count)
rpc surface.send_text "text=$MSG" >/dev/null
sleep 1
rpc surface.send_key key=enter >/dev/null
for i in {1..30}; do
  (( $(count) > BEFORE )) && { echo delivered; exit 0; }
  (( i == 5 )) && rpc surface.send_key key=enter >/dev/null
  sleep 2
done
echo not-delivered
exit 1
