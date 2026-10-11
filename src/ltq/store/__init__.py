"""Storage: the event log and derived tables in SQLite, with Alembic migrations.

``SqlStore`` implements the engine's ``Store`` protocol over SQLAlchemy Core::

    engine = connect("sqlite:///ltq.db")
    store = SqlStore(engine, project="xray")
    ltq_engine = Engine(store, registry)

Real databases are created with ``alembic upgrade head``; ``create_schema`` builds the same
tables directly, for tests. ``python -m ltq.store.rebuild`` rebuilds or verifies a project's
derived tables from its log.
"""

from ltq.store.connection import connect
from ltq.store.schema import create_schema
from ltq.store.sql import Report, SqlStore, projects

__all__ = ["Report", "SqlStore", "connect", "create_schema", "projects"]
