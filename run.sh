#!/bin/bash
# Run the development server with auto-reload.
# Expects the package to be installed (see README). Activates ./env if present.
#
# Settings, all optional: DATABASE_URI, LTQ_LABEL_TASKS (default: examples/ontologies here),
# LTQ_MEDIA_ROOT, LTQ_SESSION_HOURS, LTQ_COOKIE_SECURE, HOST, PORT.
# The first time, make an administrator:  python -m ltq.api.admin create-admin <username>
set -euo pipefail
cd "$(dirname "$0")"
[ -f env/bin/activate ] && source env/bin/activate
mkdir -p db
export DATABASE_URI="${DATABASE_URI:-sqlite:///$(pwd)/db/ltq.db}"
export LTQ_LABEL_TASKS="${LTQ_LABEL_TASKS:-$(pwd)/examples/ontologies}"

# Create the database, or bring it up to date.
alembic upgrade head

exec uvicorn ltq.api:app --reload --host "${HOST:-0.0.0.0}" --port "${PORT:-8000}"
