"""Small helpers for the deployment tables (accounts, sessions, projects)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

import sqlalchemy as sa

from labelflow.store.connection import WRITE
from labelflow.store.schema import build_metadata


def table(name: str) -> sa.Table:
    """One of the store's tables, by name."""
    return build_metadata().tables[name]


@contextmanager
def reading(engine: sa.Engine) -> Iterator[sa.Connection]:
    """A connection that sees one consistent snapshot."""
    with engine.connect() as connection:
        yield connection


@contextmanager
def writing(engine: sa.Engine) -> Iterator[sa.Connection]:
    """A write transaction that takes SQLite's write lock first; it commits on success."""
    with engine.connect().execution_options(**{WRITE: True}) as connection, connection.begin():
        yield connection


def to_text(moment: datetime) -> str:
    """A timestamp as stored: ISO 8601 in UTC."""
    return moment.astimezone(UTC).isoformat()


def from_text(text: str) -> datetime:
    return datetime.fromisoformat(text)
