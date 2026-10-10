#!/bin/bash
# Run the development server with auto-reload.
# Expects dependencies to be installed (see README). Activates ./env if present.
set -euo pipefail
cd "$(dirname "$0")"
[ -f env/bin/activate ] && source env/bin/activate
mkdir -p db
export DATABASE_URI="${DATABASE_URI:-sqlite:///$(pwd)/db/database.db}"

exec uvicorn app.main:app --reload --host "${HOST:-0.0.0.0}" --port "${PORT:-8000}"
