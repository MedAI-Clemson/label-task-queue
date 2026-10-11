"""Where a project's event log and derived tables live.

A store holds the append-only event log and the derived tables (see ``tables``). Every
engine command runs inside ``store.transaction()``. ``append`` gives an event its place in
the log (``seq`` and the fingerprint chain) and applies it to the tables through the
reducer, in the same transaction, so the tables always match the log.

``InMemoryStore`` is used by tests and simulations. Milestone 4 adds a SQLite store with the
same methods.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable, Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from typing import Any, Protocol

from labelflow.engine.errors import BrokenChain
from labelflow.engine.events import GENESIS_HASH, Event, seal
from labelflow.engine.reducer import apply_event
from labelflow.engine.tables import InMemoryTables, Row, Scalar, Tables


class Store(Tables, Protocol):
    """The event log plus the derived tables."""

    def transaction(self) -> AbstractContextManager[None]:
        """Run a command atomically: all of its events and table changes, or none.

        Transactions may nest; only the outermost one commits or rolls back. The in-memory
        store holds a lock for the duration; SQLite will use ``BEGIN IMMEDIATE``.
        """
        ...

    def append(self, event: Event) -> Event:
        """Log a new event (one without ``seq``/``hash``) and apply it to the tables.

        Returns the event as logged, with its ``seq``, ``prev_hash`` and ``hash``.
        """
        ...

    def events(self, *, workflow: str | None = None, item: str | None = None) -> list[Event]:
        """The logged events in order, optionally only those about a workflow and/or item."""
        ...

    def snapshot(self) -> dict[str, list[Row]]:
        """Every derived table's rows in primary-key order, as plain comparable data."""
        ...


class InMemoryStore:
    """A store in Python memory. A failed transaction is rolled back completely."""

    def __init__(self) -> None:
        self._tables = InMemoryTables()
        self._events: list[Event] = []
        self._lock = threading.RLock()
        self._depth = 0

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        with self._lock:
            if self._depth:
                self._depth += 1
                try:
                    yield
                finally:
                    self._depth -= 1
                return
            mark = len(self._events)
            self._depth = 1
            self._tables.begin()
            try:
                yield
            except BaseException:
                self._tables.rollback()
                del self._events[mark:]
                raise
            else:
                self._tables.commit()
            finally:
                self._depth = 0

    def transaction(self) -> AbstractContextManager[None]:
        return self._transaction()

    def append(self, event: Event) -> Event:
        if not self._depth:
            raise RuntimeError("append() must be called inside store.transaction().")
        last = self._events[-1] if self._events else None
        sealed = seal(
            event,
            seq=1 if last is None else last.seq + 1,
            prev_hash=GENESIS_HASH if last is None else last.hash,
        )
        apply_event(self._tables, sealed)
        self._events.append(sealed)
        return sealed

    def events(self, *, workflow: str | None = None, item: str | None = None) -> list[Event]:
        with self._lock:
            return [
                e
                for e in self._events
                if (workflow is None or e.workflow == workflow) and (item is None or e.item == item)
            ]

    def snapshot(self) -> dict[str, list[Row]]:
        with self._lock:
            return self._tables.snapshot()

    # The table interface, for the reducer and the engine's reads.

    def get(self, table: str, /, **key: Scalar) -> Row | None:
        return self._tables.get(table, **key)

    def insert(self, table: str, row: Mapping[str, Any], /) -> None:
        self._tables.insert(table, row)

    def update(self, table: str, key: Mapping[str, Scalar], changes: Mapping[str, Any], /) -> None:
        self._tables.update(table, key, changes)

    def select(self, table: str, /, **where: Scalar) -> list[Row]:
        return self._tables.select(table, **where)


def rebuild(events: Iterable[Event], store: Store | None = None) -> Store:
    """Replay a log into an empty store and return it.

    Each event is appended again, so its ``seq`` and fingerprint are recomputed; if one
    differs from the logged value the log was tampered with, and BrokenChain is raised.
    """
    store = InMemoryStore() if store is None else store
    if store.events():
        raise ValueError("rebuild() needs an empty store.")
    with store.transaction():
        for event in events:
            fresh = event.model_copy(update={"seq": None, "prev_hash": None, "hash": None})
            logged = store.append(fresh)
            if (logged.seq, logged.hash) != (event.seq, event.hash):
                raise BrokenChain(
                    f"Event {event.seq} does not match the log when replayed: it, or an event "
                    "before it, was changed or removed."
                )
    return store
