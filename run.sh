#!/bin/bash
# Run the development server with auto-reload.
# Expects the package to be installed (see README). Activates ./env if present.
#
# Settings, all optional: DATABASE_URI, LABELFLOW_LABEL_TASKS (default: examples/ontologies here),
# LABELFLOW_MEDIA_ROOT (the only folder the server hashes images from; without it every imported
# item must bring its content_hash), LABELFLOW_SESSION_HOURS, LABELFLOW_COOKIE_SECURE,
# LABELFLOW_MAX_REQUEST_MB, LABELFLOW_MAX_IMPORT_ITEMS, LABELFLOW_MAX_PROPERTIES_KB,
# LABELFLOW_MAX_LABEL_KB, HOST, PORT. See src/labelflow/api/settings.py.
# The first time, make an administrator:  python -m labelflow.api.admin create-admin <username>
set -euo pipefail
cd "$(dirname "$0")"
[ -f env/bin/activate ] && source env/bin/activate
mkdir -p db
export DATABASE_URI="${DATABASE_URI:-sqlite:///$(pwd)/db/labelflow.db}"
export LABELFLOW_LABEL_TASKS="${LABELFLOW_LABEL_TASKS:-$(pwd)/examples/ontologies}"

# Create the database, or bring it up to date.
alembic upgrade head

exec uvicorn labelflow.api:app --reload --host "${HOST:-0.0.0.0}" --port "${PORT:-8000}"
