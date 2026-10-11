"""Fixtures shared by the engine and store tests: fresh stores of both kinds.

Every engine test that makes a store runs twice, once on the in-memory store and once on a
SQLite file: ``make_store`` is parametrized over both. A test that needs a bare store takes
it and calls it; the engine tests' ``build`` fixture is built on it.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from labelflow.engine import InMemoryStore, Store
from labelflow.store import SqlStore, connect, create_schema

STORE_KINDS = ("memory", "sqlite")


class StoreMaker:
    """Makes empty stores of one kind. All SQLite stores of a test share one temporary
    database file (and engine); each is a project of its own, so each starts empty."""

    def __init__(self, kind: str, directory: Path):
        self.kind = kind
        self.engine = None
        if kind == "sqlite":
            # durability does not matter in tests, so skip the fsyncs
            self.engine = connect(f"sqlite:///{directory / 'store.db'}", synchronous="OFF")
            create_schema(self.engine)
        self.made = 0

    def __call__(self) -> Store:
        self.made += 1
        if self.engine is None:
            return InMemoryStore()
        return SqlStore(self.engine, f"test{self.made}")

    def close(self) -> None:
        if self.engine is not None:
            self.engine.dispose()


@pytest.fixture(params=STORE_KINDS)
def make_store(request, tmp_path) -> Iterator[StoreMaker]:
    """Makes fresh, empty stores of the kind this test run is parametrized with."""
    maker = StoreMaker(request.param, tmp_path)
    try:
        yield maker
    finally:
        maker.close()
