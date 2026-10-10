#!/bin/bash
# Run the test suite (uses an in-memory database; no DATABASE_URI needed).
# Activates ./env if present.
set -euo pipefail
cd "$(dirname "$0")"
[ -f env/bin/activate ] && source env/bin/activate

exec python -m pytest "$@"
