#!/usr/bin/env bash
# Run the whole spike check: fixtures -> headless browser -> Python verification.
#
#   PYTHON=/path/to/venv/bin/python NODE_PATH=/path/to/scratch/node_modules spikes/mask-tools/check.sh [--big] [--page konva.html]
#
# PYTHON must have the project installed (numpy, pyyaml, labelflow). NODE_PATH must contain
# a Playwright whose Chromium matches ~/.cache/ms-playwright (1.48.x for chromium-1140):
#   mkdir -p /tmp/spike-pw && cd /tmp/spike-pw && npm init -y && npm i playwright@1.48.2
#   (or `npx playwright install chromium` there to download one). Nothing is added to the project.
# --big also generates the 2048 x 2048 fixture (not committed) and records performance numbers.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
PYTHON="${PYTHON:-python}"
PAGE=index.html
BIG=""
while [ $# -gt 0 ]; do
  case "$1" in
    --big) BIG=--big ;;
    --page) PAGE="$2"; shift ;;
    *) echo "unknown argument $1" >&2; exit 2 ;;
  esac
  shift
done
if [ ! -f "$HERE/fixtures/work.json" ] || { [ -n "$BIG" ] && [ ! -f "$HERE/fixtures/work_2048.json" ]; }; then
  "$PYTHON" "$HERE/fixtures/make_fixtures.py" $BIG
fi
node "$HERE/check/run_check.mjs" --page "$PAGE" $BIG
"$PYTHON" "$HERE/check/check_export.py" "$HERE/check/out/export-${PAGE%.html}.json"
