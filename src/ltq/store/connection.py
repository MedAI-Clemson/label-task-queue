"""Opening a SQLite database the way the store needs it.

Python's ``sqlite3`` driver decides by itself when to begin a transaction, and it never does
for a SELECT or for the first statement of a write. The store needs the opposite: a write
transaction must take SQLite's write lock before it reads anything, so two claimants cannot
both read the same open slot. ``connect`` therefore stops the driver from managing
transactions (``isolation_level = None``) and has SQLAlchemy begin each one itself: ``BEGIN
IMMEDIATE`` for a write transaction, a plain ``BEGIN`` (a consistent read snapshot) otherwise.
This is the "serializable isolation" recipe from the SQLAlchemy SQLite documentation.

The database runs in WAL mode, so readers never block the writer and the writer never blocks
readers, and every connection waits up to ``busy_timeout`` seconds for the write lock instead
of failing.
"""

from __future__ import annotations

import sqlite3

import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.engine import make_url

WRITE = "ltq_write"
"""Execution option that makes a connection begin with BEGIN IMMEDIATE."""

_MARK = "ltq_connect"
"""Execution option that marks an engine as made by ``connect``."""


def connect(
    url: str | sa.URL, *, busy_timeout: float = 30.0, synchronous: str = "FULL"
) -> sa.Engine:
    """An engine for a SQLite database with the store's settings.

    ``busy_timeout`` is how many seconds to wait for another writer. ``synchronous`` is
    SQLite's ``PRAGMA synchronous``; FULL (the default) waits for every commit to reach the
    disk, which is what an audit log wants. Tests may pass OFF for speed.
    Call ``engine.dispose()`` when done.
    """
    parsed = make_url(url)
    if parsed.get_backend_name() != "sqlite":
        raise ValueError(f"Only SQLite is supported for now, not {parsed.get_backend_name()!r}.")
    if synchronous.upper() not in {"OFF", "NORMAL", "FULL", "EXTRA"}:
        raise ValueError(f"Unknown synchronous setting {synchronous!r}.")
    engine = sa.create_engine(
        parsed, connect_args={"timeout": busy_timeout}, execution_options={_MARK: True}
    )

    @event.listens_for(engine, "connect")
    def configure(dbapi_connection: sqlite3.Connection, _record: object) -> None:
        dbapi_connection.isolation_level = None  # we send BEGIN ourselves, see "begin" below
        dbapi_connection.execute(f"PRAGMA busy_timeout = {int(busy_timeout * 1000)}")
        # Switching to WAL needs a lock; do it only the first time.
        if dbapi_connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "delete":
            dbapi_connection.execute("PRAGMA journal_mode = WAL")
        dbapi_connection.execute(f"PRAGMA synchronous = {synchronous.upper()}")

    @event.listens_for(engine, "begin")
    def begin(connection: sa.Connection) -> None:
        write = connection.get_execution_options().get(WRITE)
        connection.exec_driver_sql("BEGIN IMMEDIATE" if write else "BEGIN")

    return engine


def is_configured(engine: sa.Engine) -> bool:
    """Whether the engine came from ``connect`` (or is not SQLite and needs nothing)."""
    return engine.dialect.name != "sqlite" or bool(engine.get_execution_options().get(_MARK))
