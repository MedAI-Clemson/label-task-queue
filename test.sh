#!/bin/bash
# Lint, check formatting and run the test suite. Activates ./env if present.
set -euo pipefail
cd "$(dirname "$0")"
[ -f env/bin/activate ] && source env/bin/activate

ruff check .
ruff format --check .
exec python -m pytest "$@"
