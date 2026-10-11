"""Storage: the event log and derived tables in SQLite, with Alembic migrations.

``SqlStore`` implements the engine's ``Store`` protocol over SQLAlchemy Core::

    engine = connect("sqlite:///labelflow.db")
    store = SqlStore(engine, project="xray")
    labelflow_engine = Engine(store, registry)

Real databases are created with ``alembic upgrade head``; ``create_schema`` builds the same
tables directly, for tests. ``python -m labelflow.store.rebuild`` rebuilds or verifies a project's
derived tables from its log.
"""

from labelflow.store.connection import connect
from labelflow.store.schema import create_schema
from labelflow.store.sql import Report, SqlStore, projects

__all__ = ["Report", "SqlStore", "connect", "create_schema", "projects"]
