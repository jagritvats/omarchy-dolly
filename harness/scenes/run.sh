#!/usr/bin/env bash
# Record the camera regression scene with the code in this checkout and print its plan.
#   harness/scenes/run.sh [--export]     (default: record and analyse only, no render)
# Needs a running Omarchy session (it performs the scene on an empty workspace).
set -euo pipefail
HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
SB="$(mktemp --suffix=.toml)"
trap 'rm -f "$SB"' EXIT
sed "s|@PAGE@|$HERE/page.html|" "$HERE/floating-window.toml" >"$SB"
EXPORT=--no-export
[[ ${1:-} == --export ]] && EXPORT=
OUT="$("$ROOT/bin/dolly" run "$SB" $EXPORT --json)" || { echo "$OUT"; exit 1; }
TAKE="$(printf '%s' "$OUT" | python3 -c 'import json,sys; print(json.load(sys.stdin)["project"])')"
echo "$TAKE"
python3 "$HERE/shots.py" "$TAKE"
